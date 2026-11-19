"""v0.9.1 bare `openfarm` automode TUI and command palette tests."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openfarm_core.automode import (
    AUTOMODE_PALETTE,
    detect_workspace_state,
    render_automode_summary,
)
from openfarm_core.cli import main as cli_main
from tests.test_python_e2e import build_python_toy, run_cli


class AutomodeTuiTests(unittest.TestCase):
    def test_detect_workspace_state_empty_unadopted_and_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty"
            empty.mkdir()
            st_empty = detect_workspace_state(empty)
            self.assertEqual(st_empty["stage"], "empty")
            self.assertEqual(st_empty["next_action"], "init")
            self.assertIn("Empty", render_automode_summary(st_empty))

            unadopted = Path(tmp) / "goproj"
            unadopted.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=unadopted, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (unadopted / "go.mod").write_text("module example.com/app\n\ngo 1.22\n")
            st_un = detect_workspace_state(unadopted)
            self.assertEqual(st_un["stage"], "unadopted")
            self.assertEqual(st_un["next_action"], "adopt")
            self.assertEqual(st_un["detected_language"], "go")

        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        toy.write_card(
            "CARD-1", title="Fix one bug", category="pinned-bugfix",
            unit=".", touches=["tally/bounded.py"],
            tests=[{"name": "test_a", "file": "tests/test_a.py"}],
            done_text="Fix bug.",
        )
        toy.commit("add CARD-1")
        st_ad = detect_workspace_state(toy.root)
        self.assertEqual(st_ad["stage"], "adopted")
        self.assertEqual(st_ad["next_action"], "run")
        self.assertEqual(st_ad["counts"]["ready"], 1)

    def test_automode_palette_covers_guided_and_power_tools(self) -> None:
        names = {item["id"] for item in AUTOMODE_PALETTE}
        for required in ("init", "adopt", "run", "review", "land", "triage",
                         "frontier-review", "grill", "stats", "harness", "acp"):
            self.assertIn(required, names)

    def test_bare_openfarm_launches_tui_on_tty_and_help_on_non_tty(self) -> None:
        called = []

        def fake_run_tui(**kwargs) -> int:
            called.append(kwargs)
            return 0

        with mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("sys.stdout.isatty", return_value=True), \
             mock.patch("openfarm_core.cli.run_tui", side_effect=fake_run_tui):
            rc = cli_main([])
        self.assertEqual(rc, 0)
        self.assertEqual(len(called), 1)
        self.assertIsNone(called[0]["run_id"])
        self.assertEqual(called[0]["view"], "overview")

        # Non-TTY still prints help and exits 2 (scripts/CI safety)
        with mock.patch("sys.stdin.isatty", return_value=False), \
             mock.patch("sys.stdout.isatty", return_value=False):
            rc_non_tty = cli_main([])
        self.assertEqual(rc_non_tty, 2)

    def test_cli_auto_once_json(self) -> None:
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        res = run_cli(toy.root, "auto", "--once")
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["stage"], "adopted")
        self.assertIn("palette", data)


if __name__ == "__main__":
    unittest.main()
