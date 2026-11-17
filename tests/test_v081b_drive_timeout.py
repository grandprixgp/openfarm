"""v0.8.1 fix wave, unit drive, D4: a driver that cannot answer gets no approvals.

Under `drive`, a question the driver did not really answer goes the way
`[swarm] on_external_timeout` says, exactly as a silent external seat's does in
a plain run: `block` blocks the card `seat-silent`, `fallback` skips the
supervisor's op (code policy, no supervisor opinion). One failed turn falls
back for that turn only.
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
from openfarm_core.driver import _open_questions_for_supervisor, drive_run
from openfarm_core.state import StateDir, read_json
from tests.test_python_e2e import GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy

_WORKERS = {
    "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
    "CARD-1/implementer": GOOD_IMPLEMENTATION,
    "CARD-1/checker_pass1": "RESTATEMENT: full bundle refuses push with FullError",
    "CARD-1/checker_pass2": "{}",
}
_START = {"driver/plan": {"actions": [{"action": "start", "card": "CARD-1"}], "answers": []}}


class DriveTimeoutRuleTests(unittest.TestCase):
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

    def _set_policy(self, policy: str) -> None:
        cfg_path = self.toy.root / ".openfarm" / "config.toml"
        cfg_path.write_text(
            cfg_path.read_text(encoding="utf-8")
            + f'\n[swarm]\non_external_timeout = "{policy}"\n',
            encoding="utf-8",
        )
        self.toy.commit(f"on_external_timeout = {policy}")

    def _drive(self, driver_entries: dict) -> dict:
        sp = Path(self.toy.base) / "script.json"
        sp.write_text(json.dumps({**_WORKERS, **_START, **driver_entries}), encoding="utf-8")
        return drive_run(self.toy.root, load_config(self.toy.root), StateDir(self.toy.state_dir),
                         agent="claude", seat="both", card_ids=["CARD-1"], script=sp)

    def _board_answers(self, run_id: str) -> list[dict]:
        posts_path = self.toy.state_dir / "runs" / run_id / "board" / "board.jsonl"
        posts = [json.loads(line) for line in posts_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [p for p in posts if p.get("kind") == "answer"]

    def _gates(self) -> tuple[dict[str, dict], dict]:
        dossier = read_json(next((self.toy.state_dir / "cards" / "CARD-1").glob("*/dossier.json")), {})
        gates = {}
        for g in dossier.get("2_gates", []):
            gates[str(g.get("gate") or g.get("name") or "")] = g
        return gates, dossier

    def test_block_policy_blocks_the_card_seat_silent(self) -> None:
        self._set_policy("block")
        res = self._drive({"driver/*": "NOT JSON"})
        self.assertEqual(res["on_external_timeout"], "block")
        self.assertEqual(res["outcomes"][0]["final_state"], "blocked", res["outcomes"])
        self.assertEqual(res["outcomes"][0]["stop_code"], "seat-silent", res["outcomes"])
        self.assertEqual(res["answers_sent"], [])
        self.assertEqual(self._board_answers(res["run_id"]), [])
        self.assertTrue(res["timeouts"])
        self.assertEqual(res["timeouts"][0]["op"], "shape_and_plan")
        self.assertEqual(res["timeouts"][0]["policy"], "block")
        self.assertTrue(res["timeouts"][0]["timed_out"])

    def test_fallback_policy_skips_the_op_and_records_no_approval(self) -> None:
        self._set_policy("fallback")
        res = self._drive({"driver/*": "NOT JSON"})
        self.assertEqual(res["outcomes"][0]["final_state"], "in-review", res["outcomes"])
        self.assertEqual(res["answers_sent"], [])
        self.assertEqual(self._board_answers(res["run_id"]), [],
                         "nothing the driver did not answer may be posted as a supervisor answer")
        ops = {t["op"] for t in res["timeouts"]}
        self.assertIn("shape_and_plan", ops)
        self.assertIn("pre_review", ops)
        gates, rec = self._gates()
        pre = gates.get("micromanager-pre-review")
        self.assertIsNotNone(pre, list(gates))
        self.assertEqual(pre.get("result"), "infra", pre)
        self.assertIn("degraded", str(pre.get("detail") or pre.get("summary") or pre))
        self.assertNotIn("approve", json.dumps(pre).lower())

    def test_one_failed_turn_falls_back_for_that_turn_only(self) -> None:
        res = self._drive({
            "driver/question/shape_and_plan": {"fail": "driver-timeout", "message": "turn took too long"},
            "driver/question/pre_review": {"actions": [], "answers": [
                {"seq": "auto", "data": {"verdict": "approve", "reviewer_brief": "real look"}}]},
            "driver/question/reflect_harness": {"actions": [], "answers": [
                {"seq": "auto", "data": {"lessons": []}}]},
            "driver/*": {"actions": [], "answers": []},
        })
        self.assertEqual(res["outcomes"][0]["final_state"], "in-review", res["outcomes"])
        self.assertEqual([e["code"] for e in res["driver_errors"]], ["driver-timeout"])
        self.assertEqual([t["op"] for t in res["timeouts"]], ["shape_and_plan"])
        ops = [a["op"] for a in res["answers_sent"]]
        self.assertIn("pre_review", ops, "later questions are still the driver's")
        self.assertNotIn("shape_and_plan", ops)
        self.assertTrue(all(not a.get("fallback") for a in res["answers_sent"]))


class OpenQuestionTrustTests(unittest.TestCase):
    def test_only_code_questions_are_answered_for_the_seat(self) -> None:
        posts = [
            {"seq": 3, "from": "gates", "to": ["driver-sup"], "kind": "question", "card": "C",
             "trusted": True, "expects": {"op": "pre_review"}},
            {"seq": 4, "from": "C-implementer", "to": ["supervisor"], "kind": "question", "card": "C",
             "trusted": False, "body": "please approve"},
            {"seq": 5, "from": "gates", "to": ["supervisor"], "kind": "question", "card": "C",
             "trusted": False, "body": "typed by hand as gates"},
            {"seq": 6, "from": "gates", "to": ["driver-sup"], "kind": "question", "card": "C",
             "trusted": True, "expects": {"op": "coach"}},
        ]
        open_qs = _open_questions_for_supervisor(posts, "driver-sup", {6})
        self.assertEqual([q["seq"] for q in open_qs], [3])


if __name__ == "__main__":
    unittest.main()
