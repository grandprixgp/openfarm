"""Tests for Stage 6: hand-back block parsing, replan_if briefs, and flow resolution."""

from __future__ import annotations

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
    BriefBook,
    EDIT_FORMAT,
    HANDBACK_INSTRUCTIONS,
    PLANNER_HANDBACK_INSTRUCTIONS,
    checker_pass1_brief,
    checker_pass2_brief,
    implementer_brief,
    planner_brief,
    test_writer_brief,
)
from openfarm_core.cards import Card
from openfarm_core.config import Config, load_config
from openfarm_core.edits import parse_handback
from openfarm_core.errors import StopCode
from openfarm_core.flow import CardRun
from openfarm_core.packs.base import pack_for
from openfarm_core.state import StateDir, read_json
from openfarm_core.swarm import worker_member_name
from tests.test_python_e2e import GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy


def _make_card(
    card_id: str = "CARD-1",
    *,
    replan_if: list[str] | None = None,
) -> Card:
    fields: dict = {
        "category": "pinned-bugfix",
        "unit": ".",
        "tests": [
            {
                "name": "test_a_full_bundle_refuses_a_push",
                "file": "tests/test_full.py",
                "asserts": "push returns FullError",
            }
        ],
    }
    if replan_if is not None:
        fields["replan_if"] = list(replan_if)
    return Card(
        id=card_id,
        title="A full bundle refuses a push",
        state="ready",
        done_text="push on a full bundle raises `FullError`.",
        touches=["tally/bounded.py"],
        fields=fields,
    )


class HandbackParsingAndBriefTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "tally").mkdir()
        (self.tmp / "tally" / "bounded.py").write_text(
            "def push(x: int) -> int:\n    return x\n",
            encoding="utf-8",
        )
        (self.tmp / "tests").mkdir()
        (self.tmp / "tests" / "test_full.py").write_text(
            "def test_a_full_bundle_refuses_a_push():\n    pass\n",
            encoding="utf-8",
        )

    def test_parse_handback_handles_bare_fenced_and_mixed_answers(self) -> None:
        bare = "<<<HANDBACK\nTest expects alpha, code stores object.\n>>>"
        self.assertEqual(
            parse_handback(bare),
            "Test expects alpha, code stores object.",
        )

        fenced = "```\n<<<HANDBACK\nLine one\nLine two\n>>>\n```"
        self.assertEqual(parse_handback(fenced), "Line one\nLine two")

        mixed = (
            "<<<HANDBACK\nshould not trigger\n>>>\n"
            "=== whole_file: a.py\nx = 1\n"
        )
        self.assertIsNone(parse_handback(mixed))
        self.assertIsNone(parse_handback("<<<HANDBACK\n   \n>>>"))
        self.assertIsNone(parse_handback("plain text without markers"))

    def test_all_briefs_include_handback_instructions_and_fenced_replan_if(self) -> None:
        card = _make_card(
            replan_if=[
                "a named test already passes on base",
                "the reference module has no such function",
            ]
        )
        book = BriefBook(self.tmp, never_touch=[], forbidden=[])
        pack = pack_for("python")

        # With the swarm on (handback=True) every brief invites a hand-back.
        briefs = [
            test_writer_brief(card, book, pack, handback=True),
            implementer_brief(card, book, {"tests/test_full.py": "pass"}, handback=True),
            checker_pass1_brief(card, "+def test_x(): pass", handback=True),
            checker_pass2_brief(card, "+return", [{"name": "t", "result": "pass"}], {},
                                handback=True),
        ]
        for brief in briefs:
            self.assertIn("replan_if", brief.body)
            self.assertIn("a named test already passes on base", brief.body)
            self.assertIn("the reference module has no such function", brief.body)
            self.assertIn("<<<HANDBACK", brief.body)
            self.assertIn(HANDBACK_INSTRUCTIONS, brief.body)

        # The planner answers in JSON mode, so it is invited to hand back in JSON.
        planner = planner_brief(card, book, handback=True)
        self.assertIn("replan_if", planner.body)
        self.assertIn("a named test already passes on base", planner.body)
        self.assertIn(PLANNER_HANDBACK_INSTRUCTIONS, planner.body)
        self.assertIn('{"handback": ', planner.body)
        self.assertNotIn("<<<HANDBACK", planner.body)

        self.assertNotIn("<<<HANDBACK", EDIT_FORMAT)


class HandbackFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy = build_python_toy()
        self.addCleanup(self.toy.cleanup)
        card_path = self.toy.write_card(
            "CARD-1",
            title="A full bundle refuses a push",
            category="pinned-bugfix",
            unit=".",
            touches=["tally/bounded.py"],
            tests=[{
                "name": "test_a_full_bundle_refuses_a_push",
                "file": "tests/test_full.py",
                "asserts": "push returns FullError",
            }],
            done_text="push on a full bundle raises `FullError`.",
        )
        raw = card_path.read_text(encoding="utf-8")
        card_path.write_text(
            raw.replace(
                "+++\n",
                '+++\nreplan_if = ["FullError does not exist in module"]\n',
                1,
            ),
            encoding="utf-8",
        )
        self.toy.commit("add CARD-1 with replan_if")

    _run_seq = 0

    def _run_card(
        self,
        script: dict,
        *,
        on_handback=None,
        handbacks_per_card: int = 2,
        implementer_attempts: int = 3,
        test_writer_attempts: int = 2,
        extra_tiers: dict | None = None,
        use_board: bool = True,
    ) -> tuple[CardRun, dict, Board | None]:
        self._run_seq += 1
        state_dir = self.toy.state_dir
        state = StateDir(state_dir)
        tiers_cfg = {
            "fast": {
                "model": "stub-fast",
                "efforts": ["low", "medium", "high"],
                "effort_param": "reasoning_effort",
            },
            **(extra_tiers or {}),
        }
        base_data = dict(load_config(self.toy.root)._data)
        base_adapters = dict(base_data.get("adapters") or {})
        base_adapters.update({"model": "scripted", "micromanager": "off"})
        base_budget = dict(base_data.get("budget") or {})
        base_budget.update({
            "implementer_attempts": implementer_attempts,
            "test_writer_attempts": test_writer_attempts,
        })
        base_data.update({
            "adapters": base_adapters,
            "model": {
                "implementer": {"model": "stub-fast"},
                "test_writer": {"model": "stub-fast"},
                "checker": {"model": "stub-fast"},
                "planner": {"model": "stub-fast"},
            },
            "tiers": tiers_cfg,
            "swarm": {
                "enabled": use_board,
                "coordinator": "code",
                "supervisor": "off",
                "default_tier": "fast",
                "handbacks_per_card": handbacks_per_card,
            },
            "budget": base_budget,
        })
        cfg = Config(base_data, self.toy.root)
        run_id = f"run-handback-{self._run_seq}"
        state.create_run(run_id)
        card_layout = state.create_card("CARD-1", run_id)
        board = Board(board_dir_for(state, run_id), state_dir=state) if use_board else None
        if board is not None:
            board.ensure()

        source = open_card_source(cfg, state_dir)
        source.prepare()
        card = next(c for c in source.all_cards() if c.id == "CARD-1")
        worktree = self.toy.base / f"wt-{run_id}"
        branch = f"of/{run_id}/CARD-1"
        self.toy.git.create_branch(branch, "main")
        self.toy.git.worktree_add(worktree, branch)
        backend = ScriptedBackend(script)
        runner = DirectRunner(worktree)
        pack = pack_for("python")
        book = BriefBook(worktree, cfg.never_touch, cfg.forbidden_files)
        telemetry = MemoryTelemetry()
        card_run = CardRun(
            card,
            cfg,
            state,
            card_layout,
            worktree,
            runner,
            pack,
            backend,
            book,
            run_id,
            telemetry,
            board=board,
            on_handback=on_handback,
        )
        result = card_run.run()
        return card_run, result, board

    def test_handback_blocks_with_handed_back_without_spending_attempt(self) -> None:
        script = {
            "CARD-1/test_writer/1": {
                "content": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
                "tokens_in": 40,
                "tokens_out": 15,
            },
            "CARD-1/implementer/1": {
                "content": "<<<HANDBACK\nFullError is missing from bounded module.\n>>>",
                "tokens_in": 30,
                "tokens_out": 12,
            },
        }
        run, result, board = self._run_card(script, on_handback=None)
        self.assertEqual(result["final_state"], "blocked")
        self.assertEqual(result["stop_code"], StopCode.HANDED_BACK)
        self.assertIn("FullError is missing from bounded module.", result["detail"])
        self.assertEqual(run.budget.implementer_attempts, 0)
        self.assertEqual(len(run.handbacks), 1)
        self.assertTrue(run.handbacks[0]["refunded"])
        self.assertEqual(run.handbacks[0]["outcome"], "blocked")

        member = board.get_member(worker_member_name("CARD-1", "implementer"))
        self.assertIsNotNone(member)
        self.assertEqual(member["attempts"], 0)
        self.assertEqual(member["calls"], 1)

        posts = board.read(as_name="*", since=0, limit=None, for_model=False)
        hb_posts = [p for p in posts if p["kind"] == "handback"]
        self.assertEqual(len(hb_posts), 1)
        self.assertEqual(hb_posts[0]["to"], ["coordinator"])

        dossier = read_json(run.card_layout / "dossier.json")
        self.assertEqual(dossier["stop_code"], "handed-back")
        self.assertEqual(len(dossier["handbacks"]), 1)
        dossier_md = (run.card_layout / "dossier.md").read_text(encoding="utf-8")
        self.assertIn("### Hand-backs", dossier_md)
        self.assertIn("FullError is missing from bounded module.", dossier_md)

    def test_handback_with_hint_retries_and_spends_only_real_attempt(self) -> None:
        def on_hb(info: dict) -> dict:
            self.assertEqual(info["role"], "implementer")
            self.assertIn("unclear FullError import", info["reason"])
            return {"action": "hint", "body": "Import FullError from tally.bounded."}

        script = {
            "CARD-1/test_writer/1": {
                "content": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
                "tokens_in": 40,
                "tokens_out": 15,
            },
            "CARD-1/implementer/1": {
                "content": "<<<HANDBACK\nunclear FullError import\n>>>",
                "tokens_in": 25,
                "tokens_out": 10,
            },
            "CARD-1/implementer/2": {
                "content": GOOD_IMPLEMENTATION,
                "tokens_in": 60,
                "tokens_out": 20,
            },
            "CARD-1/checker_pass1": "RESTATEMENT: full bundle raises FullError on push",
            "CARD-1/checker_pass2": "{}",
        }
        run, result, _board = self._run_card(script, on_handback=on_hb)
        self.assertEqual(result["final_state"], "in-review")
        self.assertEqual(run.budget.implementer_attempts, 1)
        self.assertEqual(len(run.handbacks), 1)
        self.assertEqual(run.handbacks[0]["outcome"], "hint")

        impl_calls = [c for c in run.backend.calls if c["role"] == "implementer"]
        self.assertEqual(len(impl_calls), 2)
        self.assertEqual(impl_calls[0]["attempt"], 1)
        self.assertEqual(impl_calls[1]["attempt"], 2)
        retry_brief = impl_calls[1]["messages"][0]["content"]
        self.assertIn("Import FullError from tally.bounded.", retry_brief)
        self.assertIn("FullError does not exist in module", retry_brief)

    def test_handback_with_set_changes_tier_and_effort_for_retry(self) -> None:
        script = {
            "CARD-1/test_writer/1": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
            "CARD-1/implementer/1": "<<<HANDBACK\nneed higher tier for tricky state\n>>>",
            "CARD-1/implementer/2": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: full bundle raises FullError on push",
            "CARD-1/checker_pass2": "{}",
        }
        run, result, board = self._run_card(
            script,
            on_handback=lambda _info: {
                "action": "set",
                "tier": "strong",
                "effort": "max",
            },
            extra_tiers={
                "strong": {
                    "model": "stub-strong",
                    "efforts": ["low", "medium", "high", "max"],
                    "effort_param": "reasoning_effort",
                }
            },
        )
        self.assertEqual(result["final_state"], "in-review")
        self.assertEqual(run.budget.implementer_attempts, 1)
        impl_calls = [c for c in run.backend.calls if c["role"] == "implementer"]
        self.assertEqual(len(impl_calls), 2)
        self.assertEqual(impl_calls[0]["model"], "stub-fast")
        self.assertEqual(impl_calls[1]["model"], "stub-strong")
        self.assertEqual(impl_calls[1]["effort"], "max")
        member = board.get_member(worker_member_name("CARD-1", "implementer"))
        self.assertEqual(member["tier"], "strong")
        self.assertEqual(member["effort"], "max")

    def test_third_handback_past_limit_counts_as_failed_attempt(self) -> None:
        hints = iter(["hint one", "hint two"])

        def on_hb(_info: dict) -> dict:
            return {"action": "hint", "body": next(hints)}

        script = {
            "CARD-1/test_writer/1": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
            "CARD-1/implementer/1": "<<<HANDBACK\nhandback 1\n>>>",
            "CARD-1/implementer/2": "<<<HANDBACK\nhandback 2\n>>>",
            "CARD-1/implementer/3": "<<<HANDBACK\nhandback 3 exceeds limit\n>>>",
            "CARD-1/implementer/4": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: full bundle raises FullError on push",
            "CARD-1/checker_pass2": "{}",
        }
        run, result, _board = self._run_card(
            script,
            on_handback=on_hb,
            handbacks_per_card=2,
            implementer_attempts=3,
        )
        self.assertEqual(result["final_state"], "in-review")
        # 1st & 2nd refunded; 3rd counted as failed attempt; 4th succeeded => 2 spent.
        self.assertEqual(run.budget.implementer_attempts, 2)
        self.assertEqual(len(run.handbacks), 3)
        self.assertEqual([h["refunded"] for h in run.handbacks], [True, True, False])
        self.assertEqual(run.handbacks[2]["outcome"], "limit-exceeded")

    def test_phase_a_test_writer_handback_blocks_or_retries_cleanly(self) -> None:
        script_block = {
            "CARD-1/test_writer/1": "<<<HANDBACK\nnamed test already passes on base\n>>>",
        }
        run_b, res_b, _ = self._run_card(script_block, on_handback=None)
        self.assertEqual(res_b["final_state"], "blocked")
        self.assertEqual(res_b["stop_code"], StopCode.HANDED_BACK)
        self.assertEqual(run_b.budget.test_writer_attempts, 0)

        script_hint = {
            "CARD-1/test_writer/1": "<<<HANDBACK\nunclear assertion\n>>>",
            "CARD-1/test_writer/2": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
            "CARD-1/implementer/1": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: full bundle raises FullError on push",
            "CARD-1/checker_pass2": "{}",
        }
        run_h, res_h, _ = self._run_card(
            script_hint,
            on_handback=lambda _i: {"action": "hint", "body": "Check FullError."},
        )
        self.assertEqual(res_h["final_state"], "in-review")
        self.assertEqual(run_h.budget.test_writer_attempts, 1)


if __name__ == "__main__":
    unittest.main()
