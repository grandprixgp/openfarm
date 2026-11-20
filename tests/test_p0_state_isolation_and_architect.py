"""P0 regression tests: state_dir repo isolation, foreign cards-source rebuild,
re-grill card sync, live Architect prompt/stack integrity, and visible TUI run refusals."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from openfarm_core.adapters.files import FileCardSource, parse_card_file
from openfarm_core.automode import draft_wave_plan, ensure_baseline_committed
from openfarm_core.bootstrap import initialize_project
from openfarm_core.config import load_config
from openfarm_core.state import StateDir
from openfarm_tui import textual_available


class P0StateIsolationAndArchitectTests(unittest.TestCase):
    def test_state_dir_collision_disambiguates_different_repos_with_same_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            repo_rust = Path(tmp) / "old-photo"
            repo_ts = Path(tmp) / "new-photo"
            prev_home = os.environ.get("HOME")
            prev_reg = os.environ.get("OPENFARM_PROJECTS_FILE")
            os.environ["HOME"] = str(fake_home)
            os.environ["OPENFARM_PROJECTS_FILE"] = str(Path(tmp) / "projects.toml")
            try:
                r1 = initialize_project(
                    "photo-of-the-day",
                    directory=repo_rust,
                    language="rust",
                    commit=True,
                )
                r2 = initialize_project(
                    "photo-of-the-day",
                    directory=repo_ts,
                    language="typescript",
                    commit=True,
                )
                # Both projects have the same display name, so they MUST get distinct state dirs!
                self.assertNotEqual(Path(r1["state_dir"]).resolve(), Path(r2["state_dir"]).resolve())
                self.assertTrue((Path(r1["state_dir"]) / "repo.path").is_file())
                self.assertTrue((Path(r2["state_dir"]) / "repo.path").is_file())
                # TypeScript project has zero .rs files and non-Rust next_steps
                rs_files = list(repo_ts.rglob("*.rs"))
                self.assertEqual(rs_files, [])
                self.assertFalse(any("invariants.rs" in step for step in r2["next_steps"]))
            finally:
                if prev_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = prev_home
                if prev_reg is None:
                    os.environ.pop("OPENFARM_PROJECTS_FILE", None)
                else:
                    os.environ["OPENFARM_PROJECTS_FILE"] = prev_reg

    def test_file_card_source_rebuilds_foreign_worktree_and_syncs_regrilled_ready_cards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shared_state = Path(tmp) / "shared-state"
            repo_old = Path(tmp) / "old-rust-repo"
            repo_new = Path(tmp) / "new-ts-repo"
            prev_reg = os.environ.get("OPENFARM_PROJECTS_FILE")
            os.environ["OPENFARM_PROJECTS_FILE"] = str(Path(tmp) / "projects.toml")
            try:
                initialize_project(
                    "app",
                    directory=repo_old,
                    language="rust",
                    state_dir=shared_state,
                    secrets_dir=Path(tmp) / "sec",
                    commit=True,
                )
                # Populate old Rust repo's cards-source inside shared_state
                draft_wave_plan(
                    repo_old,
                    answers=[{"id": "goal", "question": "Goal", "answer": "Old rust app"}],
                )
                ensure_baseline_committed(repo_old)
                old_src = FileCardSource(repo_old, "cards", worktree=shared_state / "cards-source")
                old_src.prepare()
                self.assertTrue((shared_state / "cards-source" / "Cargo.toml").is_file())

                # Now point a NEW TypeScript repo at the same state_dir (simulating legacy collision)
                initialize_project(
                    "app",
                    directory=repo_new,
                    language="typescript",
                    state_dir=shared_state,
                    secrets_dir=Path(tmp) / "sec",
                    commit=True,
                )
                plan = draft_wave_plan(
                    repo_new,
                    answers=[
                        {"id": "goal", "question": "Goal", "answer": "Daily photo gallery web app"},
                        {"id": "invariants", "question": "Invariants", "answer": "Reject empty photo captions"},
                        {"id": "slice", "question": "Slice", "answer": "Render today photo card"},
                    ],
                )
                self.assertTrue(plan["eligibility_ok"])
                ensure_baseline_committed(repo_new)

                # FileCardSource.prepare() MUST detect foreign cards-source and rebuild it for repo_new!
                new_src = FileCardSource(repo_new, "cards", worktree=shared_state / "cards-source")
                new_src.prepare()
                self.assertFalse((shared_state / "cards-source" / "Cargo.toml").exists())
                self.assertTrue((shared_state / "cards-source" / "package.json").is_file())
                card = new_src.read("W1-01")["card"]
                self.assertEqual(card["touches"], ["src/index.ts"])
                self.assertFalse(any(t.endswith(".rs") for t in card["touches"]))

                # Re-grilling updates ready card W1-01.md on main AND syncs into openfarm-cards!
                plan2 = draft_wave_plan(
                    repo_new,
                    answers=[
                        {"id": "goal", "question": "Goal", "answer": "Updated photo gallery with zoom"},
                        {"id": "invariants", "question": "Invariants", "answer": "Reject empty photo captions"},
                        {"id": "slice", "question": "Slice", "answer": "Zoomable photo card"},
                    ],
                )
                self.assertTrue(plan2["eligibility_ok"])
                card2 = new_src.read("W1-01")["card"]
                self.assertIn("Zoomable photo card", card2["title"])
            finally:
                if prev_reg is None:
                    os.environ.pop("OPENFARM_PROJECTS_FILE", None)
                else:
                    os.environ["OPENFARM_PROJECTS_FILE"] = prev_reg

    def test_architect_backend_scaffolds_typescript_golden_slice_and_multi_cards_never_rs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "photo-ts"
            prev_reg = os.environ.get("OPENFARM_PROJECTS_FILE")
            os.environ["OPENFARM_PROJECTS_FILE"] = str(Path(tmp) / "projects.toml")
            try:
                initialize_project(
                    "photo-ts",
                    directory=repo,
                    language="typescript",
                    state_dir=Path(tmp) / "st",
                    secrets_dir=Path(tmp) / "sec",
                    commit=True,
                )

                seen_prompts: list[str] = []

                def fake_architect(prompt: str) -> str:
                    seen_prompts.append(prompt)
                    return json.dumps({
                        "files": {
                            ".openfarm/PROJECT_SPEC.md": "# photo-ts\n\nDaily photo viewer in TypeScript.\n",
                            "src/photo.ts": "export interface Photo { id: string; title: string; }\nexport function formatPhoto(p: Photo): string { return `${p.id}: ${p.title}`; }\n",
                            "cards/W1-01.md": (
                                "+++\n"
                                'id = "W1-01"\n'
                                'title = "Load photo of the day"\n'
                                'state = "ready"\n'
                                'category = "small-feature"\n'
                                'touches = ["src/photo.ts"]\n'
                                'reference = "src/photo.ts"\n'
                                "new_symbols = true\n"
                                "depends = []\n"
                                'tests = [{ name = "loads_daily_photo", file = "tests/photo.test.ts", asserts = "Returns daily photo or error" }]\n'
                                "+++\n\n"
                                "## Done\n\n"
                                "- `formatPhoto` returns formatted title and returns an error on empty title.\n"
                            ),
                            "cards/W1-02.md": (
                                "+++\n"
                                'id = "W1-02"\n'
                                'title = "Expand photo lore details"\n'
                                'state = "ready"\n'
                                'category = "small-feature"\n'
                                'touches = ["src/index.ts"]\n'
                                'reference = "src/index.ts"\n'
                                "new_symbols = true\n"
                                'depends = ["W1-01"]\n'
                                'tests = [{ name = "expands_photo_details", file = "tests/details.test.ts", asserts = "Expands lore metadata" }]\n'
                                "+++\n\n"
                                "## Done\n\n"
                                "- Export `expandLore` from `src/index.ts` returning detail blocks.\n"
                            ),
                            # Even if a buggy model tried to emit a .rs file in a TS repo, it must be ignored!
                            "src/stray.rs": "pub fn nope() {}\n",
                        }
                    })

                plan = draft_wave_plan(
                    repo,
                    answers=[{"id": "goal", "question": "Goal", "answer": "Photo of the day"}],
                    backend=fake_architect,
                )
                self.assertEqual(len(seen_prompts), 1)
                self.assertIn("typescript", seen_prompts[0].lower())
                self.assertFalse((repo / "src" / "stray.rs").exists())
                self.assertTrue((repo / "src" / "photo.ts").is_file())
                self.assertEqual({c["id"] for c in plan["cards"]}, {"W1-01", "W1-02"})
                self.assertTrue(plan["eligibility_ok"])
            finally:
                if prev_reg is None:
                    os.environ.pop("OPENFARM_PROJECTS_FILE", None)
                else:
                    os.environ["OPENFARM_PROJECTS_FILE"] = prev_reg

    @unittest.skipUnless(textual_available(), "textual optional extra not installed")
    def test_tui_wave_refusal_is_visible_in_activity_log_not_silently_dropped(self) -> None:
        from openfarm_tui.app import OpenfarmApp

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "paused-ts"
            state_dir = Path(tmp) / "st-paused"
            prev_reg = os.environ.get("OPENFARM_PROJECTS_FILE")
            os.environ["OPENFARM_PROJECTS_FILE"] = str(Path(tmp) / "projects.toml")
            try:
                initialize_project(
                    "paused-ts",
                    directory=repo,
                    language="typescript",
                    state_dir=state_dir,
                    secrets_dir=Path(tmp) / "sec",
                    commit=True,
                )
                draft_wave_plan(
                    repo,
                    answers=[{"id": "goal", "question": "Goal", "answer": "Daily photo app"}],
                )
                ensure_baseline_committed(repo)
                StateDir(state_dir).pause("stale infra error")

                async def _run() -> None:
                    app = OpenfarmApp(repo=repo, initial_view="studio")
                    async with app.run_test(size=(132, 42)) as pilot:
                        await pilot.pause(0.15)
                        app.start_nonblocking_wave()
                        await pilot.pause(0.5)
                        # Must stay on studio-run and display the pause refusal loudly!
                        self.assertEqual(app.query_one("#studio-switcher").current, "studio-run")
                        log_lines = "\n".join(str(line.text) for line in app.query_one("#activity-log").lines)
                        self.assertIn("paused", log_lines.lower())

                asyncio.run(_run())
            finally:
                if prev_reg is None:
                    os.environ.pop("OPENFARM_PROJECTS_FILE", None)
                else:
                    os.environ["OPENFARM_PROJECTS_FILE"] = prev_reg


if __name__ == "__main__":
    unittest.main()
