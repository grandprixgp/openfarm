"""v0.9.2 interactive in-TUI Automode execution modal tests."""

from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openfarm_core.tui import (
    TuiController,
    _prompt_line,
    _run_automode_step,
    _run_grill_modal,
    _run_worker_key_modal,
)
from tests.test_python_e2e import build_python_toy


class _FakeStdscr:
    def __init__(self, keys: list[int], *, rows: int = 30, cols: int = 100) -> None:
        self._keys = list(keys)
        self._rows = rows
        self._cols = cols
        self.drawn: list[str] = []
        self.cursor_positions: list[tuple[int, int]] = []

    def getmaxyx(self) -> tuple[int, int]:
        return (self._rows, self._cols)

    def timeout(self, _ms: int) -> None:
        return None

    def addnstr(self, _y: int, _x: int, text: str, _n: int, _attr: int = 0) -> None:
        self.drawn.append(text)

    def addstr(self, _y: int, _x: int, text: str, _attr: int = 0) -> None:
        self.drawn.append(text)

    def move(self, y: int, x: int) -> None:
        self.cursor_positions.append((y, x))

    def refresh(self) -> None:
        return None

    def getch(self) -> int:
        return self._keys.pop(0) if self._keys else ord("\n")


class TuiModalsTests(unittest.TestCase):
    def test_prompt_line_mask_hides_secret_characters(self) -> None:
        keys = [ord(c) for c in "sk-abc"] + [ord("\n")]
        scr = _FakeStdscr(keys)
        val = _prompt_line(scr, "Key: ", mask="*")
        self.assertEqual(val, "sk-abc")
        combined = "\n".join(scr.drawn)
        self.assertNotIn("sk-abc", combined)
        self.assertIn("******", combined)

    def test_worker_key_modal_writes_atomic_0600_key_in_tui(self) -> None:
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        key_dest = toy.root.parent / "secrets" / "worker.key"
        cfg_p = toy.root / ".openfarm" / "config.toml"
        txt = cfg_p.read_text()
        txt = txt.replace('model = "scripted"', 'model = "openai-compatible"')
        if "key_file" not in txt:
            txt = txt.replace("[model]\n", f'[model]\nkey_file = "{key_dest}"\n')
        cfg_p.write_text(txt)
        ctrl = TuiController(toy.root)
        keys = [ord(c) for c in "sk-live-secret"] + [ord("\n")]
        scr = _FakeStdscr(keys)
        with mock.patch("openfarm_core.automode.list_existing_key_files", return_value=[]):
            _run_worker_key_modal(scr, ctrl)
        self.assertTrue(key_dest.is_file())
        self.assertEqual(stat.S_IMODE(key_dest.stat().st_mode), 0o600)
        self.assertEqual(key_dest.read_text(), "sk-live-secret\n")
        self.assertNotIn("sk-live-secret", "\n".join(scr.drawn))

    def test_grill_modal_writes_decisions_and_expands_spec_in_tui(self) -> None:
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        ctrl = TuiController(toy.root)
        # Accept default for all 3 grill questions with Enter
        scr = _FakeStdscr([ord("\n"), ord("\n"), ord("\n")])
        _run_grill_modal(scr, ctrl)
        self.assertTrue((toy.root / ".openfarm" / "GRILL_DECISIONS.md").is_file())
        spec_text = (toy.root / ".openfarm" / "PROJECT_SPEC.md").read_text()
        self.assertIn("Project Specification", spec_text)

    def test_automode_enter_dispatches_to_current_stage_auth_suspended(self) -> None:
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        ctrl = TuiController(toy.root)
        scr = _FakeStdscr([ord("\n")])
        fake_pipe = {
            "stage": "adopted",
            "stages": [{"id": "auth", "label": "Auth", "status": "current", "action": "auth", "provider": "claude", "prompt": "Sign in"}],
            "current_stage": {"id": "auth", "label": "Auth", "status": "current", "action": "auth", "provider": "claude", "prompt": "Sign in"},
        }
        called = []
        with mock.patch("openfarm_core.automode.compute_pipeline_stages", return_value=fake_pipe), \
             mock.patch("openfarm_core.tui._run_suspended_tty", side_effect=lambda s, argv, cwd: (called.append(argv), 0)[1]):
            ok = _run_automode_step(scr, ctrl)
        self.assertTrue(ok)
        self.assertEqual(len(called), 1)
        self.assertIn("login", called[0])
        self.assertIn("claude", called[0])

    def test_prompt_line_scrolls_horizontally_when_input_exceeds_width(self) -> None:
        long_text = "one two three four five six seven eight nine TAIL_END"
        keys = [ord(c) for c in long_text] + [ord("\n")]
        scr = _FakeStdscr(keys, rows=24, cols=40)
        val = _prompt_line(scr, "Answer> ")
        self.assertEqual(val, long_text)
        last_drawn = scr.drawn[-1]
        self.assertIn("TAIL_END", last_drawn)
        self.assertIn("…", last_drawn)
        self.assertLessEqual(len(last_drawn), 38)

    def test_grill_modal_shows_full_question_and_long_answer_without_cutoff(self) -> None:
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        ctrl = TuiController(toy.root)
        long_workflow = (
            "User uploads daily photos, tags locations and cameras, "
            "generates a static gallery with EXIF metadata and FINAL_WORKFLOW_TOKEN"
        )
        keys = [ord(c) for c in long_workflow] + [ord("\n"), ord("\n"), ord("\n")]
        scr = _FakeStdscr(keys, rows=28, cols=80)
        _run_grill_modal(scr, ctrl)
        decisions = (toy.root / ".openfarm" / "GRILL_DECISIONS.md").read_text()
        self.assertIn("FINAL_WORKFLOW_TOKEN", decisions)
        combined = "\n".join(scr.drawn)
        self.assertIn("Grill 1/3", combined)
        self.assertIn("FINAL_WORKFLOW_TOKEN", combined)
        self.assertIn("Answer> ", combined)

    def test_grill_modal_advances_pipeline_out_of_spec_stage_without_looping(self) -> None:
        from openfarm_core.automode import compute_pipeline_stages

        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        # Simulate freshly initialized repo with empty cards/ folder and minimal spec.
        cards_dir = toy.root / "cards"
        for md in cards_dir.glob("*.md"):
            md.unlink()
        spec_p = toy.root / ".openfarm" / "PROJECT_SPEC.md"
        if spec_p.exists():
            spec_p.unlink()

        before = compute_pipeline_stages(toy.root, acp_status={"oauth_ready": True})
        self.assertEqual(before["current_stage"]["id"], "spec_and_slice")
        self.assertEqual(before["current_stage"]["action"], "grill")

        ctrl = TuiController(toy.root)
        scr = _FakeStdscr([ord(c) for c in "Daily photo gallery app"] + [ord("\n"), ord("\n"), ord("\n")])
        _run_grill_modal(scr, ctrl)

        card_files = list(cards_dir.glob("*.md"))
        self.assertGreaterEqual(len(card_files), 1)
        after = compute_pipeline_stages(toy.root, acp_status={"oauth_ready": True})
        self.assertEqual(after["current_stage"]["id"], "wave")
        self.assertNotEqual(after["current_stage"]["action"], "grill")

    def test_ensure_baseline_committed_handles_unborn_head_and_respects_user_edits(self) -> None:
        from openfarm_core.automode import ensure_baseline_committed
        from openfarm_core.git import Git

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "fresh-app"
            repo.mkdir()
            git = Git(repo)
            git.init(branch="main")
            git._run(["config", "user.name", "Test User"])
            git._run(["config", "user.email", "test@example.invalid"])
            (repo / ".openfarm").mkdir()
            (repo / ".openfarm" / "PROJECT_SPEC.md").write_text("# Spec\n\nGoal\n")
            (repo / "cards").mkdir()
            (repo / "cards" / "W1-01.md").write_text("+++\nid = \"W1-01\"\n+++\n")
            self.assertFalse(git.ref_exists("HEAD"))

            # Unborn HEAD: commits initial baseline so `git rev-parse HEAD` works.
            self.assertTrue(ensure_baseline_committed(repo))
            self.assertTrue(git.ref_exists("HEAD"))
            self.assertTrue(git.is_clean())

            # Dirty only under .openfarm/ and cards/: auto-commits cleanly.
            (repo / ".openfarm" / "GRILL_DECISIONS.md").write_text("# Decisions\n")
            self.assertTrue(ensure_baseline_committed(repo))
            self.assertTrue(git.is_clean())

            # Dirty user source file when HEAD already exists: does NOT silently commit.
            (repo / "src").mkdir()
            (repo / "src" / "main.rs").write_text("fn main() {}\n")
            self.assertFalse(ensure_baseline_committed(repo))
            self.assertFalse(git.is_clean())

    def test_file_card_source_reads_and_claims_slugged_card_filename_by_header_id(self) -> None:
        from openfarm_core.adapters.files import FileCardSource, render_card_file
        from openfarm_core.git import Git

        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        slugged = toy.root / "cards" / "W1-01-golden-slice.md"
        slugged.parent.mkdir(parents=True, exist_ok=True)
        slugged.write_text(
            render_card_file(
                {"id": "W1-01", "title": "Golden slice", "state": "ready", "touches": [], "depends": [], "category": "new"},
                "## Done\n\n- Works\n",
            ),
            encoding="utf-8",
        )
        Git(toy.root).commit_all("add slugged card")
        src = FileCardSource(toy.root, "cards", worktree=toy.root.parent / "cards-source-slugged")
        src.prepare()
        card = src.read("W1-01")
        self.assertEqual(card["card"]["id"], "W1-01")
        claim = src.claim("W1-01", holder="lane-1")
        self.assertTrue(claim.get("lease_id"))
        self.assertEqual(src.read("W1-01")["card"]["state"], "in_progress")
        src.release("W1-01", lease_id=claim["lease_id"], reason="released in test")
        self.assertEqual(src.read("W1-01")["card"]["state"], "ready")

    def test_grill_scaffolded_card_passes_real_eligibility_gate(self) -> None:
        from openfarm_core.adapters.files import parse_card_file
        from openfarm_core.automode import run_grill_and_architect
        from openfarm_core.bootstrap import initialize_project
        from openfarm_core.cards import Card
        from openfarm_core.config import load_config
        from openfarm_core.gates.eligibility import check_eligibility

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "photo-app"
            state_dir = Path(tmp) / "state"
            secrets_dir = Path(tmp) / "secrets"
            initialize_project(
                "photo-app",
                directory=root,
                idea="Daily photo gallery",
                state_dir=state_dir,
                secrets_dir=secrets_dir,
                no_git=True,
            )
            run_grill_and_architect(
                root,
                answers=[
                    {"id": "goal", "question": "Core workflow", "answer": "Upload and browse daily photos"},
                    {"id": "invariants", "question": "Invariants", "answer": "No data loss on invalid input"},
                    {"id": "slice", "question": "Golden slice", "answer": "Add photo entry struct and list command"},
                ],
            )
            card_path = root / "cards" / "W1-01.md"
            self.assertTrue(card_path.is_file())
            header, body = parse_card_file(card_path.read_text(encoding="utf-8"))
            card = Card(
                id=str(header["id"]),
                title=str(header["title"]),
                state=str(header["state"]),
                done_text=body,
                touches=list(header.get("touches") or []),
                deps=list(header.get("depends") or []),
                fields=dict(header),
            )
            cfg = load_config(root)
            elig = check_eligibility(card, cfg, worktree=root)
            self.assertTrue(elig.ok, f"eligibility failed: {elig.failures}")


if __name__ == "__main__":
    unittest.main()
