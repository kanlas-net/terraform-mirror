"""Version ordering used to pick the newest version."""

from __future__ import annotations

import unittest

from terraform_mirror.semver import newest, sort_versions, versions_from_payload


class SemverTest(unittest.TestCase):
    def test_numeric_ordering_not_lexical(self):
        self.assertEqual(sort_versions(["1.10.0", "1.9.0", "1.2.0"]), ["1.2.0", "1.9.0", "1.10.0"])

    def test_prerelease_sorts_before_release(self):
        self.assertEqual(sort_versions(["1.0.0", "1.0.0-rc1"]), ["1.0.0-rc1", "1.0.0"])

    def test_leading_v_and_missing_patch(self):
        self.assertEqual(sort_versions(["v1.2", "1.10.0"]), ["v1.2", "1.10.0"])
        self.assertEqual(newest(["1.2.0", "v1.3.0-rc1"]), "v1.3.0-rc1")

    def test_newest_of_empty(self):
        self.assertIsNone(newest([]))

    def test_non_semver_sorts_first_so_newest_ignores_it(self):
        self.assertEqual(sort_versions(["latest", "1.0.0"]), ["latest", "1.0.0"])
        self.assertEqual(newest(["latest", "1.0.0"]), "1.0.0")

    def test_versions_from_payload(self):
        payload = {"modules": [{"version": "1.0.0"}, {"version": "2.0.0"}, {"nope": 1}, "junk"]}
        self.assertEqual(versions_from_payload(payload), ["1.0.0", "2.0.0"])

    def test_versions_from_garbage(self):
        self.assertEqual(versions_from_payload(None), [])
        self.assertEqual(versions_from_payload({"modules": "nope"}), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
