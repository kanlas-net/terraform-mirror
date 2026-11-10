"""Command line helpers: bad addresses must not abort a whole batch."""

from __future__ import annotations

import contextlib
import io
import unittest
from types import SimpleNamespace

from terraform_mirror.__main__ import _split_module, build_parser, cmd_prefetch


class SplitModuleTest(unittest.TestCase):
    def test_splits_address_with_version(self):
        self.assertEqual(_split_module("hashicorp/consul/aws@5.0.0"), ("hashicorp", "consul", "aws", "5.0.0"))

    def test_splits_address_without_version(self):
        self.assertEqual(_split_module("hashicorp/consul/aws"), ("hashicorp", "consul", "aws", None))

    def test_malformed_address_raises_value_error_not_system_exit(self):
        # SystemExit escapes ``except Exception`` and killed the whole prefetch command.
        for address in ("consul", "a/b", "a/b/c/d", "a/b/c@"):
            with self.subTest(address=address):
                with self.assertRaises(ValueError):
                    _split_module(address)


class PrefetchBatchTest(unittest.TestCase):
    def setUp(self):
        self.stderr = io.StringIO()

    def run_prefetch(self, cache, addresses, force=False):
        with contextlib.redirect_stderr(self.stderr):
            return cmd_prefetch(cache, addresses, force=force)

    def test_force_flag_is_parsed(self):
        args = build_parser().parse_args(["prefetch", "--force", "hashicorp/consul/aws@1.0.0"])
        self.assertTrue(args.force)
        self.assertEqual(args.modules, ["hashicorp/consul/aws@1.0.0"])

    def test_force_flag_reaches_the_cache(self):
        seen = []

        class StubCache:
            def ensure_archive(self, ref, force=False):
                seen.append(force)
                return SimpleNamespace(size=1, sha256="x", backend="fs")

        self.assertEqual(self.run_prefetch(StubCache(), ["hashicorp/consul/aws@1.0.0"], force=True), 0)
        self.assertEqual(seen, [True])

    def test_bad_address_does_not_stop_the_others(self):
        seen = []

        class StubCache:
            def ensure_archive(self, ref, force=False):
                seen.append(str(ref))
                raise RuntimeError("upstream down")

        code = self.run_prefetch(StubCache(), ["bad", "hashicorp/consul/aws@1.0.0", "hashicorp/etcd/aws@2.0.0"])
        self.assertEqual(code, 1)
        self.assertEqual(seen, ["hashicorp/consul/aws@1.0.0", "hashicorp/etcd/aws@2.0.0"])
        self.assertIn("bad:", self.stderr.getvalue())

    def test_invalid_module_characters_reported(self):
        class StubCache:
            def ensure_archive(self, ref, force=False):  # pragma: no cover - never reached
                raise AssertionError("must not be called")

        self.assertEqual(self.run_prefetch(StubCache(), ["has hic/consul/aws@1.0.0"]), 1)
        self.assertIn("BadRequest", self.stderr.getvalue())


def sys_stderr():
    import sys

    return sys.__stderr__


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
