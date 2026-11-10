"""What a draining pod leaves behind: locks and in-flight downloads.

The TTL is the last line of defence, not the mechanism: a pod that gets a normal
``SIGTERM`` must hand its locks back immediately, and only a ``SIGKILL`` should ever make a
peer wait for ``TFM_LOCK_TTL``.
"""

from __future__ import annotations

import threading
import time
import unittest

from terraform_mirror.models import ModuleRef
from terraform_mirror.server import shutdown_application
from tests.support.fake_upstream import FakeRegistry
from tests.support.harness import ProxyHarness

CONSUL = ModuleRef.build("hashicorp", "consul", "aws", "1.0.0")


def wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class ShutdownTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = FakeRegistry().start()
        cls.registry.add("consul", versions=["1.0.0"])
        # The archive response is held back for 2 s: long enough for the test to catch the
        # lock while the download is in flight on a slow CI runner, short enough to finish
        # inside the grace period the test then grants.
        cls.registry.add("slow", versions=["4.0.0"], archive_delay=2.0)

    @classmethod
    def tearDownClass(cls):
        cls.registry.stop()

    def setUp(self):
        self.registry.reset_hits()

    def make_harness(self, **overrides) -> ProxyHarness:
        harness = ProxyHarness.create(self, self.registry.base_url, workers=1, **overrides).start_server()
        self.addCleanup(harness.stop)
        return harness

    def test_the_grace_period_lets_an_in_flight_download_finish(self):
        harness = self.make_harness()
        slow = ModuleRef.build("hashicorp", "slow", "aws", "4.0.0")
        key = slow.archive_key()
        harness.queue.submit("archive", harness.cache.ensure_archive, slow, 0.0)
        self.assertTrue(
            wait_until(lambda: harness.cache.locks.peek(key) is not None), "the download never got as far as locking"
        )

        shutdown_application(harness._server, harness.app, harness.queue, grace=10.0)
        harness._server = None

        self.assertTrue(harness.storage.exists(key), "the download should have finished inside the grace period")
        self.assertIsNone(harness.cache.locks.peek(key), "a finished download releases its own lock")
        self.assertEqual(harness.cache.locks.held(), [])

    def test_a_lock_still_held_when_the_grace_runs_out_is_deleted(self):
        """The worker outlives the grace period: the lock must go anyway, not wait for the TTL."""
        harness = self.make_harness()
        key = CONSUL.archive_key()
        handle = harness.cache.locks.acquire(key)
        self.assertIsNotNone(handle)
        release = threading.Event()
        harness.queue.submit("stuck", release.wait, 5)
        self.assertTrue(wait_until(lambda: harness.queue.stats()["active"] == 1))

        shutdown_application(harness._server, harness.app, harness.queue, grace=0.0)
        harness._server = None
        release.set()

        self.assertIsNone(harness.cache.locks.peek(key), "a draining pod must not leave its lock behind")
        self.assertEqual(harness.cache.locks.held(), [])

    def test_shutdown_with_nothing_running_is_quiet(self):
        harness = self.make_harness()
        shutdown_application(harness._server, harness.app, harness.queue, grace=0.0)
        harness._server = None
        self.assertEqual(harness.cache.locks.held(), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
