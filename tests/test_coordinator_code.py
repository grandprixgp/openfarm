"""Tests for Stage 8 (Done 5): CodeCoordinator effort escalation, ID ordering, handback, cleanup."""

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

from openfarm_core.board import Board, board_dir_for
from openfarm_core.cli import EXIT_OK, EXIT_TROUBLE, main as cli_main
from openfarm_core.coordinator import CodeCoordinator
from openfarm_core.state import StateDir, read_json
from openfarm_core.swarm import worker_member_name
from tests.test_python_e2e import GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy


def _run_cli(repo: Path, *args: str) -> tuple[int, dict, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_main(["--repo", str(repo), "--json", *args])
    raw = out.getvalue().strip()
    return code, (json.loads(raw) if raw else {}), err.getvalue()


class CodeCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy = build_python_toy()
        self.addCleanup(self.toy.cleanup)

    def _configure(
        self,
        *,
        default_effort: str = "medium",
        escalate_after: int = 2,
        implementer_attempts: int = 3,
        supervisor: str = "off",
        concurrent_cards: int = 2,
    ) -> None:
        cfg_path = self.toy.root / ".openfarm" / "config.toml"
        cfg_text = cfg_path.read_text(encoding="utf-8")
        cfg_text = cfg_text.replace(
            "implementer_attempts = 3",
            f"implementer_attempts = {implementer_attempts}",
        )
        if "concurrent_cards =" in cfg_text:
            cfg_text = cfg_text.replace(
                "concurrent_cards = 1", f"concurrent_cards = {concurrent_cards}"
            )
        else:
            cfg_text = cfg_text.replace(
                "concurrent_builds = 2",
                f"concurrent_builds = 2\nconcurrent_cards = {concurrent_cards}",
            )
        cfg_text = cfg_text.replace(
            'implementer = { model = "stub", temperature = 0.3, vary_seed = true }',
            f'implementer = {{ model = "stub", temperature = 0.3, vary_seed = true, effort = "{default_effort}" }}',
        )
        cfg_text += (
            '\n[tiers.dsf]\n'
            'model = "stub"\n'
            'efforts = ["low", "medium", "high", "max"]\n'
            'effort_param = "reasoning_effort"\n'
            '\n[swarm]\n'
            'enabled = true\n'
            'coordinator = "code"\n'
            f'supervisor = "{supervisor}"\n'
            'default_tier = "dsf"\n'
            f'escalate_after = {escalate_after}\n'
        )
        cfg_path.write_text(cfg_text, encoding="utf-8")

    def test_escalates_effort_from_medium_to_high_after_two_failures(self) -> None:
        self._configure(default_effort="medium", escalate_after=2, implementer_attempts=3)
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
        self.toy.commit("add CARD-1")

        bad_impl = (
            "=== whole_file: tally/bounded.py\n"
            "class FullError(Exception):\n    pass\n\n"
            "class Bounded:\n"
            "    def __init__(self, cap):\n        self.cap = cap\n        self.items = []\n"
            "    def push(self, item):\n        self.items.append(item)\n"
        )
        script = {
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
            # Identical diff hash on attempts 1 and 2 at medium would normally trigger NO_PROGRESS,
            # but struggling triggers BEFORE _check_progress and escalates effort medium -> high!
            "CARD-1/implementer/1/medium": {
                "content": bad_impl,
                "tokens_in": 40,
                "tokens_out": 15,
            },
            "CARD-1/implementer/2/medium": {
                "content": bad_impl,
                "tokens_in": 40,
                "tokens_out": 15,
            },
            "CARD-1/implementer/3/high": {
                "content": GOOD_IMPLEMENTATION,
                "tokens_in": 60,
                "tokens_out": 20,
            },
            "CARD-1/checker_pass1": "RESTATEMENT: full bundle raises FullError on push",
            "CARD-1/checker_pass2": "{}",
        }
        tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        script_path = tmp / "script.json"
        script_path.write_text(json.dumps(script), encoding="utf-8")

        code, payload, err = _run_cli(
            self.toy.root, "run", "--cards", "1", "--script", str(script_path)
        )
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(len(payload["outcomes"]), 1)
        out = payload["outcomes"][0]
        self.assertEqual(out["final_state"], "in-review")
        impl_team = next(
            t for t in out["team"] if t["member"] == "CARD-1-implementer"
        )
        self.assertEqual(impl_team["attempts"], 3)

        by_eff = payload["tokens"]["by_effort"]
        self.assertGreater(by_eff.get("medium", 0), 0)
        self.assertGreater(by_eff.get("high", 0), 0)

        state = StateDir(self.toy.state_dir)
        board = Board(board_dir_for(state, payload["run_id"]), state_dir=state)
        posts = board.read(as_name="*", since=0, limit=None, for_model=False)
        set_posts = [
            p for p in posts
            if p["kind"] == "action" and p["body"].startswith("set CARD-1-implementer:")
        ]
        self.assertEqual(len(set_posts), 1)
        self.assertIn("effort=high", set_posts[0]["body"])
        # CodeCoordinator removes workers cleanly after finished event
        impl_member = board.get_member(worker_member_name("CARD-1", "implementer"))
        self.assertEqual(impl_member["state"], "removed")

    def test_starts_cards_in_id_order_and_respects_top_effort_no_op(self) -> None:
        self._configure(
            default_effort="max",
            escalate_after=1,
            implementer_attempts=2,
            concurrent_cards=1,
        )
        self.toy.write("tally/alpha.py", "def a(x: int) -> int:\n    return x\n")
        self.toy.write("tally/beta.py", "def b(x: int) -> int:\n    return x\n")
        self.toy.write_card(
            "CARD-2",
            title="Beta",
            category="pinned-bugfix",
            unit=".",
            touches=["tally/beta.py"],
            tests=[{"name": "test_b", "file": "tests/test_b.py", "asserts": "b(1)==2"}],
            done_text="`b(x)` returns `x + 1`.",
        )
        self.toy.write_card(
            "CARD-1",
            title="Alpha",
            category="pinned-bugfix",
            unit=".",
            touches=["tally/alpha.py"],
            tests=[{"name": "test_a", "file": "tests/test_a.py", "asserts": "a(1)==2"}],
            done_text="`a(x)` returns `x + 1`.",
        )
        self.toy.commit("add CARD-2 and CARD-1")

        script = {
            "CARD-1/test_writer": (
                "=== whole_file: tests/test_a.py\n"
                "import unittest\nfrom tally.alpha import a\n"
                "class T(unittest.TestCase):\n"
                "    def test_a(self): self.assertEqual(a(1), 2)\n"
            ),
            "CARD-1/implementer": (
                "=== whole_file: tally/alpha.py\ndef a(x: int) -> int:\n    return x + 1\n"
            ),
            "CARD-1/checker_pass1": "RESTATEMENT: a adds 1",
            "CARD-1/checker_pass2": "{}",
            "CARD-2/test_writer": (
                "=== whole_file: tests/test_b.py\n"
                "import unittest\nfrom tally.beta import b\n"
                "class T(unittest.TestCase):\n"
                "    def test_b(self): self.assertEqual(b(1), 2)\n"
            ),
            "CARD-2/implementer/1": (
                "=== whole_file: tally/beta.py\ndef b(x: int) -> int:\n    return x + 99\n"
            ),
            "CARD-2/implementer/2": (
                "=== whole_file: tally/beta.py\ndef b(x: int) -> int:\n    return x + 99\n"
            ),
            "CARD-2/checker_pass1": "RESTATEMENT: b adds 1",
            "CARD-2/checker_pass2": "{}",
        }
        tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        sp = tmp / "script.json"
        sp.write_text(json.dumps(script), encoding="utf-8")

        code, payload, err = _run_cli(
            self.toy.root, "run", "--cards", "2", "--script", str(sp)
        )
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual([o["card_id"] for o in payload["outcomes"]], ["CARD-1", "CARD-2"])
        self.assertEqual(payload["outcomes"][0]["final_state"], "in-review")
        self.assertEqual(payload["outcomes"][1]["final_state"], "blocked")
        # Already at max -> CodeCoordinator does nothing; no-progress stops CARD-2
        self.assertEqual(payload["outcomes"][1]["stop_code"], "no-progress")

        state = StateDir(self.toy.state_dir)
        board = Board(board_dir_for(state, payload["run_id"]), state_dir=state)
        starts = [
            p["card"]
            for p in board.read(as_name="*", since=0, limit=None, for_model=False)
            if p["kind"] == "action" and p["body"].startswith("start ")
        ]
        self.assertEqual(starts, ["CARD-1", "CARD-2"])


if __name__ == "__main__":
    unittest.main()
