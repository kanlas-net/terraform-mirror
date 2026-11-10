"""Who asked for what, and what we still owe them.

The proxy answers ``204 + X-Terraform-Get`` before the archive exists, so "the client
asked" and "the archive is in storage" are two different events. This ledger is the
difference between them: an entry appears when a cacheable version is requested and
disappears when the object shows up in storage.

That single number is what an operator needs: ``pending > 0`` and growing means modules
are being asked for and never landing, which no HTTP status code of the proxy would show.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

#: Hard ceiling on the ledger, so a client hammering random versions cannot grow it forever.
MAX_ENTRIES = 10000


@dataclass
class PendingEntry:
    """One module version that was requested and is not in storage yet."""

    module: str
    version: str
    first_seen: float
    last_attempt: float
    attempts: int = 0
    last_error: str = ""

    @property
    def key(self) -> str:
        return f"{self.module}@{self.version}"

    def age(self, now: float) -> float:
        return max(0.0, now - self.first_seen)

    def to_dict(self, now: Optional[float] = None) -> dict[str, object]:
        moment = time.time() if now is None else now
        return {
            "module": self.module,
            "version": self.version,
            "age_seconds": round(self.age(moment), 3),
            "attempts": self.attempts,
            "last_error": self.last_error,
        }


class PendingLedger:
    """Thread-safe set of outstanding module versions, oldest first."""

    def __init__(
        self,
        *,
        clock=time.time,
        max_entries: int = MAX_ENTRIES,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.clock = clock
        self.max_entries = max(1, int(max_entries))
        self.logger = logger or logging.getLogger("terraform_mirror.pending")
        self._entries: dict[str, PendingEntry] = {}
        self._lock = threading.Lock()
        self.abandoned = 0
        self._warned_full = False

    # ---- bookkeeping -------------------------------------------------------
    def note(self, ref) -> bool:
        """Remember that *ref* was requested. True when it is a new entry."""
        key = _key(ref)
        if not key:
            return False
        now = self.clock()
        with self._lock:
            if key in self._entries:
                return False
            if len(self._entries) >= self.max_entries:
                if not self._warned_full:
                    self.logger.warning(
                        "pending ledger is full (%d entries), dropping further requests", self.max_entries
                    )
                    self._warned_full = True
                return False
            self._entries[key] = PendingEntry(module=ref.module_id, version=ref.version, first_seen=now, last_attempt=now)
            self._warned_full = False
            return True

    def attempt(self, ref, error: str = "") -> None:
        """A download was tried and failed: keep waiting, remember why."""
        key = _key(ref)
        now = self.clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            entry.attempts += 1
            entry.last_attempt = now
            entry.last_error = error[:400]

    def abandon(self, ref, reason: str = "") -> bool:
        """Stop waiting: the archive can never arrive (unknown upstream, not an HTTP source)."""
        with self._lock:
            existed = self._entries.pop(_key(ref), None) is not None
            if existed:
                self.abandoned += 1
        if existed:
            self.logger.info("dropped %s from the pending list: %s", ref, reason or "no reason given")
        return existed

    def fulfill(self, ref) -> bool:
        """The archive is in storage. True when the entry was actually outstanding."""
        with self._lock:
            return self._entries.pop(_key(ref), None) is not None

    def forget_module(self, module_id: str) -> int:
        """Drop everything known about a module (used when its cache is purged)."""
        with self._lock:
            doomed = [key for key, entry in self._entries.items() if entry.module == module_id]
            for key in doomed:
                del self._entries[key]
        return len(doomed)

    # ---- reads -------------------------------------------------------------
    def entries(self) -> list[PendingEntry]:
        """Outstanding entries, the one we have owed longest first."""
        with self._lock:
            return sorted(self._entries.values(), key=lambda item: (item.first_seen, item.key))

    def get(self, ref) -> Optional[PendingEntry]:
        with self._lock:
            entry = self._entries.get(_key(ref))
            return None if entry is None else _copy(entry)

    def stats(self) -> dict[str, object]:
        now = self.clock()
        with self._lock:
            entries = sorted(self._entries.values(), key=lambda item: (item.first_seen, item.key))
            snapshot = [_copy(entry) for entry in entries]
        oldest = max((entry.age(now) for entry in snapshot), default=0.0)
        return {
            "pending": len(snapshot),
            "oldest_seconds": round(oldest, 3),
            "abandoned": self.abandoned,
            "entries": snapshot,
        }

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def _key(ref) -> str:
    version = getattr(ref, "version", None)
    module = getattr(ref, "module_id", "")
    if not module or not version:
        return ""
    return f"{module}@{version}"


def _copy(entry: PendingEntry) -> PendingEntry:
    return PendingEntry(
        module=entry.module,
        version=entry.version,
        first_seen=entry.first_seen,
        last_attempt=entry.last_attempt,
        attempts=entry.attempts,
        last_error=entry.last_error,
    )
