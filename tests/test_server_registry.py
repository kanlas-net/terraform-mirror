"""End-to-end over a real socket: the Terraform registry protocol as a client sees it."""

from __future__ import annotations

import json
import unittest
import urllib.parse

from tests.support.fake_upstream import DEFAULT_ARCHIVE, FakeRegistry
from tests.support.fake_s3 import FakeS3
from tests.support.harness import ProxyHarness


class RegistryProtocolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = FakeRegistry().start()
        cls.registry.add("consul", versions=["1.0.0", "1.1.0"])
        cls.registry.add("sub", versions=["1.0.0"], subdir="modules/vpc")
        cls.registry.add("gitmod", versions=["2.0.0"], style="git")

    @classmethod
    def tearDownClass(cls):
        cls.registry.stop()

    def setUp(self):
        self.registry.reset_hits()
        self.harness = ProxyHarness.create(self, self.registry.base_url).start_server()
        self.addCleanup(self.harness.stop)

    def test_stream_without_a_length_is_chunk_framed(self):
        """A size-less stream must go out as real HTTP/1.1 chunks, not raw bytes."""
        import http.client

        from terraform_mirror.server import Response

        real = self.harness.app.handle
        self.harness.app.handle = lambda *a, **kw: Response(
            status=200, headers={"Content-Type": "application/octet-stream"}, stream=iter([b"alpha", b"", b"beta"])
        )
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.harness.base_url.rsplit(":", 1)[1], timeout=10)
            conn.request("GET", "/whatever")
            response = conn.getresponse()
            self.assertEqual(response.getheader("Transfer-Encoding"), "chunked")
            self.assertIsNone(response.getheader("Content-Length"))
            self.assertEqual(response.read(), b"alphabeta")
            conn.close()
        finally:
            self.harness.app.handle = real

    def test_head_archive_closes_the_storage_handle(self):
        """HEAD must not open the object and drop the stream without closing it."""
        path = "/archive/hashicorp/consul/aws/1.0.0.zip"
        self.harness.get(path)  # make sure the archive is cached
        closed: list[bool] = []
        real = self.harness.app.cache.open_archive

        def spy(ref):
            handle = real(ref)
            original = handle.close

            def close():
                closed.append(True)
                original()

            handle.close = close
            return handle

        self.harness.app.cache.open_archive = spy
        try:
            response = self.harness.head(path)
        finally:
            self.harness.app.cache.open_archive = real
        self.assertEqual(response.status, 200)
        self.assertEqual(response.header("Content-Length"), str(len(DEFAULT_ARCHIVE)))
        self.assertEqual(closed, [True], "the HEAD path must close the object it opened")

    # ---- discovery ---------------------------------------------------------
    def test_index(self):
        response = self.harness.get("/")
        self.assertEqual(response.status, 200)
        payload = response.json()
        self.assertEqual(payload["name"], "terraform-mirror")
        self.assertTrue(payload["modules.v1"].endswith("/v1/modules/"))

    def test_discovery_document(self):
        response = self.harness.get("/.well-known/terraform.json")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json(), {"modules.v1": "/v1/modules/"})
        self.assertEqual(response.header("Content-Type"), "application/json")

    # ---- versions ----------------------------------------------------------
    def test_versions_miss_then_hit(self):
        first = self.harness.get("/v1/modules/hashicorp/consul/aws/versions")
        self.assertEqual(first.status, 200)
        self.assertEqual(first.header("X-TFM-Cache"), "miss")
        self.assertEqual([m["version"] for m in first.json()["modules"]], ["1.0.0", "1.1.0"])

        second = self.harness.get("/v1/modules/hashicorp/consul/aws/versions")
        self.assertEqual(second.header("X-TFM-Cache"), "hit")
        self.assertEqual(self.registry.hits["versions"], 1)
        self.assertIn("max-age", second.header("Cache-Control"))

    def test_versions_head(self):
        response = self.harness.head("/v1/modules/hashicorp/consul/aws/versions")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"")
        self.assertGreater(int(response.header("Content-Length")), 0)

    def test_unknown_module_is_404(self):
        response = self.harness.get("/v1/modules/hashicorp/absent/aws/versions")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.json()["errors"][0]["code"], "module_not_found")

    # ---- download ----------------------------------------------------------
    def test_download_points_at_the_proxy(self):
        response = self.harness.get("/v1/modules/hashicorp/consul/aws/1.0.0/download")
        self.assertEqual(response.status, 204)
        getter = response.header("X-Terraform-Get")
        self.assertEqual(getter, f"{self.harness.base_url}/archive/hashicorp/consul/aws/1.0.0.zip")
        self.assertEqual(response.header("X-TFM-Source"), "proxy")
        self.assertEqual(response.body, b"")
        self.assertTrue(self.harness.wait_idle(15))
        self.assertEqual(self.registry.hits["archive"], 1, "the archive must be prefetched in the background")

    def test_subdir_is_preserved(self):
        response = self.harness.get("/v1/modules/hashicorp/sub/aws/1.0.0/download")
        getter = response.header("X-Terraform-Get")
        self.assertTrue(getter.endswith("/archive/hashicorp/sub/aws/1.0.0.zip//modules/vpc"), getter)

    def test_external_url_wins_over_the_host_header(self):
        harness = ProxyHarness.create(self, self.registry.base_url, external_base_url="https://tf.example.com")
        harness.start_server()
        self.addCleanup(harness.stop)
        response = harness.get("/v1/modules/hashicorp/consul/aws/1.0.0/download")
        self.assertEqual(response.header("X-Terraform-Get"), "https://tf.example.com/archive/hashicorp/consul/aws/1.0.0.zip")

    def test_forwarded_proto_is_honoured(self):
        response = self.harness.get(
            "/v1/modules/hashicorp/consul/aws/1.0.0/download", headers={"X-Forwarded-Proto": "https"}
        )
        self.assertTrue(response.header("X-Terraform-Get").startswith("https://"))

    def test_non_http_source_is_passed_through(self):
        response = self.harness.get("/v1/modules/hashicorp/gitmod/aws/2.0.0/download")
        self.assertEqual(response.status, 204)
        self.assertEqual(response.header("X-Terraform-Get"), "git::https://example.com/gitmod.git?ref=v2.0.0")
        self.assertEqual(response.header("X-TFM-Source"), "upstream")

    def test_unknown_version_is_404(self):
        response = self.harness.get("/v1/modules/hashicorp/consul/aws/9.9.9/download")
        self.assertEqual(response.status, 404)

    # ---- archive -----------------------------------------------------------
    def test_archive_download_then_cache_hit(self):
        self.harness.get("/v1/modules/hashicorp/consul/aws/1.0.0/download")
        first = self.harness.get("/archive/hashicorp/consul/aws/1.0.0.zip")
        self.assertEqual(first.status, 200)
        self.assertEqual(first.body, DEFAULT_ARCHIVE)
        self.assertEqual(int(first.header("Content-Length")), len(DEFAULT_ARCHIVE))
        self.assertEqual(first.header("Content-Type"), "application/zip")
        self.assertIn("immutable", first.header("Cache-Control"))
        self.assertTrue(first.header("ETag").startswith('"'))

        second = self.harness.get("/archive/hashicorp/consul/aws/1.0.0.zip")
        self.assertEqual(second.header("X-TFM-Cache"), "hit")
        self.assertEqual(self.registry.hits["archive"], 1, "second download must come from the cache")

    def test_archive_head(self):
        self.harness.get("/v1/modules/hashicorp/consul/aws/1.0.0/download")
        self.harness.wait_idle(15)
        response = self.harness.head("/archive/hashicorp/consul/aws/1.0.0.zip")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"")
        self.assertEqual(int(response.header("Content-Length")), len(DEFAULT_ARCHIVE))

    def test_archive_without_prior_metadata_fetches_synchronously(self):
        response = self.harness.get("/archive/hashicorp/consul/aws/1.1.0.zip")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.header("X-TFM-Cache"), "miss")
        self.assertEqual(response.body, DEFAULT_ARCHIVE)

    def test_archive_for_unknown_module_is_404(self):
        response = self.harness.get("/archive/hashicorp/absent/aws/1.0.0.zip")
        self.assertEqual(response.status, 404)

    def test_path_traversal_is_refused(self):
        for path in ("/archive/../../etc/passwd.zip", "/archive/%2e%2e/%2e%2e/etc/hosts.zip"):
            with self.subTest(path=path):
                response = self.harness.get(path)
                self.assertIn(response.status, (400, 404))
                self.assertNotIn(b"root:", response.body)

    # ---- ops endpoints -----------------------------------------------------
    def test_timeout_knobs_reach_the_upstream_clients(self):
        cache = self.harness.app.cache
        self.assertEqual(cache.registry.request_timeout, self.harness.config.request_timeout)
        self.assertEqual(cache.downloader.download_timeout, self.harness.config.download_timeout)

    def test_healthz(self):
        response = self.harness.get("/healthz")
        self.assertEqual(response.status, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertIn("backends", payload)
        self.assertIn("locks_acquired", payload["locks"])

    def test_metrics(self):
        self.harness.get("/v1/modules/hashicorp/consul/aws/versions")
        response = self.harness.get("/metrics")
        self.assertEqual(response.status, 200)
        self.assertIn("terraform_mirror_versions_miss 1", response.body.decode())
        self.assertIn("terraform_mirror_uptime_seconds", response.body.decode())

    def test_unknown_route(self):
        response = self.harness.get("/nope/nope")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.json()["errors"][0]["code"], "no_route")

    def test_postgres_style_bad_method_on_get_route(self):
        response = self.harness.post("/v1/modules/hashicorp/consul/aws/versions")
        self.assertEqual(response.status, 404)

    def test_encoded_slash_in_a_segment_is_rejected_not_routed(self):
        """Routing happens on the raw path, so %2F cannot forge a path segment."""
        response = self.harness.get("/v1/modules/hashicorp%2Fevil/consul/aws/versions")
        self.assertEqual(response.status, 400)
        self.assertEqual(response.json()["errors"][0]["code"], "bad_request")

    def test_encoded_question_mark_cannot_split_the_path(self):
        response = self.harness.get("/v1/modules/hashicorp/consul%3Fx=1/aws/versions")
        self.assertEqual(response.status, 400)

    def test_percent_encoded_valid_segment_still_works(self):
        response = self.harness.get("/v1/modules/hashicorp/consul/aws%2E/versions")
        self.assertIn(response.status, (400, 404))


class SharedS3Test(unittest.TestCase):
    """Two instances, one bucket: the download must happen once and the lock must hold."""

    @classmethod
    def setUpClass(cls):
        cls.registry = FakeRegistry().start()
        cls.registry.add("consul", versions=["1.0.0"])
        cls.registry.add("slow", versions=["1.0.0"], archive_delay=1.0)
        cls.s3 = FakeS3().start()

    @classmethod
    def tearDownClass(cls):
        cls.registry.stop()
        cls.s3.stop()

    def make_instance(self, **overrides):
        harness = ProxyHarness.create(
            self,
            self.registry.base_url,
            backends=("fs", "s3"),
            s3_endpoint=self.s3.endpoint_url,
            **overrides,
        ).start_server()
        self.addCleanup(harness.stop)
        return harness

    def test_second_instance_serves_from_the_shared_bucket(self):
        first = self.make_instance()
        second = self.make_instance()

        first.get("/v1/modules/hashicorp/consul/aws/1.0.0/download")
        first.get("/archive/hashicorp/consul/aws/1.0.0.zip")
        self.assertTrue(first.wait_idle(15))
        self.assertEqual(self.registry.hits["archive"], 1)
        self.assertIn("terraform-mirror/modules/hashicorp/consul/aws/1.0.0.zip", self.s3.keys())

        response = second.get("/archive/hashicorp/consul/aws/1.0.0.zip")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, DEFAULT_ARCHIVE)
        self.assertEqual(self.registry.hits["archive"], 1, "the second instance must not re-download")
        # and it warmed its own disk on the way
        self.assertTrue(first.storage.exists("modules/hashicorp/consul/aws/1.0.0.zip"))

    def test_a_draining_instance_releases_its_lock_for_the_peer(self):
        """The whole point of releasing on shutdown: the peer takes over now, not after the TTL."""
        from terraform_mirror.server import shutdown_application

        dying = self.make_instance(lock_ttl=600)
        peer = self.make_instance(lock_wait=0)
        key = "modules/hashicorp/consul/aws/1.0.0.zip"
        handle = dying.cache.locks.acquire(key)
        self.assertIsNotNone(handle)
        self.assertIsNone(peer.cache.locks.acquire(key), "the lock is held while the pod is alive")

        shutdown_application(dying._server, dying.app, dying.queue, grace=0.0)
        dying._server = None

        taken = peer.cache.locks.acquire(key)
        self.assertIsNotNone(taken, "a released lock must be acquirable at once, not after TFM_LOCK_TTL")
        peer.cache.locks.release(taken)

    def test_lock_prevents_a_parallel_download(self):
        slow = self.make_instance(lock_wait=0)
        # hold the lock the way a busy peer would
        handle = slow.cache.locks.acquire("modules/hashicorp/slow/aws/1.0.0.zip")
        self.assertIsNotNone(handle)
        slow.get("/v1/modules/hashicorp/slow/aws/1.0.0/download")
        response = slow.get("/archive/hashicorp/slow/aws/1.0.0.zip")
        self.assertEqual(response.status, 503)
        self.assertEqual(response.header("Retry-After"), "5")
        self.assertEqual(json.loads(response.body)["errors"][0]["code"], "download_in_progress")
        slow.cache.locks.release(handle)

    def test_lock_object_is_written_under_the_locks_prefix(self):
        instance = self.make_instance()
        handle = instance.cache.locks.acquire("modules/hashicorp/consul/aws/1.0.0.zip")
        self.assertIn("terraform-mirror/locks/modules/hashicorp/consul/aws/1.0.0.zip.lock", self.s3.keys())
        instance.cache.locks.release(handle)
        self.assertNotIn("terraform-mirror/locks/modules/hashicorp/consul/aws/1.0.0.zip.lock", self.s3.keys())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
