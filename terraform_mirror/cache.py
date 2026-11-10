"""The cache itself: metadata documents, archives and the background refresh policy."""

from __future__ import annotations

import json
import logging
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Callable, Optional

from .errors import NotFound, ProxyError, UpstreamError
from .locking import LockManager, NullLockManager
from .metrics import Metrics
from .models import GetterSource, ModuleRef, parse_getter_source
from .pending import PendingLedger
from .semver import newest, versions_from_payload
from .storage.base import ObjectMeta, Storage
from .upstream import X_TERRAFORM_GET, ArchiveDownloader, RegistryClient
from .workers import BackgroundQueue

#: Raised when the upstream source cannot be mirrored (``git::`` sources and friends).
NOT_CACHEABLE = "not_cacheable"


@dataclass
class Document:
    """A cached registry document plus where it came from."""

    payload: bytes
    source: str  # hit | miss | stale | stale-on-error
    fetched_at: float
    age: float

    @property
    def json(self):
        return json.loads(self.payload.decode("utf-8"))


@dataclass
class DownloadInfo:
    """The resolved ``X-Terraform-Get`` for one module version."""

    source: str
    fetched_at: float
    getter: str
    url: str
    subdir: str = ""
    query: str = ""
    raw: str = ""

    @property
    def cacheable(self) -> bool:
        return self.getter in ("", "http", "https") and self.url.startswith(("http://", "https://"))

    @property
    def fetch_url(self) -> str:
        return f"{self.url}?{self.query}" if self.query else self.url

    def to_getter(self) -> GetterSource:
        return GetterSource(getter=self.getter, url=self.url, subdir=self.subdir, query=self.query, raw=self.raw)


class CacheService:
    """Coordinates upstream lookups, the storage backends and the background queue."""

    def __init__(self, config, storage: Storage, registry: RegistryClient, downloader: ArchiveDownloader, queue: BackgroundQueue, clock: Callable[[], float] = time.time, logger: Optional[logging.Logger] = None, locks: Optional[LockManager] = None, metrics: Optional[Metrics] = None, pending: Optional[PendingLedger] = None) -> None:
        self.config = config
        self.storage = storage
        self.registry = registry
        self.downloader = downloader
        self.queue = queue
        self.clock = clock
        self.logger = logger or logging.getLogger("terraform_mirror.cache")
        self.locks: LockManager = locks if locks is not None else NullLockManager(ttl=config.lock_ttl)
        self.metrics = metrics if metrics is not None else Metrics(clock=clock)
        self.pending = pending if pending is not None else PendingLedger(clock=clock, logger=self.logger.getChild("pending"))
        self._locks: "weakref.WeakValueDictionary[str, threading.Lock]" = weakref.WeakValueDictionary()
        self._locks_guard = threading.Lock()
        self._counters: dict[str, int] = {}
        self._counters_lock = threading.Lock()

    # ---- metrics -----------------------------------------------------------
    def incr(self, name: str, value: int = 1) -> None:
        with self._counters_lock:
            self._counters[name] = self._counters.get(name, 0) + value
        # The same counters feed /api/v1/cache and /metrics; exporting only one of the two
        # would let them drift apart silently.
        self.metrics.legacy(name).inc(value)

    def _upstream(self, op: str, result: str, seconds: float, reason: str = "") -> None:
        """One upstream call, one outcome. ``not_found`` is an answer, not an outage."""
        if result == "ok":
            self.metrics.upstream_ok(op, seconds)
        elif result == "not_found":
            self.metrics.upstream_not_found(op, seconds)
        else:
            self.metrics.upstream_failed(op, reason, seconds)

    def count(self, name: str) -> int:
        with self._counters_lock:
            return self._counters.get(name, 0)

    def counters(self) -> dict[str, int]:
        """Counter snapshot served by ``/api/v1/cache`` (Prometheus lives in ``self.metrics``)."""
        with self._counters_lock:
            snapshot = dict(self._counters)
        snapshot.update({f"jobs_{k}": v for k, v in self.queue.stats().items()})
        snapshot.update(self.locks.stats())
        return snapshot

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
        return lock

    # ---- version lists -----------------------------------------------------
    def versions(self, ref: ModuleRef) -> Document:
        """Return the upstream version list, from cache when possible.

        A stale document is served immediately and refreshed in the background
        (stale-while-revalidate), so a slow or broken upstream never blocks a client
        that we already know something about.
        """
        key = ref.versions_meta_key()
        now = self.clock()
        started = time.perf_counter()
        cached = self._read_document(key)
        if cached is not None:
            payload, fetched_at = cached
            age = max(0.0, now - fetched_at)
            if age < self.config.ttl_versions:
                self.incr("versions_hit")
                return Document(payload, "hit", fetched_at, age)
            if self.config.stale_revalidate:
                self._schedule_versions_refresh(ref)
                self.incr("versions_stale")
                return Document(payload, "stale", fetched_at, age)
        try:
            response = self.registry.list_versions(ref)
        except NotFound:
            self._upstream("versions", "not_found", time.perf_counter() - started)
            raise
        except UpstreamError as exc:
            self._upstream("versions", "error", time.perf_counter() - started, exc.reason)
            # A dead upstream must never break a client we already know something about.
            if cached is not None:
                payload, fetched_at = cached
                self.incr("versions_stale_on_error")
                return Document(payload, "stale-on-error", fetched_at, max(0.0, now - fetched_at))
            raise
        self._upstream("versions", "ok", time.perf_counter() - started)
        self.incr("versions_miss")
        self.storage.put_bytes(key, response.body, content_type="application/json", meta={"kind": "versions"})
        if self.config.prefetch_latest:
            self._prefetch_latest(ref, response.body)
        return Document(response.body, "miss", self.clock(), 0.0)

    def _prefetch_latest(self, ref: ModuleRef, body: bytes) -> None:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        latest = newest(versions_from_payload(payload))
        if latest:
            self.schedule_archive(ModuleRef.build(ref.namespace, ref.name, ref.provider, latest))

    def _schedule_versions_refresh(self, ref: ModuleRef) -> None:
        self.queue.submit(f"refresh:versions:{ref.module_id}", self._refresh_versions, ref)

    def _refresh_versions(self, ref: ModuleRef) -> None:
        """Background refresh: failures are counted and re-raised for the queue to log.

        The stale document stays in place, so a broken upstream degrades to "serve stale"
        instead of losing what we already knew.
        """
        started = time.perf_counter()
        try:
            response = self.registry.list_versions(ref)
        except NotFound:
            self._upstream("versions", "not_found", time.perf_counter() - started)
            self.incr("refresh_failed")
            raise
        except Exception as exc:
            self._upstream("versions", "error", time.perf_counter() - started, getattr(exc, "reason", ""))
            self.incr("refresh_failed")
            raise
        self.storage.put_bytes(
            ref.versions_meta_key(), response.body, content_type="application/json", meta={"kind": "versions"}
        )
        self.incr("versions_refreshed")

    # ---- download sources --------------------------------------------------
    def download(self, ref: ModuleRef, force: bool = False) -> DownloadInfo:
        if not ref.version:
            raise ProxyError(400, "version_required", "a specific version is required")
        key = ref.download_meta_key()
        now = self.clock()
        started = time.perf_counter()
        # ``force`` re-resolves the source too: the upstream may have moved the archive.
        cached = None if force else self._read_document(key)
        if cached is not None:
            payload, fetched_at = cached
            age = max(0.0, now - fetched_at)
            info = self._parse_download(payload, fetched_at)
            if info is not None:
                if age < self.config.ttl_download:
                    self.incr("download_hit")
                    return _with_source(info, "hit", fetched_at)
                if self.config.stale_revalidate:
                    self.queue.submit(f"refresh:download:{ref.module_id}@{ref.version}", self._refresh_download, ref)
                    self.incr("download_stale")
                    return _with_source(info, "stale", fetched_at)
        try:
            response = self.registry.download_source(ref)
        except NotFound:
            self._upstream("download", "not_found", time.perf_counter() - started)
            raise
        except UpstreamError as exc:
            self._upstream("download", "error", time.perf_counter() - started, exc.reason)
            if cached is not None:
                payload, fetched_at = cached
                info = self._parse_download(payload, fetched_at)
                if info is not None:
                    self.incr("download_stale_on_error")
                    return _with_source(info, "stale-on-error", fetched_at)
            raise
        self._upstream("download", "ok", time.perf_counter() - started)
        raw = response.headers.get(X_TERRAFORM_GET) or ""
        source = parse_getter_source(raw)
        record = {
            "getter": source.getter,
            "url": source.url,
            "subdir": source.subdir,
            "query": source.query,
            "raw": source.raw,
        }
        self.storage.put_bytes(
            key,
            json.dumps(record, sort_keys=True).encode("utf-8"),
            content_type="application/json",
            meta={"kind": "download", "module": ref.module_id, "version": ref.version or ""},
        )
        self.incr("download_miss")
        return DownloadInfo(
            source="miss",
            fetched_at=self.clock(),
            getter=source.getter,
            url=source.url,
            subdir=source.subdir,
            query=source.query,
            raw=source.raw,
        )

    def _refresh_download(self, ref: ModuleRef) -> None:
        started = time.perf_counter()
        try:
            response = self.registry.download_source(ref)
        except NotFound:
            self._upstream("download", "not_found", time.perf_counter() - started)
            self.incr("refresh_failed")
            raise
        except Exception as exc:
            self._upstream("download", "error", time.perf_counter() - started, getattr(exc, "reason", ""))
            self.incr("refresh_failed")
            raise
        raw = response.headers.get(X_TERRAFORM_GET) or ""
        source = parse_getter_source(raw)
        record = {
            "getter": source.getter,
            "url": source.url,
            "subdir": source.subdir,
            "query": source.query,
            "raw": source.raw,
        }
        self.storage.put_bytes(
            ref.download_meta_key(),
            json.dumps(record, sort_keys=True).encode("utf-8"),
            content_type="application/json",
            meta={"kind": "download", "module": ref.module_id, "version": ref.version or ""},
        )
        self.incr("download_refreshed")

    @staticmethod
    def _parse_download(payload: bytes, fetched_at: float) -> Optional[DownloadInfo]:
        try:
            record = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(record, dict) or not record.get("url"):
            return None
        return DownloadInfo(
            source="cache",
            fetched_at=fetched_at,
            getter=str(record.get("getter", "")),
            url=str(record["url"]),
            subdir=str(record.get("subdir", "")),
            query=str(record.get("query", "")),
            raw=str(record.get("raw", "")),
        )

    # ---- archives ----------------------------------------------------------
    def archive_meta(self, ref: ModuleRef) -> Optional[ObjectMeta]:
        try:
            meta = self.storage.stat(ref.archive_key())
        except Exception as exc:  # a broken backend must not break the request path
            self.metrics.storage_error("stat")
            self.logger.warning("storage stat failed for %s: %s", ref.archive_key(), exc)
            return None
        if meta is not None and meta.size == 0:
            # A zero-byte object is not an archive: it is what a killed download or a
            # hand-made cache directory leaves behind. Report it as missing so the caller
            # refetches and overwrites it instead of serving 0 bytes to every client.
            self.incr("archive_broken")
            self.logger.warning("cached archive %s is empty, refetching it", ref.archive_key())
            return None
        return meta

    def has_archive(self, ref: ModuleRef) -> bool:
        return self.archive_meta(ref) is not None

    def schedule_archive(self, ref: ModuleRef, force: bool = False) -> bool:
        """Queue a background download. Returns True when a job was actually queued.

        ``force`` re-downloads an archive that is already in storage.
        """
        if not ref.version:
            return False
        if not force and self.has_archive(ref):
            self.pending.fulfill(ref)
            return False
        # From now on we owe this version an archive: the pending ledger is what the
        # "modules are requested but never downloaded" alert is built from.
        self.pending.note(ref)
        # Background jobs never wait for a peer's lock: if another instance is already
        # fetching it we simply drop the job and let the next client request retry.
        return self.queue.submit(f"archive:{ref.module_id}@{ref.version}", self.ensure_archive, ref, 0.0, force=force)

    def ensure_archive(self, ref: ModuleRef, wait: Optional[float] = None, force: bool = False) -> ObjectMeta:
        """Download the archive if it is not cached yet (blocking).

        ``wait`` is how long to wait when *another instance* holds the download lock;
        ``None`` means "use ``TFM_LOCK_WAIT``", ``0`` means "do not wait at all".
        ``force`` ignores what is already in storage and overwrites it: the way to heal a
        leftover from a download that was killed halfway.
        """
        if not ref.version:
            raise ProxyError(400, "version_required", "a specific version is required")
        key = ref.archive_key()
        if not force:
            existing = self.archive_meta(ref)
            if existing is not None:
                self.pending.fulfill(ref)
                return existing
        with self._lock_for(key):
            if not force:
                existing = self.archive_meta(ref)  # another worker may have won the race
                if existing is not None:
                    self.pending.fulfill(ref)
                    return existing
            if force:
                self.incr("archive_refetched")
            self.pending.note(ref)
            try:
                info = self.download(ref, force=force)
            except NotFound:
                self.pending.abandon(ref, "not published upstream")
                raise
            except ProxyError as exc:
                self.pending.attempt(ref, exc.message)
                raise
            if not info.cacheable:
                self.pending.abandon(ref, "the upstream source is not an HTTP archive")
                self.metrics.archive_download("not_cacheable")
                raise ProxyError(
                    422, NOT_CACHEABLE, f"upstream source for {ref.module_id}@{ref.version} is not an HTTP archive"
                )
            timeout = float(self.config.lock_wait if wait is None else wait)
            handle = self.locks.acquire(key)
            if handle is None:
                self.incr("archive_locked_by_other")
                if not self._wait_for_peer(key, timeout, ref):
                    self.pending.attempt(ref, "another instance is fetching it")
                    raise ProxyError(
                        503,
                        "download_in_progress",
                        f"another instance is fetching {ref.module_id}@{ref.version}, retry shortly",
                        {"Retry-After": "5"},
                    )
                meta = self.archive_meta(ref)
                if meta is not None:
                    self.incr("archive_shared_hit")
                    self.pending.fulfill(ref)
                    return meta
                handle = self.locks.acquire(key)
                if handle is None:
                    self.pending.attempt(ref, "another instance is fetching it")
                    raise ProxyError(
                        503,
                        "download_in_progress",
                        f"another instance is fetching {ref.module_id}@{ref.version}, retry shortly",
                        {"Retry-After": "5"},
                    )
            started = time.perf_counter()
            try:
                self.incr("archive_fetches")
                try:
                    downloaded = self.downloader.fetch(info.fetch_url)
                except Exception as exc:
                    self.metrics.archive_download("error", time.perf_counter() - started)
                    self.pending.attempt(ref, f"{type(exc).__name__}: {exc}")
                    raise
                try:
                    # A slow download must not outlive the lock: push the expiry out.
                    self.locks.refresh(handle)
                    meta = self.storage.put_file(
                        key,
                        downloaded.path,
                        size=downloaded.size,
                        sha256=downloaded.sha256,
                        content_type="application/zip",
                        meta={
                            "module": ref.module_id,
                            "version": ref.version or "",
                            "upstream-url": info.fetch_url,
                            "fetched-by": self.locks.instance,
                        },
                    )
                except Exception as exc:
                    self.metrics.archive_download("store_error", time.perf_counter() - started)
                    self.pending.attempt(ref, f"{type(exc).__name__}: {exc}")
                    raise
                finally:
                    downloaded.cleanup()
            finally:
                self.locks.release(handle)
            self.incr("archive_stored")
            self.incr("archive_bytes", meta.size or 0)
            self.metrics.archive_download("ok", time.perf_counter() - started)
            self.pending.fulfill(ref)
            self.logger.info("cached %s (%s bytes)", key, meta.size)
            return meta

    def _wait_for_peer(self, key: str, timeout: float, ref: ModuleRef) -> bool:
        """Wait for a peer's download lock to clear. False when it is still held."""
        if timeout <= 0:
            return False
        self.logger.info("waiting up to %.0fs for another instance to fetch %s", timeout, ref)
        status = self.locks.wait_status(key, timeout)
        if status == "expired":
            # The holder died mid-download: the caller re-acquires and takes over.
            self.incr("lock_expired")
        return status != "timeout"

    def open_archive(self, ref: ModuleRef):
        return self.storage.open(ref.archive_key())

    # ---- maintenance -------------------------------------------------------
    def purge(self, ref: ModuleRef) -> list[str]:
        """Drop everything we know about a module, or about one version of it."""
        if ref.version:
            keys = [ref.archive_key(), ref.download_meta_key()]
        else:
            keys = [ref.versions_meta_key()]
        removed: list[str] = []
        for key in keys:
            try:
                if self.storage.delete(key):
                    removed.append(key)
            except Exception as exc:
                self.logger.warning("purge of %s failed: %s", key, exc)
        if not ref.version:
            for key in self.storage.list(f"meta/download/{ref.namespace}/{ref.name}/{ref.provider}/"):
                try:
                    if self.storage.delete(key):
                        removed.append(key)
                except Exception:
                    continue
            for key in self.storage.list(f"modules/{ref.namespace}/{ref.name}/{ref.provider}/"):
                try:
                    if self.storage.delete(key):
                        removed.append(key)
                except Exception:
                    continue
            # An operator who purged a module is not waiting for it any more: leaving it in
            # the pending ledger would keep the "never downloaded" alert firing for nothing.
            self.pending.forget_module(ref.module_id)
        return removed

    def cached_modules(self, prefix: str = "modules/") -> list[str]:
        return self.storage.list(prefix)

    def _read_document(self, key: str) -> Optional[tuple[bytes, float]]:
        try:
            meta = self.storage.stat(key)
            if meta is None:
                return None
            payload = self.storage.get_bytes(key)
        except Exception as exc:
            self.metrics.storage_error("read")
            self.logger.warning("cache read failed for %s: %s", key, exc)
            return None
        return payload, float(meta.fetched_at or self.clock())


def _with_source(info: DownloadInfo, source: str, fetched_at: float) -> DownloadInfo:
    return DownloadInfo(
        source=source,
        fetched_at=fetched_at,
        getter=info.getter,
        url=info.url,
        subdir=info.subdir,
        query=info.query,
        raw=info.raw,
    )
