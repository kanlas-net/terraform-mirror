"""Readiness and startup checks: is this instance safe to hand traffic to?

Three different questions live here.

``/healthz`` (liveness) asks "is the process wedged" and never touches the network, so a
broken bucket can never get a healthy process killed.

``/readyz`` (readiness) asks "can this instance serve a real ``terraform init``": the
storage backends must answer, and optionally the upstream registry too. Results are
cached for a couple of seconds because kubelet polls this endpoint far more often than
anything else does, and an S3 round trip on every scrape is a waste.

The same checks run once at startup, so a container that cannot reach its bucket says so
in its own log instead of serving 500s to every client.

:func:`probe_local_endpoint` asks the third question — "does *this* process answer on its
own socket" — which is what the image ``HEALTHCHECK`` runs.
"""

from __future__ import annotations

import logging
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Optional

STORAGE_PREFIX = "storage:"
UPSTREAM_CHECK = "upstream"
#: Listen addresses that mean "every interface"; loopback is what to probe instead.
WILDCARD_HOSTS = ("", "0.0.0.0", "::", "*")
DEFAULT_PROBE_TIMEOUT = 5.0


def local_probe_url(
    environ: Optional[Mapping[str, str]] = None,
    *,
    ready: bool = False,
    host: str = "",
    port: Optional[int] = None,
    tls: Optional[bool] = None,
) -> str:
    """Build the URL of this process' own health endpoint from the environment.

    Reads ``TFM_HOST`` / ``TFM_PORT`` / ``TFM_TLS_CERT`` directly instead of building a
    ``Config`` on purpose: the probe has to work even when the rest of the configuration
    is broken (a bad bucket setting must not look like a dead process), and the image
    ``HEALTHCHECK`` runs with nothing but the container ENV available.

    ``TFM_TLS_CERT`` decides the scheme. ``make_server`` wraps *the same* socket in TLS,
    so a plain ``http`` probe against a TLS instance dies with ``ConnectionResetError``
    and the container is marked unhealthy forever while serving clients perfectly.
    """
    env = os.environ if environ is None else environ
    target = host or (env.get("TFM_HOST") or "").strip()
    target_port = str(port) if port is not None else (str(env.get("TFM_PORT") or "").strip() or "8080")
    if tls is None:
        tls = bool((env.get("TFM_TLS_CERT") or "").strip())
    if target in WILDCARD_HOSTS:
        target = "127.0.0.1"
    if ":" in target and not target.startswith("["):
        target = f"[{target}]"  # IPv6 literal needs brackets in a URL authority
    return f"{'https' if tls else 'http'}://{target}:{target_port}{'/readyz' if ready else '/healthz'}"


def probe_http(url: str, *, timeout: float = DEFAULT_PROBE_TIMEOUT) -> Check:
    """GET a health URL and report what came back.

    An ``https`` probe does not verify the certificate: the cert is issued for the public
    hostname (``tf.example.com``), while the probe connects to loopback, and a local
    self-signed cert is normal here. The question is "is the socket answering", not "is
    the chain trusted" — that is what a real client in front of the proxy is for.
    """
    context: Optional[ssl.SSLContext] = None
    if url.startswith("https://"):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=context) as response:
            status = getattr(response, "status", 200)
            ok, detail = 200 <= int(status) < 300, f"HTTP {status}"
    except urllib.error.HTTPError as exc:  # 4xx/5xx: /readyz answers 503 when not ready
        ok, detail = False, f"HTTP {exc.code}"
    except Exception as exc:  # DNS, refused, reset, TLS handshake, timeout
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    return Check(url, ok, detail, time.perf_counter() - started)


def probe_local_endpoint(
    environ: Optional[Mapping[str, str]] = None,
    *,
    url: str = "",
    ready: bool = False,
    timeout: Optional[float] = None,
) -> Check:
    """Probe this instance' own HTTP endpoint (``terraform-mirror ping``)."""
    env = os.environ if environ is None else environ
    target = url or local_probe_url(env, ready=ready)
    if timeout is None:
        try:
            timeout = float(env.get("TFM_HEALTH_TIMEOUT") or DEFAULT_PROBE_TIMEOUT)
        except ValueError:
            timeout = DEFAULT_PROBE_TIMEOUT
    return probe_http(target, timeout=max(0.1, float(timeout)))


@dataclass
class Check:
    """One named probe result."""

    name: str
    ok: bool
    detail: str = ""
    seconds: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "seconds": round(self.seconds, 4)}


@dataclass
class HealthReport:
    """Everything the probes knew at one moment."""

    ok: bool
    checks: list[Check] = field(default_factory=list)
    taken_at: float = 0.0
    cached: bool = False
    degraded: bool = False

    @property
    def reason(self) -> str:
        broken = [check for check in self.checks if not check.ok]
        if not broken:
            return "ok"
        return "; ".join(f"{check.name}: {check.detail}" for check in broken)

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "degraded" if self.degraded else ("ok" if self.ok else "unavailable"),
            "reason": self.reason,
            "cached": self.cached,
            "checks": [check.to_dict() for check in self.checks],
        }


class HealthService:
    """Runs the probes and remembers the last answer for a moment."""

    def __init__(
        self,
        config,
        storage,
        *,
        metrics=None,
        registry=None,
        sleep=time.sleep,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config
        self.storage = storage
        self.metrics = metrics
        #: ``RegistryClient`` or None; only consulted when ``ready_upstream`` is on.
        self.registry = registry
        self.sleep = sleep
        self.logger = logger or logging.getLogger("terraform_mirror.health")
        self._lock = threading.Lock()
        self._cached: Optional[HealthReport] = None
        self._cached_at = 0.0
        self._clock = time.monotonic

    # ---- probes ------------------------------------------------------------
    def check(self, *, force: bool = False, write: Optional[bool] = None) -> HealthReport:
        """Run the checks, or reuse the last result if it is fresh enough."""
        ttl = max(0.0, float(getattr(self.config, "health_cache_ttl", 0.0)))
        now = self._clock()
        if not force and self._cached is not None and (now - self._cached_at) < ttl:
            cached = self._cached
            return HealthReport(cached.ok, cached.checks, cached.taken_at, True, cached.degraded)
        with self._lock:
            now = self._clock()
            if not force and self._cached is not None and (now - self._cached_at) < ttl:
                cached = self._cached
                return HealthReport(cached.ok, cached.checks, cached.taken_at, True, cached.degraded)
            report = self._probe(write if write is not None else bool(self.config.health_probe_write))
            self._cached = report
            self._cached_at = now
            return report

    def _probe(self, write: bool) -> HealthReport:
        checks: list[Check] = []
        for result in self.storage.probes(write):
            checks.append(Check(f"{STORAGE_PREFIX}{result.backend}", result.ok, result.detail, result.seconds))
            if self.metrics is not None:
                self.metrics.storage_probe(result.backend, result.ok, result.seconds)
        storage_checks = [check for check in checks if check.name.startswith(STORAGE_PREFIX)]
        storage_ok = all(check.ok for check in storage_checks) if storage_checks else False
        if not getattr(self.config, "ready_strict", True):
            storage_ok = any(check.ok for check in storage_checks)
        upstream_ok = True
        if getattr(self.config, "ready_upstream", False) and self.registry is not None:
            ok, detail, seconds = self.registry.probe()
            checks.append(Check(UPSTREAM_CHECK, ok, detail, seconds))
            upstream_ok = ok
        ok = storage_ok and upstream_ok
        return HealthReport(ok, checks, time.time(), False, ok and not all(check.ok for check in checks))

    # ---- startup -----------------------------------------------------------
    def run_startup_checks(self) -> HealthReport:
        """Probe storage before serving, retrying while a bucket is still waking up.

        A write probe is always part of the startup check: read-only credentials pass a
        read probe and then fail every single download.
        """
        attempts = max(1, int(self.config.startup_check_retries))
        interval = max(0.0, float(self.config.startup_check_interval))
        report = HealthReport(False, [], time.time())
        for attempt in range(1, attempts + 1):
            report = self.check(force=True, write=True)
            if report.ok:
                self.logger.info(
                    "startup check passed (%s)%s",
                    ", ".join(f"{check.name} {check.seconds * 1000:.0f}ms" for check in report.checks),
                    " with a write probe" if self.config.health_probe_write else "",
                )
                return report
            if attempt < attempts and interval:
                self.logger.warning(
                    "startup check failed (%s), retry %d/%d in %.1fs",
                    report.reason,
                    attempt,
                    attempts,
                    interval,
                )
                self.sleep(interval)
        self.logger.error("startup check failed: %s", report.reason)
        return report
