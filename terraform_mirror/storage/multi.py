"""Tiered storage: read from the first backend that has the object, write to all."""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Optional

from ..errors import NotFound
from .base import ObjectMeta, ProbeResult, Storage, StreamHandle

LOGGER = logging.getLogger("terraform_mirror.storage.tiered")


class TieredStorage(Storage):
    """Combines backends in lookup order, e.g. ``[fs, s3]``.

    * ``put`` writes to every backend (write-through), so the S3 copy is the durable one;
    * ``open``/``stat`` return the first hit and, when ``promote_first`` is set, copy the
      object into the backends that were missed (disk cache warm-up from S3);
    * ``delete`` removes the object from every backend.
    """

    name = "tiered"

    def __init__(self, backends: list[Storage], *, promote_first: bool = True) -> None:
        if not backends:
            raise ValueError("TieredStorage needs at least one backend")
        self.backends = backends
        self.promote_first = promote_first

    def describe(self) -> dict[str, str]:
        return {"backend": self.name, "tiers": "+".join(b.name for b in self.backends)}

    # ---- health ------------------------------------------------------------
    def probes(self, write: bool = False) -> list[ProbeResult]:
        """Probe every tier on its own: a dead S3 next to a healthy disk is a real finding."""
        return [backend.probe(write) for backend in self.backends]

    def probe(self, write: bool = False) -> ProbeResult:
        results = self.probes(write)
        broken = [result for result in results if not result.ok]
        detail = "; ".join(f"{result.backend}: {result.detail}" for result in results)
        return ProbeResult(self.name, not broken, detail, write, sum(result.seconds for result in results))

    # ---- locking -----------------------------------------------------------
    @property
    def lock_backend(self) -> Storage:
        """The tier that can arbitrate between instances (S3), else the first one."""
        for backend in self.backends:
            if backend.distributed:
                return backend
        return self.backends[0]

    def put_new(
        self,
        key: str,
        data: bytes,
        *,
        content_type: Optional[str] = None,
        meta: Optional[dict[str, str]] = None,
    ) -> bool:
        # Locks are only meaningful in the shared tier; local disks are per-instance.
        return self.lock_backend.put_new(key, data, content_type=content_type, meta=meta)

    # ---- read --------------------------------------------------------------
    def locate(self, key: str) -> tuple[Optional[Storage], list[Storage]]:
        """Return ``(hit, missed)`` for *key*."""
        missed: list[Storage] = []
        for backend in self.backends:
            try:
                hit = backend.exists(key)
            except Exception:  # a broken tier must not take the whole proxy down
                missed.append(backend)
                continue
            if hit:
                return backend, missed
            missed.append(backend)
        return None, missed

    def exists(self, key: str) -> bool:
        return self.locate(key)[0] is not None

    def stat(self, key: str) -> Optional[ObjectMeta]:
        backend, _ = self.locate(key)
        if backend is None:
            return None
        return backend.stat(key)

    def open(self, key: str) -> StreamHandle:
        backend, missed = self.locate(key)
        if backend is None:
            raise NotFound(f"object {key!r} is not cached")
        handle = backend.open(key)
        if self.promote_first and missed:
            self._promote(key, backend, missed, handle.size)
        return handle

    def _promote(self, key: str, source: Storage, targets: list[Storage], size: Optional[int]) -> None:
        """Best-effort copy of a hit into the tiers that missed."""
        path: Optional[str] = None
        try:
            with source.open(key) as handle:
                fd, path = tempfile.mkstemp(prefix="tfp-promote-")
                os.close(fd)
                with open(path, "wb") as out:
                    for chunk in handle.chunks():
                        out.write(chunk)
            for target in targets:
                try:
                    target.put_file(key, path, size=size)
                except Exception as exc:
                    # Promotion is a cache warm-up: a broken tier must not break the request.
                    LOGGER.debug("promote %s into %s failed: %s", key, target.name, exc)
                    continue
        except Exception:
            return
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    # ---- write -------------------------------------------------------------
    def put_file(
        self,
        key: str,
        path: str,
        *,
        size: Optional[int] = None,
        sha256: Optional[str] = None,
        content_type: Optional[str] = None,
        meta: Optional[dict[str, str]] = None,
    ) -> ObjectMeta:
        result: Optional[ObjectMeta] = None
        for backend in self.backends:
            result = (
                backend.put_file(key, path, size=size, sha256=sha256, content_type=content_type, meta=meta)
                or result
            )
        return result or ObjectMeta(key=key, size=size, sha256=sha256, content_type=content_type, backend=self.name)

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: Optional[str] = None,
        meta: Optional[dict[str, str]] = None,
    ) -> ObjectMeta:
        result: Optional[ObjectMeta] = None
        for backend in self.backends:
            result = backend.put_bytes(key, data, content_type=content_type, meta=meta) or result
        return result or ObjectMeta(key=key, size=len(data), content_type=content_type, backend=self.name)

    # ---- delete / list -----------------------------------------------------
    def delete(self, key: str) -> bool:
        removed = False
        for backend in self.backends:
            try:
                removed = backend.delete(key) or removed
            except Exception:
                continue
        return removed

    def list(self, prefix: str = "") -> list[str]:
        seen: set[str] = set()
        for backend in self.backends:
            try:
                seen.update(backend.list(prefix))
            except Exception:
                continue
        return sorted(seen)
