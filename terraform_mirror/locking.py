"""Distributed download locks.

Why this exists
---------------
Several proxy instances may share one S3 bucket. Without coordination they would all
fetch the same archive from the upstream registry at the same time, and a client that
asks instance B while instance A is still downloading would get a second concurrent
download (or a half-written object).

How it works
------------
Before downloading ``modules/a/b/c/1.0.0.zip`` an instance atomically creates
``locks/modules/a/b/c/1.0.0.zip.lock`` holding a JSON record with a unique token and an
expiry timestamp. Only the instance that created the lock downloads; the others wait for
the object to appear. The lock carries a TTL, so a process that is killed (OOM, deploy,
``kill -9``) does not block the archive forever: once ``expires_at`` is in the past any
instance may break the lock and take over. The holder also refreshes the lock around long
downloads, and only ever deletes a lock whose token is its own.

A graceful exit (``SIGTERM``, ``SIGINT``) does not wait for the TTL: the server hands the
manager a shutdown hook that deletes every lock the process still holds. Only a hard kill
leaves a lock behind, and only until ``expires_at``.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Optional

from .storage.base import Storage

LOCK_PREFIX = "locks/"
LOCK_SUFFIX = ".lock"


def lock_key_for(key: str) -> str:
    return f"{LOCK_PREFIX}{key}{LOCK_SUFFIX}"


@dataclass
class LockHandle:
    """Proof that *we* hold a lock."""

    key: str
    token: str
    started_at: float
    expires_at: float
    instance: str = ""

    @property
    def lock_key(self) -> str:
        return lock_key_for(self.key)

    def ttl_left(self, now: float) -> float:
        return self.expires_at - now

    def to_record(self) -> dict[str, object]:
        return {
            "token": self.token,
            "key": self.key,
            "instance": self.instance,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_record(cls, raw: bytes) -> Optional["LockHandle"]:
        try:
            record = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(record, dict) or "token" not in record:
            return None
        try:
            return cls(
                key=str(record.get("key", "")),
                token=str(record["token"]),
                started_at=float(record.get("started_at", 0.0)),
                expires_at=float(record.get("expires_at", 0.0)),
                instance=str(record.get("instance", "")),
            )
        except (TypeError, ValueError):
            return None


class LockManager:
    """Coordination primitive built on ``storage.put_new``."""

    def __init__(
        self,
        storage: Storage,
        *,
        ttl: float = 900.0,
        instance: Optional[str] = None,
        clock: Callable[[], float] = time.time,
        logger: Optional[logging.Logger] = None,
        break_stale: bool = True,
    ) -> None:
        self.storage = getattr(storage, "lock_backend", storage)
        self.ttl = float(ttl)
        self.clock = clock
        self.break_stale = break_stale
        self.instance = instance or f"{socket.gethostname()}:{os.getpid()}"
        self.logger = logger or logging.getLogger("terraform_mirror.locks")
        self.acquired = 0
        self.busy = 0
        self.broken = 0
        self.refreshed = 0
        #: Every lock this process currently believes it owns, keyed by token.
        #: ``release_all()`` walks it so a pod that is going away hands its locks back
        #: instead of making its peers wait out the whole TTL.
        self._held: dict[str, LockHandle] = {}
        self._held_guard = threading.Lock()

    # ---- records -----------------------------------------------------------
    def peek(self, key: str) -> Optional[LockHandle]:
        raw = self.storage.read_or_none(lock_key_for(key))
        if raw is None:
            return None
        return LockHandle.from_record(raw)

    def _write(self, handle: LockHandle) -> None:
        self.storage.put_bytes(
            handle.lock_key,
            json.dumps(handle.to_record(), sort_keys=True).encode("utf-8"),
            content_type="application/json",
            meta={"lock-key": handle.key, "token": handle.token},
        )

    # ---- acquire / release -------------------------------------------------
    def acquire(self, key: str, ttl: Optional[float] = None) -> Optional[LockHandle]:
        """Try to take the lock. Returns ``None`` when somebody else holds it."""
        now = self.clock()
        ttl = self.ttl if ttl is None else float(ttl)
        for attempt in (0, 1):
            handle = LockHandle(
                key=key, token=uuid.uuid4().hex, started_at=now, expires_at=now + ttl, instance=self.instance
            )
            if self.storage.put_new(
                handle.lock_key,
                json.dumps(handle.to_record(), sort_keys=True).encode("utf-8"),
                content_type="application/json",
                meta={"lock-key": key, "token": handle.token},
            ):
                self.acquired += 1
                with self._held_guard:
                    self._held[handle.token] = handle
                self.logger.debug("lock acquired %s (ttl %.0fs)", key, ttl)
                return handle
            if attempt:
                break
            existing = self.peek(key)
            if existing is None:
                continue  # released between our failed create and the read: retry once
            if self.break_stale and existing.expires_at <= now:
                self.logger.info("breaking stale lock %s (held by %s, expired %.0fs ago)",
                                  key, existing.instance or "?", now - existing.expires_at)
                if self._delete_if_token(key, existing.token):
                    self.broken += 1
                    continue
            break
        self.busy += 1
        return None

    def release(self, handle: LockHandle) -> bool:
        """Delete the lock, but only if it is still ours.

        A lock that is already gone or has been taken over stops being tracked: there is
        nothing left for us to delete. A delete that *failed* stays tracked, so
        :meth:`release_all` gets a second chance.
        """
        existing = self.peek(handle.key)
        if existing is None:
            self._forget(handle)
            return True  # already gone
        if existing.token != handle.token:
            self._forget(handle)
            return False
        try:
            self.storage.delete(handle.lock_key)
        except Exception as exc:  # pragma: no cover - storage hiccup
            self.logger.warning("could not delete lock %s: %s", handle.key, exc)
            return False
        self._forget(handle)
        return True

    # ---- giving everything back --------------------------------------------
    def held(self) -> list[LockHandle]:
        """The locks this instance still believes it owns."""
        with self._held_guard:
            return list(self._held.values())

    def release_all(self, reason: str = "shutdown") -> int:
        """Hand back every lock we hold. Called on shutdown.

        A pod that is being restarted or drained must not leave its in-flight downloads
        locked for the whole ``TFM_LOCK_TTL``: peers would answer ``503`` to every client
        asking for that module until the TTL runs out. Only our own tokens are deleted.
        """
        handles = self.held()
        released = sum(1 for handle in handles if self.release(handle))
        if handles:
            self.logger.info("released %d of %d lock(s) (%s)", released, len(handles), reason)
        return released

    def _forget(self, handle: LockHandle) -> None:
        with self._held_guard:
            self._held.pop(handle.token, None)

    def refresh(self, handle: LockHandle, ttl: Optional[float] = None) -> bool:
        """Push the expiry further into the future; False if we lost the lock.

        There is no compare-and-swap in the storage API, so a narrow race remains: another
        instance may break the lock between the ``peek`` below and our write, and our write
        would then overwrite its record. The consequence is bounded - both sides download
        the same archive, which is idempotent - and the post-write check catches the far
        more likely ordering (we write first, they take over afterwards).
        """
        existing = self.peek(handle.key)
        if existing is None or existing.token != handle.token:
            self._forget(handle)  # somebody else owns it now, we must not delete it later
            return False
        ttl = self.ttl if ttl is None else float(ttl)
        expires_at = self.clock() + ttl
        previous = handle.expires_at
        handle.expires_at = expires_at
        self._write(handle)
        # A slow refresh can land after another instance broke the lock and took it over:
        # put_bytes would have clobbered the new owner, so verify and report the loss.
        after = self.peek(handle.key)
        if after is None or after.token != handle.token:
            self.logger.warning("lost lock %s while refreshing", handle.key)
            handle.expires_at = previous
            self._forget(handle)
            return False
        self.refreshed += 1
        return True

    def _delete_if_token(self, key: str, token: str) -> bool:
        existing = self.peek(key)
        if existing is None:
            return True  # already gone
        if existing.token != token:
            return False
        try:
            self.storage.delete(lock_key_for(key))
        except Exception as exc:  # pragma: no cover - storage hiccup
            self.logger.warning("could not delete lock %s: %s", key, exc)
            return False
        return True

    # ---- waiting -----------------------------------------------------------
    def wait_status(self, key: str, timeout: float = 120.0, poll: float = 0.5) -> str:
        """Block until the lock is gone or expires; report *how* the wait ended.

        ``"free"`` the holder released it, ``"expired"`` the TTL ran out (the holder
        is presumed dead and the next :meth:`acquire` may break the lock),
        ``"timeout"`` the deadline passed with the lock still held. The deadline
        uses the monotonic clock; expiry is judged with the injected clock.
        """
        deadline = time.monotonic() + timeout
        while True:
            handle = self.peek(key)
            if handle is None:
                return "free"
            if handle.expires_at <= self.clock():
                return "expired"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout"
            time.sleep(min(poll, remaining))

    def wait_for_release(self, key: str, timeout: float = 120.0, poll: float = 0.5) -> bool:
        """Block until the lock is no longer held. True when it is free or expired."""
        return self.wait_status(key, timeout, poll) != "timeout"

    @contextmanager
    def hold(self, key: str, ttl: Optional[float] = None):
        """Context manager yielding a handle or ``None`` when the lock is busy."""
        handle = self.acquire(key, ttl)
        try:
            yield handle
        finally:
            if handle is not None:
                try:
                    self.release(handle)
                except Exception as exc:  # pragma: no cover
                    self.logger.warning("lock release failed for %s: %s", key, exc)

    def stats(self) -> dict[str, int]:
        with self._held_guard:
            held = len(self._held)
        return {
            "locks_acquired": self.acquired,
            "locks_busy": self.busy,
            "locks_broken": self.broken,
            "locks_refreshed": self.refreshed,
            "locks_held": held,
        }


class NullLockManager(LockManager):
    """Locking disabled: every acquire succeeds without touching storage."""

    def __init__(self, **kwargs) -> None:  # type: ignore[override]
        kwargs.setdefault("storage", None)
        self.storage = kwargs["storage"]
        self.ttl = float(kwargs.get("ttl", 0.0))
        self.clock = kwargs.get("clock", time.time)
        self.instance = kwargs.get("instance") or "disabled"
        self.logger = kwargs.get("logger") or logging.getLogger("terraform_mirror.locks")
        self.break_stale = False
        self.acquired = self.busy = self.broken = self.refreshed = 0
        # stats() is inherited and reports how many locks are held, so it needs the
        # bookkeeping even though this manager never takes one.
        self._held: dict[str, LockHandle] = {}
        self._held_guard = threading.Lock()

    def peek(self, key: str) -> Optional[LockHandle]:
        return None

    def acquire(self, key: str, ttl: Optional[float] = None) -> Optional[LockHandle]:
        self.acquired += 1
        return LockHandle(key=key, token="disabled", started_at=self.clock(), expires_at=self.clock(), instance="*")

    def release(self, handle: LockHandle) -> bool:
        return True

    def refresh(self, handle: LockHandle, ttl: Optional[float] = None) -> bool:
        return True

    def held(self) -> list[LockHandle]:
        return []

    def release_all(self, reason: str = "shutdown") -> int:
        return 0

    def wait_status(self, key: str, timeout: float = 0.0, poll: float = 0.0) -> str:
        return "free"

    def wait_for_release(self, key: str, timeout: float = 0.0, poll: float = 0.0) -> bool:
        return True
