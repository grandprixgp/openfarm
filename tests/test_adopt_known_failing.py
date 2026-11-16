from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adopt import propose_known_failing
from openfarm_core.errors import Refused


class ProposeKnownFailingTests(unittest.TestCase):
    def test_failed_names_are_proposed_sorted(self) -> None:
        parsed = {
            "results": [
                {"name": "b", "status": "FAILED"},
                {"name": "a", "status": "FAILED"},
                {"name": "c", "status": "ok"},
                {"name": "b", "status": "FAILED"},
            ],
            "compile_errors": [],
        }
        self.assertEqual(propose_known_failing(parsed), ["a", "b"])

    def test_ignored_tests_are_not_proposed(self) -> None:
        parsed = {
            "results": [
                {"name": "a", "status": "FAILED"},
                {"name": "b", "status": "ignored"},
                {"name": "c", "status": "ok"},
            ],
            "compile_errors": [],
        }
        self.assertEqual(propose_known_failing(parsed), ["a"])

    def test_a_suite_that_does_not_compile_is_refused(self) -> None:
        parsed = {
            "results": [],
            "compile_errors": [{"binary": "tests/test_broken.py"}],
        }
        with self.assertRaises(Refused) as caught:
            propose_known_failing(parsed)
        self.assertEqual(caught.exception.code, "suite-does-not-compile")


if __name__ == "__main__":
    unittest.main()