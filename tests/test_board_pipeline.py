"""Tests for Stage 4: pipeline on the board, hints, live team settings, and reports."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adapters.direct import DirectRunner
from openfarm_core.adapters.files import FileCardSource
from openfarm_core.adapters.jsonl import MemoryTelemetry
from openfarm_core.adapters.openai_compat import ScriptedBackend
from openfarm_core.board import Board, board_dir_for
from openfarm_core.briefs import BriefBook
from openfarm_core.cards import Card
from openfarm_core.cli import EXIT_OK, main as cli_main
from openfarm_core.config import Config
from openfarm_core.flow import CardRun
from openfarm_core.git import Git
from openfarm_core.packs.base import pack_for
from openfarm_core.state import StateDir, read_json
from openfarm_core.swarm import swarm_hint, swarm_set, swarm_start, worker_member_name
from tests.test_python_e2e import GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy


def _run_cli(repo: Path, *args: str) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_main(["--repo", str(repo), "--json", *args])
    return code, out.getvalue(), err.getvalue()


def _enable_swarm_in_config(toy, *, tiers_text: str = "") -> None:
    cfg_path = toy.root / ".openfarm" / "config.toml"
    base = cfg_path.read_text(encoding="utf-8")
    extra = (
        tiers_text
        + '\n[swarm]\nenabled = true\ncoordinator = "code"\nsupervisor = "off"\n'
    )
    cfg_path.write_text(base + extra, encoding="utf-8")
    toy.commit("enable swarm in config")


class BoardPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy = build_python_toy()
        self.addCleanup(self.toy.cleanup)

    def test_full_card_pipeline_posts_to_board_and_populates_reports(self) -> None:
        _enable_swarm_in_config(
            self.toy,
            tiers_text=(
                '\n[tiers.dsf]\nmodel = "stub"\n'
                'efforts = ["low", "medium", "high", "max"]\n'
                'effort_param = "reasoning_effort"\n'
            ),
        )
        self.toy.write_card(
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
        self.toy.commit("tally: add CARD-1")

        script_path = Path(tempfile.mkdtemp(prefix="openfarm-")) / "script.json"
        self.addCleanup(shutil.rmtree, script_path.parent, True)
        script_path.write_text(
            json.dumps({
                "CARD-1/test_writer": {
                    "content": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
                    "tokens_in": 120,
                    "tokens_out": 40,
                },
                "CARD-1/implementer": {
                    "content": GOOD_IMPLEMENTATION,
                    "tokens_in": 200,
                    "tokens_out": 60,
                },
                "CARD-1/checker_pass1": {
                    "content": "RESTATEMENT: full bundle raises FullError on push",
                    "tokens_in": 80,
                    "tokens_out": 20,
                },
                "CARD-1/checker_pass2": {
                    "content": "{}",
                    "tokens_in": 90,
                    "tokens_out": 15,
                },
            }),
            encoding="utf-8",
        )

        code, out, _ = _run_cli(
            self.toy.root,
            "run", "--card", "CARD-1", "--run-id", "run-s4", "--script", str(script_path),
        )
        self.assertEqual(code, EXIT_OK)
        run_payload = json.loads(out)
        self.assertEqual(run_payload["outcomes"][0]["final_state"], "in-review")

        board = Board(
            board_dir_for(self.toy.state_dir, "run-s4"), state_dir=self.toy.state_dir
        )
        self.assertTrue(board.is_ended())
        posts = board.read(as_name="*", since=0, limit=None, for_model=False)
        kinds = [p["kind"] for p in posts]
        self.assertEqual(kinds[0], "note")  # claim
        self.assertIn("brief", kinds)
        self.assertIn("result", kinds)
        self.assertIn("gate", kinds)
        self.assertEqual(kinds[-1], "done")

        # Gates must never address coordinator; final done addresses coordinator with dossier
        for post in posts:
            if post["kind"] == "gate":
                self.assertNotIn("coordinator", post["to"])
        done_post = posts[-1]
        self.assertEqual(done_post["from"], "gates")
        self.assertEqual(done_post["to"], ["coordinator"])
        self.assertTrue(any(ev.endswith("dossier.md") for ev in done_post["evidence"]))

        # Verify order: claim -> eligibility -> test_writer brief -> result -> ... -> done
        tw_brief_idx = next(
            i for i, p in enumerate(posts)
            if p["kind"] == "brief" and p["to"] == ["CARD-1-test-writer"]
        )
        tw_result_idx = next(
            i for i, p in enumerate(posts)
            if p["kind"] == "result" and p["from"] == "CARD-1-test-writer"
        )
        impl_brief_idx = next(
            i for i, p in enumerate(posts)
            if p["kind"] == "brief" and p["to"] == ["CARD-1-implementer"]
        )
        self.assertLess(tw_brief_idx, tw_result_idx)
        self.assertLess(tw_result_idx, impl_brief_idx)

        # Dossier JSON and Markdown contain team table and board path
        dossier_json = read_json(
            self.toy.state_dir / "cards" / "CARD-1" / "run-s4" / "dossier.json"
        )
        self.assertEqual(dossier_json["board_path"], str(board.board_dir))
        self.assertEqual(len(dossier_json["team"]), 4)
        impl_row = next(r for r in dossier_json["team"] if r["role"] == "implementer")
        self.assertEqual(impl_row["tokens"], 260)
        self.assertEqual(impl_row["attempts"], 1)

        dossier_md = (
            self.toy.state_dir / "cards" / "CARD-1" / "run-s4" / "dossier.md"
        ).read_text(encoding="utf-8")
        self.assertIn("## Team and board", dossier_md)
        self.assertIn("CARD-1-implementer", dossier_md)

        # Telemetry & card-record carry team and handbacks
        card_record = read_json(
            self.toy.state_dir / "cards" / "CARD-1" / "run-s4" / "card-record.json"
        )
        self.assertEqual(len(card_record["team"]), 4)
        self.assertEqual(card_record["handbacks"], [])

        # run.json tokens carries by_member and by_effort
        run_json = read_json(self.toy.state_dir / "runs" / "run-s4" / "run.json")
        self.assertEqual(run_json["tokens"]["by_member"]["CARD-1-implementer"], 260)
        self.assertEqual(run_json["tokens"]["by_member"]["CARD-1-test-writer"], 160)
        self.assertIn("none", run_json["tokens"]["by_effort"])
        self.assertEqual(run_json["tokens"]["by_effort"]["none"], 625)

    def test_live_member_set_hints_gate_routing_and_counter_defect(self) -> None:
        _enable_swarm_in_config(
            self.toy,
            tiers_text=(
                '\n[tiers.dsf]\nmodel = "stub-dsf"\n'
                'efforts = ["low", "medium", "high", "max"]\n'
                'effort_param = "reasoning_effort"\n'
            ),
        )
        self.toy.write_card(
            "CARD-2",
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
        self.toy.commit("tally: add CARD-2")

        cfg = Config.load_config(self.toy.root) if hasattr(Config, "load_config") else None
        from openfarm_core.config import load_config
        cfg = load_config(self.toy.root)
        state = StateDir(self.toy.state_dir)
        board = Board(board_dir_for(state, "run-hints"), state_dir=state)
        board.ensure()
        board.upsert_member("coordinator", kind="code", role="coordinator")
        board.upsert_member("gates", kind="code", role="gates")

        source = FileCardSource(
            self.toy.root, "cards", worktree=state.root / "cards-source", base_branch="main"
        )
        source.prepare()

        # Start card on board with custom effort, then set + hint implementer before claim/run
        swarm_start(
            board, cfg, caller="coordinator", card_id="CARD-2",
            team={"implementer": {"tier": "dsf", "effort": "low"}}, source=source,
        )
        impl_name = worker_member_name("CARD-2", "implementer")
        swarm_set(
            board, cfg, caller="coordinator", member_name=impl_name,
            effort="high", reason="tricky card",
        )
        swarm_hint(
            board, cfg, caller="coordinator", member_name=impl_name,
            body="check capacity with >=",
        )

        claim = source.claim("CARD-2", "openfarm:run-hints", cfg.lease_ttl_s)
        card = Card.from_dict(source.read("CARD-2")["card"])

        worktree = state.worktrees / "CARD-2-run-hints"
        Git(self.toy.root).worktree_add(worktree, "main")
        self.addCleanup(Git(self.toy.root).worktree_remove, worktree)

        card_dir = state.create_card("CARD-2", "run-hints")
        pack = pack_for("python", cwd=worktree, python=sys.executable)
        pack.configure(worktree)
        backend = ScriptedBackend({
            "CARD-2/test_writer": {
                "content": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
                "tokens_in": 100,
                "tokens_out": 50,
            },
            "CARD-2/implementer/1": {
                "content": (
                    "=== whole_file: tally/bounded.py\n"
                    "class FullError(Exception):\n"
                    "    pass\n\n\n"
                    "class Bounded:\n"
                    "    def __init__(self, capacity: int) -> None:\n"
                    "        self.capacity = capacity\n"
                    "        self.segments: list[int] = []\n\n"
                    "    def push(self, value: int) -> int:\n"
                    "        self.segments.append(value)\n"
                    "        return len(self.segments) - 1\n"
                ),
                "tokens_in": 110,
                "tokens_out": 40,
            },
            "CARD-2/implementer/2": {
                "content": (
                    "=== whole_file: tally/bounded.py\n"
                    "class FullError(Exception):\n"
                    "    pass\n\n\n"
                    "class Bounded:\n"
                    "    def __init__(self, capacity: int) -> None:\n"
                    "        self.capacity = capacity\n"
                    "        self.segments: list[int] = []\n\n"
                    "    def push(self, value: int) -> int:\n"
                    "        if len(self.segments) >= self.capacity:\n"
                    '            raise FullError(f"capacity {self.capacity} reached")\n'
                    "        self.segments.append(value)\n"
                    "        return len(self.segments) - 1\n"
                ),
                "tokens_in": 130,
                "tokens_out": 50,
            },
            "CARD-2/checker_pass1": {
                "content": "RESTATEMENT: full bundle raises FullError on push",
                "tokens_in": 50,
                "tokens_out": 20,
            },
            "CARD-2/checker_pass2": {
                "content": json.dumps({
                    "counter_tests": [{
                        "name": "test_counter_check",
                        "file": "tests/test_counter.py",
                        "content": "import unittest\nclass CounterTest(unittest.TestCase):\n    def test_counter_check(self):\n        self.assertEqual(1, 2)\n",
                    }]
                }),
                "tokens_in": 60,
                "tokens_out": 30,
            },
        })
        book = BriefBook(worktree, cfg.never_touch, cfg.forbidden_files)
        book.pack = pack
        telemetry = MemoryTelemetry()

        cr = CardRun(
            card=card, cfg=cfg, state=state, card_dir=card_dir, worktree=worktree,
            runner=DirectRunner(worktree), pack=pack, backend=backend, book=book,
            run_id="run-hints", telemetry=telemetry, deps_done=True,
            board=board, swarm=cfg.swarm,
        )
        cr.card_source = source
        rec = cr.run(claim=claim)
        self.assertEqual(rec["final_state"], "in-review", rec)

        # Verify ScriptedBackend received live tier model + effort + fenced hint
        impl_calls = [c for c in backend.calls if c["role"] == "implementer"]
        self.assertEqual(len(impl_calls), 2)
        self.assertEqual(impl_calls[0]["model"], "stub-dsf")
        self.assertEqual(impl_calls[0]["effort"], "high")
        self.assertEqual(impl_calls[0]["effort_param"], "reasoning_effort")
        self.assertIn("check capacity with >=", impl_calls[0]["messages"][0]["content"])
        self.assertIn("Coordinator hint (data):", impl_calls[0]["messages"][0]["content"])

        # Gate failure on attempt 1 was addressed to implementer, not coordinator
        posts = board.read(as_name="*", since=0, limit=None, for_model=False)
        fail_gates = [
            p for p in posts
            if p["kind"] == "gate" and ": fail" in p["body"]
        ]
        self.assertTrue(fail_gates)
        self.assertTrue(all(g["to"] == [impl_name] for g in fail_gates))

        # Checker counter-test failure posted kind="defect" to implementer
        defects = [p for p in posts if p["kind"] == "defect"]
        self.assertEqual(len(defects), 1)
        self.assertEqual(defects[0]["from"], "CARD-2-checker")
        self.assertEqual(defects[0]["to"], [impl_name])

        # Dossier captures sets (with reason) and team tokens
        dossier_json = read_json(card_dir / "dossier.json")
        self.assertEqual(len(dossier_json["sets"]), 1)
        self.assertIn("tricky card", dossier_json["sets"][0]["detail"])
        dossier_md = (card_dir / "dossier.md").read_text(encoding="utf-8")
        self.assertIn("Tier and effort changes", dossier_md)
        self.assertIn("tricky card", dossier_md)


if __name__ == "__main__":
    unittest.main()
