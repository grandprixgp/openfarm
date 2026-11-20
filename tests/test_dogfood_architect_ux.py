"""Dogfood regression tests: TypeScript .js->.ts relative import repair, baseline verification
in draft_wave_plan, and visible live progress/locking in Studio Grill."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from openfarm_core.automode import draft_wave_plan
from openfarm_core.bootstrap import initialize_project
from openfarm_tui import textual_available


class DogfoodArchitectAndUxTests(unittest.TestCase):
    def test_draft_wave_plan_fixes_typescript_dot_js_imports_and_verifies_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "ts-photo"
            prev_reg = os.environ.get("OPENFARM_PROJECTS_FILE")
            os.environ["OPENFARM_PROJECTS_FILE"] = str(Path(tmp) / "projects.toml")
            try:
                initialize_project(
                    "ts-photo",
                    directory=repo,
                    language="typescript",
                    state_dir=Path(tmp) / "st",
                    secrets_dir=Path(tmp) / "sec",
                    commit=True,
                )

                def fake_arch_with_dot_js(_prompt: str) -> str:
                    return json.dumps({
                        "files": {
                            ".openfarm/PROJECT_SPEC.md": "# ts-photo\n\nDaily photo app.\n",
                            "src/photo.ts": "export function formatTitle(t: string): string { return t.trim(); }\n",
                            # Classic NodeNext mistake from Frontier models: importing .js when only .ts exists
                            "tests/photo.test.ts": (
                                'import test from "node:test";\n'
                                'import assert from "node:assert/strict";\n'
                                'import { formatTitle } from "../src/photo.js";\n\n'
                                'test("formats title", () => {\n'
                                '  assert.equal(formatTitle("  Dawn  "), "Dawn");\n'
                                "});\n"
                            ),
                            "cards/W1-01.md": (
                                "+++\n"
                                'id = "W1-01"\n'
                                'title = "Add subtitle helper"\n'
                                'state = "ready"\n'
                                'category = "small-feature"\n'
                                'touches = ["src/photo.ts"]\n'
                                'reference = "src/photo.ts"\n'
                                "new_symbols = true\n"
                                "depends = []\n"
                                'tests = [{ name = "formats_subtitle", file = "tests/subtitle.test.ts", asserts = "Returns subtitle" }]\n'
                                "+++\n\n"
                                "## Done\n\n"
                                "- Export `formatSubtitle` in `src/photo.ts` returning trimmed string or throwing an error.\n"
                            ),
                        }
                    })

                plan = draft_wave_plan(
                    repo,
                    answers=[{"id": "goal", "question": "Goal", "answer": "Daily photo app"}],
                    backend=fake_arch_with_dot_js,
                    verify_baseline=True,
                )
                test_text = (repo / "tests" / "photo.test.ts").read_text(encoding="utf-8")
                self.assertIn("../src/photo.ts", test_text)
                self.assertNotIn("../src/photo.js", test_text)
                self.assertTrue(plan["baseline_ok"])
                self.assertEqual(plan["baseline_error"], "")
            finally:
                if prev_reg is None:
                    os.environ.pop("OPENFARM_PROJECTS_FILE", None)
                else:
                    os.environ["OPENFARM_PROJECTS_FILE"] = prev_reg

    @unittest.skipUnless(textual_available(), "textual optional extra not installed")
    def test_tui_grill_shows_live_status_and_locks_button_during_architect_run(self) -> None:
        from openfarm_tui.app import OpenfarmApp

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "ui-progress"
            prev_reg = os.environ.get("OPENFARM_PROJECTS_FILE")
            os.environ["OPENFARM_PROJECTS_FILE"] = str(Path(tmp) / "projects.toml")
            try:
                initialize_project(
                    "ui-progress",
                    directory=repo,
                    language="typescript",
                    state_dir=Path(tmp) / "st",
                    secrets_dir=Path(tmp) / "sec",
                    commit=True,
                )

                async def _run() -> None:
                    app = OpenfarmApp(repo=repo, initial_view="studio")
                    async with app.run_test(size=(110, 32)) as pilot:
                        await pilot.pause(0.15)
                        # Button must be visible without scrolling off a 32-row screen
                        btn = app.query_one("#btn-draft-plan")
                        self.assertTrue(btn.visible)
                        # Status banner exists and toggles visibly when drafting state is entered
                        app._set_drafting_state(True, "Designing golden slice with claude ACP...")
                        await pilot.pause(0.05)
                        self.assertTrue(btn.disabled)
                        status_w = app.query_one("#grill-status")
                        self.assertFalse(status_w.has_class("hidden"))
                        self.assertIn("Designing golden slice", str(status_w.render()))
                        self.assertIn("ARCHITECTING", str(app.query_one("#status-badge").render()))
                        app._set_drafting_state(False, "")
                        await pilot.pause(0.05)
                        self.assertFalse(btn.disabled)

                asyncio.run(_run())
            finally:
                if prev_reg is None:
                    os.environ.pop("OPENFARM_PROJECTS_FILE", None)
                else:
                    os.environ["OPENFARM_PROJECTS_FILE"] = prev_reg


if __name__ == "__main__":
    unittest.main()
