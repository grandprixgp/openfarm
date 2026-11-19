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
    def __init__(self, keys: list[int]) -> None:
        self._keys = list(keys)
        self.drawn: list[str] = []

    def getmaxyx(self) -> tuple[int, int]:
        return (30, 100)

    def timeout(self, _ms: int) -> None:
        return None

    def addnstr(self, _y: int, _x: int, text: str, _n: int, _attr: int = 0) -> None:
        self.drawn.append(text)

    def addstr(self, _y: int, _x: int, text: str, _attr: int = 0) -> None:
        self.drawn.append(text)

    def move(self, _y: int, _x: int) -> None:
        return None

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


if __name__ == "__main__":
    unittest.main()
