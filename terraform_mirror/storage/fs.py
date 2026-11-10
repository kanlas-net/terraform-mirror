"""Filesystem storage backend with atomic writes."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from typing import Optional

from ..errors import NotFound, StorageError
from .base import ObjectMeta, ProbeResult, Storage, StreamHandle, hash_file

META_SUFFIX = ".meta.json"


def safe_join(root: str, key: str) -> str:
    """Join *key* under *root*, refusing traversal outside of it."""
    if not key or key.startswith("/") or "\\" in key:
        raise StorageError(f"unsafe storage key: {key!r}")
    parts = [p for p in key.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise StorageError(f"unsafe storage key: {key!r}")
    path = os.path.normpath(os.path.join(root, *parts))
    root_abs = os.path.abspath(root)
    if os.path.commonpath([os.path.abspath(path), root_abs]) != root_abs:
        raise StorageError(f"unsafe storage key: {key!r}")
    return path


class FilesystemStorage(Storage):
    """Stores each object as a file plus a ``.meta.json`` sidecar."""

    name = "fs"
    #: A local disk is per-instance, so it cannot arbitrate between instances.
    distributed = False

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    # ---- paths -------------------------------------------------------------
    def path_for(self, key: str) -> str:
        return safe_join(self.root, key)

    def _meta_path(self, key: str) -> str:
        return self.path_for(key) + META_SUFFIX

    # ---- read --------------------------------------------------------------
    def exists(self, key: str) -> bool:
        return os.path.isfile(self.path_for(key))

    def stat(self, key: str) -> Optional[ObjectMeta]:
        path = self.path_for(key)
        try:
            size = os.path.getsize(path)
            mtime = os.path.getmtime(path)
        except OSError:
            # Missing, or deleted by another thread between exists() and the stat.
            return None
        extra: dict[str, str] = {}
        content_type = None
        sha256 = None
        fetched_at = None
        try:
            with open(self._meta_path(key), "r", encoding="utf-8") as handle:
                stored = json.load(handle)
            extra = {str(k): str(v) for k, v in stored.get("extra", {}).items()}
            content_type = stored.get("content_type")
            sha256 = stored.get("sha256")
            fetched_at = stored.get("fetched_at")
        except (OSError, ValueError):
            pass
        if fetched_at is None:
            fetched_at = mtime
        try:
            fetched_at = float(fetched_at)
        except (TypeError, ValueError):
            # A hand-edited or half-written sidecar must not take down stat().
            fetched_at = mtime
        if sha256 is None:
            # Cheap integrity hint: computed lazily only when the caller asks.
            sha256 = extra.get("sha256")
        return ObjectMeta(
            key=key,
            size=size,
            sha256=sha256,
            content_type=content_type,
            fetched_at=fetched_at,
            backend=self.name,
            extra=extra,
        )

    def open(self, key: str) -> StreamHandle:
        path = self.path_for(key)
        meta = self.stat(key)
        if meta is None:
            raise NotFound(f"object {key!r} is not cached")
        try:
            handle = open(path, "rb")
        except FileNotFoundError as exc:  # deleted between stat() and open()
            raise NotFound(f"object {key!r} is not cached") from exc

        def read(size: int) -> bytes:
            return handle.read(size)

        return StreamHandle(meta, read, handle.close)

    # ---- write -------------------------------------------------------------
    def _write_atomic(self, path: str, data: bytes) -> None:
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            with_suppressed_errors(lambda: os.unlink(tmp))
            raise

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: Optional[str] = None,
        meta: Optional[dict[str, str]] = None,
    ) -> ObjectMeta:
        path = self.path_for(key)
        self._write_atomic(path, data)
        digest = _sha256_bytes(data)
        self._write_meta(key, sha256=digest, content_type=content_type, meta=meta)
        return ObjectMeta(
            key=key,
            size=len(data),
            sha256=digest,
            content_type=content_type,
            fetched_at=time.time(),
            backend=self.name,
            extra=dict(meta or {}),
        )

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
        target = self.path_for(key)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.abspath(path) != target:
            # Copy to a temp file first so the object never appears half written.
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".tmp-")
            os.close(fd)
            try:
                shutil.copyfile(path, tmp)
                os.replace(tmp, target)
            except BaseException:
                with_suppressed_errors(lambda: os.unlink(tmp))
                raise
        if sha256 is None:
            digest, size = hash_file(target)
            sha256 = digest
        if size is None:
            size = os.path.getsize(target)
        self._write_meta(key, sha256=sha256, content_type=content_type, meta=meta)
        return ObjectMeta(
            key=key,
            size=size,
            sha256=sha256,
            content_type=content_type,
            fetched_at=time.time(),
            backend=self.name,
            extra=dict(meta or {}),
        )

    def _write_meta(
        self, key: str, *, sha256: str, content_type: Optional[str], meta: Optional[dict[str, str]]
    ) -> None:
        payload = {
            "sha256": sha256,
            "content_type": content_type,
            "fetched_at": time.time(),
            "extra": dict(meta or {}),
        }
        self._write_atomic(self._meta_path(key), json.dumps(payload, sort_keys=True).encode("utf-8"))

    # ---- delete / list -----------------------------------------------------
    def put_new(
        self,
        key: str,
        data: bytes,
        *,
        content_type: Optional[str] = None,
        meta: Optional[dict[str, str]] = None,
    ) -> bool:
        """Atomic create on a local filesystem: ``O_CREAT | O_EXCL``."""
        path = self.path_for(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            with_suppressed_errors(lambda: os.unlink(path))
            raise
        self._write_meta(key, sha256=_sha256_bytes(data), content_type=content_type, meta=meta)
        return True

    def delete(self, key: str) -> bool:
        removed = False
        for path in (self.path_for(key), self._meta_path(key)):
            try:
                os.unlink(path)
                removed = True
            except FileNotFoundError:
                continue
        return removed

    def list(self, prefix: str = "") -> list[str]:
        root = self.root
        out: list[str] = []
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                if filename.startswith(".tmp-") or filename.endswith(META_SUFFIX):
                    continue
                full = os.path.join(dirpath, filename)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                if rel.startswith(prefix):
                    out.append(rel)
        return sorted(out)

    def describe(self) -> dict[str, str]:
        return {"backend": self.name, "root": self.root}

    def probe(self, write: bool = False) -> ProbeResult:
        """The disk must exist and be writable; a write probe does a real round trip."""
        started = time.perf_counter()
        ok, detail = True, self.root
        try:
            if not os.path.isdir(self.root):
                ok, detail = False, f"{self.root} is not a directory"
            elif not os.access(self.root, os.W_OK | os.X_OK):
                ok, detail = False, f"{self.root} is not writable"
            else:
                os.listdir(self.root)
        except OSError as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        if ok and write:
            ok, detail = self.probe_write()
        return ProbeResult(self.name, ok, detail, write, time.perf_counter() - started)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def with_suppressed_errors(func) -> None:
    try:
        func()
    except OSError:
        pass
