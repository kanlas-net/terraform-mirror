"""Admin/ops API: token protection, prefetch, purge, cache listing."""

from __future__ import annotations

import json
import unittest

from tests.support.fake_upstream import DEFAULT_ARCHIVE, FakeRegistry
from tests.support.harness import ProxyHarness


class AdminApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = FakeRegistry().start()
        cls.registry.add("consul", versions=["1.0.0", "1.1.0"])

    @classmethod
    def tearDownClass(cls):
        cls.registry.stop()

    def setUp(self):
        self.registry.reset_hits()
        self.harness = ProxyHarness.create(self, self.registry.base_url, admin_token="s3cret").start_server()
        self.addCleanup(self.harness.stop)
        self.auth = {"X-Admin-Token": "s3cret"}

    def body(self, **payload) -> bytes:
        return json.dumps(payload).encode("utf-8")

    # ---- authentication ----------------------------------------------------
    def test_admin_endpoints_need_the_token(self):
        for method, path in (
            ("GET", "/api/v1/cache"),
            ("POST", "/api/v1/prefetch"),
            ("DELETE", "/api/v1/cache/hashicorp/consul/aws"),
            ("DELETE", "/api/v1/cache/hashicorp/consul/aws/1.0.0"),
        ):
            with self.subTest(method=method, path=path):
                response = self.harness.request(method, path, body=b"{}" if method == "POST" else None)
                self.assertEqual(response.status, 401)
                self.assertEqual(response.json()["errors"][0]["code"], "unauthorized")

    def test_wrong_token_is_rejected(self):
        response = self.harness.get("/api/v1/cache", headers={"X-Admin-Token": "wrong"})
        self.assertEqual(response.status, 401)

    def test_bearer_authorization_is_accepted(self):
        response = self.harness.get("/api/v1/cache", headers={"Authorization": "Bearer s3cret"})
        self.assertEqual(response.status, 200)

    def test_registry_endpoints_stay_public(self):
        self.assertEqual(self.harness.get("/v1/modules/hashicorp/consul/aws/versions").status, 200)

    def test_open_admin_when_no_token_is_configured(self):
        open_harness = ProxyHarness.create(self, self.registry.base_url).start_server()
        self.addCleanup(open_harness.stop)
        self.assertEqual(open_harness.get("/api/v1/cache").status, 200)

    # ---- prefetch ----------------------------------------------------------
    def test_prefetch_reports_already_queued(self):
        body = self.body(namespace="hashicorp", name="consul", provider="aws", versions="1.0.0")
        self.harness.post("/api/v1/prefetch", body=body, headers=self.auth)
        second = self.harness.post("/api/v1/prefetch", body=body, headers=self.auth)
        payload = second.json()
        self.assertEqual(payload["already_queued"] + payload["already_cached"], ["1.0.0"])
        self.assertTrue(self.harness.wait_idle(15))

    def test_prefetch_specific_version(self):
        response = self.harness.post(
            "/api/v1/prefetch",
            body=self.body(namespace="hashicorp", name="consul", provider="aws", versions="1.0.0"),
            headers=self.auth,
        )
        self.assertEqual(response.status, 202)
        self.assertEqual(response.json()["scheduled"], ["1.0.0"])
        self.assertTrue(self.harness.wait_idle(15))
        self.assertEqual(self.registry.hits["archive"], 1)
        listing = self.harness.get("/api/v1/cache", headers=self.auth).json()
        self.assertIn("modules/hashicorp/consul/aws/1.0.0.zip", listing["keys"])
        self.assertEqual(listing["modules"], ["hashicorp/consul/aws"])

    def test_prefetch_latest_when_no_version_given(self):
        harness = ProxyHarness.create(self, self.registry.base_url, prefetch_latest=False).start_server()
        self.addCleanup(harness.stop)
        self.auth = {"X-Admin-Token": "s3cret"}
        self.harness = harness
        response = self.harness.post(
            "/api/v1/prefetch",
            body=self.body(namespace="hashicorp", name="consul", provider="aws"),
            headers=self.auth,
        )
        self.assertEqual(response.json()["scheduled"], ["1.1.0"])
        self.assertTrue(self.harness.wait_idle(15))
        self.assertEqual(self.registry.hits["archive"], 1)

    def test_prefetch_list_of_versions(self):
        response = self.harness.post(
            "/api/v1/prefetch",
            body=self.body(namespace="hashicorp", name="consul", provider="aws", versions=["1.0.0", "1.1.0"]),
            headers=self.auth,
        )
        self.assertEqual(sorted(response.json()["scheduled"]), ["1.0.0", "1.1.0"])
        self.assertTrue(self.harness.wait_idle(20))
        self.assertEqual(self.registry.hits["archive"], 2)

    def test_prefetch_force_redownloads_a_cached_archive(self):
        """``force`` is how an operator heals an archive the cache should not be trusted on."""
        body = self.body(namespace="hashicorp", name="consul", provider="aws", versions="1.0.0")
        self.harness.post("/api/v1/prefetch", body=body, headers=self.auth)
        self.assertTrue(self.harness.wait_idle(15))
        self.assertEqual(self.registry.hits["archive"], 1)

        plain = self.harness.post("/api/v1/prefetch", body=body, headers=self.auth)
        self.assertEqual(plain.json()["already_cached"], ["1.0.0"])
        self.assertEqual(self.registry.hits["archive"], 1, "without force the cached copy stands")

        forced = self.harness.post(
            "/api/v1/prefetch",
            body=self.body(namespace="hashicorp", name="consul", provider="aws", versions="1.0.0", force=True),
            headers=self.auth,
        )
        self.assertEqual(forced.status, 202)
        payload = forced.json()
        self.assertTrue(payload["force"])
        self.assertEqual(payload["scheduled"], ["1.0.0"])
        self.assertTrue(self.harness.wait_idle(15))
        self.assertEqual(self.registry.hits["archive"], 2, "force must re-fetch from the origin")

    def test_prefetch_validates_input(self):
        bad_bodies = [b"not json", b"[]", b'{"namespace":"hashicorp"}', b'{"namespace":"..","name":"a","provider":"aws"}']
        for body in bad_bodies:
            with self.subTest(body=body):
                response = self.harness.post("/api/v1/prefetch", body=body, headers=self.auth)
                self.assertEqual(response.status, 400)

    def test_body_size_is_limited(self):
        response = self.harness.post("/api/v1/prefetch", body=b"x" * (70 * 1024), headers=self.auth)
        self.assertEqual(response.status, 400)

    # ---- listing and purge -------------------------------------------------
    def test_cache_listing(self):
        self.harness.get("/archive/hashicorp/consul/aws/1.0.0.zip")
        listing = self.harness.get("/api/v1/cache", headers=self.auth).json()
        self.assertEqual(listing["objects"], 1)
        self.assertIn("metrics", listing)
        self.assertIn("jobs", listing)
        self.assertEqual(listing["backends"]["backend"], "fs")

    def test_purge_version(self):
        self.harness.get("/archive/hashicorp/consul/aws/1.0.0.zip")
        response = self.harness.delete("/api/v1/cache/hashicorp/consul/aws/1.0.0", headers=self.auth)
        self.assertEqual(response.status, 200)
        self.assertIn("modules/hashicorp/consul/aws/1.0.0.zip", response.json()["removed"])
        self.assertEqual(self.harness.get("/api/v1/cache", headers=self.auth).json()["objects"], 0)

    def test_purge_module(self):
        self.harness.get("/v1/modules/hashicorp/consul/aws/versions")
        self.harness.get("/archive/hashicorp/consul/aws/1.0.0.zip")
        self.harness.wait_idle(15)
        response = self.harness.delete("/api/v1/cache/hashicorp/consul/aws", headers=self.auth)
        removed = response.json()["removed"]
        self.assertTrue(any(key.startswith("meta/") for key in removed))
        self.assertTrue(any(key.startswith("modules/") for key in removed))
        self.assertEqual(self.harness.get("/api/v1/cache", headers=self.auth).json()["objects"], 0)

    def test_purge_unknown_module_is_harmless(self):
        response = self.harness.delete("/api/v1/cache/hashicorp/absent/aws", headers=self.auth)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()["removed"], [])

    def test_metrics_reflect_activity(self):
        self.harness.get("/archive/hashicorp/consul/aws/1.0.0.zip")
        body = self.harness.get("/metrics").body.decode()
        self.assertIn("terraform_mirror_archive_fetches 1", body)
        self.assertIn("terraform_mirror_archive_bytes", body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
