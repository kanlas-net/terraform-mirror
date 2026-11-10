"""Filesystem storage backend."""

from __future__ import annotations

import os
import tempfile
import unittest

from terraform_mirror.errors import NotFound, StorageError
from terraform_mirror.storage.fs import FilesystemStorage, safe_join


class SafeJoinTest(unittest.TestCase):
    def test_allows_normal_keys(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(safe_join(root, "modules/a/b/c/1.zip"), os.path.join(root, "modules/a/b/c/1.zip"))

    def test_blocks_traversal(self):
        with tempfile.TemporaryDirectory() as root:
            for key in ("../escape", "modules/../../escape", "/absolute", "a/./../../b", "back\\slash"):
                with self.subTest(key=key):
                    with self.assertRaises(StorageError):
                        safe_join(root, key)


class FilesystemStorageTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = FilesystemStorage(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_put_and_read_bytes(self):
        self.storage.put_bytes("meta/x.json", b'{"a":1}', content_type="application/json")
        self.assertTrue(self.storage.exists("meta/x.json"))
        self.assertEqual(self.storage.get_bytes("meta/x.json"), b'{"a":1}')
        meta = self.storage.stat("meta/x.json")
        self.assertEqual(meta.size, 7)
        self.assertEqual(meta.content_type, "application/json")
        self.assertEqual(meta.backend, "fs")
        self.assertIsNotNone(meta.fetched_at)

    def test_missing_object(self):
        self.assertIsNone(self.storage.stat("nope"))
        self.assertFalse(self.storage.exists("nope"))
        with self.assertRaises(NotFound):
            self.storage.open("nope")

    def test_put_file_records_sha(self):
        import hashlib

        with tempfile.NamedTemporaryFile("wb", suffix=".zip", delete=False) as handle:
            handle.write(b"zip bytes")
            path = handle.name
        try:
            meta = self.storage.put_file("modules/a/b/c/1.0.0.zip", path)
        finally:
            os.unlink(path)
        self.assertEqual(meta.sha256, hashlib.sha256(b"zip bytes").hexdigest())
        self.assertEqual(meta.size, len(b"zip bytes"))
        self.assertEqual(self.storage.get_bytes("modules/a/b/c/1.0.0.zip"), b"zip bytes")

    def test_streaming_chunks(self):
        payload = b"x" * (300 * 1024)
        self.storage.put_bytes("big.bin", payload)
        with self.storage.open("big.bin") as handle:
            self.assertEqual(handle.size, len(payload))
            chunks = list(handle.chunks(64 * 1024))
        self.assertEqual(b"".join(chunks), payload)
        self.assertGreater(len(chunks), 1)

    def test_put_new_is_exclusive(self):
        self.assertTrue(self.storage.put_new("locks/a.lock", b"mine"))
        self.assertFalse(self.storage.put_new("locks/a.lock", b"theirs"))
        self.assertEqual(self.storage.get_bytes("locks/a.lock"), b"mine")

    def test_delete(self):
        self.storage.put_bytes("a/b.json", b"{}")
        self.assertTrue(self.storage.delete("a/b.json"))
        self.assertFalse(self.storage.delete("a/b.json"))
        self.assertFalse(self.storage.exists("a/b.json"))
        # the sidecar must go away too
        self.assertFalse(os.path.exists(os.path.join(self._tmp.name, "a", "b.json.meta.json")))

    def test_list_skips_sidecars_and_temp(self):
        self.storage.put_bytes("modules/a/b/c/1.zip", b"x")
        self.storage.put_bytes("modules/a/b/c/2.zip", b"x")
        self.assertEqual(self.storage.list("modules/"), ["modules/a/b/c/1.zip", "modules/a/b/c/2.zip"])
        self.assertEqual(len(self.storage.list()), 2)

    def test_no_partial_object_after_failed_write(self):
        class Boom(bytes):
            def __iter__(self):  # pragma: no cover - never used
                raise RuntimeError("boom")

        self.storage.put_bytes("ok.json", b"{}")
        self.assertTrue(self.storage.exists("ok.json"))
        leftovers = [name for name in os.listdir(os.path.join(self._tmp.name)) if name.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_rejects_traversal_key(self):
        with self.assertRaises(StorageError):
            self.storage.put_bytes("../escaped.json", b"{}")

    def test_describe(self):
        self.assertEqual(self.storage.describe()["backend"], "fs")

    def test_corrupt_sidecar_falls_back_to_mtime(self):
        """A hand-edited sidecar must not make stat() raise."""
        import json
        import os

        self.storage.put_bytes("modules/a.zip", b"payload")
        with open(self.storage._meta_path("modules/a.zip"), "w", encoding="utf-8") as handle:
            json.dump({"sha256": "0" * 64, "content_type": None, "fetched_at": "not-a-number"}, handle)
        meta = self.storage.stat("modules/a.zip")
        self.assertIsNotNone(meta)
        self.assertGreater(meta.fetched_at, 0)
        self.assertEqual(meta.size, 7)

    def test_stream_rejects_reads_after_close(self):
        import tempfile

        self.storage.put_file("modules/a.zip", __file__)
        handle = self.storage.open("modules/a.zip")
        handle.close()
        with self.assertRaises(ValueError):
            list(handle.chunks())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
