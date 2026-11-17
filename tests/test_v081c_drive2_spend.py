"""J4: one spend total for a driven run.

The driver's turns go into the run's live night budget as each one ends, so
`--spend` and `spend_per_night` stop the run's claims too; a running card's
worker spend counts toward the driver's own cap check before the card ends;
`run.json` and the payload agree on the total, the driver's share shown apart;
attached to a run another process owns, the drive says its cost is not counted.
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
from openfarm_core.driver import _DrivenNightBudget, drive_run
from openfarm_core.state import StateDir, read_json
from tests.test_python_e2e import GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy


def _answer(op_data: dict, **usage) -> dict:
    return {"actions": [], "answers": [{"seq": "auto", "data": op_data}], **usage}


def _workers(card: str, *, cost: float = 0.0) -> dict:
    return {
        f"{card}/test_writer": {"content": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
                                "cost": cost},
        f"{card}/implementer": GOOD_IMPLEMENTATION,
        f"{card}/checker_pass1": "RESTATEMENT: full bundle refuses push with FullError",
        f"{card}/checker_pass2": "{}",
    }


class DrivenNightBudgetTests(unittest.TestCase):
    def test_live_card_spend_and_driver_spend_count_until_the_card_ends(self) -> None:
        mm = type("_MM", (), {"spend": 0.0})()
        night = _DrivenNightBudget(1.0, mm_budget=mm)
        backend = night.metered_backend("CARD-1", type("_B", (), {
            "complete": lambda self, *a, **k: type("_C", (), {"cost": 0.25})(),
            "name": "inner"})())
        backend.complete("role")
        self.assertEqual(backend.name, "inner")
        night.note_driver(0.5)
        mm.spend = 0.1
        self.assertAlmostEqual(night.live_total(), 0.85)
        self.assertIsNone(night.exhausted())
        backend.complete("role")
        self.assertIn("reached its cap of 1.0000", night.exhausted())
        self.assertIn("(driver 0.5000)", night.exhausted())
        # The card's record replaces its live tally: nothing is counted twice.
        night.finish_card("CARD-1", 0.5, 0.1)
        self.assertAlmostEqual(night.live_total(), 1.1)
        self.assertAlmostEqual(night.total_spend(), 1.1)

    def test_the_cards_cap_still_holds(self) -> None:
        night = _DrivenNightBudget(0.0, cards_cap=1)
        night.note_driver(5.0)
        self.assertIsNone(night.exhausted(), "a zero cap is no cap")
        night.note_card()
        self.assertIn("cards claimed", night.exhausted())


class DriveSpendTotalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy = build_python_toy()
        self.addCleanup(self.toy.cleanup)
        for n in (1, 2):
            self.toy.write_card(
                f"CARD-{n}", title=f"A full bundle refuses a push {n}",
                category="pinned-bugfix", unit=".", touches=["tally/bounded.py"],
                tests=[{"name": "test_a_full_bundle_refuses_a_push",
                        "file": "tests/test_full.py", "asserts": "push raises FullError"}],
                done_text="push on a full bundle raises `FullError`.",
            )
        self.toy.commit("add cards")
        self.sp = Path(self.toy.base) / "script.json"

    def _script(self, entries: dict) -> Path:
        self.sp.write_text(json.dumps({"driver/*": {"actions": [], "answers": []}, **entries}),
                           encoding="utf-8")
        return self.sp

    def _drive(self, sp: Path, cards: list[str], spend: float) -> dict:
        return drive_run(self.toy.root, load_config(self.toy.root), StateDir(self.toy.state_dir),
                         agent="claude", seat="both", card_ids=cards, spend=spend, script=sp)

    def test_the_drivers_spend_stops_the_runs_claims(self) -> None:
        sp = self._script({
            **_workers("CARD-1"),
            "driver/plan": {"actions": [{"action": "start", "card": "CARD-1"}], "answers": [],
                            "tokens_in": 10, "tokens_out": 1, "cost_usd": 0.06},
        })
        res = self._drive(sp, ["CARD-1"], 0.05)
        self.assertEqual([o.get("final_state") for o in res["outcomes"]
                          if o.get("final_state")], [], res["outcomes"])
        skipped = [o.get("skipped", "") for o in res["outcomes"]]
        self.assertTrue(any("reached its cap of 0.0500" in s and "(driver 0.0600)" in s
                            for s in skipped), res["outcomes"])
        self.assertEqual(list((self.toy.state_dir / "cards").glob("CARD-1/*/card-record.json")),
                         [], "the card was claimed past the night's cap")

    def test_a_running_cards_spend_counts_before_the_card_ends(self) -> None:
        sp = self._script({
            **_workers("CARD-1", cost=0.04),
            "driver/plan": {"actions": [{"action": "start", "card": "CARD-1"}], "answers": [],
                            "tokens_in": 10, "tokens_out": 1, "cost_usd": 0.005},
            "driver/question/shape_and_plan": _answer(
                {"verdict": "ready", "clarified_notes": "ok"},
                tokens_in=10, tokens_out=1, cost_usd=0.005),
            "driver/question/pre_review": _answer(
                {"verdict": "approve", "reviewer_brief": "should never be asked"},
                tokens_in=10, tokens_out=1, cost_usd=0.005),
            "driver/question/reflect_harness": _answer(
                {"lessons": []}, tokens_in=10, tokens_out=1, cost_usd=0.005),
        })
        res = self._drive(sp, ["CARD-1"], 0.05)
        ops = [a["op"] for a in res["answers_sent"]]
        self.assertIn("shape_and_plan", ops)
        self.assertNotIn("pre_review", ops,
                         "the driver answered although the card's workers had spent the cap")
        self.assertIn("reached its cap of 0.0500", res["spend_stopped"])
        self.assertAlmostEqual(res["driver_spend"], 0.01, places=6)

        record = read_json(self.toy.state_dir / "runs" / res["run_id"] / "run.json", {})
        self.assertAlmostEqual(record["spend"], res["run_spend"], places=6)
        self.assertAlmostEqual(record["driver_spend"], res["driver_spend"], places=6)
        parts = (record["worker_spend"] + record["micromanager_spend"]
                 + record["coordinator_spend"] + record["driver_spend"])
        self.assertAlmostEqual(record["spend"], parts, places=6)
        self.assertAlmostEqual(record["worker_spend"], 0.04, places=6)
        self.assertTrue(res["driver_counted_in_run"])

    def test_attached_the_drive_says_its_cost_is_not_counted(self) -> None:
        sp = self._script({
            **_workers("CARD-1"),
            "driver/plan": {"actions": [{"action": "start", "card": "CARD-1"}], "answers": []},
            "driver/question/shape_and_plan": _answer({"verdict": "ready", "clarified_notes": "ok"}),
            "driver/question/pre_review": _answer({"verdict": "approve", "reviewer_brief": "ok"}),
            "driver/question/reflect_harness": _answer({"lessons": []}),
        })
        first = self._drive(sp, ["CARD-1"], 0.5)
        before = read_json(self.toy.state_dir / "runs" / first["run_id"] / "run.json", {})
        wave = Path(self.toy.base) / "wave.json"
        wave.write_text(json.dumps({
            "driver/*": {"actions": [], "answers": []},
            "driver/wave/CARD-1": {"verdict": "accept", "note": "fine",
                                   "cost_usd": 0.02, "tokens_in": 5, "tokens_out": 1},
        }), encoding="utf-8")
        for as_json in (True, False):
            out, err = io.StringIO(), io.StringIO()
            argv = ["--repo", str(self.toy.root)] + (["--json"] if as_json else []) + [
                "drive", "--run", first["run_id"], "--wave", "--driver-name", f"w{int(as_json)}",
                "--script", str(wave)]
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli_main(argv)
            self.assertEqual(rc, EXIT_OK, err.getvalue() or out.getvalue())
            if as_json:
                payload = json.loads(out.getvalue())
                self.assertFalse(payload["driver_counted_in_run"])
                self.assertIsNone(payload["run_spend"])
                self.assertTrue(any("is not counted against that run's caps" in n
                                    for n in payload["notes"]), payload["notes"])
            else:
                self.assertIn("is not counted against that run's caps", out.getvalue())
                self.assertIn("not counted: the run belongs to another process", out.getvalue())
        after = read_json(self.toy.state_dir / "runs" / first["run_id"] / "run.json", {})
        self.assertEqual(after["spend"], before["spend"])


if __name__ == "__main__":
    unittest.main()
