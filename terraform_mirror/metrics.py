"""A small Prometheus registry: counters, gauges and histograms, standard library only.

Why hand written: the runtime must stay dependency-free (see ``AGENTS.md``), and the
exposition format is small enough to write down. What is implemented here is the subset
Prometheus actually scrapes: ``# HELP`` / ``# TYPE``, labelled samples, histograms with
cumulative buckets and ``+Inf``.

The bundle class :class:`Metrics` holds every series the proxy exports, so a reader who
wants the full metric list has exactly one place to look.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Callable, Iterable, Optional, Sequence

#: The only values ``terraform_mirror_cache_events_total{status}`` may take. A handler that invents a
#: new one lands in ``other`` instead of becoming an unbounded label value.
CACHE_STATUSES = frozenset({"hit", "miss", "stale", "stale-on-error", "refresh"})

#: Latency buckets for short HTTP calls (seconds).
HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
#: Buckets for archive downloads, which are slow on purpose.
DOWNLOAD_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0)


def format_value(value: float) -> str:
    """Render a sample the way the exposition format expects it."""
    number = float(value)
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "+Inf" if number > 0 else "-Inf"
    if number == int(number) and abs(number) < 1e15:
        return str(int(number))
    return repr(number)


def escape_label(value: object) -> str:
    """Escape a label value: backslash, quote and newline are meaningful (RFC in the spec)."""
    text = str(value)
    if not text:
        return ""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _label_pairs(labelnames: Sequence[str], values: Sequence[object]) -> list[tuple[str, str]]:
    if len(values) != len(labelnames):
        raise ValueError(f"expected {len(labelnames)} label values, got {len(values)}")
    return list(zip(labelnames, (escape_label(value) for value in values), strict=True))


def _render_labels(pairs: Iterable[tuple[str, str]]) -> str:
    rendered = ",".join(f'{name}="{value}"' for name, value in pairs)
    return "{" + rendered + "}" if rendered else ""


class Child:
    """One label combination of one metric. This is what ``.labels(...)`` hands back."""

    __slots__ = ("metric", "key", "_lock", "value", "count", "total", "buckets")

    def __init__(self, metric: "Metric", key: tuple[str, ...]) -> None:
        self.metric = metric
        self.key = key
        self._lock = threading.Lock()
        self.value = 0.0
        self.count = 0
        self.total = 0.0
        self.buckets = [0] * len(metric.buckets)

    # ---- mutations ---------------------------------------------------------
    def inc(self, value: float = 1.0) -> None:
        if value < 0:
            raise ValueError(f"{self.metric.name} is a {self.metric.kind}, it cannot decrease")
        with self._lock:
            self.value += value

    def dec(self, value: float = 1.0) -> None:
        if self.metric.kind != "gauge":
            raise ValueError(f"{self.metric.name} is a {self.metric.kind}, only a gauge can decrease")
        with self._lock:
            self.value -= value

    def set(self, value: float) -> None:
        with self._lock:
            self.value = float(value)

    def observe(self, value: float) -> None:
        """Record an observation. Only meaningful on a histogram."""
        if self.metric.kind != "histogram":
            raise ValueError(f"{self.metric.name} is a {self.metric.kind}, it has no buckets")
        number = float(value)
        bounds = self.metric.buckets
        with self._lock:
            self.count += 1
            self.total += number
            for index, bound in enumerate(bounds):
                if number <= bound:
                    self.buckets[index] += 1
                    break

    # ---- reads -------------------------------------------------------------
    def get(self) -> float:
        with self._lock:
            return self.value

    def samples(self) -> list[tuple[str, str, float]]:
        """``(suffix, labels, value)`` triples for the exposition."""
        pairs = _label_pairs(self.metric.labelnames, self.key)
        kind = self.metric.kind
        with self._lock:
            if kind == "histogram":
                out: list[tuple[str, str, float]] = []
                cumulative = 0
                for bound, count in zip(self.metric.buckets, self.buckets, strict=True):
                    cumulative += count
                    out.append(("_bucket", _render_labels(pairs + [("le", format_value(bound))]), cumulative))
                out.append(("_bucket", _render_labels(pairs + [("le", "+Inf")]), self.count))
                out.append(("_count", _render_labels(pairs), self.count))
                out.append(("_sum", _render_labels(pairs), self.total))
                return out
            return [("", _render_labels(pairs), self.value)]


class Metric:
    """A named metric with any number of label combinations."""

    def __init__(
        self,
        registry: "Registry",
        name: str,
        doc: str,
        kind: str,
        labelnames: Sequence[str] = (),
        buckets: Sequence[float] = (),
    ) -> None:
        self.name = name
        self.doc = doc
        self.kind = kind
        self.labelnames = tuple(labelnames)
        self.buckets = tuple(buckets)
        self._children: dict[tuple[str, ...], Child] = {}
        self._lock = threading.Lock()
        registry.register(self)

    def labels(self, *values: object, **kwargs: object) -> Child:
        """Return (creating if needed) the child for these label values."""
        if kwargs:
            if set(kwargs) != set(self.labelnames):
                raise ValueError(f"{self.name} needs labels {self.labelnames}, got {sorted(kwargs)}")
            key = tuple(str(kwargs[name]) for name in self.labelnames)
        else:
            if len(values) != len(self.labelnames):
                raise ValueError(f"{self.name} needs {len(self.labelnames)} label values, got {len(values)}")
            key = tuple(str(value) for value in values)
        with self._lock:
            child = self._children.get(key)
            if child is None:
                child = Child(self, key)
                self._children[key] = child
            return child

    # ---- shortcuts for the unlabelled case ---------------------------------
    def inc(self, value: float = 1.0) -> None:
        self.labels().inc(value)

    def dec(self, value: float = 1.0) -> None:
        self.labels().dec(value)

    def set(self, value: float) -> None:
        self.labels().set(value)

    def observe(self, value: float) -> None:
        self.labels().observe(value)

    # ---- introspection -----------------------------------------------------
    def get(self, *values: object, **kwargs: object) -> float:
        key = tuple(str(value) for value in values) or tuple(str(kwargs[name]) for name in self.labelnames)
        with self._lock:
            child = self._children.get(key)
        return child.get() if child else 0.0

    def keys(self) -> list[tuple[str, ...]]:
        with self._lock:
            return list(self._children)

    def forget(self, key: tuple[str, ...]) -> None:
        """Drop a label combination so it stops being exported.

        Gauges whose series come from a snapshot (the pending list) must forget the
        combinations that disappeared, otherwise a module that was cached long ago keeps
        reporting an age forever.
        """
        with self._lock:
            self._children.pop(key, None)

    def render(self) -> list[str]:
        lines = [
            f"# HELP {self.name} {self.doc.replace(chr(10), ' ')}",
            f"# TYPE {self.name} {self.kind}",
        ]
        with self._lock:
            children = list(self._children.values())
        for child in children:
            for suffix, labels, value in child.samples():
                lines.append(f"{self.name}{suffix}{labels} {format_value(value)}")
        return lines


class Registry:
    """Holds the metrics and renders them in the Prometheus text format."""

    def __init__(self) -> None:
        self._metrics: dict[str, Metric] = {}
        self._lock = threading.Lock()

    def register(self, metric: Metric) -> None:
        with self._lock:
            existing = self._metrics.get(metric.name)
            if existing is not None and existing.kind != metric.kind:
                raise ValueError(f"{metric.name} is already registered as {existing.kind}")
            self._metrics.setdefault(metric.name, metric)

    def metric(self, name: str) -> Optional[Metric]:
        with self._lock:
            return self._metrics.get(name)

    def counter(self, name: str, doc: str, labels: Sequence[str] = ()) -> Metric:
        return self._declare(name, doc, "counter", labels)

    def gauge(self, name: str, doc: str, labels: Sequence[str] = ()) -> Metric:
        return self._declare(name, doc, "gauge", labels)

    def histogram(self, name: str, doc: str, labels: Sequence[str] = (), buckets: Sequence[float] = HTTP_BUCKETS) -> Metric:
        return self._declare(name, doc, "histogram", labels, buckets)

    def _declare(self, name: str, doc: str, kind: str, labels: Sequence[str], buckets: Sequence[float] = ()) -> Metric:
        with self._lock:
            existing = self._metrics.get(name)
            if existing is not None:
                if existing.kind != kind:
                    raise ValueError(f"{name} is already registered as {existing.kind}, not {kind}")
                return existing
        return Metric(self, name, doc, kind, labels, buckets)

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._metrics)

    def render(self) -> str:
        with self._lock:
            metrics = [self._metrics[name] for name in sorted(self._metrics)]
        lines: list[str] = []
        for metric in metrics:
            lines.extend(metric.render())
        return "\n".join(lines) + "\n"


class Metrics:
    """Every series the proxy exports, plus the helpers that keep them consistent.

    ``clock`` is injectable so a test can move time without sleeping.
    """

    def __init__(
        self,
        registry: Optional[Registry] = None,
        *,
        clock: Callable[[], float] = time.time,
        version: str = "",
        max_pending_series: int = 50,
    ) -> None:
        self.registry = registry if registry is not None else Registry()
        self.clock = clock
        self.started_at = clock()
        self.max_pending_series = max(0, int(max_pending_series))
        self._legacy: dict[str, Metric] = {}
        self._legacy_lock = threading.Lock()
        self._pending_keys: set[tuple[str, str]] = set()

        reg = self.registry
        # ---- client facing HTTP -------------------------------------------
        self.http_requests = reg.counter(
            "terraform_mirror_http_requests_total",
            "Requests handled, by route, method and status class.",
            ("route", "method", "status"),
        )
        self.http_errors = reg.counter(
            "terraform_mirror_http_errors_total",
            "Requests answered with 4xx or 5xx, by route and error code.",
            ("route", "code"),
        )
        self.http_seconds = reg.histogram(
            "terraform_mirror_http_request_seconds", "Request duration in seconds.", ("route",), HTTP_BUCKETS
        )
        # ---- upstream registry --------------------------------------------
        self.upstream_requests = reg.counter(
            "terraform_mirror_upstream_requests_total",
            "Calls to the upstream registry, by operation and result.",
            ("op", "result"),
        )
        self.upstream_errors = reg.counter(
            "terraform_mirror_upstream_errors_total",
            "Failed upstream calls, by operation and reason. Alert on this: the registry is unhealthy.",
            ("op", "reason"),
        )
        self.upstream_failures = reg.gauge(
            "terraform_mirror_upstream_consecutive_failures",
            "Failed upstream calls in a row, reset by the next success.",
            ("op",),
        )
        self.upstream_last_success = reg.gauge(
            "terraform_mirror_upstream_last_success_timestamp_seconds",
            "Unix time of the last successful upstream call for an operation.",
            ("op",),
        )
        self.upstream_seconds = reg.histogram(
            "terraform_mirror_upstream_request_seconds", "Upstream call duration in seconds.", ("op",), HTTP_BUCKETS
        )
        # ---- archives ------------------------------------------------------
        self.archive_downloads = reg.counter(
            "terraform_mirror_archive_downloads_total", "Archive fetches from the origin, by result.", ("result",)
        )
        self.archive_download_seconds = reg.histogram(
            "terraform_mirror_archive_download_seconds", "Time to fetch and store one archive.", (), DOWNLOAD_BUCKETS
        )
        self.archive_pending_modules = reg.gauge(
            "terraform_mirror_archive_pending_modules",
            "Module versions clients asked for that are still not in storage.",
        )
        self.archive_pending_oldest = reg.gauge(
            "terraform_mirror_archive_pending_oldest_seconds",
            "Age of the oldest requested-but-not-downloaded module version. Alert on this.",
        )
        self.archive_pending_age = reg.gauge(
            "terraform_mirror_archive_pending_age_seconds",
            "How long each requested module version has been waiting for its archive.",
            ("module", "version"),
        )
        # ---- provider mirror ------------------------------------------------
        self.provider_events = reg.counter(
            "terraform_mirror_provider_events_total",
            "Provider mirror events: packages stored, packages that could not be served.",
            ("event",),
        )
        # ---- cache ----------------------------------------------------------
        self.cache_events = reg.counter(
            "terraform_mirror_cache_events_total",
            "Answered requests by what served them: hit, miss, stale, stale-on-error, refresh.",
            ("kind", "status"),
        )
        # ---- storage -------------------------------------------------------
        self.storage_probe_up = reg.gauge(
            "terraform_mirror_storage_probe_up", "Last storage probe: 1 reachable, 0 broken.", ("backend",)
        )
        self.storage_probe_seconds = reg.gauge(
            "terraform_mirror_storage_probe_seconds", "Duration of the last storage probe.", ("backend",)
        )
        self.storage_errors = reg.counter(
            "terraform_mirror_storage_errors_total", "Storage backend failures that were swallowed.", ("op",)
        )
        # ---- runtime -------------------------------------------------------
        self.uptime = reg.gauge("terraform_mirror_uptime_seconds", "Seconds since the process started.")
        self.threads = reg.gauge("terraform_mirror_process_threads", "Live threads in this process.")
        self.max_rss = reg.gauge("terraform_mirror_process_max_rss_bytes", "Peak resident set size in bytes.")
        self.jobs = reg.gauge("terraform_mirror_jobs", "Background queue counters.", ("name",))
        self.locks = reg.gauge("terraform_mirror_locks", "Distributed lock counters.", ("name",))
        self.build = reg.gauge("terraform_mirror_build_info", "Build information.", ("version",))
        self.build.labels(version=version or "unknown").set(1)

    # ---- legacy counters ---------------------------------------------------
    def legacy(self, name: str) -> Metric:
        """A counter for the unlabelled counters ``CacheService.incr`` maintains.

        They existed before the Prometheus endpoint did and are still what
        ``/api/v1/cache`` returns, so they are exported under the same names.
        """
        with self._legacy_lock:
            metric = self._legacy.get(name)
            if metric is None:
                metric = self.registry.counter(f"terraform_mirror_{name}", "Cache counter (see /api/v1/cache).")
                self._legacy[name] = metric
            return metric

    # ---- provider mirror ---------------------------------------------------
    def provider_event(self, event: str) -> None:
        """Count one provider mirror event. The label set is bounded on purpose."""
        self.provider_events.labels(event=event).inc()

    # ---- upstream ----------------------------------------------------------
    def upstream_ok(self, op: str, seconds: float) -> None:
        self.upstream_requests.labels(op=op, result="ok").inc()
        self.upstream_seconds.labels(op=op).observe(seconds)
        self.upstream_failures.labels(op=op).set(0)
        self.upstream_last_success.labels(op=op).set(self.clock())

    def upstream_failed(self, op: str, reason: str, seconds: float) -> None:
        self.upstream_requests.labels(op=op, result="error").inc()
        self.upstream_seconds.labels(op=op).observe(seconds)
        self.upstream_errors.labels(op=op, reason=reason or "error").inc()
        self.upstream_failures.labels(op=op).inc()

    def upstream_not_found(self, op: str, seconds: float) -> None:
        """A 404 upstream is an answer, not an outage: it must not feed the failure streak."""
        self.upstream_requests.labels(op=op, result="not_found").inc()
        self.upstream_seconds.labels(op=op).observe(seconds)
        self.upstream_failures.labels(op=op).set(0)
        self.upstream_last_success.labels(op=op).set(self.clock())

    # ---- HTTP --------------------------------------------------------------
    def observe_http(self, route: str, method: str, status: int, seconds: float, code: str = "") -> None:
        self.http_requests.labels(route=route, method=method, status=f"{status // 100}xx").inc()
        self.http_seconds.labels(route=route).observe(seconds)
        if status >= 400:
            self.http_errors.labels(route=route, code=code or f"{status}").inc()

    # ---- cache -------------------------------------------------------------
    def cache_event(self, kind: str, status: str) -> None:
        """Count one answered request by its cache status.

        ``kind`` separates the two protocols (``module``, ``provider``); an unknown status is
        bucketed as ``other`` so a new internal word cannot multiply series.
        """
        self.cache_events.labels(kind=kind, status=status if status in CACHE_STATUSES else "other").inc()

    # ---- storage -----------------------------------------------------------
    def storage_error(self, op: str) -> None:
        self.storage_errors.labels(op=op).inc()

    def storage_probe(self, backend: str, ok: bool, seconds: float) -> None:
        self.storage_probe_up.labels(backend=backend).set(1 if ok else 0)
        self.storage_probe_seconds.labels(backend=backend).set(seconds)

    def archive_download(self, result: str, seconds: Optional[float] = None) -> None:
        self.archive_downloads.labels(result=result).inc()
        if seconds is not None:
            self.archive_download_seconds.observe(seconds)

    # ---- snapshot gauges ---------------------------------------------------
    def refresh(self, cache=None) -> None:
        """Refresh the gauges that are a snapshot of live objects.

        Called by the scrape handler, so the numbers are always as fresh as the scrape
        instead of depending on someone remembering to update them.
        """
        now = self.clock()
        self.uptime.set(now - self.started_at)
        self.threads.set(threading.active_count())
        self.max_rss.set(_max_rss_bytes())
        if cache is None:
            return
        queue = getattr(cache, "queue", None)
        if queue is not None:
            for name, value in queue.stats().items():
                self.jobs.labels(name=name).set(value)
        locks = getattr(cache, "locks", None)
        if locks is not None:
            for name, value in locks.stats().items():
                self.locks.labels(name=name.removeprefix("locks_")).set(value)
        self.refresh_pending(cache, now)

    def refresh_pending(self, cache, now: Optional[float] = None) -> None:
        """Export the pending ledger: how many modules are still waiting, and for how long."""
        pending = getattr(cache, "pending", None)
        if pending is None:
            return
        stats = pending.stats()
        moment = self.clock() if now is None else now
        self.archive_pending_modules.set(stats["pending"])
        self.archive_pending_oldest.set(stats["oldest_seconds"])
        keep: set[tuple[str, str]] = set()
        # A limit of zero means "no per-module series at all", not "no limit".
        entries = stats["entries"][: self.max_pending_series]
        for entry in entries:
            key = (entry.module, entry.version)
            keep.add(key)
            self.archive_pending_age.labels(module=entry.module, version=entry.version).set(
                max(0.0, moment - entry.first_seen)
            )
        for key in self._pending_keys - keep:
            self.archive_pending_age.forget(key)
        self._pending_keys = keep


def _max_rss_bytes() -> float:
    import resource

    try:
        return float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024.0
    except (OSError, ValueError):  # pragma: no cover - platform without rusage
        return float("nan")
