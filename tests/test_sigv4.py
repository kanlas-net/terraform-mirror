"""AWS Signature V4: golden vectors + an optional cross-check against botocore.

The golden vectors below were produced with ``botocore.auth.SigV4Auth`` (botocore
1.43.x) using the example credentials from the AWS documentation, so they are an
independent reference, not something this implementation produced for itself.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import unittest

from terraform_mirror.sigv4 import (
    EMPTY_SHA256,
    UNSIGNED_PAYLOAD,
    canonical_querystring,
    canonical_uri,
    sha256_file,
    sign_request,
    uri_encode,
)

AK = "AKIAIOSFODNN7EXAMPLE"
SK = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
REGION = "ru-central1"
STAMP = dt.datetime(2026, 11, 4, 8, 19, 51, tzinfo=dt.timezone.utc)
STAMP2 = dt.datetime(2026, 11, 4, 8, 11, 34, tzinfo=dt.timezone.utc)


def signature_of(authorization: str) -> str:
    return authorization.split("Signature=")[1]


class UriEncodingTest(unittest.TestCase):
    def test_unreserved_is_kept(self):
        self.assertEqual(uri_encode("aZ09-_.~"), "aZ09-_.~")

    def test_slash_handling(self):
        self.assertEqual(uri_encode("a/b c", encode_slash=False), "a/b%20c")
        self.assertEqual(uri_encode("a/b c", encode_slash=True), "a%2Fb%20c")

    def test_reserved_and_unicode(self):
        self.assertEqual(uri_encode("café+1.zip"), "caf%C3%A9%2B1.zip")
        self.assertEqual(uri_encode("日本語+1.zip"), "%E6%97%A5%E6%9C%AC%E8%AA%9E%2B1.zip")

    def test_canonical_uri_defaults_to_root(self):
        self.assertEqual(canonical_uri(""), "/")
        self.assertEqual(canonical_uri("a/b"), "/a/b")

    def test_query_string_is_sorted_and_encoded(self):
        self.assertEqual(
            canonical_querystring({"prefix": "modules/hashicorp", "list-type": "2"}),
            "list-type=2&prefix=modules%2Fhashicorp",
        )

    def test_string_query_is_treated_as_canonical(self):
        self.assertEqual(canonical_querystring("a=1&b=2"), "a=1&b=2")


class GoldenVectorTest(unittest.TestCase):
    """Signatures must match what botocore produced for the same inputs."""

    def _sign(self, method, url, *, headers=None, payload=EMPTY_SHA256, timestamp=STAMP, query=None):
        return sign_request(
            method,
            url,
            region=REGION,
            service="s3",
            access_key=AK,
            secret_key=SK,
            headers=headers or {},
            payload_hash=payload,
            timestamp=timestamp,
            canonical_query=query,
        )

    def test_put_object(self):
        body = b"hello s3"
        sha = hashlib.sha256(body).hexdigest()
        headers = self._sign(
            "PUT",
            "https://tf-cache.s3.ru-central1.storage.example/modules/hashicorp/consul/aws/5.1.2.zip",
            headers={"Content-Type": "application/zip", "x-amz-content-sha256": sha},
            payload=sha,
        )
        self.assertEqual(
            signature_of(headers["authorization"]),
            "7e5452804ae61c8321a70a33846f724fbf2d5be5ac393765af639dffcfd8c1fe",
        )
        self.assertEqual(
            headers["authorization"].split("SignedHeaders=")[1].split(",")[0],
            "content-type;host;x-amz-content-sha256;x-amz-date",
        )
        self.assertEqual(headers["x-amz-date"], "20261104T081951Z")

    def test_list_objects_query(self):
        query = canonical_querystring({"list-type": "2", "max-keys": "1000", "prefix": "modules/hashicorp"})
        headers = self._sign(
            "GET",
            "https://b.s3.ru-central1.example.com/?" + query,
            headers={"x-amz-content-sha256": EMPTY_SHA256},
            timestamp=STAMP2,
            query=query,
        )
        self.assertEqual(
            signature_of(headers["authorization"]),
            "b41046025691bfeea38032b3218c7fb2de4e4c4a6759a6afc780c51eccfa2b53",
        )

    def test_head_object(self):
        headers = self._sign(
            "HEAD",
            "https://b.s3.ru-central1.example.com/modules/hashicorp/consul/aws/5.1.2.zip",
            headers={"x-amz-content-sha256": EMPTY_SHA256},
            timestamp=STAMP2,
        )
        self.assertEqual(
            signature_of(headers["authorization"]),
            "f3fefd1d05a55948e3ab1a5e7663f40a93f5f08bd1e3c0de43d2a6a9585ce55e",
        )

    def test_delete_object(self):
        headers = self._sign(
            "DELETE",
            "https://b.s3.ru-central1.example.com/terraform-mirror/modules/a/b/c/1.0.0.zip",
            headers={"x-amz-content-sha256": EMPTY_SHA256},
            timestamp=STAMP2,
        )
        self.assertEqual(
            signature_of(headers["authorization"]),
            "01e6ea23ef6b469133e418f6d276a1d8fbbee44089c4cd6f3a155862c75d8f61",
        )

    def test_session_token_is_signed_and_returned(self):
        headers = sign_request(
            "GET",
            "https://b.s3.example.com/key",
            region=REGION,
            service="s3",
            access_key=AK,
            secret_key=SK,
            session_token="TOKEN",
            timestamp=STAMP,
        )
        self.assertEqual(headers["x-amz-security-token"], "TOKEN")
        self.assertIn("x-amz-security-token", headers["authorization"])

    def test_port_is_part_of_the_signed_host(self):
        headers = sign_request(
            "GET", "http://127.0.0.1:9000/bucket/key", region=REGION, service="s3",
            access_key=AK, secret_key=SK, timestamp=STAMP,
        )
        self.assertIn("host;x-amz-content-sha256;x-amz-date", headers["authorization"])
        self.assertEqual(headers["host"], "127.0.0.1:9000")

    def test_default_port_is_not_repeated(self):
        headers = sign_request(
            "GET", "https://example.com/key", region=REGION, service="s3",
            access_key=AK, secret_key=SK, timestamp=STAMP,
        )
        self.assertEqual(headers["host"], "example.com")

    def test_unsigned_payload_constant(self):
        self.assertEqual(UNSIGNED_PAYLOAD, "UNSIGNED-PAYLOAD")
        self.assertEqual(len(EMPTY_SHA256), 64)

    def test_sha256_file(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "blob")
            with open(path, "wb") as handle:
                handle.write(b"abc" * 1000)
            digest, size = sha256_file(path)
        self.assertEqual(size, 3000)
        self.assertEqual(digest, hashlib.sha256(b"abc" * 1000).hexdigest())


@unittest.skipUnless(importlib.util.find_spec("botocore"), "botocore is not installed")
class BotocoreCrossCheckTest(unittest.TestCase):
    """When botocore is importable, compare signatures request by request."""

    def _compare(self, method, url, headers=None, body=None):
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.credentials import Credentials

        # Our client always sends x-amz-content-sha256 (S3 wants it); make botocore sign
        # the same header set so the comparison is about the algorithm, not the header list.
        headers = dict(headers or {})
        headers.setdefault("X-Amz-Content-Sha256", EMPTY_SHA256)

        request = AWSRequest(method=method, url=url, headers=headers, data=body)
        SigV4Auth(Credentials(AK, SK), "s3", REGION).add_auth(request)
        reference = request.headers["Authorization"]
        amz_date = request.headers["X-Amz-Date"]
        moment = dt.datetime.strptime(amz_date, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
        payload_hash = request.headers.get("X-Amz-Content-Sha256", EMPTY_SHA256)
        mine = sign_request(
            method,
            url,
            region=REGION,
            service="s3",
            access_key=AK,
            secret_key=SK,
            headers=headers,
            payload_hash=payload_hash,
            timestamp=moment,
        )
        self.assertEqual(signature_of(mine["authorization"]), signature_of(reference), f"mismatch for {method} {url}")

    def test_get(self):
        self._compare("GET", "https://b.s3.ru-central1.example.com/modules/a/b/c/1.0.0.zip")

    def test_get_with_query(self):
        self._compare(
            "GET",
            "https://b.s3.ru-central1.example.com/?list-type=2&prefix=modules%2Fa",
            {"X-Amz-Content-Sha256": EMPTY_SHA256},
        )

    def test_put(self):
        body = b"payload bytes"
        sha = hashlib.sha256(body).hexdigest()
        self._compare(
            "PUT",
            "https://b.s3.ru-central1.example.com/modules/a/b/c/1.0.0.zip",
            {"Content-Type": "application/zip", "X-Amz-Content-Sha256": sha},
            body,
        )

    def test_delete(self):
        self._compare("DELETE", "https://b.s3.ru-central1.example.com/locks/modules/a.zip.lock")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
