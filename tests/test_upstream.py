"""Upstream registry client and archive downloader."""

from __future__ import annotations

import hashlib
import unittest

from terraform_mirror.errors import NotFound, UpstreamError
from terraform_mirror.models import ModuleRef
from terraform_mirror.upstream import ArchiveDownloader, Headers, RegistryClient, UpstreamResponse
from tests.support.fake_upstream import DEFAULT_ARCHIVE, FakeModule, FakeRegistry


class HeadersTest(unittest.TestCase):
    def test_lookup_ignores_header_case(self):
        headers = Headers({"X-Terraform-Get": "https://example.com/a.zip"})
        self.assertEqual(headers.get("x-terraform-get"), "https://example.com/a.zip")
        self.assertEqual(headers["X-TERRAFORM-GET"], "https://example.com/a.zip")
        self.assertIn("x-Terraform-get", headers)
        self.assertIsNone(headers.get("absent"))
        headers.update({"X-Terraform-Get": "y"})
        self.assertEqual(headers.get("x-terraform-get"), "y")
        self.assertEqual(len(headers), 1, "update must not add a second spelling of the name")
        self.assertEqual(headers.pop("X-TERRAFORM-GET"), "y")

    def test_response_coerces_a_plain_dict(self):
        response = UpstreamResponse(status=204, body=b"", headers={"X-Terraform-Get": "x"})
        self.assertIsInstance(response.headers, Headers)
        self.assertEqual(response.headers.get("x-terraform-get"), "x")



class RegistryClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = FakeRegistry().start()
        cls.registry.add("consul", versions=["1.0.0", "2.0.0"])

    @classmethod
    def tearDownClass(cls):
        cls.registry.stop()

    def setUp(self):
        self.client = RegistryClient(self.registry.base_url)
        self.registry.reset_hits()

    def test_list_versions(self):
        response = self.client.list_versions(ModuleRef.build("hashicorp", "consul", "aws"))
        self.assertEqual(response.status, 200)
        payload = response.json()
        self.assertEqual([m["version"] for m in payload["modules"]], ["1.0.0", "2.0.0"])

    def test_unknown_module_is_not_found(self):
        with self.assertRaises(NotFound):
            self.client.list_versions(ModuleRef.build("hashicorp", "absent", "aws"))

    def test_download_source_header(self):
        response = self.client.download_source(ModuleRef.build("hashicorp", "consul", "aws", "1.0.0"))
        self.assertEqual(response.status, 204)
        header = response.headers.get("X-Terraform-Get") or response.headers.get("x-terraform-get")
        self.assertIn("/archives/hashicorp-consul-aws-1.0.0.zip", header)
        self.assertIn("?ref=v1.0.0", header)

    def test_unknown_version_is_not_found(self):
        with self.assertRaises(NotFound):
            self.client.download_source(ModuleRef.build("hashicorp", "consul", "aws", "9.9.9"))

    def test_missing_header_is_an_upstream_error(self):
        self.registry.add("noheader", versions=["1.0.0"], style="noheader")
        with self.assertRaises(UpstreamError):
            self.client.download_source(ModuleRef.build("hashicorp", "noheader", "aws", "1.0.0"))

    def test_unreachable_upstream(self):
        client = RegistryClient("http://127.0.0.1:1")
        with self.assertRaises(UpstreamError):
            client.list_versions(ModuleRef.build("a", "b", "aws"))


class ArchiveDownloaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = FakeRegistry().start()
        cls.module = cls.registry.add("big", versions=["1.0.0"], archive=DEFAULT_ARCHIVE, archive_repeat=4)

    @classmethod
    def tearDownClass(cls):
        cls.registry.stop()

    def test_fetch_writes_file_and_hashes(self):
        url = f"{self.registry.base_url}/archives/hashicorp-big-aws-1.0.0.zip"
        downloader = ArchiveDownloader()
        downloaded = downloader.fetch(url)
        try:
            with open(downloaded.path, "rb") as handle:
                body = handle.read()
            self.assertEqual(body, DEFAULT_ARCHIVE * 4)
            self.assertEqual(downloaded.size, len(body))
            self.assertEqual(downloaded.sha256, hashlib.sha256(body).hexdigest())
        finally:
            downloaded.cleanup()

    def test_size_limit_is_enforced(self):
        url = f"{self.registry.base_url}/archives/hashicorp-big-aws-1.0.0.zip"
        downloader = ArchiveDownloader(max_bytes=16)
        with self.assertRaises(UpstreamError):
            downloader.fetch(url)

    def test_error_status_is_reported(self):
        self.registry.add("broken", versions=["1.0.0"], archive_status=503)
        downloader = ArchiveDownloader()
        with self.assertRaises(UpstreamError):
            downloader.fetch(f"{self.registry.base_url}/archives/hashicorp-broken-aws-1.0.0.zip")

    def test_unreachable_source(self):
        with self.assertRaises(UpstreamError):
            ArchiveDownloader().fetch("http://127.0.0.1:1/nope.zip")

    def test_local_schemes_are_refused(self):
        """A non-HTTP source must never be fetched, whatever the upstream claimed."""
        for url in ("file:///etc/passwd", "/etc/passwd", "ftp://host/a.zip"):
            with self.subTest(url=url):
                with self.assertRaises(UpstreamError):
                    ArchiveDownloader().fetch(url)

    def test_expected_checksum_mismatch(self):
        url = f"{self.registry.base_url}/archives/hashicorp-big-aws-1.0.0.zip"
        with self.assertRaises(UpstreamError):
            ArchiveDownloader().fetch(url, expected_sha256="0" * 64)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
