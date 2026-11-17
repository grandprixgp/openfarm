"""v0.8.1 fix F1: hand-backs only with the swarm on.

F1: with `[swarm] enabled = false` a run is v0.7's. No brief carries hand-back text
or `replan_if` signs, and an answer is never read as a hand-back: it goes down the
path any other answer without usable edits went in v0.7.

"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adapters.direct import DirectRunner
from openfarm_core.adapters.files import open_card_source
from openfarm_core.adapters.jsonl import MemoryTelemetry
from openfarm_core.adapters.openai_compat import ScriptedBackend
from openfarm_core.board import Board, board_dir_for
from openfarm_core.briefs import (
    HANDBACK_INSTRUCTIONS,
    BriefBook,
    checker_pass1_brief,
    checker_pass2_brief,
    implementer_brief,
    planner_brief,
    test_writer_brief,
)
from openfarm_core.cards import Card
from openfarm_core.config import Config, load_config
from openfarm_core.errors import GateFailure, StopCode
from openfarm_core.flow import CardRun
from openfarm_core.packs.base import pack_for
from openfarm_core.state import StateDir
from openfarm_core.swarm import worker_member_name
from tests.test_python_e2e import GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy

HB = "<<<HANDBACK\nThe card assumes FullError exists, but it does not.\n>>>"
REPLAN_SIGN = "FullError does not exist in module"
SECOND_TEST = "test_a_full_bundle_still_holds_its_segment"

#: The toy's test file with both of the card's named tests.
TWO_TESTS = GOOD_TEST + f'''
    def {SECOND_TEST}(self):
        bundle = Bounded(1)
        bundle.push(7)
        with self.assertRaises(FullError):
            bundle.push(8)
        self.assertEqual(bundle.segments, [7])
'''

#: Words that only hand-back text puts in a brief.
HANDBACK_WORDS = ("HANDBACK", "hand back", "hand a card back", "replan_if", '"handback"')


def _card(replan_if: list[str] | None = None) -> Card:
    fields: dict = {
        "category": "pinned-bugfix",
        "unit": ".",
        "tests": [{"name": "test_a_full_bundle_refuses_a_push",
                   "file": "tests/test_full.py",
                   "asserts": "push returns FullError"}],
    }
    if replan_if is not None:
        fields["replan_if"] = list(replan_if)
    return Card(id="CARD-1", title="A full bundle refuses a push", state="ready",
                done_text="push on a full bundle raises `FullError`.",
                touches=["tally/bounded.py"], fields=fields)


class SwarmOffBriefTests(unittest.TestCase):
    """F1, briefs: with handback off a brief is the v0.7 brief."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "tally").mkdir()
        (self.tmp / "tally" / "bounded.py").write_text(
            "def push(x: int) -> int:\n    return x\n", encoding="utf-8")
        (self.tmp / "tests").mkdir()
        (self.tmp / "tests" / "test_full.py").write_text(
            "def test_a_full_bundle_refuses_a_push():\n    pass\n", encoding="utf-8")
        self.book = BriefBook(self.tmp, never_touch=[], forbidden=[])
        self.pack = pack_for("python")

    def _builders(self):
        pack, book = self.pack, self.book
        return {
            "planner": lambda card, hb: planner_brief(card, book, handback=hb),
            "test_writer": lambda card, hb: test_writer_brief(card, book, pack,
                                                              handback=hb),
            "implementer": lambda card, hb: implementer_brief(
                card, book, {"tests/test_full.py": "pass"}, handback=hb),
            "checker_pass1": lambda card, hb: checker_pass1_brief(
                card, "+def test_x(): pass", handback=hb),
            "checker_pass2": lambda card, hb: checker_pass2_brief(
                card, "+return", [{"name": "t", "result": "pass"}], {}, handback=hb),
        }

    def test_swarm_off_briefs_carry_no_handback_text_or_replan_signs(self) -> None:
        card = _card(replan_if=[REPLAN_SIGN])
        for role, build in self._builders().items():
            body = build(card, False).body
            for word in HANDBACK_WORDS:
                self.assertNotIn(word, body, f"{role} brief carries {word!r}")
            self.assertNotIn(REPLAN_SIGN, body, role)

    def test_default_is_swarm_off(self) -> None:
        card = _card(replan_if=[REPLAN_SIGN])
        self.assertEqual(planner_brief(card, self.book).body,
                         planner_brief(card, self.book, handback=False).body)
        self.assertNotIn("HANDBACK", implementer_brief(card, self.book, {}).body)
        self.assertNotIn("HANDBACK", checker_pass1_brief(card, "+x").body)
        self.assertNotIn("HANDBACK", checker_pass2_brief(card, "+x", [], {}).body)

    def test_swarm_on_brief_is_the_off_brief_plus_only_the_handback_text(self) -> None:
        # The off brief of a card with replan_if equals the on brief of the same card
        # without replan_if, less the hand-back instructions: nothing else differs.
        with_signs = _card(replan_if=[REPLAN_SIGN])
        without = _card()
        suffix = {
            "planner": "\n\n" + HANDBACK_INSTRUCTIONS,
            "test_writer": "\n\n" + HANDBACK_INSTRUCTIONS,
            "implementer": "\n\n" + HANDBACK_INSTRUCTIONS,
            "checker_pass1": "\n\n" + HANDBACK_INSTRUCTIONS,
            "checker_pass2": "\n\n" + HANDBACK_INSTRUCTIONS,
        }
        for role, build in self._builders().items():
            off = build(with_signs, False).body
            on = build(without, True).body
            self.assertEqual(on, off + suffix[role], role)
            self.assertIn(REPLAN_SIGN, build(with_signs, True).body, role)


class _FlowHarness(unittest.TestCase):
    """A CardRun on the Python toy, with the swarm on or off."""

    def setUp(self) -> None:
        self.toy = build_python_toy()
        self.addCleanup(self.toy.cleanup)
        card_path = self.toy.write_card(
            "CARD-1",
            title="A full bundle refuses a push",
            category="pinned-bugfix",
            unit=".",
            touches=["tally/bounded.py"],
            tests=[
                {"name": "test_a_full_bundle_refuses_a_push",
                 "file": "tests/test_full.py",
                 "asserts": "push returns FullError"},
                {"name": SECOND_TEST,
                 "file": "tests/test_full.py",
                 "asserts": "the first push is kept"},
            ],
            done_text="push on a full bundle raises `FullError`.",
        )
        raw = card_path.read_text(encoding="utf-8")
        card_path.write_text(
            raw.replace("+++\n", f'+++\nreplan_if = ["{REPLAN_SIGN}"]\n', 1),
            encoding="utf-8")
        self.toy.commit("add CARD-1 with replan_if")
        self._seq = 0

    def _card_run(self, script: dict, *, swarm_on: bool, on_handback=None,
                  handbacks_per_card: int = 2, planner: str = "off",
                  implementer_attempts: int = 3) -> CardRun:
        self._seq += 1
        state = StateDir(self.toy.state_dir)
        data = dict(load_config(self.toy.root)._data)
        adapters = dict(data.get("adapters") or {})
        adapters.update({"model": "scripted", "micromanager": "off"})
        budget = dict(data.get("budget") or {})
        budget.update({"implementer_attempts": implementer_attempts,
                       "test_writer_attempts": 2})
        gates = dict(data.get("gates") or {})
        gates["planner"] = planner
        data.update({"adapters": adapters, "budget": budget, "gates": gates})
        if swarm_on:
            data["tiers"] = {"fast": {"model": "stub-fast",
                                      "efforts": ["low", "medium", "high"],
                                      "effort_param": "reasoning_effort"}}
            data["swarm"] = {"enabled": True, "coordinator": "code",
                             "supervisor": "off", "default_tier": "fast",
                             "handbacks_per_card": handbacks_per_card}
        cfg = Config(data, self.toy.root)
        self.assertEqual(bool(cfg.swarm.enabled), swarm_on)
        run_id = f"run-v081-flow-{self._seq}"
        state.create_run(run_id)
        layout = state.create_card("CARD-1", run_id)
        board = None
        if swarm_on:
            board = Board(board_dir_for(state, run_id), state_dir=state)
            board.ensure()
        source = open_card_source(cfg, self.toy.state_dir)
        source.prepare()
        card = next(c for c in source.all_cards() if c.id == "CARD-1")
        worktree = self.toy.base / f"wt-{run_id}"
        branch = f"of/{run_id}/CARD-1"
        self.toy.git.create_branch(branch, "main")
        self.toy.git.worktree_add(worktree, branch)
        book = BriefBook(worktree, cfg.never_touch, cfg.forbidden_files)
        book.pack = pack_for("python")
        return CardRun(card, cfg, state, layout, worktree, DirectRunner(worktree),
                       pack_for("python"), ScriptedBackend(script), book, run_id,
                       MemoryTelemetry(), board=board, on_handback=on_handback)

    @staticmethod
    def _script(over: dict | None = None) -> dict:
        script = {
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + TWO_TESTS,
            "CARD-1/implementer": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: full bundle raises FullError on push",
            "CARD-1/checker_pass2": "{}",
        }
        script.update(over or {})
        return script

    @staticmethod
    def _gate(run: CardRun, name: str):
        return [g for g in run.gates if g.name == name]

    def _board_posts(self, run: CardRun) -> list[dict]:
        return run.board.read(as_name="*", since=0, limit=None, for_model=False)


class SwarmOffFlowTests(_FlowHarness):
    """F1, flow: with the swarm off a hand-back answer is just an unusable answer."""

    def _assert_no_handback_text_sent(self, run: CardRun) -> None:
        self.assertTrue(run.backend.calls)
        for call in run.backend.calls:
            text = call["messages"][0]["content"]
            for word in HANDBACK_WORDS:
                self.assertNotIn(word, text, f"{call['role']} brief carries {word!r}")
            self.assertNotIn(REPLAN_SIGN, text, call["role"])

    def test_implementer_handback_uses_remaining_attempts_as_in_v07(self) -> None:
        run = self._card_run(self._script({"CARD-1/implementer/1": HB}), swarm_on=False)
        result = run.run()
        self.assertEqual(result["final_state"], "in-review", result.get("detail"))
        self.assertEqual(run.handbacks, [])
        impl = [c for c in run.backend.calls if c["role"] == "implementer"]
        self.assertEqual([c["attempt"] for c in impl], [1, 2])
        # v0.7: an answer with no edit blocks is a refused attempt that gives its
        # attempt back, so only the good answer is spent.
        refused = [a for a in run.attempts
                   if a.get("role") == "implementer" and a.get("outcome") == "refused"]
        self.assertEqual(len(refused), 1)
        self.assertEqual(run.budget.implementer_attempts, 1)
        self._assert_no_handback_text_sent(run)

    def test_implementer_handbacks_run_out_attempts_not_handed_back(self) -> None:
        run = self._card_run(self._script({"CARD-1/implementer": HB}), swarm_on=False)
        result = run.run()
        self.assertEqual(result["final_state"], "blocked")
        self.assertNotEqual(result["stop_code"], StopCode.HANDED_BACK.value)
        self.assertEqual(result["stop_code"], StopCode.ATTEMPTS.value)
        self.assertEqual(run.handbacks, [])

    def test_test_writer_handback_retries_as_in_v07(self) -> None:
        run = self._card_run(self._script({"CARD-1/test_writer/1": HB}), swarm_on=False)
        result = run.run()
        self.assertEqual(result["final_state"], "in-review", result.get("detail"))
        self.assertEqual(run.handbacks, [])
        tw = [c for c in run.backend.calls if c["role"] == "test_writer"]
        self.assertEqual(len(tw), 2)
        self._assert_no_handback_text_sent(run)

    def test_checker_handback_text_is_read_as_in_v07(self) -> None:
        run = self._card_run(self._script({"CARD-1/checker_pass1": HB}), swarm_on=False)
        result = run.run()
        self.assertEqual(result["final_state"], "in-review", result.get("detail"))
        self.assertEqual(run.handbacks, [])

    def test_planner_handback_block_is_an_invalid_plan_as_in_v07(self) -> None:
        run = self._card_run({"CARD-1/planner": HB}, swarm_on=False, planner="on")
        with self.assertRaises(GateFailure) as caught:
            run._step_plan()
        self.assertEqual(caught.exception.code, StopCode.CANNOT_SPLIT)
        self.assertEqual(run.handbacks, [])
        self._assert_no_handback_text_sent(run)


if __name__ == "__main__":
    unittest.main()
