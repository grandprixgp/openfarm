"""m14: a correction turn passes the same spend stop as any other driver turn.

A driver turn whose answer cannot be used is followed by a correction turn. That
turn costs like any other, so the spend stop is checked before it, in the main
loop and in the wave review. When the stop fires the correction is not sent and
the drive stops the way it does before a first turn.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openfarm_core.config import load_config
from openfarm_core.driver import drive_run
from openfarm_core.state import StateDir
from tests.test_python_e2e import GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy

_WORKERS = {
    "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
    "CARD-1/implementer": GOOD_IMPLEMENTATION,
    "CARD-1/checker_pass1": "RESTATEMENT: full bundle refuses push with FullError",
    "CARD-1/checker_pass2": "{}",
}


def _answer(op_data: dict, **usage) -> dict:
    return {"actions": [], "answers": [{"seq": "auto", "data": op_data}], **usage}


class CorrectionTurnSpendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy = build_python_toy()
        self.addCleanup(self.toy.cleanup)
        self.toy.write_card(
            "CARD-1", title="A full bundle refuses a push", category="pinned-bugfix", unit=".",
            touches=["tally/bounded.py"],
            tests=[{"name": "test_a_full_bundle_refuses_a_push", "file": "tests/test_full.py",
                    "asserts": "push raises FullError"}],
            done_text="push on a full bundle raises `FullError`.",
        )
        self.toy.commit("add CARD-1")
        self.sp = Path(self.toy.base) / "script.json"

    def _drive(self, entries: dict, **kwargs) -> dict:
        self.sp.write_text(json.dumps({**_WORKERS, "driver/*": {"actions": [], "answers": []},
                                       **entries}), encoding="utf-8")
        return drive_run(self.toy.root, load_config(self.toy.root), StateDir(self.toy.state_dir),
                         agent="claude", seat="supervisor", card_ids=["CARD-1"], spend=0.05,
                         script=self.sp, **kwargs)

    def test_no_correction_turn_past_the_cap_in_the_main_loop(self) -> None:
        res = self._drive({
            # The first answer cannot be used and takes the night past --spend.
            "driver/question/shape_and_plan": {"content": "this is not json", "cost_usd": 0.06},
            "driver/correction/1": _answer({"verdict": "ready", "clarified_notes": "late"},
                                           cost_usd=0.01),
            "driver/question/pre_review": _answer({"verdict": "approve", "reviewer_brief": "ok"},
                                                  cost_usd=0.01),
        })
        self.assertEqual(res["driver_turns"], 1, "a correction turn was sent past the cap")
        self.assertAlmostEqual(res["driver_spend"], 0.06, places=6)
        self.assertIn("reached its cap of 0.0500", res["spend_stopped"])
        self.assertEqual(res["answers_sent"], [])
        shape = [t for t in res["timeouts"] if t["op"] == "shape_and_plan"]
        self.assertTrue(shape, res["timeouts"])
        self.assertIn("reached its cap", shape[0]["reason"])
        # The rest of the drive goes the way it does once the cap is reached.
        pre = [t for t in res["timeouts"] if t["op"] == "pre_review"]
        self.assertTrue(pre, res["timeouts"])
        self.assertIn("reached its cap", pre[0]["reason"])
        self.assertEqual(res["outcomes"][0]["final_state"], "in-review", res["outcomes"])

    def test_a_correction_under_the_cap_is_still_sent(self) -> None:
        res = self._drive({
            "driver/question/shape_and_plan": {"content": "this is not json", "cost_usd": 0.01},
            "driver/correction/1": _answer({"verdict": "ready", "clarified_notes": "ok"},
                                           cost_usd=0.01),
            "driver/question/pre_review": _answer({"verdict": "approve", "reviewer_brief": "ok"},
                                                  cost_usd=0.01),
            "driver/question/reflect_harness": _answer({"lessons": []}),
        })
        self.assertEqual(res["spend_stopped"], "")
        self.assertIn("shape_and_plan", [a["op"] for a in res["answers_sent"]])

    def test_no_wave_correction_turn_past_the_cap(self) -> None:
        res = self._drive({
            "driver/question/shape_and_plan": _answer({"verdict": "ready", "clarified_notes": "ok"}),
            "driver/question/pre_review": _answer({"verdict": "approve", "reviewer_brief": "ok"}),
            "driver/question/reflect_harness": _answer({"lessons": []}),
            "driver/wave/CARD-1": {"content": "not a verdict", "cost_usd": 0.06},
            "driver/wave/CARD-1/correction": {"content": json.dumps(
                {"verdict": "clean", "note": "looked fine"}), "cost_usd": 0.01},
        }, wave=True)
        self.assertEqual(res["outcomes"][0]["final_state"], "in-review", res["outcomes"])
        review = res["reviews"][0]
        self.assertFalse(review["reviewed"], review)
        self.assertTrue(review["not_reviewed_reason"].startswith("no review: night spend"),
                        review)
        self.assertIn("reached its cap", review["not_reviewed_reason"])
        self.assertAlmostEqual(res["driver_spend"], 0.06, places=6)
        self.assertIn("reached its cap", res["spend_stopped"])


if __name__ == "__main__":
    unittest.main()
