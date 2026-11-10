"""Background work queue: the "answer now, cache later" half of the proxy."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class JobRecord:
    key: str
    error: str
    at: float = field(default_factory=time.time)


class BackgroundQueue:
    """Fixed-size thread pool with per-key de-duplication.

    ``submit`` returns immediately; the callable runs later on a worker thread. A key that
    is already queued or running is never queued twice, so a busy registry does not turn
    into a stampede of identical downloads.
    """

    def __init__(self, workers: int = 4, maxsize: int = 2000, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger("terraform_mirror.background")
        self._queue: queue.Queue[tuple[str, Callable[..., Any], tuple, dict]] = queue.Queue(maxsize=maxsize)
        self._workers: list[threading.Thread] = []
        self._worker_count = max(1, workers)
        self._lock = threading.Lock()
        self._active = 0
        self._inflight: set[str] = set()
        self._stop = threading.Event()
        self.submitted = 0
        self.skipped = 0
        self.succeeded = 0
        self.failed = 0
        self.errors: deque[JobRecord] = deque(maxlen=50)

    # ---- lifecycle ---------------------------------------------------------
    def start(self) -> "BackgroundQueue":
        if self._workers:
            return self
        # A queue may be restarted after shutdown(); clear the stop flag the old shutdown set.
        self._stop.clear()
        for index in range(self._worker_count):
            thread = threading.Thread(target=self._run, name=f"tfp-fetch-{index}", daemon=True)
            thread.start()
            self._workers.append(thread)
        return self

    def shutdown(self, wait: bool = False, timeout: float = 5.0) -> None:
        """Stop the workers. ``timeout`` is the **total** budget when ``wait`` is set.

        A shutdown hook in Kubernetes gets one grace period for the whole process, not one
        per thread, so the deadline is shared: with three workers and 15 s every in-flight
        job gets whatever is left after the one before it finished.
        """
        self._stop.set()
        workers = self._workers
        for _ in workers:
            self._queue.put(("", _sentinel, (), {}))
        if wait:
            deadline = time.monotonic() + max(0.0, float(timeout))
            for thread in workers:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if not any(thread.is_alive() for thread in workers):
                # Nothing is running any more, so per-key de-duplication must not keep
                # blocking those keys; otherwise wait_idle() would never settle again.
                with self._lock:
                    self._inflight.clear()
        self._workers = []

    # ---- submitting --------------------------------------------------------
    def submit(self, key: str, func: Callable[..., Any], *args: Any, **kwargs: Any) -> bool:
        """Queue *func* unless the same key is already pending. Returns True if queued."""
        with self._lock:
            if key in self._inflight:
                self.skipped += 1
                return False
            self._inflight.add(key)
        try:
            self._queue.put_nowait((key, func, args, kwargs))
        except queue.Full:
            with self._lock:
                self._inflight.discard(key)
            self.logger.warning("background queue is full, dropping job %s", key)
            return False
        self.submitted += 1
        return True

    # ---- introspection -----------------------------------------------------
    @property
    def queued(self) -> int:
        return self._queue.qsize()

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "queued": self._queue.qsize(),
                "active": self._active,
                "inflight": len(self._inflight),
                "submitted": self.submitted,
                "skipped": self.skipped,
                "succeeded": self.succeeded,
                "failed": self.failed,
            }

    def wait_idle(self, timeout: float = 30.0, poll: float = 0.005) -> bool:
        """Block until nothing is queued or running. Mostly used by the tests.

        ``_inflight`` is the authoritative set: a job is removed from it only when the
        worker finishes, so a job that has been dequeued but not yet counted as active
        still keeps this from reporting the queue as idle.
        """
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                if self._active == 0 and self._queue.empty() and not self._inflight:
                    return True
            if time.monotonic() > deadline:
                return False
            time.sleep(poll)

    # ---- worker loop -------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                key, func, args, kwargs = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if key == "" and func is _sentinel:
                self._queue.task_done()
                break
            with self._lock:
                self._active += 1
            try:
                func(*args, **kwargs)
            except Exception as exc:  # a failing prefetch must never kill a worker
                self.failed += 1
                self.errors.appendleft(JobRecord(key=key, error=f"{type(exc).__name__}: {exc}"))
                self.logger.warning("background job %s failed: %s", key, exc)
            else:
                self.succeeded += 1
            finally:
                with self._lock:
                    self._active -= 1
                    self._inflight.discard(key)
                self._queue.task_done()


def _sentinel() -> None:  # pragma: no cover - only used to unblock workers on shutdown
    return None
