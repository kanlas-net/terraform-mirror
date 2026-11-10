"""Tiered storage: write-through, read fallback, promotion, delete everywhere."""

from __future__ import annotations

import tempfile
import unittest

from terraform_mirror.config import S3Config
from terraform_mirror.errors import NotFound
from terraform_mirror.storage.fs import FilesystemStorage
from terraform_mirror.storage.multi import TieredStorage
from terraform_mirror.storage.s3 import S3Storage
from tests.support.fake_s3 import FakeS3


class TieredStorageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s3 = FakeS3().start()

    @classmethod
    def tearDownClass(cls):
        cls.s3.stop()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fs = FilesystemStorage(self._tmp.name)
        self.s3.objects.clear()
        self.s3_storage = S3Storage(
            S3Config(
                bucket=self.s3.bucket,
                region=self.s3.region,
                endpoint_url=self.s3.endpoint_url,
                access_key=self.s3.access_key,
                secret_key="secret-test",
                prefix="terraform-mirror",
                path_style=True,
                lock_verify_delay=0.0,
            )
        )
        self.tiered = TieredStorage([self.fs, self.s3_storage])

    def tearDown(self):
        self._tmp.cleanup()

    def test_put_writes_every_tier(self):
        self.tiered.put_bytes("modules/a/b/c/1.zip", b"payload")
        self.assertTrue(self.fs.exists("modules/a/b/c/1.zip"))
        self.assertIn("terraform-mirror/modules/a/b/c/1.zip", self.s3.keys())

    def test_read_prefers_the_first_tier(self):
        self.tiered.put_bytes("modules/a/b/c/1.zip", b"payload")
        with self.tiered.open("modules/a/b/c/1.zip") as handle:
            self.assertEqual(handle.meta.backend, "fs")

    def test_s3_hit_is_promoted_to_disk(self):
        self.s3_storage.put_bytes("modules/a/b/c/2.zip", b"from s3")
        self.assertFalse(self.fs.exists("modules/a/b/c/2.zip"))
        with self.tiered.open("modules/a/b/c/2.zip") as handle:
            self.assertEqual(b"".join(handle.chunks()), b"from s3")
        self.assertTrue(self.fs.exists("modules/a/b/c/2.zip"), "promotion should warm the disk cache")

    def test_promotion_can_be_disabled(self):
        tiered = TieredStorage([self.fs, self.s3_storage], promote_first=False)
        self.s3_storage.put_bytes("modules/a/b/c/3.zip", b"from s3")
        with tiered.open("modules/a/b/c/3.zip") as handle:
            self.assertEqual(b"".join(handle.chunks()), b"from s3")
        self.assertFalse(self.fs.exists("modules/a/b/c/3.zip"))

    def test_exists_and_stat(self):
        self.s3_storage.put_bytes("modules/a/b/c/4.zip", b"x")
        self.assertTrue(self.tiered.exists("modules/a/b/c/4.zip"))
        self.assertEqual(self.tiered.stat("modules/a/b/c/4.zip").backend, "s3")
        self.assertFalse(self.tiered.exists("modules/a/b/c/missing.zip"))
        self.assertIsNone(self.tiered.stat("modules/a/b/c/missing.zip"))

    def test_missing_raises_not_found(self):
        with self.assertRaises(NotFound):
            self.tiered.open("nope")

    def test_delete_removes_from_every_tier(self):
        self.tiered.put_bytes("modules/a/b/c/5.zip", b"x")
        self.assertTrue(self.tiered.delete("modules/a/b/c/5.zip"))
        self.assertFalse(self.fs.exists("modules/a/b/c/5.zip"))
        self.assertFalse(self.s3_storage.exists("modules/a/b/c/5.zip"))
        self.assertFalse(self.tiered.delete("modules/a/b/c/5.zip"))

    def test_list_merges_tiers_without_duplicates(self):
        self.fs.put_bytes("modules/a/b/c/local.zip", b"x")
        self.s3_storage.put_bytes("modules/a/b/c/remote.zip", b"x")
        keys = self.tiered.list("modules/")
        self.assertEqual(keys, ["modules/a/b/c/local.zip", "modules/a/b/c/remote.zip"])

    def test_broken_tier_does_not_break_the_other(self):
        class Broken(FilesystemStorage):
            name = "broken"

            def exists(self, key):
                raise RuntimeError("disk on fire")

            def stat(self, key):
                raise RuntimeError("disk on fire")

            def list(self, prefix=""):
                raise RuntimeError("disk on fire")

            def delete(self, key):
                raise RuntimeError("disk on fire")

        tiered = TieredStorage([Broken(self._tmp.name), self.s3_storage])
        self.s3_storage.put_bytes("modules/a/b/c/6.zip", b"x")
        self.assertTrue(tiered.exists("modules/a/b/c/6.zip"))
        self.assertEqual(tiered.list("modules/"), ["modules/a/b/c/6.zip"])
        self.assertTrue(tiered.delete("modules/a/b/c/6.zip"))

    def test_describe(self):
        self.assertEqual(self.tiered.describe()["tiers"], "fs+s3")

    def test_empty_backends_rejected(self):
        with self.assertRaises(ValueError):
            TieredStorage([])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
