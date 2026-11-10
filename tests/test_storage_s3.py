"""S3 backend against an in-process S3-compatible server (signature is verified there)."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest

from terraform_mirror.config import S3Config
from terraform_mirror.errors import NotFound, StorageError, UpstreamError
from terraform_mirror.storage.s3 import S3Storage
from tests.support.fake_s3 import FakeS3


class S3StorageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s3 = FakeS3().start()

    @classmethod
    def tearDownClass(cls):
        cls.s3.stop()

    def setUp(self):
        self.s3.reset()
        self.s3.allow_unsigned = True
        self.cfg = S3Config(
            bucket=self.s3.bucket,
            region=self.s3.region,
            endpoint_url=self.s3.endpoint_url,
            access_key=self.s3.access_key,
            secret_key="secret-test",
            prefix="terraform-mirror",
            path_style=True,
            lock_verify_delay=0.05,
        )
        self.storage = S3Storage(self.cfg, timeout=10.0)

    # ---- addressing --------------------------------------------------------
    def test_path_style_and_virtual_host_urls(self):
        path_style = S3Storage(S3Config(bucket="bucket", region="r", endpoint_url="http://h:9000", path_style=True))
        self.assertEqual(path_style.object_url("modules/a.zip"), "http://h:9000/bucket/modules/a.zip")
        virtual = S3Storage(S3Config(bucket="bucket", region="ru-central1", endpoint_url="https://storage.example", path_style=False))
        self.assertEqual(virtual.object_url("modules/a.zip"), "https://bucket.storage.example/modules/a.zip")
        aws = S3Storage(S3Config(bucket="bucket", region="us-east-1"))
        self.assertEqual(aws.object_url("k"), "https://bucket.s3.us-east-1.amazonaws.com/k")

    def test_full_key_uses_prefix(self):
        self.assertEqual(self.storage.full_key("modules/a.zip"), "terraform-mirror/modules/a.zip")
        plain = S3Storage(S3Config(bucket="bucket", region="r", endpoint_url="http://h:1", prefix=""))
        self.assertEqual(plain.full_key("modules/a.zip"), "modules/a.zip")

    # ---- round trip --------------------------------------------------------
    def test_put_get_stat_delete(self):
        meta = self.storage.put_bytes("meta/v.json", b'{"modules":[]}', content_type="application/json")
        self.assertEqual(meta.backend, "s3")
        self.assertEqual(self.s3.auth_errors, [])
        self.assertIn("terraform-mirror/meta/v.json", self.s3.keys())
        self.assertEqual(self.storage.get_bytes("meta/v.json"), b'{"modules":[]}')
        stat = self.storage.stat("meta/v.json")
        self.assertEqual(stat.size, len(b'{"modules":[]}'))
        self.assertEqual(stat.content_type, "application/json")
        self.assertIsNotNone(stat.fetched_at)
        self.assertTrue(self.storage.exists("meta/v.json"))
        self.assertTrue(self.storage.delete("meta/v.json"))
        self.assertFalse(self.storage.exists("meta/v.json"))
        self.assertFalse(self.storage.delete("meta/v.json"))

    def test_retry_rewinds_a_file_body(self):
        """A retry after a half-sent upload must replay the body, not send zero bytes."""
        import urllib.error

        sent: list[bytes] = []
        calls: list[str] = []

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def flaky(request, timeout=None):
            body = request.data
            calls.append(request.get_method())
            if len(calls) == 1:
                body.read()  # the connection died after the whole body was written out
                raise urllib.error.URLError("connection reset by peer")
            sent.append(body.read())
            return Response()

        with tempfile.NamedTemporaryFile("wb", suffix=".zip", delete=False) as handle:
            handle.write(b"0123456789")
            path = handle.name
        self.addCleanup(os.unlink, path)

        storage = S3Storage(self.cfg, opener=flaky, retries=2)
        storage.put_file("modules/a.zip", path)
        self.assertEqual(sent, [b"0123456789"], "the retry must send the whole body again")

    def test_unseekable_body_is_not_retried(self):
        """A stream we cannot rewind must fail loudly instead of uploading nothing."""
        import urllib.error

        calls: list[str] = []

        class Unseekable:
            def read(self, size=-1):
                return b"chunk"

            def seekable(self):
                return False

            def tell(self):
                raise OSError("unseekable")

        def flaky(request, timeout=None):
            calls.append(request.get_method())
            raise urllib.error.URLError("connection reset by peer")

        storage = S3Storage(self.cfg, opener=flaky, retries=2)
        with self.assertRaises(StorageError):
            storage._request("PUT", "modules/a.zip", data=Unseekable(), content_length=5)
        self.assertEqual(calls, ["PUT"], "an unrewindable body must not be sent twice")

    def test_put_file_stores_sha_metadata(self):
        with tempfile.NamedTemporaryFile("wb", suffix=".zip", delete=False) as handle:
            handle.write(b"archive-bytes")
            path = handle.name
        try:
            meta = self.storage.put_file("modules/a/b/c/1.zip", path, content_type="application/zip")
        finally:
            os.unlink(path)
        stored = self.s3.objects["terraform-mirror/modules/a/b/c/1.zip"]
        self.assertEqual(stored.body, b"archive-bytes")
        self.assertEqual(stored.meta["x-amz-meta-sha256"], hashlib.sha256(b"archive-bytes").hexdigest())
        self.assertEqual(meta.sha256, hashlib.sha256(b"archive-bytes").hexdigest())
        self.assertEqual(self.storage.stat("modules/a/b/c/1.zip").sha256, meta.sha256)

    def test_streaming_open(self):
        payload = b"y" * (300 * 1024)
        self.storage.put_bytes("big.bin", payload)
        with self.storage.open("big.bin") as handle:
            self.assertEqual(b"".join(handle.chunks(128 * 1024)), payload)

    def test_missing_object(self):
        self.assertIsNone(self.storage.stat("nothing/here"))
        self.assertFalse(self.storage.exists("nothing/here"))
        with self.assertRaises(NotFound):
            self.storage.open("nothing/here")

    def test_list_pagination(self):
        for index in range(5):
            self.storage.put_bytes(f"modules/{index}.zip", b"x")
        keys = self.storage.list("modules/")
        self.assertEqual(len(keys), 5)
        self.assertTrue(all(key.startswith("modules/") for key in keys), keys)
        self.assertEqual(self.storage.list("meta/"), [])

    # ---- create-if-absent --------------------------------------------------
    def test_put_new_handshake(self):
        self.assertTrue(self.storage.put_new("locks/a.lock", b"mine"))
        self.assertFalse(self.storage.put_new("locks/a.lock", b"theirs"))
        self.assertEqual(self.storage.get_bytes("locks/a.lock"), b"mine")
        self.assertEqual(self.s3.auth_errors, [])

    def test_put_new_conditional(self):
        cfg = self.cfg
        cfg.conditional_locks = True
        try:
            storage = S3Storage(cfg)
            self.assertTrue(storage.put_new("locks/cond.lock", b"mine"))
            self.assertFalse(storage.put_new("locks/cond.lock", b"theirs"))
            self.assertTrue(any(h.get("if-none-match") == "*" for h in self.sent_headers("locks/cond.lock")))
        finally:
            cfg.conditional_locks = False

    def test_unsigned_payload_is_only_used_over_https(self):
        # Over plain HTTP the body hash is always sent: unsigned payloads would be
        # unprotected against in-flight tampering.
        self.s3.allow_unsigned = False
        self.cfg.unsigned_payload = True
        try:
            storage = S3Storage(self.cfg)
            self.assertFalse(storage._is_https())
            meta = storage.put_file("modules/a/b/c/unsigned.zip", self._tmpfile(), content_type="application/zip")
            self.assertEqual(self.s3.auth_errors, [])
            self.assertEqual(meta.sha256, hashlib.sha256(b"unsigned body").hexdigest())
            # Pretend the endpoint is TLS: now the hash is skipped on purpose.
            self.s3.allow_unsigned = True
            self.s3.reset()
            storage._is_https = lambda: True
            storage.put_file("modules/a/b/c/unsigned2.zip", self._tmpfile(), content_type="application/zip")
            self.assertEqual(self.sent_headers("unsigned2.zip")[0]["x-amz-content-sha256"], "UNSIGNED-PAYLOAD")
        finally:
            self.cfg.unsigned_payload = False
            self.s3.allow_unsigned = True

    def sent_headers(self, key_suffix: str) -> list[dict[str, str]]:
        """Request headers as actually sent, lower-cased like an HTTP client would see."""
        return [
            {k.lower(): v for k, v in headers.items()}
            for _method, key, headers in self.s3.requests
            if key.endswith(key_suffix)
        ]

    def _tmpfile(self):
        handle, path = tempfile.mkstemp(suffix=".zip")
        with os.fdopen(handle, "wb") as out:
            out.write(b"unsigned body")
        self.addCleanup(os.unlink, path)
        return path

    def test_wrong_credentials_are_rejected(self):
        bad = S3Storage(
            S3Config(
                bucket=self.s3.bucket,
                region=self.s3.region,
                endpoint_url=self.s3.endpoint_url,
                access_key="WRONG",
                secret_key="nope",
                prefix="",
                path_style=True,
            )
        )
        with self.assertRaises(Exception):
            bad.exists("whatever")

    def test_describe(self):
        described = self.storage.describe()
        self.assertEqual(described["bucket"], self.s3.bucket)
        self.assertEqual(described["path_style"], "True")

    def test_session_token_is_sent(self):
        self.s3.session_token_required = True
        try:
            cfg = self.cfg
            cfg.session_token = "session-token"
            storage = S3Storage(cfg)
            storage.put_bytes("with/token.json", b"{}")
            self.assertEqual(self.s3.auth_errors, [])
        finally:
            self.s3.session_token_required = False
            self.cfg.session_token = None


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
