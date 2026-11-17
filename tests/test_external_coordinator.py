"""Tests for Stage 8 (Done 8): external coordinator driving runs via CLI swarm commands."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.board import Board, board_dir_for
from openfarm_core.cli import EXIT_OK, main as cli_main
from openfarm_core.errors import StopCode
from openfarm_core.state import StateDir
from tests.test_python_e2e import GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy


def _run_cli(repo: Path, *args: str) -> tuple[int, dict, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_main(["--repo", str(repo), "--json", *args])
    raw = out.getvalue().strip()
    return code, (json.loads(raw) if raw else {}), err.getvalue()


class ExternalCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy = build_python_toy()
        self.addCleanup(self.toy.cleanup)

    def _configure_external(
        self,
        *,
        external_wait: str = "10s",
        on_external_timeout: str = "fallback",
        concurrent_cards: int = 2,
    ) -> None:
        self.toy.write("tally/alpha.py", "def a(x: int) -> int:\n    return x\n")
        self.toy.write("tally/beta.py", "def b(x: int) -> int:\n    return x\n")
        self.toy.commit("add alpha and beta")

        cfg_path = self.toy.root / ".openfarm" / "config.toml"
        cfg_text = cfg_path.read_text(encoding="utf-8")
        if "concurrent_cards =" in cfg_text:
            cfg_text = cfg_text.replace(
                "concurrent_cards = 1", f"concurrent_cards = {concurrent_cards}"
            )
        else:
            cfg_text = cfg_text.replace(
                "concurrent_builds = 2",
                f"concurrent_builds = 2\nconcurrent_cards = {concurrent_cards}",
            )
        cfg_text += (
            '\n[tiers.fast]\n'
            'model = "stub"\n'
            'efforts = ["low", "medium", "high", "max"]\n'
            'effort_param = "reasoning_effort"\n'
            '\n[swarm]\n'
            'enabled = true\n'
            'coordinator = "external"\n'
            'supervisor = "off"\n'
            'default_tier = "fast"\n'
            f'external_wait = "{external_wait}"\n'
            f'on_external_timeout = "{on_external_timeout}"\n'
        )
        cfg_path.write_text(cfg_text, encoding="utf-8")

    def test_external_coordinator_drives_two_cards_to_review_via_swarm_commands(self) -> None:
        self._configure_external()
        self.toy.write_card(
            "CARD-1",
            title="Alpha",
            category="pinned-bugfix",
            unit=".",
            touches=["tally/alpha.py"],
            tests=[{"name": "test_a", "file": "tests/test_a.py", "asserts": "a(1)==2"}],
            done_text="`a(x)` returns `x + 1`.",
        )
        self.toy.write_card(
            "CARD-2",
            title="Beta",
            category="pinned-bugfix",
            unit=".",
            touches=["tally/beta.py"],
            tests=[{"name": "test_b", "file": "tests/test_b.py", "asserts": "b(1)==3"}],
            done_text="`b(x)` returns `x + 2`.",
        )
        self.toy.commit("add CARD-1 and CARD-2")

        script = {
            "CARD-1/test_writer": (
                "=== whole_file: tests/test_a.py\n"
                "import unittest\nfrom tally.alpha import a\n"
                "class T(unittest.TestCase):\n"
                "    def test_a(self): self.assertEqual(a(1), 2)\n"
            ),
            "CARD-1/implementer/1/low": {
                "content": "=== whole_file: tally/alpha.py\ndef a(x: int) -> int:\n    return x + 1\n",
                "tokens_in": 20,
                "tokens_out": 8,
            },
            "CARD-1/checker_pass1": "RESTATEMENT: a returns x + 1",
            "CARD-1/checker_pass2": "{}",
            "CARD-2/test_writer": (
                "=== whole_file: tests/test_b.py\n"
                "import unittest\nfrom tally.beta import b\n"
                "class T(unittest.TestCase):\n"
                "    def test_b(self): self.assertEqual(b(1), 3)\n"
            ),
            "CARD-2/implementer/1/high": {
                "content": "=== whole_file: tally/beta.py\ndef b(x: int) -> int:\n    return x + 2\n",
                "tokens_in": 35,
                "tokens_out": 12,
            },
            "CARD-2/checker_pass1": "RESTATEMENT: b returns x + 2",
            "CARD-2/checker_pass2": "{}",
        }
        tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        sp = tmp / "script.json"
        sp.write_text(json.dumps(script), encoding="utf-8")

        run_holder: dict = {}

        def _run_bg() -> None:
            run_holder["res"] = _run_cli(
                self.toy.root, "run", "--cards", "2", "--script", str(sp)
            )

        bg = threading.Thread(target=_run_bg, daemon=True)
        bg.start()

        state = StateDir(self.toy.state_dir)
        pause_ev = threading.Event()
        rid = ""
        for _ in range(300):
            holder = state.lock_holder()
            if holder and holder.get("run_id"):
                bdir = board_dir_for(state, str(holder["run_id"]))
                if (bdir / "board.jsonl").exists():
                    rid = str(holder["run_id"])
                    break
            if not bg.is_alive():
                break
            pause_ev.wait(0.02)
        self.assertTrue(rid, f"run never initialized board: {run_holder}")

        c_join, out_join, err_join = _run_cli(
            self.toy.root, "swarm", "join", "--name", "bob", "--seat", "coordinator", "--kind", "agent"
        )
        self.assertEqual(c_join, EXIT_OK, err_join)
        self.assertEqual(out_join["member"]["name"], "bob")

        c_read, out_read, err_read = _run_cli(
            self.toy.root, "board", "read", "--as", "bob", "--since", "0"
        )
        self.assertEqual(c_read, EXIT_OK, err_read)
        self.assertTrue(any("event: plan" in p["body"] for p in out_read["posts"]))

        c_s1, _out_s1, err_s1 = _run_cli(
            self.toy.root,
            "swarm",
            "start",
            "CARD-1",
            "--team",
            "implementer=fast:low",
            "--as",
            "bob",
        )
        self.assertEqual(c_s1, EXIT_OK, err_s1)

        # Wait for CARD-1 done post, remove its implementer, then start CARD-2!
        card1_done = False
        for _ in range(400):
            _rc, r_posts, _ = _run_cli(
                self.toy.root, "board", "read", "--as", "*", "--since", "0", "--limit", "200"
            )
            if any(p.get("card") == "CARD-1" and p.get("kind") == "done" for p in r_posts.get("posts", [])):
                card1_done = True
                break
            pause_ev.wait(0.02)
        self.assertTrue(card1_done, "CARD-1 did not post done")

        # Code retires a finished card's workers itself (v0.8.1), so an external
        # coordinator's own remove either wins the race or fails cleanly.
        c_rem, out_rem, err_rem = _run_cli(
            self.toy.root, "swarm", "remove", "CARD-1-implementer", "--as", "bob"
        )
        if c_rem != EXIT_OK:
            self.assertEqual(out_rem.get("kind"), "refused", (out_rem, err_rem))
            self.assertIn("already removed", out_rem.get("error", ""))

        c_s2, _out_s2, err_s2 = _run_cli(
            self.toy.root,
            "swarm",
            "start",
            "CARD-2",
            "--team",
            "implementer=fast:high",
            "--as",
            "bob",
        )
        self.assertEqual(c_s2, EXIT_OK, err_s2)

        bg.join(timeout=15.0)
        self.assertFalse(bg.is_alive())
        code, payload, err = run_holder["res"]
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(
            {o["card_id"]: o["final_state"] for o in payload["outcomes"]},
            {"CARD-1": "in-review", "CARD-2": "in-review"},
        )
        self.assertEqual(payload["tokens"]["by_effort"].get("low"), 28)
        self.assertEqual(payload["tokens"]["by_effort"].get("high"), 47)

    def test_external_coordinator_handback_hint_and_timeout_block(self) -> None:
        self._configure_external(external_wait="10s", on_external_timeout="block")
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

        script = {
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
            "CARD-1/implementer/1": "<<<HANDBACK\nunclear module export\n>>>",
            "CARD-1/implementer/2": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: full bundle raises FullError on push",
            "CARD-1/checker_pass2": "{}",
        }
        tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        sp = tmp / "script.json"
        sp.write_text(json.dumps(script), encoding="utf-8")

        run_holder: dict = {}

        def _run_bg() -> None:
            run_holder["res"] = _run_cli(
                self.toy.root, "run", "--cards", "1", "--script", str(sp)
            )

        bg = threading.Thread(target=_run_bg, daemon=True)
        bg.start()

        state = StateDir(self.toy.state_dir)
        pause_ev = threading.Event()
        rid = ""
        for _ in range(300):
            holder = state.lock_holder()
            if holder and holder.get("run_id"):
                bdir = board_dir_for(state, str(holder["run_id"]))
                if (bdir / "board.jsonl").exists():
                    rid = str(holder["run_id"])
                    break
            if not bg.is_alive():
                break
            pause_ev.wait(0.02)
        self.assertTrue(rid, f"run never initialized board: {run_holder}")

        c_j, _, e_j = _run_cli(
            self.toy.root, "swarm", "join", "--name", "bob", "--seat", "coordinator"
        )
        self.assertEqual(c_j, EXIT_OK, e_j)
        c_s, _, e_s = _run_cli(
            self.toy.root, "swarm", "start", "CARD-1", "--as", "bob"
        )
        self.assertEqual(c_s, EXIT_OK, e_s)

        seen_hb = False
        for _ in range(400):
            _rc, r_posts, _ = _run_cli(
                self.toy.root, "board", "read", "--as", "*", "--since", "0", "--limit", "200"
            )
            if any(p.get("kind") == "handback" and p.get("card") == "CARD-1" for p in r_posts.get("posts", [])):
                seen_hb = True
                break
            pause_ev.wait(0.02)
        self.assertTrue(seen_hb, "expected handback post on board")

        c_h, _, e_h = _run_cli(
            self.toy.root,
            "swarm",
            "hint",
            "CARD-1-implementer",
            "--body",
            "Raise FullError from bounded.",
            "--as",
            "bob",
        )
        self.assertEqual(c_h, EXIT_OK, e_h)

        bg.join(timeout=15.0)
        self.assertFalse(bg.is_alive())
        code, payload, err = run_holder["res"]
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(payload["outcomes"][0]["final_state"], "in-review")

    def test_external_timeout_block_gives_seat_silent(self) -> None:
        self._configure_external(external_wait="1s", on_external_timeout="block")
        self.toy.write_card(
            "CARD-1",
            title="Alpha",
            category="pinned-bugfix",
            unit=".",
            touches=["tally/alpha.py"],
            tests=[{"name": "test_a", "file": "tests/test_a.py", "asserts": "a(1)==2"}],
            done_text="`a(x)` returns `x + 1`.",
        )
        self.toy.commit("add CARD-1")

        tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        sp = tmp / "script.json"
        sp.write_text("{}", encoding="utf-8")

        code, payload, err = _run_cli(
            self.toy.root, "run", "--cards", "1", "--script", str(sp)
        )
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(payload["outcomes"][0]["final_state"], "blocked")
        self.assertEqual(payload["outcomes"][0]["stop_code"], StopCode.SEAT_SILENT.value)


if __name__ == "__main__":
    unittest.main()
