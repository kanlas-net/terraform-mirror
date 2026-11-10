"""Cache policy: hits, TTL, stale-while-revalidate, archive fetching, locks."""

from __future__ import annotations

import hashlib
import threading
import time
import unittest

from terraform_mirror.errors import NotFound, ProxyError, UpstreamError
from terraform_mirror.models import ModuleRef
from terraform_mirror.server import build_application
from tests.support.fake_upstream import DEFAULT_ARCHIVE, FakeModule, FakeRegistry
from tests.support.harness import ProxyHarness

CONSUL = ModuleRef.build("hashicorp", "consul", "aws")


def ref(name: str, version: str | None = None) -> ModuleRef:
    return ModuleRef.build("hashicorp", name, "aws", version)


class CacheTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = FakeRegistry().start()
        cls.registry.add("consul", versions=["1.0.0", "1.1.0"])
        cls.registry.add("gitmod", versions=["2.0.0"], style="git")
        cls.registry.add("broken", versions=["3.0.0"], archive_status=500)
        cls.registry.add("slow", versions=["4.0.0"], archive_delay=0.4)

    @classmethod
    def tearDownClass(cls):
        cls.registry.stop()

    def setUp(self):
        self.registry.reset_hits()
        self.harness = ProxyHarness.create(self, self.registry.base_url)
        self.addCleanup(self.harness.stop)
        self.cache = self.harness.app.cache

    def wait(self, timeout: float = 15.0) -> None:
        self.assertTrue(self.harness.wait_idle(timeout), "background jobs did not finish")


class VersionsTest(CacheTestCase):
    def test_miss_then_hit(self):
        first = self.cache.versions(CONSUL)
        self.assertEqual(first.source, "miss")
        second = self.cache.versions(CONSUL)
        self.assertEqual(second.source, "hit")
        self.assertEqual(second.payload, first.payload)
        self.assertEqual(self.registry.hits["versions"], 1, "second call must not touch upstream")

    def test_unknown_module_propagates_404(self):
        with self.assertRaises(NotFound):
            self.cache.versions(ref("absent"))

    def test_stale_is_served_and_refreshed_in_background(self):
        self.cache.versions(CONSUL)
        self.assertEqual(self.registry.hits["versions"], 1)
        real_time = time.time
        self.cache.clock = lambda: real_time() + self.harness.config.ttl_versions + 1
        stale = self.cache.versions(CONSUL)
        self.assertEqual(stale.source, "stale")
        self.wait()
        self.assertEqual(self.registry.hits["versions"], 2, "stale answer must trigger a background refresh")
        self.cache.clock = real_time
        self.assertEqual(self.cache.versions(CONSUL).source, "hit")

    def test_stale_on_error_when_upstream_is_down(self):
        self.cache.versions(CONSUL)
        dead = ProxyHarness.create(self, "http://127.0.0.1:1", stale_revalidate=False)
        self.addCleanup(dead.stop)
        dead.cache.storage = self.cache.storage  # same cache directory, dead upstream
        real_time = time.time
        dead.cache.clock = lambda: real_time() + self.harness.config.ttl_versions + 1
        document = dead.cache.versions(CONSUL)
        self.assertEqual(document.source, "stale-on-error")

    def test_error_propagates_when_stale_is_disabled(self):
        harness = ProxyHarness.create(self, "http://127.0.0.1:1", stale_revalidate=False)
        self.addCleanup(harness.stop)
        with self.assertRaises(UpstreamError):
            harness.app.cache.versions(CONSUL)

    def test_prefetches_the_newest_version(self):
        self.cache.versions(CONSUL)
        self.wait()
        self.assertTrue(self.cache.has_archive(ref("consul", "1.1.0")))
        self.assertFalse(self.cache.has_archive(ref("consul", "1.0.0")))


class DownloadTest(CacheTestCase):
    def test_source_is_cached(self):
        first = self.cache.download(ref("consul", "1.0.0"))
        self.assertEqual(first.source, "miss")
        self.assertTrue(first.cacheable)
        second = self.cache.download(ref("consul", "1.0.0"))
        self.assertEqual(second.source, "hit")
        self.assertEqual(self.registry.hits["download"], 1)

    def test_subdir_and_query_survive_the_cache(self):
        self.registry.add("sub", versions=["1.0.0"], subdir="modules/vpc")
        info = self.cache.download(ref("sub", "1.0.0"))
        self.assertEqual(info.subdir, "modules/vpc")
        self.assertEqual(info.query, "ref=v1.0.0")
        cached = self.cache.download(ref("sub", "1.0.0"))
        self.assertEqual(cached.subdir, "modules/vpc")
        self.assertEqual(cached.fetch_url, info.fetch_url)

    def test_version_is_required(self):
        with self.assertRaises(ProxyError):
            self.cache.download(CONSUL)


class ArchiveTest(CacheTestCase):
    def test_fetch_once_then_served_from_cache(self):
        meta = self.cache.ensure_archive(ref("consul", "1.0.0"))
        self.assertEqual(meta.size, len(DEFAULT_ARCHIVE))
        self.assertEqual(meta.sha256, hashlib.sha256(DEFAULT_ARCHIVE).hexdigest())
        self.assertEqual(self.registry.hits["archive"], 1)
        again = self.cache.ensure_archive(ref("consul", "1.0.0"))
        self.assertEqual(again.sha256, meta.sha256)
        self.assertEqual(self.registry.hits["archive"], 1, "second call must come from the cache")
        with self.cache.open_archive(ref("consul", "1.0.0")) as handle:
            self.assertEqual(b"".join(handle.chunks()), DEFAULT_ARCHIVE)

    def test_background_schedule_is_deduplicated(self):
        target = ref("consul", "1.0.0")
        self.assertTrue(self.cache.schedule_archive(target))
        self.assertFalse(self.cache.schedule_archive(target))
        self.wait()
        self.assertTrue(self.cache.has_archive(target))
        self.assertEqual(self.registry.hits["archive"], 1)

    def test_git_source_is_not_cacheable(self):
        with self.assertRaises(ProxyError) as caught:
            self.cache.ensure_archive(ref("gitmod", "2.0.0"), wait=0)
        self.assertEqual(caught.exception.code, "not_cacheable")
        self.assertEqual(caught.exception.status, 422)
        self.assertFalse(self.cache.has_archive(ref("gitmod", "2.0.0")))

    def test_failed_download_stores_nothing(self):
        with self.assertRaises(UpstreamError):
            self.cache.ensure_archive(ref("broken", "3.0.0"), wait=0)
        self.assertFalse(self.cache.has_archive(ref("broken", "3.0.0")))
        self.assertEqual(self.harness.storage.list("modules/"), [])

    def test_force_redownloads_and_overwrites_what_is_cached(self):
        target = ref("consul", "1.0.0")
        self.cache.ensure_archive(target)
        self.assertEqual(self.registry.hits["archive"], 1)
        # Something else is sitting in the slot: a bad object, or the upstream re-published
        # the version. Without ``force`` the cache is taken at its word forever.
        self.harness.storage.put_bytes(target.archive_key(), b"leftover", content_type="application/zip")
        self.assertEqual(self.cache.archive_meta(target).size, 8)

        meta = self.cache.ensure_archive(target, force=True)
        self.assertEqual(meta.sha256, hashlib.sha256(DEFAULT_ARCHIVE).hexdigest())
        self.assertEqual(self.registry.hits["archive"], 2, "force must go back to the origin")
        self.assertEqual(self.cache.counters()["archive_refetched"], 1)
        with self.cache.open_archive(target) as handle:
            self.assertEqual(b"".join(handle.chunks()), DEFAULT_ARCHIVE)

    def test_force_also_re_resolves_the_download_source(self):
        target = ref("consul", "1.0.0")
        self.cache.ensure_archive(target)
        downloads_before = self.registry.hits["download"]
        self.cache.ensure_archive(target, force=True)
        self.assertGreater(self.registry.hits["download"], downloads_before)

    def test_force_can_be_queued_for_a_cached_archive(self):
        target = ref("consul", "1.0.0")
        self.cache.ensure_archive(target)
        self.assertFalse(self.cache.schedule_archive(target), "already cached: nothing to queue")
        self.assertTrue(self.cache.schedule_archive(target, force=True))
        self.wait()
        self.assertEqual(self.registry.hits["archive"], 2)

    def test_an_empty_leftover_is_treated_as_missing(self):
        """A zero-byte object is what a killed download or a hand-made cache leaves behind.

        Serving it would hand every client an empty zip; reporting it as missing makes the
        next request overwrite it, with no operator action.
        """
        target = ref("consul", "1.0.0")
        self.harness.storage.put_bytes(target.archive_key(), b"", content_type="application/zip")
        self.assertFalse(self.cache.has_archive(target))
        self.assertIsNone(self.cache.archive_meta(target))

        meta = self.cache.ensure_archive(target)
        self.assertEqual(meta.size, len(DEFAULT_ARCHIVE))
        self.assertGreaterEqual(self.cache.counters()["archive_broken"], 1, "the heal must be visible in the counters")

    def test_lock_held_by_another_instance_is_not_waited_for_in_background(self):
        target = ref("consul", "1.0.0")
        handle = self.cache.locks.acquire(target.archive_key())
        self.assertIsNotNone(handle)
        with self.assertRaises(ProxyError) as caught:
            self.cache.ensure_archive(target, wait=0)
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(caught.exception.code, "download_in_progress")
        self.assertEqual(caught.exception.headers.get("Retry-After"), "5")

    def test_blocking_call_waits_for_the_peer_and_then_serves(self):
        target = ref("consul", "1.0.0")
        handle = self.cache.locks.acquire(target.archive_key())

        def peer_finishes():
            time.sleep(0.3)
            self.harness.storage.put_bytes(target.archive_key(), b"stored by the peer", content_type="application/zip")
            self.cache.locks.release(handle)

        thread = threading.Thread(target=peer_finishes)
        thread.start()
        try:
            meta = self.cache.ensure_archive(target, wait=5)
            self.assertIsNotNone(meta)
            self.assertEqual(self.registry.hits["archive"], 0, "the peer fetched it, not us")
            self.assertEqual(self.cache.counters()["archive_shared_hit"], 1)
        finally:
            thread.join(5)

    def test_a_dead_peers_lock_is_broken_and_taken_over(self):
        target = ref("consul", "1.0.0")
        stale = self.cache.locks.acquire(target.archive_key(), ttl=-1)
        self.assertIsNotNone(stale, "the lock is created already expired")
        meta = self.cache.ensure_archive(target, wait=0)
        self.assertEqual(meta.size, len(DEFAULT_ARCHIVE))
        self.assertEqual(self.registry.hits["archive"], 1, "we took the download over")
        self.assertEqual(self.cache.counters()["locks_broken"], 1)

    def test_purge_removes_everything(self):
        self.cache.ensure_archive(ref("consul", "1.0.0"))
        self.cache.versions(CONSUL)
        self.wait()  # the /versions miss prefetches the latest version in the background
        removed = self.cache.purge(ref("consul", "1.0.0"))
        self.assertIn("modules/hashicorp/consul/aws/1.0.0.zip", removed)
        self.assertFalse(self.cache.has_archive(ref("consul", "1.0.0")))
        removed_all = self.cache.purge(CONSUL)
        self.assertTrue(any(key.startswith("meta/") for key in removed_all))
        self.assertEqual(self.cache.cached_modules(), [])

    def test_metrics_are_counted(self):
        self.cache.ensure_archive(ref("consul", "1.0.0"))
        self.cache.versions(CONSUL)
        metrics = self.cache.counters()
        self.assertEqual(metrics["versions_miss"], 1)
        self.assertEqual(metrics["archive_fetches"], 1)
        self.assertEqual(metrics["archive_stored"], 1)
        self.assertIn("locks_acquired", metrics)


class BackgroundRefreshFailureTest(CacheTestCase):
    """A background refresh that blows up must be counted and must keep the stale copy."""

    class Dead:
        def list_versions(self, ref):
            raise UpstreamError(502, "upstream is down")

        def download_source(self, ref):
            raise UpstreamError(502, "upstream is down")

    def test_versions_refresh_failure_is_counted(self):
        self.cache.versions(CONSUL)
        real = self.cache.registry
        self.cache.registry = self.Dead()
        try:
            with self.assertRaises(UpstreamError):
                self.cache._refresh_versions(CONSUL)
        finally:
            self.cache.registry = real
        self.assertEqual(self.cache.count("refresh_failed"), 1)
        self.assertEqual(self.cache.versions(CONSUL).source, "hit", "the cached document survives")

    def test_download_refresh_failure_is_counted(self):
        target = ref("consul", "1.0.0")
        self.cache.download(target)
        real = self.cache.registry
        self.cache.registry = self.Dead()
        try:
            with self.assertRaises(UpstreamError):
                self.cache._refresh_download(target)
        finally:
            self.cache.registry = real
        self.assertEqual(self.cache.count("refresh_failed"), 1)
        self.assertEqual(self.cache.download(target).source, "hit")


class LockingDisabledTest(CacheTestCase):
    def test_locks_can_be_turned_off(self):
        harness = ProxyHarness.create(self, self.registry.base_url, locks_enabled=False)
        self.addCleanup(harness.stop)
        meta = harness.app.cache.ensure_archive(ref("consul", "1.0.0"))
        self.assertIsNotNone(meta.sha256)
        self.assertEqual(harness.storage.list("locks/"), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
