from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.packs.python import PythonPack


class TestPythonSignaturePytest(unittest.TestCase):
    def test_pytest_failed_lines_are_named(self):
        pack = PythonPack(runner="pytest", ruff=False)
        signature = pack.error_signature(
            "FAILED tests/test_x.py::test_a - assert 1 == 2\n"
        )

        self.assertEqual(signature["failed_tests"], ["tests/test_x.py::test_a"])

    def test_pytest_exception_lines_are_codes(self):
        pack = PythonPack(runner="pytest", ruff=False)
        signature = pack.error_signature("E   KeyError: 'k'\n")

        self.assertEqual(signature["codes"], ["KeyError"])

    def test_the_signature_ignores_durations(self):
        pack = PythonPack(runner="pytest", ruff=False)
        fast = pack.error_signature(
            "FAILED tests/test_x.py::test_a - assert 1 == 2\n"
            "=== 1 failed in 0.05s ===\n"
        )
        slow = pack.error_signature(
            "FAILED tests/test_x.py::test_a - assert 1 == 2\n"
            "=== 1 failed in 1.20s ===\n"
        )

        self.assertEqual(fast["signature"], slow["signature"])


if __name__ == "__main__":
    unittest.main()
