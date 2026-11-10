"""HTTP layer: routing, registry protocol responses and a stdlib server."""

from __future__ import annotations

import hmac
import json
import logging
import re
import signal
import ssl
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from .cache import CacheService
from .config import Config
from .errors import BadRequest, NotFound, ProxyError, Unauthorized, UpstreamError
from .health import HealthService
from .locking import LockManager, NullLockManager
from .metrics import Metrics
from .models import ModuleRef
from .pending import PendingLedger
from .providers import ProviderCacheService, ProviderRef, ProviderRegistryClient, ProviderTrace
from .storage.base import Storage
from .upstream import X_TERRAFORM_GET
from .workers import BackgroundQueue

JSON_TYPE = "application/json"
MAX_ADMIN_BODY = 64 * 1024

_ROUTES: list[tuple[str, re.Pattern[str], str]] = [
    ("GET", re.compile(r"^/\.well-known/terraform\.json/?$"), "discovery"),
    ("GET", re.compile(r"^/healthz/?$"), "health"),
    ("GET", re.compile(r"^/readyz/?$"), "ready"),
    ("GET", re.compile(r"^/metrics/?$"), "metrics"),
    ("GET", re.compile(r"^/?$"), "index"),
    ("GET", re.compile(r"^/v1/modules/(?P<ns>[^/]+)/(?P<name>[^/]+)/(?P<prov>[^/]+)/versions/?$"), "versions"),
    (
        "GET",
        re.compile(r"^/v1/modules/(?P<ns>[^/]+)/(?P<name>[^/]+)/(?P<prov>[^/]+)/(?P<ver>[^/]+)/download/?$"),
        "download",
    ),
    ("GET", re.compile(r"^/archive/(?P<ns>[^/]+)/(?P<name>[^/]+)/(?P<prov>[^/]+)/(?P<ver>[^/]+)\.zip$"), "archive"),
    ("GET", re.compile(r"^/api/v1/cache/?$"), "cache_list"),
    ("GET", re.compile(r"^/api/v1/pending/?$"), "pending_list"),
    ("POST", re.compile(r"^/api/v1/prefetch/?$"), "prefetch"),
    (
        "DELETE",
        re.compile(r"^/api/v1/cache/(?P<ns>[^/]+)/(?P<name>[^/]+)/(?P<prov>[^/]+)/(?P<ver>[^/]+)/?$"),
        "purge_version",
    ),
    ("DELETE", re.compile(r"^/api/v1/cache/(?P<ns>[^/]+)/(?P<name>[^/]+)/(?P<prov>[^/]+)/?$"), "purge_module"),
]


def provider_routes(config: Config) -> list[tuple[str, re.Pattern[str], str]]:
    """Routes of the provider network mirror protocol, or none when it is switched off.

    The order matters: ``index.json`` has to be matched before the ``:version.json``
    pattern, which would otherwise read ``index`` as a version. The archive lives next
    to ``index.json`` — the flat layout public mirrors use — because the URL we hand out
    in ``<version>.json`` is resolved relative to that file.
    """
    if not getattr(config, "provider_mirror", True):
        return []
    prefix = str(getattr(config, "provider_mirror_prefix", "") or "").strip("/")
    stem = f"(?:/{re.escape(prefix)})?" if prefix else ""
    host = r"(?P<host>[A-Za-z0-9.-]+\.[A-Za-z]{2,})"
    ns = r"(?P<ns>[A-Za-z0-9._-]+)"
    provider = r"(?P<type>[A-Za-z0-9._-]+)"
    return [
        ("GET", re.compile(rf"^{stem}/{host}/{ns}/{provider}/index\.json$"), "provider_index"),
        ("GET", re.compile(rf"^{stem}/{host}/{ns}/{provider}/(?P<ver>[^/]+)\.json$"), "provider_version"),
        (
            "GET",
            re.compile(rf"^{stem}/{host}/{ns}/{provider}/(?P<file>terraform-provider-[^/]+\.zip)$"),
            "provider_archive",
        ),
    ]


@dataclass
class Response:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: Optional[bytes] = None
    stream: Optional[Iterator[bytes]] = None
    content_length: Optional[int] = None

    def json(self) -> dict:
        return json.loads((self.body or b"{}").decode("utf-8"))

    @property
    def length(self) -> Optional[int]:
        if self.stream is not None:
            return self.content_length
        if self.body is not None:
            return len(self.body)
        return self.content_length


def error_body(code: str, message: str) -> bytes:
    return json.dumps({"errors": [{"code": code, "message": message}]}, ensure_ascii=False).encode("utf-8")


class ProxyApplication:
    """All request handling, independent of the socket layer (easy to unit test)."""

    def __init__(
        self,
        config: Config,
        cache: CacheService,
        storage: Storage,
        queue: BackgroundQueue,
        logger: Optional[logging.Logger] = None,
        metrics: Optional[Metrics] = None,
        health: Optional[HealthService] = None,
        providers: Optional[ProviderCacheService] = None,
    ) -> None:
        self.config = config
        self.cache = cache
        self.storage = storage
        self.queue = queue
        self.logger = logger or logging.getLogger("terraform_mirror.http")
        self.metrics = metrics if metrics is not None else Metrics()
        self.health = health if health is not None else HealthService(config, storage, metrics=self.metrics, registry=cache.registry)
        #: The provider mirror shares storage, locks and the queue with the module cache.
        self.providers = providers
        self.routes = list(_ROUTES) + provider_routes(config)

    # ---- entry point -------------------------------------------------------
    def handle(
        self,
        method: str,
        path: str,
        query: str = "",
        headers: Optional[dict[str, str]] = None,
        body: bytes = b"",
    ) -> Response:
        headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        method = method.upper()
        path = path or "/"
        head_request = method == "HEAD"
        probe = "GET" if head_request else method
        started = time.perf_counter()

        for name, pattern in [(n, p) for verb, p, n in self.routes if verb == probe]:
            match = pattern.match(path)
            if not match:
                continue
            # Route on the raw path and decode each captured segment afterwards: decoding
            # first would let %2F, %3F or %23 rewrite the route itself, and a decoded
            # separator or '?' inside a segment must reach validation as a plain bad id.
            params = {key: unquote(value) for key, value in match.groupdict().items()}
            code = ""
            try:
                response = getattr(self, f"_on_{name}")(params, headers, body, query, method)
            except ProxyError as exc:
                code = exc.code
                response = self._error(exc.status, exc.code, exc.message, exc.headers)
            except Exception as exc:  # pragma: no cover - last line of defence
                code = "internal_error"
                self.logger.exception("unhandled error on %s %s", method, path)
                response = self._error(500, "internal_error", f"{type(exc).__name__}: {exc}")
            self.metrics.observe_http(name, method, response.status, time.perf_counter() - started, code)
            if not head_request:
                self._count_cache_status(name, response)
            if head_request:
                if response.content_length is None and response.body is not None:
                    response.content_length = len(response.body)
                response.body = None
                response.stream = None
            return response
        response = self._error(404, "no_route", f"no handler for {method} {path}")
        self.metrics.observe_http("unknown", method, 404, time.perf_counter() - started, "no_route")
        return response

    def _count_cache_status(self, route: str, response: Response) -> None:
        """Count one answer in ``terraform_mirror_cache_events_total``, split by protocol.

        Only requests that carry a body are counted: Terraform sends ``HEAD`` before every
        provider download, and counting that too would double each install in the hit ratio.
        A response without the header (errors, health endpoints) is not a cache event at all.
        """
        status = _response_header(response, "X-TFM-Cache")
        if not status:
            return
        self.metrics.cache_event("provider" if route.startswith("provider_") else "module", status)

    # ---- discovery / ops ---------------------------------------------------
    def _on_discovery(self, _params, _headers, _body, _query, _method) -> Response:
        return self._json(200, {"modules.v1": "/v1/modules/"}, {"Cache-Control": "public, max-age=3600"})

    def _on_index(self, _params, _headers, _body, _query, _method) -> Response:
        return self._json(
            200,
            {
                "name": "terraform-mirror",
                "modules.v1": self.config.public_base() + "/v1/modules/",
                "backends": self.storage.describe(),
            },
        )

    def _on_health(self, _params, _headers, _body, _query, _method) -> Response:
        """Liveness: the process is up and its threads are running. Never touches the network."""
        stats = self.queue.stats()
        return self._json(
            200,
            {
                "status": "ok",
                "backends": self.storage.describe(),
                "queued": stats["queued"],
                "active": stats["active"],
                "pending": self.cache.pending.stats()["pending"],
                "instance": self.cache.locks.instance,
                "locks": self.cache.locks.stats(),
            },
        )

    def _on_ready(self, _params, _headers, _body, query, _method) -> Response:
        """Readiness: can this instance serve a real ``terraform init`` right now?

        Storage must answer (and, with ``TFM_READY_UPSTREAM``, the registry too). Pass
        ``?force=1`` to skip the short result cache and probe right now.
        """
        report = self.health.check(force=_wants_force(query))
        payload = report.to_dict()
        stats = self.queue.stats()
        pending = self.cache.pending.stats()
        payload.update(
            {
                "backends": self.storage.describe(),
                "instance": self.cache.locks.instance,
                "queued": stats["queued"],
                "active": stats["active"],
                "pending": {"modules": pending["pending"], "oldest_seconds": pending["oldest_seconds"]},
            }
        )
        return self._json(200 if report.ok else 503, payload, {"Cache-Control": "no-store"})

    def _on_metrics(self, _params, _headers, _body, _query, _method) -> Response:
        self.metrics.refresh(self.cache)
        return Response(
            status=200,
            headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"},
            body=self.metrics.registry.render().encode("utf-8"),
        )

    # ---- registry protocol -------------------------------------------------
    def _on_versions(self, params, _headers, _body, _query, _method) -> Response:
        ref = ModuleRef.build(params["ns"], params["name"], params["prov"])
        document = self.cache.versions(ref)
        return Response(
            status=200,
            headers={
                "Content-Type": JSON_TYPE,
                "Cache-Control": f"public, max-age={self.config.ttl_versions}",
                "X-TFM-Cache": document.source,
                "X-TFM-Fetched-At": f"{document.fetched_at:.3f}",
                "X-TFM-Age": f"{document.age:.0f}",
            },
            body=document.payload,
        )

    def _on_download(self, params, headers, _body, _query, _method) -> Response:
        ref = ModuleRef.build(params["ns"], params["name"], params["prov"], params["ver"])
        info = self.cache.download(ref)
        if info.cacheable:
            base = self.config.public_base(headers.get("host", ""), _scheme(headers, self.config))
            subdir = f"//{info.subdir}" if info.subdir else ""
            getter = f"{base}/archive/{ref.namespace}/{ref.name}/{ref.provider}/{ref.version}.zip{subdir}"
            self.cache.schedule_archive(ref)
        else:
            getter = info.raw
            self.logger.info("%s@%s has a non-HTTP source, passing it through", ref.module_id, ref.version)
        return Response(
            status=204,
            headers={
                X_TERRAFORM_GET: getter,
                "X-TFM-Cache": info.source,
                "X-TFM-Source": "proxy" if info.cacheable else "upstream",
            },
        )

    def _on_archive(self, params, _headers, _body, _query, method) -> Response:
        ref = ModuleRef.build(params["ns"], params["name"], params["prov"], params["ver"])
        meta = self.cache.archive_meta(ref)
        source = "hit" if meta is not None else "miss"
        if meta is None:
            # The background job may still be running (or this client got here first):
            # fall back to a synchronous fetch instead of answering 404.
            meta = self.cache.ensure_archive(ref)
        try:
            handle = self.cache.open_archive(ref)
        except NotFound:
            # Purged or evicted between the stat and the open: fetch it once, then fail.
            self.cache.ensure_archive(ref)
            handle = self.cache.open_archive(ref)
        size = handle.size or (meta.size if meta else None) or 0
        headers = {
            "Content-Type": "application/zip",
            "X-TFM-Cache": source,
            "Cache-Control": "public, max-age=31536000, immutable",
        }
        if meta and meta.sha256:
            headers["ETag"] = f'"{meta.sha256}"'
        if method == "HEAD":
            handle.close()
            return Response(status=200, headers=headers, content_length=size)
        return Response(status=200, headers=headers, stream=_closing_stream(handle), content_length=size)

    # ---- provider network mirror -------------------------------------------
    def _provider_ref(self, params, version: str = "") -> ProviderRef:
        return ProviderRef(host=params["host"], namespace=params["ns"], type=params["type"], version=version)

    def _require_providers(self) -> ProviderCacheService:
        if self.providers is None:  # pragma: no cover - the routes are not registered then
            raise NotFound("the provider mirror is disabled", "no_route")
        return self.providers

    def _on_provider_index(self, params, _headers, _body, _query, _method) -> Response:
        ref = self._provider_ref(params)
        trace = ProviderTrace()
        payload = self._require_providers().index_payload(ref, trace=trace)
        age = int(getattr(self.config, "ttl_provider_versions", 3600))
        return self._json(
            200,
            payload,
            {"Cache-Control": f"public, max-age={age}", "X-TFM-Cache": trace.status},
        )

    def _on_provider_version(self, params, _headers, _body, _query, _method) -> Response:
        ref = self._provider_ref(params, params["ver"])
        trace = ProviderTrace()
        archives = self._require_providers().archives_for(ref, trace=trace)
        if not archives:
            raise UpstreamError(
                502,
                f"no package could be served for {ref} on the platforms this mirror hosts",
                "no_package",
            )
        return self._json(
            200,
            {"archives": archives},
            {"Cache-Control": "public, max-age=31536000, immutable", "X-TFM-Cache": trace.status},
        )

    def _on_provider_archive(self, params, _headers, _body, _query, method) -> Response:
        version, platform = ProviderRef.platform_from_name(params["file"])
        if not version:
            raise NotFound(f"cannot read a version and platform out of {params['file']!r}", "no_route")
        providers = self._require_providers()
        ref = self._provider_ref(params, version).with_platform(platform)
        meta = providers.meta(ref)
        source = "hit" if meta is not None else "miss"
        if meta is None:
            meta = providers.ensure(ref)
        handle = providers.open_archive(ref)
        size = handle.size or (meta or {}).get("size") or 0
        headers = {
            "Content-Type": "application/zip",
            "X-TFM-Cache": source,
            "Cache-Control": "public, max-age=31536000, immutable",
        }
        if (meta or {}).get("sha256"):
            headers["ETag"] = f'"{meta["sha256"]}"'
        if method == "HEAD":
            handle.close()
            return Response(status=200, headers=headers, content_length=size)
        return Response(status=200, headers=headers, stream=_closing_stream(handle), content_length=size)

    # ---- admin API ---------------------------------------------------------
    def _require_admin(self, headers: dict[str, str]) -> None:
        token = self.config.admin_token
        if not token:
            return
        supplied = headers.get("x-admin-token") or headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(supplied, token):
            raise Unauthorized()

    def _on_cache_list(self, _params, headers, _body, _query, _method) -> Response:
        self._require_admin(headers)
        objects = self.cache.cached_modules()
        pending = self.cache.pending.stats()
        return self._json(
            200,
            {
                "objects": len(objects),
                "modules": sorted(
                    {"/".join(key.split("/")[1:4]) for key in objects if key.startswith("modules/")}
                ),
                "keys": objects,
                "metrics": self.cache.counters(),
                "jobs": self.queue.stats(),
                "pending": {"modules": pending["pending"], "oldest_seconds": pending["oldest_seconds"]},
                "backends": self.storage.describe(),
            },
        )

    def _on_pending_list(self, _params, headers, _body, _query, _method) -> Response:
        """What clients asked for that is still not in storage, oldest first."""
        self._require_admin(headers)
        stats = self.cache.pending.stats()
        now = time.time()
        stats["entries"] = [entry.to_dict(now) for entry in stats["entries"]]
        return self._json(200, stats)

    def _on_prefetch(self, _params, headers, body, _query, _method) -> Response:
        self._require_admin(headers)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise BadRequest("request body must be JSON") from exc
        if not isinstance(payload, dict):
            raise BadRequest("request body must be a JSON object")
        ref = ModuleRef.build(
            str(payload.get("namespace", "")), str(payload.get("name", "")), str(payload.get("provider", ""))
        )
        versions = payload.get("versions")
        if isinstance(versions, str):
            versions = [versions]
        if not versions:
            document = self.cache.versions(ref)
            from .semver import newest, versions_from_payload

            latest = newest(versions_from_payload(document.json))
            versions = [latest] if latest else []
        # "force": true re-downloads archives that are already cached - the way to heal a
        # leftover from a download that died with the pod that started it.
        force = bool(payload.get("force"))
        scheduled, cached, queued = [], [], []
        for version in versions:
            one = ModuleRef.build(ref.namespace, ref.name, ref.provider, str(version))
            if not force and self.cache.has_archive(one):
                cached.append(str(version))
            elif self.cache.schedule_archive(one, force=force):
                scheduled.append(str(version))
            else:
                queued.append(str(version))
        return self._json(
            202,
            {
                "module": ref.module_id,
                "force": force,
                "scheduled": scheduled,
                "already_cached": cached,
                "already_queued": queued,
            },
        )

    def _on_purge_module(self, params, headers, _body, _query, _method) -> Response:
        self._require_admin(headers)
        ref = ModuleRef.build(params["ns"], params["name"], params["prov"])
        return self._json(200, {"removed": self.cache.purge(ref)})

    def _on_purge_version(self, params, headers, _body, _query, _method) -> Response:
        self._require_admin(headers)
        ref = ModuleRef.build(params["ns"], params["name"], params["prov"], params["ver"])
        return self._json(200, {"removed": self.cache.purge(ref)})

    # ---- helpers -----------------------------------------------------------
    def _json(self, status: int, payload: dict, headers: Optional[dict[str, str]] = None) -> Response:
        merged = {"Content-Type": JSON_TYPE}
        merged.update(headers or {})
        return Response(status=status, headers=merged, body=json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _error(self, status: int, code: str, message: str, headers: Optional[dict[str, str]] = None) -> Response:
        merged = {"Content-Type": JSON_TYPE, "Cache-Control": "no-store"}
        merged.update(headers or {})
        return Response(
            status=status,
            headers=merged,
            body=error_body(code, message),
        )


def _wants_force(query: Optional[str]) -> bool:
    """``?force`` skips the probe cache, ``?force=0`` does not."""
    values = parse_qs(query or "").get("force")
    if values is None:
        return False
    return values[0].strip().lower() not in ("", "0", "false", "no")


def _response_header(response: Response, name: str) -> str:
    """Read one response header back, case-insensitively (we set them by hand)."""
    wanted = name.lower()
    for key, value in response.headers.items():
        if key.lower() == wanted:
            return value
    return ""


def _access_log_tail(response: Response) -> str:
    """The size field of the access line: bytes, then the cache status when we know it.

    ``10.0.0.1 "GET /…zip HTTP/1.1" 200 6094041 cache=hit`` — ``cache=hit`` means storage
    alone answered it, ``miss`` means something was fetched during this request, ``stale``
    and ``stale-on-error`` that a cached copy was served while the upstream was away.
    """
    parts = ["-" if response.length is None else str(response.length)]
    cache = _response_header(response, "X-TFM-Cache")
    if cache:
        parts.append(f"cache={cache}")
    return " ".join(parts)


def _closing_stream(handle) -> Iterator[bytes]:
    try:
        yield from handle.chunks()
    finally:
        handle.close()


def _scheme(headers: dict[str, str], config: Config) -> str:
    forwarded = headers.get("x-forwarded-proto", "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return "https" if config.tls_cert else "http"


# --------------------------------------------------------------------------
# Socket server
# --------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    server_version = "terraform-mirror"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    app: ProxyApplication

    def log_message(self, fmt: str, *args) -> None:  # route to logging, not stderr
        self.app.logger.info("%s %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _read_body(self, limit: int = MAX_ADMIN_BODY) -> bytes:
        raw = self.headers.get("Content-Length")
        if not raw:
            return b""
        try:
            wanted = int(raw)
        except ValueError as exc:
            raise BadRequest("invalid Content-Length") from exc
        if wanted > limit:
            raise BadRequest(f"request body larger than {limit} bytes")
        return self.rfile.read(wanted)

    def _dispatch(self, method: str) -> None:
        parts = urlsplit(self.path)
        headers = {key: value for key, value in self.headers.items()}
        try:
            body = self._read_body() if method in ("POST", "PUT", "PATCH") else b""
            response = self.app.handle(method, parts.path, parts.query, headers, body)
        except ProxyError as exc:
            headers = {"Content-Type": JSON_TYPE, "Cache-Control": "no-store"}
            headers.update(getattr(exc, "headers", {}) or {})
            response = Response(status=exc.status, headers=headers, body=error_body(exc.code, exc.message))
        except Exception as exc:  # pragma: no cover - defensive
            self.app.logger.exception("transport error")
            response = Response(
                status=500,
                headers={"Content-Type": JSON_TYPE},
                body=error_body("internal_error", f"{type(exc).__name__}: {exc}"),
            )
        self._send(response, head=(method == "HEAD"))

    def _send(self, response: Response, head: bool = False) -> None:
        body = response.body or b""
        length = response.length
        # send_response() would log the request before anything about the response is known,
        # so the access line would carry neither the size nor the cache status. We write the
        # status line and the two standard headers ourselves and log what actually happened.
        self.send_response_only(response.status)
        self.send_header("Server", self.version_string())
        self.send_header("Date", self.date_time_string())
        self.log_request(response.status, _access_log_tail(response))
        for name, value in response.headers.items():
            self.send_header(name, value)
        if response.status not in (204, 304):
            if length is None:
                # Size unknown: frame the body as real HTTP chunks and close the socket
                # afterwards, so a client that stops reading cannot wedge keep-alive.
                self.send_header("Transfer-Encoding", "chunked")
                self.close_connection = True
            else:
                self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if head:
            return
        try:
            if response.stream is not None:
                if length is None:
                    for chunk in response.stream:
                        if chunk:
                            self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                else:
                    for chunk in response.stream:
                        self.wfile.write(chunk)
            elif body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - client hung up
            self.close_connection = True


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: ProxyApplication) -> None:
        self.app = app
        _Handler.app = app
        super().__init__(address, _Handler)

    def handle_error(self, request, client_address) -> None:  # pragma: no cover - noise filter
        import traceback

        self.app.logger.warning("connection from %s failed: %s", client_address, traceback.format_exc(limit=2))

    def serve_in_thread(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, name="terraform-mirror-http", daemon=True)
        thread.start()
        return thread

    @property
    def port(self) -> int:
        return self.server_address[1]


def build_application(config: Config) -> tuple[ProxyApplication, Storage, BackgroundQueue]:
    """Wire storage + upstream client + background queue into an application."""
    from . import __version__
    from .storage import build_storage
    from .upstream import ArchiveDownloader, RegistryClient

    logger = logging.getLogger("terraform_mirror")
    storage = build_storage(config)
    metrics = Metrics(version=__version__, max_pending_series=config.metrics_pending_series)
    registry = RegistryClient(
        config.upstream_url,
        request_timeout=config.request_timeout,
        user_agent=config.user_agent,
    )
    downloader = ArchiveDownloader(
        download_timeout=config.download_timeout,
        max_bytes=config.max_archive_bytes,
        user_agent=config.user_agent,
        tmp_dir=config.cache_dir,
    )
    queue = BackgroundQueue(workers=config.workers, maxsize=config.queue_max, logger=logger.getChild("background"))
    queue.start()
    if config.locks_enabled:
        locks: LockManager = LockManager(
            storage, ttl=config.lock_ttl, clock=time.time, logger=logger.getChild("locks")
        )
    else:
        locks = NullLockManager(ttl=config.lock_ttl, logger=logger.getChild("locks"))
    cache = CacheService(
        config=config,
        storage=storage,
        registry=registry,
        downloader=downloader,
        queue=queue,
        locks=locks,
        metrics=metrics,
        pending=PendingLedger(logger=logger.getChild("pending")),
    )
    app = ProxyApplication(
        config,
        cache,
        storage,
        queue,
        metrics=metrics,
        health=HealthService(config, storage, metrics=metrics, registry=registry, logger=logger.getChild("health")),
        providers=ProviderCacheService(
            config,
            storage,
            ProviderRegistryClient(
                config.provider_upstream_url,
                request_timeout=config.request_timeout,
                user_agent=config.user_agent,
            ),
            downloader,
            locks=locks,
            queue=queue,
            metrics=metrics,
            logger=logger.getChild("providers"),
        ),
    )
    return app, storage, queue


def make_server(config: Config, app: Optional[ProxyApplication] = None) -> ProxyServer:
    if app is None:
        app, _storage, _queue = build_application(config)
    server = ProxyServer((config.listen_host, config.listen_port), app)
    if config.tls_cert and config.tls_key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(config.tls_cert, config.tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def shutdown_application(server: "ProxyServer", app: ProxyApplication, queue: BackgroundQueue, grace: float) -> None:
    """Stop accepting, let in-flight downloads finish, then hand the locks back.

    Split out of :func:`serve` so the order is testable without sending signals: a pod that
    is being drained must not leave the lock of the download it was running behind for the
    whole ``TFM_LOCK_TTL``.
    """
    server.shutdown()
    server.server_close()
    queue.shutdown(wait=True, timeout=max(0.0, float(grace)))
    # Anything still in flight (a download longer than the grace period) gets its lock
    # deleted here instead of being left for the TTL to expire.
    app.cache.locks.release_all()


def serve(config: Config) -> int:
    """Run until SIGINT/SIGTERM, then shut the worker threads down cleanly.

    The socket is bound before the startup probe runs, so kubelet can watch ``/readyz``
    go from 503 to 200 instead of getting a connection refused. Returns the process exit
    code: 1 when the startup probe failed and ``TFM_STARTUP_REQUIRED`` is on.

    The shutdown order matters for a shared bucket: stop accepting, give in-flight
    downloads ``TFM_SHUTDOWN_GRACE`` seconds to finish and release their own locks, then
    hand back whatever is still held so the next pod does not wait out ``TFM_LOCK_TTL``.
    """
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    app, storage, queue = build_application(config)
    server = make_server(config, app)
    log = logging.getLogger("terraform_mirror")
    log.info(
        "terraform-mirror on %s://%s:%s -> upstream %s, backends %s",
        "https" if config.tls_cert else "http",
        config.listen_host,
        server.port,
        config.upstream_url,
        storage.describe(),
    )
    server.serve_in_thread()
    stop = threading.Event()

    def _shutdown(signum, _frame):  # pragma: no cover - signal path
        log.info("signal %s, shutting down", signum)
        stop.set()

    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), _shutdown)
            except (ValueError, OSError):  # pragma: no cover - not the main thread
                pass
    code = 0
    try:
        if config.startup_check:
            report = app.health.run_startup_checks()
            if not report.ok and config.startup_required:
                log.error(
                    "startup check failed (%s) and TFM_STARTUP_REQUIRED is on: exiting instead of serving",
                    report.reason,
                )
                code = 1
            else:
                stop.wait()
        else:
            log.info("startup check disabled (TFM_STARTUP_CHECK=false)")
            stop.wait()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        shutdown_application(server, app, queue, float(config.shutdown_grace))
        log.info("stopped")
    return code
