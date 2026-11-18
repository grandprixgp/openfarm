"""v0.8.5: one supervisor call shapes a card and declares its contract.

On the v0.8.1 bench the shape call answered `ready` on all 58 cards and the
scaffold call ran on every card that declared new symbols, each with the same
card and signatures, so the two are one call when a card needs both.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.cards import Card
from openfarm_core.errors import Refused
from openfarm_core.micromanager import (
    SUPERVISOR_OP_EXPECTS,
    CmdMicromanager,
    Micromanager,
    ScriptedMicromanager,
    _build_op_prompt,
)
from tests.test_python_e2e import build_python_toy, run_cli, script


def _card(new_symbols=True) -> Card:
    return Card(
        id="CARD-1", title="A bundle says when it is full", state="ready", done_text="d",
        touches=["tally/bounded.py"],
        fields={"category": "feature", "new_symbols": new_symbols,
                "tests": [{"name": "test_is_full", "file": "tests/test_full.py"}]})


STUB = {"format": "search_replace", "path": "tally/bounded.py",
        "search": "    def push(self, value: int) -> int:",
        "replace": ("    def is_full(self) -> bool:\n        raise NotImplementedError\n\n"
                    "    def push(self, value: int) -> int:")}
READY = {"verdict": "ready", "clarified_notes": "say full at capacity",
         "extra_context_files": [], "steps": [], "findings": []}


class WantsTests(unittest.TestCase):
    def _mm(self, script_map, **flags) -> Micromanager:
        return Micromanager(ScriptedMicromanager(script_map), feature_flags=flags)

    def test_a_scripted_backend_takes_it_only_when_scripted_for_it(self) -> None:
        self.assertFalse(self._mm({"CARD-1/shape_and_plan": READY}).wants_shape_and_scaffold(_card()))
        self.assertTrue(self._mm({"CARD-1/shape_and_scaffold": READY}).wants_shape_and_scaffold(_card()))

    def test_only_a_card_that_needs_a_scaffold(self) -> None:
        mm = self._mm({"shape_and_scaffold": READY})
        self.assertFalse(mm.wants_shape_and_scaffold(_card(new_symbols=False)))

    def test_any_feature_off_keeps_the_two_calls(self) -> None:
        for flag in ("combine_calls", "shape_cards", "scaffold_contracts"):
            mm = self._mm({"shape_and_scaffold": READY}, **{flag: False})
            self.assertFalse(mm.wants_shape_and_scaffold(_card()), flag)
        advisory = Micromanager(ScriptedMicromanager({"shape_and_scaffold": READY}),
                                mode="advisory")
        self.assertFalse(advisory.wants_shape_and_scaffold(_card()))

    def test_a_cmd_adapter_opts_in_through_its_hello(self) -> None:
        cmd = CmdMicromanager.__new__(CmdMicromanager)
        cmd._hello = {"versions": [1], "ops": ["hello", "assist"]}
        self.assertFalse(cmd.supports_op("shape_and_scaffold"))
        cmd._hello = {"versions": [1], "ops": ["hello", "assist"],
                      "supervisor_ops": ["shape_and_scaffold"]}
        self.assertTrue(cmd.supports_op("shape_and_scaffold"))


class AnswerTests(unittest.TestCase):
    def _call(self, answer):
        backend = ScriptedMicromanager({"CARD-1/shape_and_scaffold": answer})
        mm = Micromanager(backend)
        return mm.shape_and_scaffold(_card(), {}, {}, {"tally/bounded.py": "x"}), backend

    def test_both_halves_come_back(self) -> None:
        (decision, edits), backend = self._call({**READY, "edits": [STUB]})
        self.assertEqual(decision.verdict, "ready")
        self.assertEqual(decision.clarified_notes, "say full at capacity")
        self.assertEqual([e.path for e in edits], ["tally/bounded.py"])
        self.assertEqual([c["op"] for c in backend.calls], ["shape_and_scaffold"])
        self.assertIn("base_files", backend.calls[0]["payload"])

    def test_no_edits_is_no_scaffold(self) -> None:
        (decision, edits), _ = self._call(READY)
        self.assertEqual(edits, [])

    def test_a_bad_scaffold_is_handed_back_not_raised(self) -> None:
        bad = {**STUB, "replace": "    def is_full(self) -> bool:\n        return True\n"}
        (decision, edits), _ = self._call({**READY, "edits": [bad]})
        self.assertEqual(decision.verdict, "ready")
        self.assertIsInstance(edits, Refused)

    def test_a_bad_shape_still_raises(self) -> None:
        from openfarm_core.errors import Invalid

        with self.assertRaises(Invalid):
            self._call({"verdict": "maybe", "edits": [STUB]})

    def test_the_prompt_and_the_expected_answer_name_both_halves(self) -> None:
        prompt = _build_op_prompt("shape_and_scaffold", {"card": {"id": "CARD-1"}})
        self.assertIn('"edits"', prompt)
        self.assertIn('"verdict"', prompt)
        self.assertIn("NotImplementedError", prompt)
        props = SUPERVISOR_OP_EXPECTS["shape_and_scaffold"]["properties"]
        self.assertIn("edits", props)
        self.assertIn("verdict", props)


IS_FULL_TEST = '''\
import unittest

from tally.bounded import Bounded


class FullTests(unittest.TestCase):
    def test_is_full(self):
        bundle = Bounded(1)
        self.assertFalse(bundle.is_full())
        bundle.push(7)
        self.assertTrue(bundle.is_full())
'''

IS_FULL_IMPL = '''\
=== search_replace: tally/bounded.py
<<< SEARCH
    def is_full(self) -> bool:
        raise NotImplementedError
>>> REPLACE
    def is_full(self) -> bool:
        return len(self.segments) >= self.capacity
'''


class FlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy = build_python_toy()
        self.addCleanup(self.toy.cleanup)
        self.toy.write_card(
            "CARD-1", title="A bundle says when it is full", category="small-feature",
            unit=".", touches=["tally/bounded.py"], new_symbols=True,
            reference="tally/bounded.py",
            tests=[{"name": "test_is_full", "file": "tests/test_full.py"}],
            done_text="`Bounded.is_full()` is true once the bundle holds `capacity` segments.")
        self.toy.commit("tally: CARD-1")

    def _run(self, mm_entries: dict) -> tuple[dict, Path]:
        model = script(**{
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + IS_FULL_TEST,
            "CARD-1/implementer": IS_FULL_IMPL,
            "CARD-1/checker_pass1": "RESTATEMENT: a bundle says when it is full",
            "CARD-1/checker_pass2": "{}"})
        mm = script(**{**mm_entries, "CARD-1/pre_review": {
            "verdict": "approve", "reviewer_brief": "ok", "missed_clause": "", "findings": []}})
        result = run_cli(self.toy.root, "run", "--card", "CARD-1", "--cards", "1",
                         "--script", str(model), "--mm-script", str(mm))
        payload = json.loads(result.stdout)
        run_dir = next((self.toy.state_dir / "cards" / "CARD-1").iterdir())
        return payload["outcomes"][0], run_dir

    def test_one_call_shapes_and_scaffolds_the_card(self) -> None:
        outcome, run_dir = self._run({"CARD-1/shape_and_scaffold": {**READY, "edits": [STUB]}})
        self.assertEqual(outcome["final_state"], "in-review", outcome.get("detail"))
        gates = {p.stem: json.loads(p.read_text()) for p in (run_dir / "gates" / "1").glob("*.json")}
        self.assertEqual(gates["micromanager-shape"]["result"], "pass")
        self.assertEqual(gates["micromanager-scaffold"]["detail"], "scaffold applied")
        self.assertTrue((run_dir / "edits" / "A0-scaffold.json").exists())

    def test_a_refused_scaffold_goes_the_old_way(self) -> None:
        bad = {**STUB, "replace": "    def is_full(self) -> bool:\n        return True\n\n"
                                  "    def push(self, value: int) -> int:"}
        outcome, run_dir = self._run({"CARD-1/shape_and_scaffold": {**READY, "edits": [bad]}})
        scaffold = json.loads((run_dir / "gates" / "1" / "micromanager-scaffold.json").read_text())
        self.assertEqual(scaffold["result"], "fail")
        self.assertIn("scaffold_contract degraded", scaffold["detail"])

    def test_two_scripted_ops_still_run_as_two(self) -> None:
        outcome, run_dir = self._run({"CARD-1/shape_and_plan": READY,
                                      "CARD-1/scaffold_contract": {"edits": [STUB]}})
        self.assertEqual(outcome["final_state"], "in-review", outcome.get("detail"))
        scaffold = json.loads((run_dir / "gates" / "1" / "micromanager-scaffold.json").read_text())
        self.assertEqual(scaffold["detail"], "scaffold applied")


if __name__ == "__main__":
    unittest.main()
