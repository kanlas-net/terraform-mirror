"""Prometheus metrics: the registry primitives and what the proxy actually exports."""

from __future__ import annotations

import socket
import unittest

from terraform_mirror.metrics import Metrics, Registry, format_value
from terraform_mirror.models import ModuleRef
from terraform_mirror.pending import PendingLedger
from tests.support.fake_provider_registry import FakeProviderRegistry
from tests.support.fake_upstream import FakeRegistry
from tests.support.harness import ProxyHarness


def samples(body: str) -> dict[str, float]:
    """Parse an exposition body into ``{"sample[name]{labels}": value}``."""
    out: dict[str, float] = {}
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _sep, value = line.rpartition(" ")
        out[name] = float(value)
    return out


def dead_port() -> int:
    """A port nothing listens on: the proxy gets ECONNREFUSED immediately."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class FormatTest(unittest.TestCase):
    def test_integers_have_no_decimal_point(self):
        self.assertEqual(format_value(1), "1")
        self.assertEqual(format_value(1.0), "1")
        self.assertEqual(format_value(0), "0")

    def test_floats_keep_their_fraction(self):
        self.assertEqual(format_value(1.5), "1.5")

    def test_infinities_and_nan(self):
        self.assertEqual(format_value(float("inf")), "+Inf")
        self.assertEqual(format_value(float("-inf")), "-Inf")
        self.assertEqual(format_value(float("nan")), "NaN")


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.registry = Registry()

    def test_counter_with_labels(self):
        counter = self.registry.counter("terraform_mirror_test_total", "Doc.", ("route",))
        counter.labels(route="versions").inc()
        counter.labels(route="versions").inc()
        counter.labels(route="download").inc(3)
        body = self.registry.render()
        self.assertIn("# TYPE terraform_mirror_test_total counter", body)
        self.assertIn('terraform_mirror_test_total{route="versions"} 2', body)
        self.assertIn('terraform_mirror_test_total{route="download"} 3', body)

    def test_labels_can_be_passed_as_keywords(self):
        counter = self.registry.counter("terraform_mirror_kw_total", "Doc.", ("a", "b"))
        counter.labels(b="two", a="one").inc()
        self.assertIn('terraform_mirror_kw_total{a="one",b="two"} 1', self.registry.render())

    def test_wrong_label_count_is_a_programming_error(self):
        counter = self.registry.counter("terraform_mirror_bad_total", "Doc.", ("a",))
        with self.assertRaises(ValueError):
            counter.labels()

    def test_gauge_can_go_down(self):
        gauge = self.registry.gauge("terraform_mirror_gauge", "Doc.")
        gauge.set(5)
        gauge.dec(2)
        self.assertEqual(gauge.get(), 3)

    def test_counter_refuses_to_decrease(self):
        counter = self.registry.counter("terraform_mirror_never_total", "Doc.")
        counter.inc()
        with self.assertRaises(ValueError):
            counter.dec()

    def test_histogram_is_cumulative_and_ends_at_plus_inf(self):
        histogram = self.registry.histogram("terraform_mirror_seconds", "Doc.", ("op",), (0.1, 1.0))
        for value in (0.05, 0.5, 5.0):
            histogram.labels(op="versions").observe(value)
        body = samples(self.registry.render())
        self.assertEqual(body['terraform_mirror_seconds_bucket{op="versions",le="0.1"}'], 1)
        self.assertEqual(body['terraform_mirror_seconds_bucket{op="versions",le="1"}'], 2)
        self.assertEqual(body['terraform_mirror_seconds_bucket{op="versions",le="+Inf"}'], 3)
        self.assertEqual(body['terraform_mirror_seconds_count{op="versions"}'], 3)
        self.assertAlmostEqual(body['terraform_mirror_seconds_sum{op="versions"}'], 5.55, places=6)

    def test_label_values_are_escaped(self):
        counter = self.registry.counter("terraform_mirror_esc_total", "Doc.", ("path",))
        counter.labels(path='a"b\\c').inc()
        self.assertIn(r'terraform_mirror_esc_total{path="a\"b\\c"} 1', self.registry.render())

    def test_registering_the_same_name_with_another_kind_fails(self):
        self.registry.counter("terraform_mirror_clash", "Doc.")
        with self.assertRaises(ValueError):
            self.registry.gauge("terraform_mirror_clash", "Doc.")

    def test_declaring_the_same_metric_twice_returns_the_same_object(self):
        first = self.registry.counter("terraform_mirror_twice", "Doc.")
        second = self.registry.counter("terraform_mirror_twice", "Doc.")
        self.assertIs(first, second)

    def test_forget_stops_exporting_a_series(self):
        gauge = self.registry.gauge("terraform_mirror_age", "Doc.", ("module",))
        gauge.labels(module="a").set(10)
        self.assertIn('terraform_mirror_age{module="a"} 10', self.registry.render())
        gauge.forget(("a",))
        self.assertNotIn('terraform_mirror_age{module="a"}', self.registry.render())

    def test_help_and_type_are_emitted_for_every_metric(self):
        self.registry.counter("terraform_mirror_helped_total", "What it does.")
        lines = self.registry.render().splitlines()
        self.assertIn("# HELP terraform_mirror_helped_total What it does.", lines)
        self.assertIn("# TYPE terraform_mirror_helped_total counter", lines)


class MetricsBundleTest(unittest.TestCase):
    def test_a_success_resets_the_failure_streak(self):
        metrics = Metrics()
        metrics.upstream_failed("versions", "unreachable", 0.1)
        metrics.upstream_failed("versions", "unreachable", 0.1)
        self.assertEqual(metrics.upstream_failures.get("versions"), 2)
        metrics.upstream_ok("versions", 0.1)
        self.assertEqual(metrics.upstream_failures.get("versions"), 0)

    def test_a_404_upstream_is_not_an_outage(self):
        metrics = Metrics()
        metrics.upstream_not_found("versions", 0.1)
        body = metrics.registry.render()
        self.assertIn('terraform_mirror_upstream_requests_total{op="versions",result="not_found"} 1', body)
        self.assertNotIn("terraform_mirror_upstream_errors_total{", body)
        self.assertEqual(metrics.upstream_failures.get("versions"), 0)

    def test_http_errors_only_count_4xx_and_5xx(self):
        metrics = Metrics()
        metrics.observe_http("versions", "GET", 200, 0.01)
        metrics.observe_http("versions", "GET", 502, 0.01, "upstream_error")
        body = metrics.registry.render()
        self.assertIn('terraform_mirror_http_requests_total{route="versions",method="GET",status="2xx"} 1', body)
        self.assertIn('terraform_mirror_http_errors_total{route="versions",code="upstream_error"} 1', body)

    def test_pending_series_are_forgotten_when_the_module_lands(self):
        metrics = Metrics()
        ledger = PendingLedger()
        ref = ModuleRef.build("hashicorp", "consul", "aws", "1.0.0")
        ledger.note(ref)

        class Cache:
            pending = ledger

        metrics.refresh_pending(Cache())
        self.assertIn('terraform_mirror_archive_pending_age_seconds{module="hashicorp/consul/aws",version="1.0.0"}', metrics.registry.render())
        ledger.fulfill(ref)
        metrics.refresh_pending(Cache())
        self.assertEqual(metrics.archive_pending_modules.get(), 0)
        self.assertNotIn("terraform_mirror_archive_pending_age_seconds{", metrics.registry.render())


class PendingLedgerTest(unittest.TestCase):
    def setUp(self):
        self.clock = [1000.0]
        self.ledger = PendingLedger(clock=lambda: self.clock[0])
        self.ref = ModuleRef.build("hashicorp", "consul", "aws", "1.0.0")

    def test_note_is_idempotent(self):
        self.assertTrue(self.ledger.note(self.ref))
        self.assertFalse(self.ledger.note(self.ref))
        self.assertEqual(len(self.ledger), 1)

    def test_versionless_refs_are_ignored(self):
        self.assertFalse(self.ledger.note(ModuleRef.build("hashicorp", "consul", "aws")))

    def test_attempt_records_the_error(self):
        self.ledger.note(self.ref)
        self.clock[0] += 30
        self.ledger.attempt(self.ref, "UpstreamError: HTTP 500")
        entry = self.ledger.get(self.ref)
        self.assertEqual(entry.attempts, 1)
        self.assertEqual(entry.last_error, "UpstreamError: HTTP 500")
        self.assertEqual(entry.age(self.clock[0]), 30)

    def test_fulfill_and_abandon_remove_the_entry(self):
        self.ledger.note(self.ref)
        self.assertTrue(self.ledger.fulfill(self.ref))
        self.assertFalse(self.ledger.fulfill(self.ref))
        self.ledger.note(self.ref)
        self.assertTrue(self.ledger.abandon(self.ref, "not published upstream"))
        self.assertEqual(len(self.ledger), 0)
        self.assertEqual(self.ledger.abandoned, 1)

    def test_stats_are_ordered_with_the_oldest_first(self):
        old = ModuleRef.build("hashicorp", "old", "aws", "1.0.0")
        new = ModuleRef.build("hashicorp", "new", "aws", "1.0.0")
        self.ledger.note(old)
        self.clock[0] += 60
        self.ledger.note(new)
        stats = self.ledger.stats()
        self.assertEqual(stats["pending"], 2)
        self.assertEqual([entry.module for entry in stats["entries"]], ["hashicorp/old/aws", "hashicorp/new/aws"])
        self.assertEqual(stats["oldest_seconds"], 60)

    def test_the_ledger_is_bounded(self):
        small = PendingLedger(clock=lambda: self.clock[0], max_entries=2)
        for index in range(5):
            small.note(ModuleRef.build("hashicorp", f"m{index}", "aws", "1.0.0"))
        self.assertEqual(len(small), 2)

    def test_forget_module_drops_every_version(self):
        self.ledger.note(self.ref)
        self.ledger.note(ModuleRef.build("hashicorp", "consul", "aws", "2.0.0"))
        self.ledger.note(ModuleRef.build("hashicorp", "etcd", "aws", "1.0.0"))
        self.assertEqual(self.ledger.forget_module("hashicorp/consul/aws"), 2)
        self.assertEqual(len(self.ledger), 1)


class MetricsEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = FakeRegistry().start()
        cls.registry.add("consul", versions=["1.0.0", "1.1.0"])

    @classmethod
    def tearDownClass(cls):
        cls.registry.stop()

    def make(self, **overrides) -> ProxyHarness:
        harness = ProxyHarness.create(self, self.registry.base_url, prefetch_latest=False, **overrides)
        self.addCleanup(harness.stop)
        return harness.start_server()

    def body(self, harness: ProxyHarness) -> dict[str, float]:
        response = harness.get("/metrics")
        self.assertEqual(response.status, 200)
        self.assertIn("text/plain", response.header("Content-Type") or "")
        return samples(response.body.decode("utf-8"))

    def test_exposition_has_help_and_type_for_every_family(self):
        harness = self.make()
        text = harness.get("/metrics").body.decode("utf-8")
        self.assertIn("# HELP terraform_mirror_http_requests_total", text)
        self.assertIn("# TYPE terraform_mirror_upstream_requests_total counter", text)
        self.assertIn("# TYPE terraform_mirror_http_request_seconds histogram", text)
        self.assertIn("terraform_mirror_build_info{version=", text)

    def test_a_version_request_is_counted_end_to_end(self):
        harness = self.make()
        self.assertEqual(harness.get("/v1/modules/hashicorp/consul/aws/versions").status, 200)
        body = self.body(harness)
        self.assertEqual(body['terraform_mirror_http_requests_total{route="versions",method="GET",status="2xx"}'], 1)
        self.assertEqual(body['terraform_mirror_upstream_requests_total{op="versions",result="ok"}'], 1)
        self.assertGreater(body['terraform_mirror_upstream_request_seconds_count{op="versions"}'], 0)
        self.assertEqual(body["terraform_mirror_versions_miss"], 1)

    def test_upstream_outage_is_counted_for_alerting(self):
        harness = ProxyHarness.create(self, f"http://127.0.0.1:{dead_port()}")
        self.addCleanup(harness.stop)
        harness.start_server()
        response = harness.get("/v1/modules/hashicorp/consul/aws/versions")
        self.assertEqual(response.status, 502)
        body = self.body(harness)
        self.assertEqual(body['terraform_mirror_upstream_errors_total{op="versions",reason="unreachable"}'], 1)
        self.assertEqual(body['terraform_mirror_upstream_consecutive_failures{op="versions"}'], 1)
        self.assertEqual(body['terraform_mirror_http_errors_total{route="versions",code="upstream_error"}'], 1)

    def test_a_missing_module_is_not_an_outage(self):
        harness = self.make()
        self.assertEqual(harness.get("/v1/modules/hashicorp/nope/aws/versions").status, 404)
        body = self.body(harness)
        self.assertEqual(body['terraform_mirror_upstream_requests_total{op="versions",result="not_found"}'], 1)
        self.assertNotIn('terraform_mirror_upstream_errors_total{op="versions"', self.rendered(harness))

    def rendered(self, harness: ProxyHarness) -> str:
        return harness.get("/metrics").body.decode("utf-8")

    def test_legacy_counters_are_still_exported(self):
        harness = self.make()
        harness.get("/archive/hashicorp/consul/aws/1.0.0.zip")
        body = self.body(harness)
        self.assertEqual(body["terraform_mirror_archive_fetches"], 1)
        self.assertGreater(body["terraform_mirror_archive_bytes"], 0)
        self.assertEqual(body['terraform_mirror_archive_downloads_total{result="ok"}'], 1)

    def test_process_and_queue_gauges_are_refreshed_on_scrape(self):
        harness = self.make()
        body = self.body(harness)
        self.assertGreaterEqual(body["terraform_mirror_uptime_seconds"], 0)
        self.assertGreater(body["terraform_mirror_process_threads"], 0)
        self.assertIn("terraform_mirror_jobs{name=\"queued\"}", body)
        self.assertIn("terraform_mirror_locks{name=\"acquired\"}", body)
        self.assertIn("terraform_mirror_locks{name=\"held\"}", body, "how many locks this instance still owns")


class PendingMetricsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = FakeRegistry().start()
        cls.module = cls.registry.add("slow", versions=["1.0.0"])

    @classmethod
    def tearDownClass(cls):
        cls.registry.stop()

    def setUp(self):
        self.module.archive_status = 500
        self.harness = ProxyHarness.create(self, self.registry.base_url, prefetch_latest=False).start_server()
        self.addCleanup(self.harness.stop)

    def body(self) -> dict[str, float]:
        return samples(self.harness.get("/metrics").body.decode("utf-8"))

    def test_a_failed_download_stays_pending(self):
        response = self.harness.get("/archive/hashicorp/slow/aws/1.0.0.zip")
        self.assertEqual(response.status, 502)
        body = self.body()
        self.assertEqual(body["terraform_mirror_archive_pending_modules"], 1)
        self.assertGreaterEqual(body["terraform_mirror_archive_pending_oldest_seconds"], 0)
        self.assertIn(
            'terraform_mirror_archive_pending_age_seconds{module="hashicorp/slow/aws",version="1.0.0"}',
            self.harness.get("/metrics").body.decode(),
        )
        self.assertEqual(body['terraform_mirror_archive_downloads_total{result="error"}'], 1)

    def test_pending_endpoint_explains_the_wait(self):
        self.harness.get("/archive/hashicorp/slow/aws/1.0.0.zip")
        payload = self.harness.get("/api/v1/pending").json()
        self.assertEqual(payload["pending"], 1)
        entry = payload["entries"][0]
        self.assertEqual(entry["module"], "hashicorp/slow/aws")
        self.assertEqual(entry["version"], "1.0.0")
        self.assertGreaterEqual(entry["attempts"], 1)
        self.assertIn("HTTP 500", entry["last_error"])

    def test_a_successful_download_clears_the_pending_series(self):
        self.harness.get("/archive/hashicorp/slow/aws/1.0.0.zip")
        self.assertEqual(self.body()["terraform_mirror_archive_pending_modules"], 1)
        self.module.archive_status = 200
        self.assertEqual(self.harness.get("/archive/hashicorp/slow/aws/1.0.0.zip").status, 200)
        body = self.body()
        self.assertEqual(body["terraform_mirror_archive_pending_modules"], 0)
        self.assertEqual(body["terraform_mirror_archive_pending_oldest_seconds"], 0)
        self.assertNotIn("terraform_mirror_archive_pending_age_seconds{", self.harness.get("/metrics").body.decode())

    def test_a_non_http_source_is_dropped_from_the_pending_list(self):
        git = self.registry.add("gitmod", versions=["1.0.0"], style="git")
        response = self.harness.get("/archive/hashicorp/gitmod/aws/1.0.0.zip")
        self.assertEqual(response.status, 422)
        self.assertEqual(self.body()["terraform_mirror_archive_pending_modules"], 0)
        self.registry.modules.remove(git)

    def test_pending_series_can_be_capped(self):
        harness = ProxyHarness.create(
            self, self.registry.base_url, prefetch_latest=False, metrics_pending_series=0
        ).start_server()
        self.addCleanup(harness.stop)
        harness.get("/archive/hashicorp/slow/aws/1.0.0.zip")
        body = harness.get("/metrics").body.decode()
        self.assertIn("terraform_mirror_archive_pending_modules 1", body)
        self.assertNotIn("terraform_mirror_archive_pending_age_seconds{", body)


class CacheEventsTest(unittest.TestCase):
    """``terraform_mirror_cache_events_total``: how much of the traffic the cache answered."""

    @classmethod
    def setUpClass(cls):
        cls.registry = FakeRegistry().start()
        cls.registry.add("consul", versions=["1.0.0"])
        cls.provider_registry = FakeProviderRegistry().start()
        cls.provider_registry.add("hashicorp/consul", versions=("1.0.0",))

    @classmethod
    def tearDownClass(cls):
        cls.registry.stop()
        cls.provider_registry.stop()

    def setUp(self):
        self.harness = ProxyHarness.create(
            self,
            self.registry.base_url,
            prefetch_latest=False,
            provider_upstream_url=self.provider_registry.base_url,
        ).start_server()
        self.addCleanup(self.harness.stop)

    def body(self) -> dict[str, float]:
        return samples(self.harness.get("/metrics").body.decode("utf-8"))

    def count(self, body, kind, status):
        return body.get(f'terraform_mirror_cache_events_total{{kind="{kind}",status="{status}"}}', 0)

    def test_a_miss_then_a_hit_are_counted_separately(self):
        self.harness.get("/v1/modules/hashicorp/consul/aws/versions")
        self.harness.get("/v1/modules/hashicorp/consul/aws/versions")
        body = self.body()
        self.assertEqual(self.count(body, "module", "miss"), 1)
        self.assertEqual(self.count(body, "module", "hit"), 1)

    def test_the_provider_mirror_is_its_own_kind(self):
        self.harness.get("/registry.terraform.io/hashicorp/consul/index.json")
        self.harness.get("/registry.terraform.io/hashicorp/consul/index.json")
        body = self.body()
        self.assertEqual(self.count(body, "provider", "miss"), 1)
        self.assertEqual(self.count(body, "provider", "hit"), 1)

    def test_head_is_not_counted_twice(self):
        """Terraform sends HEAD before a provider download; that must not double the ratio."""
        self.harness.get("/archive/hashicorp/consul/aws/1.0.0.zip")  # the miss
        self.harness.head("/archive/hashicorp/consul/aws/1.0.0.zip")
        self.harness.head("/archive/hashicorp/consul/aws/1.0.0.zip")
        body = self.body()
        self.assertEqual(self.count(body, "module", "miss"), 1)
        self.assertEqual(self.count(body, "module", "hit"), 0)

    def test_errors_are_not_cache_events(self):
        self.assertEqual(self.harness.get("/v1/modules/hashicorp/nope/aws/versions").status, 404)
        self.assertEqual(self.harness.get("/healthz").status, 200)
        self.assertEqual(self.harness.get("/readyz").status, 200)
        self.assertNotIn("terraform_mirror_cache_events_total{", self.harness.get("/metrics").body.decode())

    def test_an_unknown_status_cannot_become_a_label(self):
        metrics = Metrics()
        metrics.cache_event("module", "brand-new-word")
        metrics.cache_event("module", "hit")
        body = samples(metrics.registry.render())
        self.assertEqual(body['terraform_mirror_cache_events_total{kind="module",status="other"}'], 1)
        self.assertEqual(body['terraform_mirror_cache_events_total{kind="module",status="hit"}'], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
