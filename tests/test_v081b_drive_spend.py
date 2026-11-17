"""v0.8.1 fix wave, unit drive, D3: the driver's own spend and guessed tokens.

The cost each driver turn reports joins the run's spend and counts against
`--spend` and the night cap; guessed token counts are marked estimated in the
payload, `run.json` and the human output; a CLI that prices nothing makes the
run say the caps cannot fire for the driver; turns are booked on the seat
member they worked for.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openfarm_core.cli import EXIT_OK, main as cli_main
from openfarm_core.config import load_config
from openfarm_core.driver import drive_run
from openfarm_core.state import StateDir, read_json
from tests.test_python_e2e import GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy

_WORKERS = {
    "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
    "CARD-1/implementer": GOOD_IMPLEMENTATION,
    "CARD-1/checker_pass1": "RESTATEMENT: full bundle refuses push with FullError",
    "CARD-1/checker_pass2": "{}",
}


def _answer(op_data: dict, **usage) -> dict:
    return {"actions": [], "answers": [{"seq": "auto", "data": op_data}], **usage}


class DriverSpendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy = build_python_toy()
        self.addCleanup(self.toy.cleanup)
        self.toy.write_card(
            "CARD-1",
            title="A full bundle refuses a push",
            category="pinned-bugfix",
            unit=".",
            touches=["tally/bounded.py"],
            tests=[{
                "name": "test_a_full_bundle_refuses_a_push",
                "file": "tests/test_full.py",
                "asserts": "push raises FullError",
            }],
            done_text="push on a full bundle raises `FullError`.",
        )
        self.toy.commit("add CARD-1")
        self.sp = Path(self.toy.base) / "script.json"

    def _script(self, entries: dict) -> Path:
        self.sp.write_text(json.dumps({**_WORKERS, "driver/*": {"actions": [], "answers": []}, **entries}),
                           encoding="utf-8")
        return self.sp

    def _members(self, run_id: str) -> dict:
        return read_json(self.toy.state_dir / "runs" / run_id / "board" / "members.json", {})

    def test_priced_turns_join_the_run_spend_and_are_booked_by_seat(self) -> None:
        sp = self._script({
            "driver/plan": {"actions": [{"action": "start", "card": "CARD-1"}], "answers": [],
                            "tokens_in": 1000, "tokens_out": 100, "cost_usd": 0.01},
            "driver/question/shape_and_plan": _answer(
                {"verdict": "ready", "clarified_notes": "ok"},
                tokens_in=50, tokens_out=5, cost_usd=0.002),
            "driver/question/pre_review": _answer(
                {"verdict": "approve", "reviewer_brief": "ok"},
                tokens_in=60, tokens_out=6, cost_usd=0.003),
            "driver/question/reflect_harness": _answer(
                {"lessons": []}, tokens_in=70, tokens_out=7, cost_usd=0.004),
        })
        res = drive_run(self.toy.root, load_config(self.toy.root), StateDir(self.toy.state_dir),
                        agent="claude", seat="both", card_ids=["CARD-1"], script=sp)
        self.assertEqual(res["outcomes"][0]["final_state"], "in-review")
        members = self._members(res["run_id"])
        self.assertEqual(members["driver-coord"]["tokens_in"], 1000)
        self.assertEqual(members["driver-sup"].get("tokens_in", 0), 180,
                         "question turns belong to the supervisor's member")

        self.assertAlmostEqual(res["driver_spend"], 0.019, places=6)
        self.assertTrue(res["driver_spend_measured"])
        self.assertFalse(res["driver_tokens_estimated"])
        self.assertEqual(res["notes"], [])
        self.assertEqual(res["driver_by_part"]["coordinator"]["tokens_in"], 1000)
        self.assertEqual(res["driver_by_part"]["supervisor"]["tokens_in"], 180)

        record = read_json(self.toy.state_dir / "runs" / res["run_id"] / "run.json", {})
        self.assertTrue(res["run_record_updated"])
        self.assertAlmostEqual(record["driver_spend"], 0.019, places=6)
        self.assertGreaterEqual(record["spend"], 0.019 - 1e-9, "the driver's spend is the run's spend")
        self.assertFalse(record["driver"]["tokens_estimated"])
        self.assertEqual(record["driver"]["tokens_in"], 1180)

    def test_guessed_tokens_and_unpriced_turns_are_marked(self) -> None:
        sp = self._script({
            "driver/plan": {"actions": [{"action": "start", "card": "CARD-1"}], "answers": []},
            "driver/question/shape_and_plan": _answer({"verdict": "ready", "clarified_notes": "ok"}),
            "driver/question/pre_review": _answer({"verdict": "approve", "reviewer_brief": "ok"}),
            "driver/question/reflect_harness": _answer({"lessons": []}),
        })
        rc_out, rc_err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(rc_out), contextlib.redirect_stderr(rc_err):
            rc = cli_main(["--repo", str(self.toy.root), "drive", "--agent", "codex",
                           "--card", "CARD-1", "--script", str(sp)])
        self.assertEqual(rc, EXIT_OK, rc_err.getvalue() or rc_out.getvalue())
        human = rc_out.getvalue()
        self.assertIn("(estimated)", human)
        self.assertIn("caps cannot fire for the driver", human)

        run_id = sorted(p.name for p in (self.toy.state_dir / "runs").iterdir())[-1]
        record = read_json(self.toy.state_dir / "runs" / run_id / "run.json", {})
        self.assertTrue(record["driver"]["tokens_estimated"])
        self.assertFalse(record["driver"]["spend_measured"])
        self.assertTrue(any("caps cannot fire for the driver" in n for n in record["notes"]), record["notes"])

    def test_driver_spend_counts_against_the_spend_cap(self) -> None:
        sp = self._script({
            "driver/plan": {"actions": [{"action": "start", "card": "CARD-1"}], "answers": [],
                            "tokens_in": 10, "tokens_out": 1, "cost_usd": 0.03},
            "driver/question/shape_and_plan": _answer(
                {"verdict": "ready", "clarified_notes": "ok"},
                tokens_in=10, tokens_out=1, cost_usd=0.03),
            "driver/question/pre_review": _answer(
                {"verdict": "approve", "reviewer_brief": "should never be asked"},
                tokens_in=10, tokens_out=1, cost_usd=0.03),
        })
        res = drive_run(self.toy.root, load_config(self.toy.root), StateDir(self.toy.state_dir),
                        agent="claude", seat="both", card_ids=["CARD-1"], spend=0.05, script=sp)
        self.assertNotIn("pre_review", [a["op"] for a in res["answers_sent"]],
                         "the driver kept spending past --spend")
        self.assertIn("reached its cap of 0.0500", res["spend_stopped"])
        self.assertAlmostEqual(res["driver_spend"], 0.06, places=6)
        pre = [t for t in res["timeouts"] if t["op"] == "pre_review"]
        self.assertTrue(pre, res["timeouts"])
        self.assertIn("reached its cap", pre[0]["reason"])


if __name__ == "__main__":
    unittest.main()
