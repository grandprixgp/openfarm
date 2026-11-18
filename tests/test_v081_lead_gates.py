"""Lead, from the outside review (M1): a failed run with nothing red parsed is not green.

`suite_green` used to return PASS whenever the parse held no failing row, no
compile error and no missing footer, whatever the runner said about the run.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.gates.checks import FAIL, INFRA, PASS, suite_green

EMPTY = {"results": [], "footers": [], "missing_footers": [], "compile_errors": []}


def _footer(passed: int = 0, failed: int = 0, ignored: int = 0, status: str = "ok") -> dict:
    return {"binary": "t", "status": status, "passed": passed, "failed": failed,
            "ignored": ignored, "filtered": 0}


class FailedRunWithNothingRedTests(unittest.TestCase):
    def test_cargo_that_stopped_before_building_is_not_green(self) -> None:
        gate = suite_green(dict(EMPTY), [], run_cls="fail", exit_code=101)
        self.assertEqual(gate.result, FAIL)
        self.assertIn("exit code 101", gate.detail)
        self.assertIn("cannot be read as green", gate.detail)

    def test_a_non_zero_exit_after_every_test_passed_is_not_green(self) -> None:
        parsed = dict(EMPTY, results=[{"name": "test_a", "status": "ok"}],
                      footers=[_footer(passed=1)])
        gate = suite_green(parsed, [], run_cls="fail", exit_code=1)
        self.assertEqual(gate.result, FAIL)
        self.assertEqual(gate.evidence["exit_code"], 1)

    def test_an_empty_run_is_still_a_finished_run(self) -> None:
        # Python 3.12+: `NO TESTS RAN`, exit code 5; pytest: `no tests ran`, exit 5.
        gate = suite_green(dict(EMPTY, footers=[_footer()]), [], run_cls="fail", exit_code=5)
        self.assertEqual(gate.result, PASS)

    def test_known_red_explains_a_failed_run(self) -> None:
        parsed = dict(EMPTY, results=[{"name": "test_old", "status": "FAILED"}],
                      footers=[_footer(failed=1, status="FAILED")])
        gate = suite_green(parsed, ["test_old"], run_cls="fail", exit_code=1)
        self.assertEqual(gate.result, PASS)
        self.assertEqual(gate.evidence["known-red"], ["test_old"])

    def test_an_interrupted_run_that_says_no_tests_ran_is_not_the_empty_run(self) -> None:
        # Second outside review, 4: pytest stopped by Ctrl-C exits 2 and prints
        # "no tests ran"; only exit 5 is a finished run with nothing in it.
        empty = dict(EMPTY, footers=[_footer()])
        self.assertEqual(suite_green(empty, [], run_cls="fail", exit_code=5).result, PASS)
        for code in (2, 1, 4, 130):
            with self.subTest(code=code):
                gate = suite_green(empty, [], run_cls="fail", exit_code=code)
                self.assertEqual(gate.result, FAIL)
                self.assertIn(f"exit code {code}", gate.detail)

    def test_the_other_answers_are_unchanged(self) -> None:
        passed = dict(EMPTY, results=[{"name": "t", "status": "ok"}], footers=[_footer(passed=1)])
        self.assertEqual(suite_green(passed, [], run_cls="pass", exit_code=0).result, PASS)
        self.assertEqual(suite_green(dict(EMPTY), [], run_cls="infra", exit_code=-9).result, INFRA)
        red = dict(EMPTY, results=[{"name": "t", "status": "FAILED"}],
                   footers=[_footer(failed=1, status="FAILED")])
        self.assertEqual(suite_green(red, [], run_cls="fail", exit_code=1).result, FAIL)
        died = dict(EMPTY, missing_footers=["bin"])
        self.assertEqual(suite_green(died, [], run_cls="fail", exit_code=1).result, INFRA)


class CounterTestStaysInsideTests(unittest.TestCase):
    """From the outside review (m11): the fallback counter-test path is checked too."""

    def test_a_linked_tests_folder_is_not_written_through(self) -> None:
        import shutil
        import tempfile
        from types import SimpleNamespace
        from openfarm_core.flow import CardRun
        tmp = Path(tempfile.mkdtemp(prefix="of-v081-lead-ct-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        worktree, outside = tmp / "wt", tmp / "outside"
        (worktree / "real_tests").mkdir(parents=True)
        outside.mkdir()
        (worktree / "tests").symlink_to(outside)
        run = SimpleNamespace(worktree=worktree)
        self.assertFalse(CardRun._counter_stays_inside(run, "tests/openfarm_counter_1.rs"))
        self.assertTrue(CardRun._counter_stays_inside(run, "real_tests/openfarm_counter_1.rs"))
        self.assertFalse(CardRun._counter_stays_inside(run, "../outside/x.rs"))


if __name__ == "__main__":
    unittest.main()
