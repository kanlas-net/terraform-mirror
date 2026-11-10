"""Health and readiness: liveness stays cheap, readiness proves the storage really answers."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import socket
import tempfile
import unittest
from unittest import mock

from tests.support.certs import write_self_signed_pair
from tests.support.fake_s3 import FakeS3
from tests.support.fake_upstream import FakeRegistry
from tests.support.harness import ProxyHarness
from terraform_mirror.__main__ import main
from terraform_mirror.config import Config
from terraform_mirror.health import local_probe_url, probe_http, probe_local_endpoint
from terraform_mirror.server import build_application
from terraform_mirror.storage.base import PROBE_KEY, ProbeResult, Storage
from terraform_mirror.upstream import RegistryClient

HAS_CRYPTOGRAPHY = importlib.util.find_spec("cryptography") is not None


def dead_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def list_requests(fake: FakeS3) -> int:
    """How many ListObjectsV2 calls the fake saw (that is what a read probe does)."""
    return len([row for row in fake.requests if row[0] == "GET" and row[1] == ""])


class BrokenStorage(Storage):
    """A backend whose every read explodes: the probe must report, not raise."""

    name = "broken"

    def exists(self, key):  # pragma: no cover - unused by the probe
        raise RuntimeError("nope")

    def stat(self, key):
        raise RuntimeError("disk on fire")

    def open(self, key):  # pragma: no cover
        raise RuntimeError("nope")

    def put_file(self, key, path, **kwargs):  # pragma: no cover
        raise RuntimeError("nope")

    def put_bytes(self, key, data, **kwargs):  # pragma: no cover
        raise RuntimeError("nope")

    def delete(self, key):  # pragma: no cover
        raise RuntimeError("nope")

    def list(self, prefix=""):  # pragma: no cover
        raise RuntimeError("nope")


class ProbeUnitTest(unittest.TestCase):
    def test_a_backend_that_raises_is_reported_not_raised(self):
        result = BrokenStorage().probe()
        self.assertFalse(result.ok)
        self.assertIn("disk on fire", result.detail)
        self.assertEqual(result.backend, "broken")

    def test_write_probe_reports_a_mismatch(self):
        class LyingStorage(BrokenStorage):
            name = "lying"

            def stat(self, key):
                return None

            def put_bytes(self, key, data, **kwargs):
                return None

            def read_or_none(self, key):
                return b"something else"

            def delete(self, key):
                return True

        ok, detail = LyingStorage().probe_write()
        self.assertFalse(ok)
        self.assertIn("read back", detail)

    def test_write_probe_uses_a_key_of_its_own_process(self):
        """Two replicas on one bucket must not delete each other's probe object."""

        class Recorder(BrokenStorage):
            name = "recorder"

            def __init__(self):
                self.written: list[str] = []
                self.deleted: list[str] = []
                self.body: dict[str, bytes] = {}

            def stat(self, key):
                return None

            def put_bytes(self, key, data, **kwargs):
                self.written.append(key)
                self.body[key] = data

            def read_or_none(self, key):
                return self.body.get(key)

            def delete(self, key):
                self.deleted.append(key)
                self.body.pop(key, None)
                return True

        storage = Recorder()
        self.assertTrue(storage.probe_write()[0])
        self.assertTrue(storage.probe_write()[0])
        self.assertEqual(len(set(storage.written)), 1, "one process reuses its own probe object")
        self.assertIn(str(os.getpid()), storage.written[0])
        self.assertNotEqual(storage.written[0], PROBE_KEY, "the shared key would collide between replicas")
        self.assertEqual(storage.deleted, storage.written)

    def test_a_write_probe_that_could_not_write_deletes_nothing(self):
        class ReadOnly(BrokenStorage):
            name = "read-only"

            def stat(self, key):
                return None

            def put_bytes(self, key, data, **kwargs):
                raise RuntimeError("AccessDenied")

            def delete(self, key):  # pragma: no cover - a probe that wrote nothing must not delete
                raise AssertionError("the probe deleted an object it never wrote")

        ok, detail = ReadOnly().probe_write()
        self.assertFalse(ok)
        self.assertIn("AccessDenied", detail)

    def test_probe_result_serialises(self):
        payload = ProbeResult("fs", True, "ok", True, 0.0001).to_dict()
        self.assertEqual(payload["backend"], "fs")
        self.assertTrue(payload["write"])


class ReadinessFilesystemTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = FakeRegistry().start()
        cls.registry.add("consul", versions=["1.0.0"])

    @classmethod
    def tearDownClass(cls):
        cls.registry.stop()

    def make(self, **overrides) -> ProxyHarness:
        harness = ProxyHarness.create(self, self.registry.base_url, **overrides)
        self.addCleanup(harness.stop)
        return harness.start_server()

    def test_readyz_is_ok_when_the_disk_backend_answers(self):
        harness = self.make()
        response = harness.get("/readyz")
        self.assertEqual(response.status, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["checks"][0]["name"], "storage:fs")
        self.assertTrue(payload["checks"][0]["ok"])
        self.assertIn("backends", payload)
        self.assertIn("pending", payload)

    def test_the_write_probe_leaves_nothing_behind(self):
        harness = self.make()
        harness.get("/readyz?force=1")
        leftovers = [
            name
            for _root, _dirs, files in os.walk(harness.config.cache_dir)
            for name in files
            if name.startswith("probe")
        ]
        self.assertEqual(leftovers, [])

    def test_force_zero_keeps_the_cached_result(self):
        harness = self.make(health_cache_ttl=60.0)
        self.assertFalse(harness.get("/readyz").json()["cached"])
        self.assertTrue(harness.get("/readyz?force=0").json()["cached"])
        self.assertTrue(harness.get("/readyz?xforce=1").json()["cached"])
        self.assertFalse(harness.get("/readyz?force=1").json()["cached"])

    def test_healthz_never_probes_and_always_answers(self):
        harness = self.make()
        response = harness.get("/healthz")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_readiness_result_is_cached_between_scrapes(self):
        harness = self.make()

        class CountingStorage:
            def __init__(self) -> None:
                self.calls: list[bool] = []

            def probes(self, write: bool = False) -> list[ProbeResult]:
                self.calls.append(write)
                return [ProbeResult("fs", True, "counted", write, 0.0)]

        counter = CountingStorage()
        harness.app.health.storage = counter
        for _ in range(3):
            self.assertEqual(harness.get("/readyz").status, 200)
        self.assertEqual(counter.calls, [True])
        self.assertEqual(harness.get("/readyz?force=1").status, 200)
        self.assertEqual(counter.calls, [True, True])

    def test_a_broken_disk_makes_the_instance_not_ready(self):
        harness = self.make()
        harness.app.health.storage = BrokenStorage()
        response = harness.get("/readyz?force=1")
        self.assertEqual(response.status, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "unavailable")
        self.assertIn("disk on fire", payload["reason"])
        # Liveness is untouched: a broken bucket must not get the process killed.
        self.assertEqual(harness.get("/healthz").status, 200)

    def test_startup_check_passes_and_reports_timings(self):
        harness = self.make()
        sleeps: list[float] = []
        harness.app.health.sleep = sleeps.append
        report = harness.app.health.run_startup_checks()
        self.assertTrue(report.ok)
        self.assertEqual(sleeps, [])

    def test_startup_check_retries_before_giving_up(self):
        harness = self.make()
        sleeps: list[float] = []
        harness.app.health.sleep = sleeps.append
        harness.app.health.storage = BrokenStorage()
        harness.config.startup_check_retries = 3
        harness.config.startup_check_interval = 2.5
        report = harness.app.health.run_startup_checks()
        self.assertFalse(report.ok)
        self.assertEqual(sleeps, [2.5, 2.5])


class ReadinessS3Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = FakeRegistry().start()
        cls.registry.add("consul", versions=["1.0.0"])
        cls.s3 = FakeS3().start()

    @classmethod
    def tearDownClass(cls):
        cls.registry.stop()
        cls.s3.stop()

    def setUp(self):
        self.s3.reset()
        self.s3.allow_unsigned = True

    def make(self, **overrides) -> ProxyHarness:
        harness = ProxyHarness.create(
            self,
            self.registry.base_url,
            backends=("fs", "s3"),
            s3_endpoint=self.s3.endpoint_url,
            locks_enabled=False,
            **overrides,
        )
        self.addCleanup(harness.stop)
        return harness.start_server()

    def test_bucket_reachable_is_ready(self):
        harness = self.make()
        response = harness.get("/readyz")
        self.assertEqual(response.status, 200)
        names = {check["name"]: check["ok"] for check in response.json()["checks"]}
        self.assertEqual(names, {"storage:fs": True, "storage:s3": True})
        self.assertGreaterEqual(list_requests(self.s3), 1)

    def test_write_probe_writes_and_cleans_up(self):
        harness = self.make()
        self.assertEqual(harness.get("/readyz?force=1").status, 200)
        self.assertIn("PUT", [row[0] for row in self.s3.requests])
        leftovers = [key for key in self.s3.objects if key.startswith("terraform-mirror/health/")]
        self.assertEqual(leftovers, [])

    def test_read_only_probe_does_not_write(self):
        harness = self.make(health_probe_write=False)
        self.assertEqual(harness.get("/readyz?force=1").status, 200)
        self.assertNotIn("PUT", [row[0] for row in self.s3.requests])

    def test_bad_credentials_make_the_instance_not_ready(self):
        harness = self.make(s3_access_key="wrong-key")
        response = harness.get("/readyz")
        self.assertEqual(response.status, 503)
        payload = response.json()
        self.assertIn("403", payload["reason"])
        self.assertIn("SignatureDoesNotMatch", payload["reason"])
        self.assertEqual(harness.get("/healthz").status, 200)

    def test_permissive_readiness_keeps_serving_from_the_healthy_tier(self):
        harness = self.make(s3_access_key="wrong-key", ready_strict=False)
        response = harness.get("/readyz")
        self.assertEqual(response.status, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "degraded")

    def test_a_missing_bucket_is_reported_by_name(self):
        harness = ProxyHarness.create(
            self,
            self.registry.base_url,
            backends=("s3",),
            s3_endpoint=self.s3.endpoint_url,
            s3_bucket="another-bucket",
            locks_enabled=False,
        )
        self.addCleanup(harness.stop)
        harness.start_server()
        payload = harness.get("/readyz").json()
        self.assertEqual(payload["status"], "unavailable")
        self.assertIn("NoSuchBucket", payload["reason"])

    def test_readiness_cache_protects_the_bucket_from_kubelet(self):
        harness = self.make()
        for _ in range(4):
            harness.get("/readyz")
        self.assertEqual(list_requests(self.s3), 1)
        harness.get("/readyz?force=1")
        self.assertEqual(list_requests(self.s3), 2)


class UpstreamReadinessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = FakeRegistry().start()
        cls.registry.add("consul", versions=["1.0.0"])

    @classmethod
    def tearDownClass(cls):
        cls.registry.stop()

    def test_probe_accepts_any_http_answer(self):
        client = RegistryClient(self.registry.base_url)
        ok, detail, seconds = client.probe()
        self.assertTrue(ok)
        self.assertIn("HTTP", detail)
        self.assertGreaterEqual(seconds, 0)

    def test_probe_reports_an_unreachable_registry(self):
        client = RegistryClient(f"http://127.0.0.1:{dead_port()}", request_timeout=1.0)
        ok, detail, _seconds = client.probe()
        self.assertFalse(ok)
        self.assertIn("unreachable", detail)

    def test_upstream_is_not_part_of_readiness_by_default(self):
        harness = ProxyHarness.create(self, f"http://127.0.0.1:{dead_port()}")
        self.addCleanup(harness.stop)
        harness.start_server()
        payload = harness.get("/readyz").json()
        self.assertEqual(payload["status"], "ok")
        self.assertNotIn("upstream", [check["name"] for check in payload["checks"]])

    def test_ready_upstream_lets_the_registry_take_an_instance_out(self):
        harness = ProxyHarness.create(self, self.registry.base_url, ready_upstream=True)
        self.addCleanup(harness.stop)
        harness.start_server()
        self.assertEqual(harness.get("/readyz").status, 200)
        harness.app.health.registry = RegistryClient(f"http://127.0.0.1:{dead_port()}", request_timeout=1.0)
        response = harness.get("/readyz?force=1")
        self.assertEqual(response.status, 503)
        self.assertIn("upstream", response.json()["reason"])


class HealthcheckCommandTest(unittest.TestCase):
    """``main()`` configures logging for the whole process: keep that noise out of the suite."""

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def test_exit_zero_when_the_disk_answers(self):
        with tempfile.TemporaryDirectory() as tempdir:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["healthcheck", "--backend", "fs", "--cache-dir", tempdir])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "ok")

    def test_exit_one_when_the_bucket_is_unreachable(self):
        with tempfile.TemporaryDirectory() as tempdir:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(
                    [
                        "healthcheck",
                        "--backend",
                        "s3",
                        "--cache-dir",
                        tempdir,
                        "--s3-bucket",
                        "probe-bucket",
                        "--s3-region",
                        "ru-central1",
                        "--s3-endpoint",
                        f"http://127.0.0.1:{dead_port()}",
                        "--s3-access-key",
                        "AK",
                        "--s3-secret-key",
                        "secret",
                    ]
                )
        self.assertEqual(code, 1)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "unavailable")

    def test_pending_can_be_included(self):
        with tempfile.TemporaryDirectory() as tempdir:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                main(["healthcheck", "--backend", "fs", "--cache-dir", tempdir, "--pending"])
        self.assertIn("pending", json.loads(out.getvalue()))


class LocalProbeUrlTest(unittest.TestCase):
    """Where ``terraform-mirror ping`` (and therefore the image HEALTHCHECK) knocks.

    The old probe was a hardcoded ``http://127.0.0.1:8080/healthz``: on a TLS instance the
    very same socket is wrapped in TLS, and on any instance with ``TFM_PORT`` set the port
    was simply wrong. Both made a perfectly serving container "unhealthy" forever.
    """

    def test_plain_http_on_the_default_port(self):
        self.assertEqual(local_probe_url({}), "http://127.0.0.1:8080/healthz")

    def test_tls_env_switches_the_scheme(self):
        env = {"TFM_PORT": "8443", "TFM_TLS_CERT": "/etc/tf/cert.pem", "TFM_TLS_KEY": "/etc/tf/key.pem"}
        self.assertEqual(local_probe_url(env), "https://127.0.0.1:8443/healthz")

    def test_blank_tls_env_stays_plain_http(self):
        self.assertEqual(local_probe_url({"TFM_TLS_CERT": "  "}), "http://127.0.0.1:8080/healthz")

    def test_a_bound_address_is_probed_instead_of_loopback(self):
        self.assertEqual(local_probe_url({"TFM_HOST": "10.0.0.7"}), "http://10.0.0.7:8080/healthz")

    def test_wildcard_hosts_probe_loopback(self):
        for host in ("", "0.0.0.0", "::"):
            with self.subTest(host=host):
                self.assertEqual(local_probe_url({"TFM_HOST": host}), "http://127.0.0.1:8080/healthz")

    def test_an_ipv6_literal_gets_brackets(self):
        self.assertEqual(local_probe_url({"TFM_HOST": "::1", "TFM_PORT": "9090"}), "http://[::1]:9090/healthz")

    def test_ready_endpoint(self):
        env = {"TFM_PORT": "8443", "TFM_TLS_CERT": "/c"}
        self.assertEqual(local_probe_url(env, ready=True), "https://127.0.0.1:8443/readyz")


class ProbeHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = FakeRegistry().start()
        cls.registry.add("consul", versions=["1.0.0"])

    @classmethod
    def tearDownClass(cls):
        cls.registry.stop()

    def make(self, **overrides) -> ProxyHarness:
        harness = ProxyHarness.create(self, self.registry.base_url, **overrides)
        self.addCleanup(harness.stop)
        return harness.start_server()

    def test_200_is_ok(self):
        harness = self.make()
        check = probe_http(f"{harness.base_url}/healthz")
        self.assertTrue(check.ok, check.detail)
        self.assertEqual(check.detail, "HTTP 200")

    def test_a_refused_socket_is_not_ok(self):
        check = probe_http(f"http://127.0.0.1:{dead_port()}/healthz", timeout=2.0)
        self.assertFalse(check.ok)
        self.assertIn("URLError", check.detail)

    def test_a_non_2xx_is_reported_as_a_status(self):
        harness = self.make(ready_upstream=True)
        harness.app.health.registry = RegistryClient(f"http://127.0.0.1:{dead_port()}", request_timeout=1.0)
        check = probe_http(f"{harness.base_url}/readyz?force=1")
        self.assertFalse(check.ok)
        self.assertEqual(check.detail, "HTTP 503")


@unittest.skipUnless(HAS_CRYPTOGRAPHY, "cryptography is not installed")
class TlsProbeTest(unittest.TestCase):
    """The whole point of ``terraform-mirror ping``: TLS on the same socket, probe follows."""

    @classmethod
    def setUpClass(cls):
        cls.registry = FakeRegistry().start()
        cls.registry.add("consul", versions=["1.0.0"])

    @classmethod
    def tearDownClass(cls):
        cls.registry.stop()

    def test_tls_socket_needs_a_tls_probe(self):
        with tempfile.TemporaryDirectory() as tempdir:
            cert, key = write_self_signed_pair(tempdir)
            harness = ProxyHarness.create(self, self.registry.base_url, tls_cert=cert, tls_key=key)
            self.addCleanup(harness.stop)
            harness.start_server()
            env = {"TFM_HOST": "127.0.0.1", "TFM_PORT": str(harness.config.listen_port), "TFM_TLS_CERT": cert}

            check = probe_local_endpoint(env)
            self.assertTrue(check.ok, check.detail)
            self.assertTrue(check.name.startswith("https://"), check.name)

            # The bug this guards: the same socket, probed over plain http.
            plain = probe_http(f"http://127.0.0.1:{harness.config.listen_port}/healthz", timeout=2.0)
            self.assertFalse(plain.ok, "a TLS socket must not answer a plain http probe")

    def test_the_certificate_name_is_not_checked(self):
        """The cert is issued for ``tf.example.lan``, the probe connects to 127.0.0.1."""
        with tempfile.TemporaryDirectory() as tempdir:
            cert, key = write_self_signed_pair(tempdir, common_name="tf.example.lan")
            harness = ProxyHarness.create(self, self.registry.base_url, tls_cert=cert, tls_key=key)
            self.addCleanup(harness.stop)
            harness.start_server()
            url = f"https://127.0.0.1:{harness.config.listen_port}/healthz"
            self.assertTrue(probe_http(url).ok)


class PingCommandTest(unittest.TestCase):
    """``terraform-mirror ping`` is what the image HEALTHCHECK runs, so it must stay cheap."""

    @classmethod
    def setUpClass(cls):
        cls.registry = FakeRegistry().start()
        cls.registry.add("consul", versions=["1.0.0"])

    @classmethod
    def tearDownClass(cls):
        cls.registry.stop()

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.out = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def make(self, **overrides) -> ProxyHarness:
        harness = ProxyHarness.create(self, self.registry.base_url, **overrides)
        self.addCleanup(harness.stop)
        return harness.start_server()

    def test_exit_zero_against_a_live_endpoint(self):
        harness = self.make()
        self.assertEqual(main(["ping", "--url", f"{harness.base_url}/healthz"]), 0)
        self.assertIn("HTTP 200", self.out.getvalue())

    def test_exit_one_when_nothing_listens(self):
        self.assertEqual(main(["ping", "--url", f"http://127.0.0.1:{dead_port()}/healthz"]), 1)
        self.assertIn("fail", self.out.getvalue())

    def test_the_environment_decides_the_endpoint(self):
        harness = self.make()
        with mock.patch.dict(os.environ, {"TFM_PORT": str(harness.config.listen_port)}, clear=False):
            self.assertEqual(main(["ping"]), 0)

    def test_ready_flag_probes_readyz(self):
        harness = self.make(ready_upstream=True)
        harness.app.health.registry = RegistryClient(f"http://127.0.0.1:{dead_port()}", request_timeout=1.0)
        with mock.patch.dict(os.environ, {"TFM_PORT": str(harness.config.listen_port)}, clear=False):
            self.assertEqual(main(["ping", "--ready"]), 1)
        self.assertIn("HTTP 503", self.out.getvalue())

    def test_a_broken_storage_config_does_not_stop_a_liveness_ping(self):
        """Liveness must not depend on storage settings: no config, no application."""
        harness = self.make()
        with mock.patch.dict(os.environ, {"TFM_BACKENDS": "s3"}, clear=False):
            self.assertEqual(main(["ping", "--url", f"{harness.base_url}/healthz"]), 0)


class BuildApplicationTest(unittest.TestCase):
    def test_the_application_wires_one_metrics_object_everywhere(self):
        with tempfile.TemporaryDirectory() as tempdir:
            config = Config(listen_port=0, cache_dir=tempdir, backends=["fs"]).validate()
            app, _storage, queue = build_application(config)
            self.addCleanup(queue.shutdown, wait=True, timeout=3.0)
            self.assertIs(app.metrics, app.cache.metrics)
            self.assertIs(app.health.metrics, app.metrics)
            report = app.health.check(force=True)
            self.assertTrue(report.ok)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
