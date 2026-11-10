"""Module references, storage keys and go-getter source parsing."""

from __future__ import annotations

import unittest

from terraform_mirror.errors import BadRequest
from terraform_mirror.models import ModuleRef, parse_getter_source


class ModuleRefTest(unittest.TestCase):
    def test_build_and_keys(self):
        ref = ModuleRef.build("hashicorp", "consul", "aws", "5.1.2")
        self.assertEqual(ref.module_id, "hashicorp/consul/aws")
        self.assertEqual(ref.archive_key(), "modules/hashicorp/consul/aws/5.1.2.zip")
        self.assertEqual(ref.versions_meta_key(), "meta/versions/hashicorp/consul/aws.json")
        self.assertEqual(ref.download_meta_key(), "meta/download/hashicorp/consul/aws/5.1.2.json")

    def test_rejects_traversal_and_junk(self):
        for bad in ("..", "../etc", "a/b", "with space", "", ".hidden", "semi;colon", "back\\slash"):
            with self.subTest(bad=bad):
                with self.assertRaises(BadRequest):
                    ModuleRef.build(bad, "consul", "aws")

    def test_empty_version_means_unpinned(self):
        self.assertIsNone(ModuleRef.build("ns", "name", "aws", "").version)

    def test_rejects_bad_version(self):
        for bad in ("..", "1.0 /etc", "1.0\n", "1.0\r\n", "two words", "-1.0"):
            with self.subTest(bad=bad):
                with self.assertRaises(BadRequest):
                    ModuleRef.build("ns", "name", "aws", bad)

    def test_download_meta_requires_version(self):
        with self.assertRaises(BadRequest):
            ModuleRef.build("ns", "name", "aws").download_meta_key()

    def test_archive_key_requires_version(self):
        with self.assertRaises(BadRequest):
            ModuleRef.build("ns", "name", "aws").archive_key()


class GetterSourceTest(unittest.TestCase):
    def test_plain_archive(self):
        source = parse_getter_source("https://github.com/a/b/archive/v1.zip")
        self.assertEqual(source.getter, "")
        self.assertEqual(source.url, "https://github.com/a/b/archive/v1.zip")
        self.assertTrue(source.cacheable)
        self.assertTrue(source.looks_like_archive)

    def test_git_source_is_not_cacheable(self):
        source = parse_getter_source("git::https://github.com/a/b.git?ref=v1")
        self.assertEqual(source.getter, "git")
        self.assertEqual(source.url, "https://github.com/a/b.git")
        self.assertEqual(source.query, "ref=v1")
        self.assertFalse(source.cacheable)

    def test_subdir_written_after_the_query(self):
        # Hand written registries put the subdir last; go-getter accepts both.
        source = parse_getter_source("https://example.com/a.zip?archive=tar.gz//modules/vpc")
        self.assertEqual(source.url, "https://example.com/a.zip")
        self.assertEqual(source.query, "archive=tar.gz")
        self.assertEqual(source.subdir, "modules/vpc")
        self.assertEqual(source.fetch_url, "https://example.com/a.zip?archive=tar.gz")

    def test_subdir_and_query(self):
        source = parse_getter_source("https://example.com/a.zip//modules/vpc?ref=v1")
        self.assertEqual(source.url, "https://example.com/a.zip")
        self.assertEqual(source.subdir, "modules/vpc")
        self.assertEqual(source.fetch_url, "https://example.com/a.zip?ref=v1")
        self.assertEqual(source.extra, {"ref": "v1"})

    def test_http_getter_prefix_is_cacheable(self):
        self.assertTrue(parse_getter_source("http::https://example.com/a.zip").cacheable)

    def test_empty_is_rejected(self):
        with self.assertRaises(BadRequest):
            parse_getter_source("")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
