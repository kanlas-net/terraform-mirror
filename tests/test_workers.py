"""Background queue: de-duplication, error isolation, idle detection."""

from __future__ import annotations

import threading
import time
import unittest

from terraform_mirror.workers import BackgroundQueue


class BackgroundQueueTest(unittest.TestCase):
    def setUp(self):
        self.queue = BackgroundQueue(workers=3, maxsize=100).start()

    def tearDown(self):
        self.queue.shutdown(wait=True)

    def test_runs_the_job(self):
        done = threading.Event()
        self.queue.submit("a", done.set)
        self.assertTrue(done.wait(5))

    def test_deduplicates_by_key(self):
        started = threading.Event()
        release = threading.Event()

        def slow():
            started.set()
            release.wait(5)

        self.assertTrue(self.queue.submit("same", slow))
        self.assertTrue(started.wait(5))
        self.assertFalse(self.queue.submit("same", lambda: None))
        release.set()
        self.assertTrue(self.queue.wait_idle(5))
        stats = self.queue.stats()
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["submitted"], 1)

    def test_same_key_can_be_queued_again_after_completion(self):
        ran = []
        self.queue.submit("k", lambda: ran.append(1))
        self.assertTrue(self.queue.wait_idle(5))
        self.assertTrue(self.queue.submit("k", lambda: ran.append(2)))
        self.assertTrue(self.queue.wait_idle(5))
        self.assertEqual(sorted(ran), [1, 2])

    def test_failing_job_does_not_kill_the_worker(self):
        def boom():
            raise RuntimeError("nope")

        self.queue.submit("bad", boom)
        done = threading.Event()
        self.queue.submit("good", done.set)
        self.assertTrue(done.wait(5))
        self.assertTrue(self.queue.wait_idle(5))
        self.assertEqual(self.queue.stats()["failed"], 1)
        self.assertIn("nope", self.queue.errors[0].error)

    def test_wait_idle_times_out(self):
        release = threading.Event()
        self.queue.submit("busy", release.wait, 5)
        self.assertFalse(self.queue.wait_idle(0.2))
        release.set()
        self.assertTrue(self.queue.wait_idle(5))

    def test_full_queue_drops_the_job(self):
        import logging

        tiny = BackgroundQueue(workers=1, maxsize=1, logger=logging.getLogger("quiet.test")).start()
        logging.getLogger("quiet.test").setLevel(logging.CRITICAL)
        try:
            release = threading.Event()
            accepted = tiny.submit("blocker", release.wait, 5)
            dropped = [tiny.submit(f"job-{i}", lambda: None) for i in range(50)]
            release.set()
            self.assertTrue(accepted)
            self.assertIn(False, dropped)
            self.assertTrue(tiny.wait_idle(5))
        finally:
            tiny.shutdown(wait=True)

    def test_shutdown_is_idempotent(self):
        queue = BackgroundQueue(workers=1).start()
        queue.shutdown(wait=True)
        queue.shutdown(wait=True)

    def test_shutdown_timeout_is_one_shared_budget(self):
        """``timeout`` is the whole shutdown budget, not one per worker thread.

        Kubernetes gives the pod a single grace period; three workers must not triple it.
        """
        queue = BackgroundQueue(workers=3, maxsize=10).start()
        started = threading.Event()
        release = threading.Event()
        queue.submit("slow", lambda: (started.set(), release.wait(5)))
        self.assertTrue(started.wait(5))
        try:
            began = time.monotonic()
            queue.shutdown(wait=True, timeout=0.3)
            elapsed = time.monotonic() - began
        finally:
            release.set()
        self.assertLess(elapsed, 1.0, "the budget is shared between workers, not one per worker")

    def test_queue_can_be_restarted(self):
        queue = BackgroundQueue(workers=2, maxsize=10).start()
        first = threading.Event()
        queue.submit("a", first.set)
        self.assertTrue(first.wait(5))
        queue.shutdown(wait=True)

        queue.start()
        second = threading.Event()
        queue.submit("b", second.set)
        self.assertTrue(second.wait(5), "workers must run again after a restart")
        queue.shutdown(wait=True)

    def test_inflight_keys_do_not_outlive_the_workers(self):
        queue = BackgroundQueue(workers=1, maxsize=10).start()
        started = threading.Event()
        release = threading.Event()
        queue.submit("busy", lambda: (started.set(), release.wait(5)))
        self.assertTrue(started.wait(5))
        queue.submit("queued", lambda: None)  # the only worker is busy, so this one waits
        release.set()
        queue.shutdown(wait=True)
        self.assertEqual(queue.stats()["inflight"], 0, "a shutdown must not keep keys marked in flight")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
