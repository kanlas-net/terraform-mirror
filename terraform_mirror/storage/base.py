"""Storage interface shared by the filesystem and S3 backends."""

from __future__ import annotations

import hashlib
import os
import socket
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

DEFAULT_CHUNK = 256 * 1024

#: Key used by the write probe. It lives inside the proxy's own tree, never a module key.
PROBE_KEY = "health/probe.json"
#: Per-process write-probe keys start here, so replicas sharing a bucket never collide.
PROBE_KEY_PREFIX = "health/probe-"


@dataclass
class ObjectMeta:
    """Metadata about one stored object."""

    key: str
    size: Optional[int] = None
    sha256: Optional[str] = None
    content_type: Optional[str] = None
    fetched_at: Optional[float] = None
    backend: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def is_fresh(self, ttl: float, now: float) -> bool:
        if self.fetched_at is None:
            return False
        return (now - self.fetched_at) < ttl


class StreamHandle:
    """An opened object. Use as a context manager; ``chunks()`` yields bytes."""

    def __init__(
        self,
        meta: ObjectMeta,
        read_fn: Callable[[int], bytes],
        close_fn: Optional[Callable[[], None]] = None,
    ) -> None:
        self.meta = meta
        self.key = meta.key
        self.size = meta.size
        self._read = read_fn
        self._close = close_fn or (lambda: None)
        self._closed = False

    @property
    def sha256(self) -> Optional[str]:
        return self.meta.sha256

    def chunks(self, chunk_size: int = DEFAULT_CHUNK) -> Iterator[bytes]:
        if self._closed:
            raise ValueError(f"stream for {self.key!r} is already closed")
        while True:
            chunk = self._read(chunk_size)
            if not chunk:
                return
            yield chunk

    def read_bytes(self) -> bytes:
        return b"".join(self.chunks())

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._close()

    def __enter__(self) -> "StreamHandle":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@dataclass
class ProbeResult:
    """Outcome of one reachability check against one backend."""

    backend: str
    ok: bool
    detail: str = ""
    write: bool = False
    seconds: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "ok": self.ok,
            "write": self.write,
            "detail": self.detail,
            "seconds": round(self.seconds, 4),
        }


class Storage(ABC):
    """Key/value object storage. Keys are ``/`` separated and always relative."""

    name: str = "storage"
    #: True when the backend is shared between proxy instances (S3) rather than local.
    distributed: bool = False

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def stat(self, key: str) -> Optional[ObjectMeta]: ...

    @abstractmethod
    def open(self, key: str) -> StreamHandle:
        """Open an object for reading. Raise ``NotFound`` when it is absent."""

    @abstractmethod
    def put_file(
        self,
        key: str,
        path: str,
        *,
        size: Optional[int] = None,
        sha256: Optional[str] = None,
        content_type: Optional[str] = None,
        meta: Optional[dict[str, str]] = None,
    ) -> ObjectMeta: ...

    @abstractmethod
    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: Optional[str] = None,
        meta: Optional[dict[str, str]] = None,
    ) -> ObjectMeta: ...

    @abstractmethod
    def delete(self, key: str) -> bool: ...

    @abstractmethod
    def list(self, prefix: str = "") -> list[str]: ...

    # ---- create-if-absent --------------------------------------------------
    def put_new(
        self,
        key: str,
        data: bytes,
        *,
        content_type: Optional[str] = None,
        meta: Optional[dict[str, str]] = None,
    ) -> bool:
        """Create the object only when it does not exist yet; True when *we* created it.

        Backends override this with a genuinely atomic create. This fallback is a
        read-then-write and is only safe on a single node.
        """
        if self.exists(key):
            return False
        self.put_bytes(key, data, content_type=content_type, meta=meta)
        return True

    def read_or_none(self, key: str) -> Optional[bytes]:
        """Read an object, returning ``None`` instead of raising when it is missing."""
        from ..errors import NotFound

        try:
            return self.get_bytes(key)
        except NotFound:
            return None

    # ---- helpers shared by the backends -----------------------------------
    def get_bytes(self, key: str) -> bytes:
        with self.open(key) as handle:
            return handle.read_bytes()

    def describe(self) -> dict[str, str]:
        return {"backend": self.name}

    # ---- health ------------------------------------------------------------
    def probe(self, write: bool = False) -> ProbeResult:
        """Is this backend usable right now? Never raises.

        The default asks for one key, which proves connectivity, credentials and bucket
        existence for a network backend without transferring anything. Backends override
        it with something cheaper or more meaningful.
        """
        started = time.perf_counter()
        ok, detail = True, "reachable"
        try:
            self.stat(PROBE_KEY)
        except Exception as exc:  # noqa: BLE001 - a probe reports failures, it never raises
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        if ok and write:
            ok, detail = self.probe_write()
        return ProbeResult(self.name, ok, detail, write, time.perf_counter() - started)

    def probes(self, write: bool = False) -> list[ProbeResult]:
        """Every backend this object answers for (a tiered storage has several)."""
        return [self.probe(write)]

    def probe_write(self, key: Optional[str] = None) -> tuple[bool, str]:
        """Write, read back and delete one small object. Returns ``(ok, detail)``.

        The object is unique to this process: two replicas sharing a bucket must not
        delete each other's probe and report a read-back mismatch that is not real.
        A failed write is not cleaned up - there is nothing of ours to remove.
        """
        key = key or f"{PROBE_KEY_PREFIX}{socket.gethostname()}-{os.getpid()}.json"
        payload = f"terraform-mirror probe {time.time()}".encode("utf-8")
        try:
            self.put_bytes(key, payload, content_type="application/json", meta={"probe": "true"})
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return False, f"write failed: {type(exc).__name__}: {exc}"
        try:
            body = self.read_or_none(key)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            body = None
        finally:
            try:
                self.delete(key)
            except Exception:  # pragma: no cover - a leftover probe object is not an outage
                pass
        if body != payload:
            return False, "the object read back is not the object written"
        return True, "write, read and delete ok"


def hash_file(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size
