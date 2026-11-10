"""The provider network mirror: addresses, hashes, the upstream client and the wire.

Three layers are covered here:

* pure helpers (:class:`ProviderRef`, :func:`package_hashes`, :func:`normalize_package`,
  :func:`resolve_provider_source`) — no sockets;
* :class:`ProviderCacheService` against :class:`FakeProviderRegistry`, which is where
  caching, locking and the flattening of GitHub-style archives live;
* the HTTP surface through ``ProxyHarness``, because the protocol is defined by the
  exact paths and JSON shapes ``terraform init`` asks for.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from tests.support.fake_provider_registry import FakeProviderRegistry, make_package
from tests.support.harness import ProxyHarness
from terraform_mirror.errors import BadRequest, NotFound, ProxyError, UpstreamError
from terraform_mirror.providers import (
    ProviderCacheService,
    ProviderRef,
    ProviderRegistryClient,
    ProviderTrace,
    check_hostname,
    normalize_package,
    package_hashes,
    resolve_provider_source,
)
from terraform_mirror.storage.fs import FilesystemStorage

HOST = "registry.terraform.io"


def expected_h1(entries: dict[str, bytes]) -> str:
    """An independent reimplementation of dirhash.Hash1, for the known-vector test."""
    inner = hashlib.sha256()
    for name in sorted(entries):
        inner.update(f"{hashlib.sha256(entries[name]).hexdigest()}  {name}\n".encode())
    return "h1:" + base64.b64encode(inner.digest()).decode()


def read_all(handle) -> bytes:
    return b"".join(handle.chunks())


class ProviderTraceTest(unittest.TestCase):
    def test_a_package_miss_wins(self):
        trace = ProviderTrace(versions="hit", packages={"linux_amd64": "hit"})
        self.assertEqual(trace.status, "hit")
        trace.packages["darwin_arm64"] = "miss"
        self.assertEqual(trace.status, "miss")

    def test_a_refreshed_list_is_not_reported_as_a_hit(self):
        self.assertEqual(ProviderTrace(versions="miss").status, "miss")
        self.assertEqual(ProviderTrace(versions="refresh").status, "refresh")
        self.assertEqual(ProviderTrace(versions="refresh", packages={"linux_amd64": "hit"}).status, "refresh")
        self.assertEqual(ProviderTrace(versions="stale-on-error").status, "stale-on-error")
        self.assertEqual(ProviderTrace().status, "hit")


class ProviderRefTest(unittest.TestCase):
    def test_two_part_address_gets_the_default_host(self):
        ref = ProviderRef.from_address("hashicorp/consul")
        self.assertEqual((ref.host, ref.namespace, ref.type), (HOST, "hashicorp", "consul"))
        self.assertEqual(ref.version, "")

    def test_host_and_version_are_parsed(self):
        ref = ProviderRef.from_address("example.com/Corp/provider-one@1.2.3")
        self.assertEqual(ref.host, "example.com")
        # Namespace and type keep their case: Terraform treats them case-sensitively.
        self.assertEqual(ref.namespace, "Corp")
        self.assertEqual(ref.type, "provider-one")
        self.assertEqual(ref.version, "1.2.3")
        self.assertEqual(str(ref), "example.com/Corp/provider-one@1.2.3")

    def test_malformed_addresses(self):
        for address in ("consul", "a/b/c/d", "hashicorp/consul@", "/hashicorp/consul/x/y"):
            with self.subTest(address=address):
                with self.assertRaises(BadRequest):
                    ProviderRef.from_address(address)

    def test_hostname_must_look_like_a_hostname(self):
        self.assertEqual(check_hostname("Registry.Terraform.IO."), HOST)
        for host in ("localhost", "registry_terraform_io", "not a host", ""):
            with self.subTest(host=host):
                with self.assertRaises(BadRequest):
                    check_hostname(host)

    def test_platform_is_validated(self):
        with self.assertRaises(BadRequest):
            ProviderRef(host=HOST, namespace="a", type="b", platform="linux-amd64")

    def test_storage_keys_and_archive_name(self):
        ref = ProviderRef.from_address("hashicorp/consul@5.13.0").with_platform("linux_amd64")
        self.assertEqual(ref.base_prefix, f"providers/{HOST}/hashicorp/consul/")
        self.assertEqual(ref.versions_key, f"providers/{HOST}/hashicorp/consul/versions.json")
        self.assertEqual(ref.archive_name(), "terraform-provider-consul_5.13.0_linux_amd64.zip")
        self.assertEqual(
            ref.archive_key("windows_amd64"),
            f"providers/{HOST}/hashicorp/consul/terraform-provider-consul_5.13.0_windows_amd64.zip",
        )
        self.assertTrue(ref.meta_key().endswith("_linux_amd64.zip.info.json"))

    def test_archive_name_needs_version_and_platform(self):
        bare = ProviderRef.from_address("hashicorp/consul")
        with self.assertRaises(BadRequest):
            bare.archive_name("linux_amd64")
        with self.assertRaises(BadRequest):
            ProviderRef.from_address("hashicorp/consul@1.0.0").archive_name()

    def test_platform_from_name_reads_version_and_platform(self):
        self.assertEqual(
            ProviderRef.platform_from_name("terraform-provider-consul_5.13.0_linux_arm64.zip"),
            ("5.13.0", "linux_arm64"),
        )
        # A version may contain dots and hyphens; the parse works from the right.
        self.assertEqual(
            ProviderRef.platform_from_name("terraform-provider-aws-beta_1.2.3-rc1_darwin_arm64.zip"),
            ("1.2.3-rc1", "darwin_arm64"),
        )
        for name in ("index.json", "terraform-provider-consul.zip", "consul_1.0.0_linux_amd64.zip"):
            with self.subTest(name=name):
                self.assertEqual(ProviderRef.platform_from_name(name), ("", ""))


class PackageHashTest(unittest.TestCase):
    def test_hashes_match_an_independent_computation(self):
        entries = {
            "LICENSE.txt": b"license text\n",
            "terraform-provider-consul_v5.13.0_x5": b"fake plugin binary\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "pkg.zip")
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, data in entries.items():
                    archive.writestr(name, data)
            h1, zh = package_hashes(path)
            self.assertEqual(h1, expected_h1(entries))
            with open(path, "rb") as handle:
                self.assertEqual(zh, "zh:" + base64.b64encode(hashlib.sha256(handle.read()).digest()).decode())

    def test_h1_ignores_zip_order_but_zh_does_not(self):
        entries = {"b.txt": b"b\n", "a.txt": b"a\n"}
        with tempfile.TemporaryDirectory() as tmp:
            first = str(Path(tmp) / "first.zip")
            second = str(Path(tmp) / "second.zip")
            for path, order in ((first, ["b.txt", "a.txt"]), (second, ["a.txt", "b.txt"])):
                with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                    for name in order:
                        archive.writestr(name, entries[name])
            h1_first, zh_first = package_hashes(first)
            h1_second, zh_second = package_hashes(second)
            self.assertEqual(h1_first, h1_second)
            self.assertNotEqual(zh_first, zh_second)

    def test_directory_entries_are_not_hashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "pkg.zip")
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("dir/", b"")
                archive.writestr("dir/file", b"data\n")
            h1, _zh = package_hashes(path)
            self.assertEqual(h1, expected_h1({"dir/file": b"data\n"}))


class NormalizePackageTest(unittest.TestCase):
    def test_flat_archive_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "flat.zip")
            Path(path).write_bytes(make_package("terraform-provider-consul_v1.0.0_x5"))
            kept, repacked = normalize_package(path)
            self.assertFalse(repacked)
            self.assertEqual(kept, path)
            with zipfile.ZipFile(kept) as archive:
                self.assertEqual(sorted(archive.namelist()), ["LICENSE.txt", "terraform-provider-consul_v1.0.0_x5"])

    def test_nested_archive_is_flattened(self):
        """A GitHub codeload archive nests everything under ``repo-ref/``."""
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "nested.zip")
            Path(path).write_bytes(make_package("terraform-provider-consul_v1.0.0_x5", nested="repo-1.0.0"))
            kept, repacked = normalize_package(path)
            self.assertTrue(repacked)
            self.assertNotEqual(kept, path)
            self.assertFalse(Path(path).exists(), "the original nested archive must be removed")
            with zipfile.ZipFile(kept) as archive:
                self.assertEqual(sorted(archive.namelist()), ["LICENSE.txt", "terraform-provider-consul_v1.0.0_x5"])
            # ...and the flattened package hashes as the flat one does.
            flat = str(Path(tmp) / "flat.zip")
            Path(flat).write_bytes(make_package("terraform-provider-consul_v1.0.0_x5"))
            self.assertEqual(package_hashes(kept)[0], package_hashes(flat)[0])

    def test_archive_without_a_plugin_is_rejected(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("README.md", b"nothing to see\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "junk.zip")
            Path(path).write_bytes(buffer.getvalue())
            with self.assertRaises(UpstreamError) as caught:
                normalize_package(path)
            self.assertEqual(caught.exception.reason, "bad_archive")


class ResolveSourceTest(unittest.TestCase):
    def test_github_getter_becomes_an_archive_url(self):
        url, subdir = resolve_provider_source("github://hashicorp/tf-provider:refs/tags/v1.2.3.zip?subdir=dist/linux")
        self.assertEqual(url, "https://github.com/hashicorp/tf-provider/archive/refs/tags/v1.2.3.zip")
        self.assertEqual(subdir, "dist/linux")

    def test_plain_archive_url_is_used_as_is(self):
        url, subdir = resolve_provider_source("https://releases.example.com/pkg.zip")
        self.assertEqual(url, "https://releases.example.com/pkg.zip")
        self.assertEqual(subdir, "")

    def test_sources_we_cannot_fetch(self):
        for raw in ("", "git::https://example.com/x.git", "s3://bucket/key.zip", "github://only-owner"):
            with self.subTest(raw=raw):
                with self.assertRaises(UpstreamError):
                    resolve_provider_source(raw)


class CacheFixture(unittest.TestCase):
    """Shared wiring: a fake registry, a filesystem store and a cache service."""

    platforms = ("linux_amd64",)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeProviderRegistry().start()
        self.addCleanup(self.fake.stop)
        self.storage = FilesystemStorage(str(Path(self.tmp.name) / "store"))
        self.config = SimpleNamespace(
            provider_platforms=list(self.platforms),
            ttl_provider_versions=3600,
            provider_block_wait=0.0,
            lock_wait=0.0,
        )
        self.client = ProviderRegistryClient(self.fake.base_url)
        from terraform_mirror.upstream import ArchiveDownloader

        self.providers = ProviderCacheService(
            self.config, self.storage, self.client, ArchiveDownloader(tmp_dir=self.tmp.name)
        )


class ProviderClientTest(CacheFixture):
    def test_versions_are_normalised(self):
        self.fake.add("hashicorp/consul", versions=("2.0.0", "1.0.0"), platforms=("linux_amd64", "darwin_arm64"))
        rows = self.client.versions(ProviderRef.from_address("hashicorp/consul"))
        self.assertEqual([row["version"] for row in rows], ["2.0.0", "1.0.0"])
        self.assertEqual(rows[0]["platforms"], ["linux_amd64", "darwin_arm64"])
        self.assertEqual(rows[0]["protocols"], ["5.0"])

    def test_unknown_provider_is_not_found(self):
        with self.assertRaises(NotFound):
            self.client.versions(ProviderRef.from_address("hashicorp/nope"))

    def test_download_reports_url_and_checksum(self):
        self.fake.add("hashicorp/consul", versions=("1.0.0",))
        ref = ProviderRef.from_address("hashicorp/consul@1.0.0").with_platform("linux_amd64")
        target = self.client.download(ref)
        self.assertTrue(target.url.startswith(self.fake.base_url))
        self.assertEqual(target.filename, "terraform-provider-consul_1.0.0_linux_amd64.zip")
        self.assertEqual(len(target.sha256), 64)
        self.assertTrue(target.cacheable)

    def test_missing_platform_is_not_found(self):
        self.fake.add("hashicorp/consul", versions=("1.0.0",))
        ref = ProviderRef.from_address("hashicorp/consul@1.0.0").with_platform("plan9_mips")
        with self.assertRaises(NotFound):
            self.client.download(ref)

    def test_download_needs_version_and_platform(self):
        with self.assertRaises(ProxyError):
            self.client.download(ProviderRef.from_address("hashicorp/consul"))

    def test_probe_reports_an_unreachable_registry(self):
        self.fake.add("hashicorp/random", versions=("1.0.0",))
        ok, detail, _elapsed = self.client.probe()
        self.assertTrue(ok, detail)
        self.fake.stop()
        dead = ProviderRegistryClient(self.fake.base_url, request_timeout=2.0)
        ok, detail, _elapsed = dead.probe()
        self.assertFalse(ok)
        self.assertTrue(detail)


class ProviderCacheTest(CacheFixture):
    def setUp(self):
        super().setUp()
        self.fake.add("hashicorp/consul", versions=("2.0.0", "1.0.0"))

    def ref(self, version="2.0.0", platform="linux_amd64"):
        return ProviderRef.from_address(f"hashicorp/consul@{version}").with_platform(platform)

    # ---- version list ------------------------------------------------------
    def test_index_payload_lists_every_version(self):
        payload = self.providers.index_payload(ProviderRef.from_address("hashicorp/consul"))
        self.assertEqual(sorted(payload["versions"]), ["1.0.0", "2.0.0"])
        self.assertEqual(payload["versions"]["2.0.0"], {})

    def test_version_list_is_cached(self):
        ref = ProviderRef.from_address("hashicorp/consul")
        self.providers.versions(ref)
        self.providers.versions(ref)
        self.assertEqual(self.fake.count("/versions"), 1)

    def test_expired_version_list_is_refetched(self):
        ref = ProviderRef.from_address("hashicorp/consul")
        cached = self.providers.versions(ref)
        cached["fetched_at"] = 0
        self.storage.put_bytes(ref.versions_key, json.dumps(cached).encode())
        self.providers.versions(ref)
        self.assertEqual(self.fake.count("/versions"), 2)

    def test_a_cached_list_is_served_when_the_registry_dies(self):
        ref = ProviderRef.from_address("hashicorp/consul")
        self.providers.versions(ref)
        self.fake.stop()
        trace = ProviderTrace()
        payload = self.providers.versions(ref, force=True, trace=trace)
        self.assertEqual(sorted(row["version"] for row in payload["versions"]), ["1.0.0", "2.0.0"])
        self.assertEqual(trace.versions, "stale-on-error")

    def test_trace_says_what_had_to_be_fetched(self):
        ref = self.ref()
        first = ProviderTrace()
        self.providers.archives_for(ref, block_wait=0.0, trace=first)
        self.assertEqual(first.versions, "miss")
        self.assertEqual(first.packages, {"linux_amd64": "miss"})
        self.assertEqual(first.status, "miss")
        second = ProviderTrace()
        self.providers.archives_for(ref, block_wait=0.0, trace=second)
        self.assertEqual(second.versions, "hit")
        self.assertEqual(second.packages, {"linux_amd64": "hit"})
        self.assertEqual(second.status, "hit")

    # ---- packages ----------------------------------------------------------
    def test_ensure_fetches_once_and_records_hashes(self):
        ref = self.ref()
        meta = self.providers.ensure(ref)
        self.assertEqual(meta["provider"], f"{HOST}/hashicorp/consul")
        self.assertEqual(meta["filename"], "terraform-provider-consul_2.0.0_linux_amd64.zip")
        self.assertFalse(meta["repacked"])
        self.assertEqual(meta["size"], len(self.fake.packages[meta["filename"]]))
        self.assertEqual(meta["zh"], "zh:" + base64.b64encode(hashlib.sha256(
            self.fake.packages[meta["filename"]]).digest()).decode())
        self.assertEqual(meta["h1"].split(":")[0], "h1")
        self.assertEqual(len(meta["h1"].split(":")[1]), 44)  # base64 of a sha256
        self.assertEqual(len(meta["sha256"]), 64)
        # The object is really in storage and reads back byte for byte.
        with self.providers.open_archive(ref) as handle:
            self.assertEqual(read_all(handle), self.fake.packages[meta["filename"]])
        # Second call is served from storage: one versions call, one archive download.
        again = self.providers.ensure(ref)
        self.assertEqual(again, meta)
        self.assertEqual(self.fake.count("/download"), 1)

    def test_the_description_does_not_clobber_the_backends_own_metadata(self):
        """A ``.meta.json`` key would be the zip's own sidecar on the filesystem backend."""
        ref = self.ref()
        meta = self.providers.ensure(ref)
        stat = self.storage.stat(ref.archive_key())
        self.assertEqual(stat.content_type, "application/zip")
        self.assertEqual(stat.size, meta["size"])
        self.assertEqual([row["filename"] for row in self.providers.list_providers()], [meta["filename"]])

    def test_nested_archive_is_flattened_before_it_is_stored(self):
        self.fake.add("hashicorp/nested", versions=("1.0.0",), nested="terraform-provider-nested-1.0.0")
        ref = ProviderRef.from_address("hashicorp/nested@1.0.0").with_platform("linux_amd64")
        meta = self.providers.ensure(ref)
        self.assertTrue(meta["repacked"])
        with self.providers.open_archive(ref) as handle:
            body = read_all(handle)
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertEqual(sorted(archive.namelist()), ["LICENSE.txt", "terraform-provider-nested_1.0.0"])
        # The hashes describe what we serve, not what upstream handed us.
        self.assertEqual(meta["zh"], "zh:" + base64.b64encode(hashlib.sha256(body).digest()).decode())

    def test_a_wrong_checksum_is_refused_and_nothing_is_stored(self):
        self.fake.sha256 = "0" * 64
        with self.assertRaises(UpstreamError):
            self.providers.ensure(self.ref())
        self.assertIsNone(self.providers.meta(self.ref()))
        self.assertEqual(self.storage.list("providers/"), [])

    def test_archives_for_offers_url_and_hashes(self):
        archives = self.providers.archives_for(self.ref(), block_wait=0.0)
        self.assertEqual(list(archives), ["linux_amd64"])
        entry = archives["linux_amd64"]
        self.assertEqual(entry["url"], "terraform-provider-consul_2.0.0_linux_amd64.zip")
        self.assertEqual(len(entry["hashes"]), 2)
        self.assertTrue(all(item.startswith(("h1:", "zh:")) for item in entry["hashes"]))

    def test_archives_for_only_offers_platforms_we_host(self):
        self.fake.add("hashicorp/multi", versions=("1.0.0",), platforms=("linux_amd64", "windows_amd64"))
        ref = ProviderRef.from_address("hashicorp/multi@1.0.0")
        self.assertEqual(self.providers.platforms_for(ref), ["linux_amd64"])
        self.assertEqual(list(self.providers.archives_for(ref, block_wait=0.0)), ["linux_amd64"])

    def test_unknown_version_is_not_found(self):
        with self.assertRaises(NotFound):
            self.providers.archives_for(self.ref("9.9.9"), block_wait=0.0)

    def test_schedule_is_a_noop_for_a_complete_ref(self):
        self.assertFalse(self.providers.schedule(ProviderRef.from_address("hashicorp/consul")))
        self.assertFalse(self.providers.schedule(self.ref()))  # queue is None in this fixture

    # ---- maintenance -------------------------------------------------------
    def test_list_and_purge(self):
        self.providers.ensure(self.ref("2.0.0"))
        self.providers.ensure(self.ref("1.0.0"))
        listed = self.providers.list_providers()
        self.assertEqual([row["version"] for row in listed], ["1.0.0", "2.0.0"])
        removed = self.providers.purge(ProviderRef.from_address("hashicorp/consul@1.0.0"))
        self.assertTrue(removed)
        self.assertEqual([row["version"] for row in self.providers.list_providers()], ["2.0.0"])
        self.providers.purge(ProviderRef.from_address("hashicorp/consul"))
        self.assertEqual(self.providers.list_providers(), [])


class ProviderMirrorHttpTest(unittest.TestCase):
    """The wire protocol, served by the proxy itself."""

    def make(self, **overrides) -> ProxyHarness:
        self.fake = FakeProviderRegistry().start()
        self.addCleanup(self.fake.stop)
        self.fake.add("hashicorp/consul", versions=("2.0.0", "1.0.0"))
        harness = ProxyHarness.create(
            self,
            "http://127.0.0.1:9",  # the module registry is not exercised here
            prefetch_latest=False,
            provider_upstream_url=self.fake.base_url,
            **overrides,
        )
        harness.start_server()
        self.addCleanup(harness.stop)
        return harness

    def test_index_version_and_archive(self):
        harness = self.make()
        base = f"/{HOST}/hashicorp/consul"

        index = harness.get(f"{base}/index.json")
        self.assertEqual(index.status, 200)
        self.assertEqual(sorted(json.loads(index.body)["versions"]), ["1.0.0", "2.0.0"])
        self.assertEqual(index.header("X-TFM-Cache"), "miss")
        self.assertEqual(harness.get(f"{base}/index.json").header("X-TFM-Cache"), "hit")

        version = harness.get(f"{base}/2.0.0.json")
        self.assertEqual(version.status, 200)
        self.assertEqual(version.header("X-TFM-Cache"), "miss")  # it fetched the package
        archives = json.loads(version.body)["archives"]
        url = archives["linux_amd64"]["url"]
        self.assertEqual(url, "terraform-provider-consul_2.0.0_linux_amd64.zip")
        self.assertEqual(len(archives["linux_amd64"]["hashes"]), 2)

        # The archive URL is relative to the version document, so it sits beside it.
        archive = harness.get(f"{base}/{url}")
        self.assertEqual(archive.status, 200)
        self.assertEqual(archive.headers.get("Content-Type"), "application/zip")
        self.assertEqual(archive.headers.get("X-TFM-Cache"), "hit")  # <version>.json fetched it
        self.assertEqual(archive.headers.get("Content-Length"), str(len(archive.body)))
        with zipfile.ZipFile(io.BytesIO(archive.body)) as package:
            self.assertIn("LICENSE.txt", package.namelist())

        head = harness.head(f"{base}/{url}")
        self.assertEqual(head.status, 200)
        self.assertEqual(head.body, b"")
        self.assertEqual(head.headers.get("Content-Length"), str(len(archive.body)))

    def test_the_access_log_says_whether_the_cache_answered(self):
        harness = self.make()
        with self.assertLogs("terraform_mirror.http", level="INFO") as captured:
            harness.get(f"/{HOST}/hashicorp/consul/index.json")
            harness.get(f"/{HOST}/hashicorp/consul/index.json")
        lines = [row for row in captured.output if "index.json" in row]
        self.assertTrue(lines, f"no access log line for the request: {captured.output}")
        self.assertIn("cache=miss", lines[0])
        self.assertIn("cache=hit", lines[1])
        # the size field is no longer the stdlib placeholder
        self.assertRegex(lines[0], r'"GET [^"]+" 200 \d+ cache=miss')

    def test_index_json_is_not_read_as_a_version(self):
        harness = self.make()
        self.assertEqual(harness.get(f"/{HOST}/hashicorp/consul/index.json").status, 200)

    def test_unknown_provider_is_404(self):
        harness = self.make()
        response = harness.get(f"/{HOST}/hashicorp/nope/index.json")
        self.assertEqual(response.status, 404)

    def test_a_provider_we_cannot_serve_is_not_advertised_as_empty(self):
        harness = self.make()
        response = harness.get(f"/{HOST}/hashicorp/consul/9.9.9.json")
        self.assertEqual(response.status, 404)

    def test_mirror_can_be_switched_off(self):
        harness = self.make(provider_mirror=False)
        response = harness.get(f"/{HOST}/hashicorp/consul/index.json")
        self.assertEqual(response.status, 404)
        self.assertIn(b"no_route", response.body)

    def test_mirror_can_live_under_a_prefix(self):
        harness = self.make(provider_mirror_prefix="providers")
        self.assertEqual(harness.get("/providers/index.json").status, 404)
        response = harness.get(f"/providers/{HOST}/hashicorp/consul/index.json")
        self.assertEqual(response.status, 200)
        self.assertEqual(sorted(json.loads(response.body)["versions"]), ["1.0.0", "2.0.0"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
