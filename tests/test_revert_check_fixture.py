"""R1.6: the revert check keeps the test writer's fixtures, and says "did not run".

In the 20 Nov dogfood run, W1-01's revert check blocked a sound card: its worktree
left out `tests/fixtures/featured.json`, the test file crashed on load, and the four
named tests (never run) were reported as "did not fail".
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from openfarm_core.flow import _named_failures, _revert_detail
from openfarm_core.packs.typescript import TypeScriptPack
from tests.test_python_e2e import (GOOD_IMPLEMENTATION, build_python_toy, run_cli,
                                   script)

FIXTURES = Path(__file__).parent / "fixtures"
W1_01_TESTS = [
    "feedUrl builds the dated endpoint",
    "normaliseFeed maps the fixture",
    "normaliseFeed rejects feeds without an image",
    "fetchDay returns null on failure",
]

TEST_READING_A_FIXTURE = '''\
import json
import unittest
from pathlib import Path

from tally.bounded import Bounded, FullError

CAPACITY = json.loads((Path(__file__).parent / "fixtures" / "cap.json").read_text())["capacity"]


class FullTests(unittest.TestCase):
    def test_a_full_bundle_refuses_a_push(self):
        bundle = Bounded(CAPACITY)
        bundle.push(7)
        with self.assertRaises(FullError):
            bundle.push(8)
'''


class RevertCheckFixtureTests(unittest.TestCase):
    def test_w1_01_output_reads_as_not_run_and_is_worded_that_way(self) -> None:
        pack = TypeScriptPack(tsc=False, prettier=False)
        parsed = pack.parse_tests(
            (FIXTURES / "w1-01-revert-check.stdout.txt").read_text(),
            (FIXTURES / "w1-01-revert-check.stderr.txt").read_text())
        failures = _named_failures(
            parsed, W1_01_TESTS, {name: "tests/feed.test.ts" for name in W1_01_TESTS},
            getattr(pack, "row_file", None))
        self.assertEqual(set(failures.values()), {"not-run"})
        detail = _revert_detail(failures, parsed.get("compile_errors", []))
        self.assertIn("did not run", detail)
        self.assertNotIn("did not fail", detail)

    def test_a_card_whose_tests_read_a_fixture_passes_the_revert_check(self) -> None:
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        toy.write_card(
            "CARD-1",
            title="A full bundle refuses a push",
            category="pinned-bugfix",
            unit=".",
            touches=["tally/bounded.py", "tests/fixtures/cap.json"],
            tests=[{"name": "test_a_full_bundle_refuses_a_push",
                    "file": "tests/test_full.py",
                    "asserts": "push raises FullError"}],
            done_text="`Bounded(1).push(8)` after one push raises `FullError`.")
        toy.commit("tally: CARD-1")
        script_path = script(**{
            "CARD-1/test_writer": (
                "=== whole_file: tests/fixtures/cap.json\n{\"capacity\": 1}\n"
                "=== whole_file: tests/test_full.py\n" + TEST_READING_A_FIXTURE),
            "CARD-1/implementer": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: a full bundle refuses a push",
            "CARD-1/checker_pass2": "{}",
        })
        result = run_cli(toy.root, "run", "--card", "CARD-1",
                         "--script", str(script_path), "--cards", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        outcome = json.loads(result.stdout)["outcomes"][0]
        self.assertEqual(outcome["final_state"], "in-review",
                         f"{outcome.get('stop_code')}: {outcome.get('detail')}")


if __name__ == "__main__":
    unittest.main()
