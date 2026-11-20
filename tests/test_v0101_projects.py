"""v0.10.1 tests: Projects Registry (list, register, edit, non-destructive remove) + Textual Project Switcher."""

from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from openfarm_core.bootstrap import initialize_project
from openfarm_core.cli import main as cli_main
from openfarm_core.config import load_config
from openfarm_tui import textual_available


class ProjectsRegistryV0101Tests(unittest.TestCase):
    def test_register_list_edit_and_nondestructive_remove(self) -> None:
        from openfarm_core.projects import (
            edit_project,
            list_projects,
            register_project,
            remove_project,
        )

        with tempfile.TemporaryDirectory() as tmp:
            reg_path = Path(tmp) / "cfg" / "projects.toml"
            state_root = Path(tmp) / "state-root"
            repo_a = Path(tmp) / "alpha-app"
            repo_b = Path(tmp) / "beta-web"
            prev = os.environ.get("OPENFARM_PROJECTS_FILE")
            os.environ["OPENFARM_PROJECTS_FILE"] = str(reg_path)
            try:
                initialize_project(
                    "alpha-app",
                    directory=repo_a,
                    language="python",
                    idea="Alpha tracker",
                    state_dir=state_root / "alpha-app",
                    secrets_dir=Path(tmp) / "secrets",
                    no_git=True,
                )
                initialize_project(
                    "beta-web",
                    directory=repo_b,
                    language="typescript",
                    idea="Beta gallery",
                    state_dir=state_root / "beta-web",
                    secrets_dir=Path(tmp) / "secrets",
                    no_git=True,
                )

                # Both projects were auto-registered by initialize_project
                items = list_projects(path=reg_path)
                ids = {p["id"] for p in items}
                self.assertEqual(ids, {"alpha-app", "beta-web"})

                # Edit alpha-app metadata
                edited = edit_project(
                    "alpha-app",
                    name="Alpha Renamed",
                    idea="Updated alpha idea",
                    base_branch="trunk",
                    path=reg_path,
                )
                self.assertEqual(edited["name"], "Alpha Renamed")
                self.assertEqual(edited["idea"], "Updated alpha idea")
                self.assertEqual(edited["base_branch"], "trunk")
                # Also updated [project] base_branch in repo's .openfarm/config.toml
                cfg_a = load_config(repo_a)
                self.assertEqual(cfg_a.base_branch, "trunk")

                # Add a user file in alpha-app and verify remove_project NEVER deletes user files on disk!
                user_file = repo_a / "IMPORTANT_USER_FILE.txt"
                user_file.write_text("do not delete me\n", encoding="utf-8")
                state_dir_a = state_root / "alpha-app"
                self.assertTrue(state_dir_a.is_dir())

                removed = remove_project(
                    "alpha-app",
                    clear_state_dir=True,
                    allowed_state_root=state_root,
                    path=reg_path,
                )
                self.assertTrue(removed["removed"])
                self.assertTrue(removed["state_cleared"])
                # Registry no longer lists alpha-app
                remaining = {p["id"] for p in list_projects(path=reg_path)}
                self.assertEqual(remaining, {"beta-web"})
                # User's project directory and files are 100% untouched!
                self.assertTrue(repo_a.is_dir())
                self.assertTrue(user_file.is_file())
                self.assertEqual(user_file.read_text(encoding="utf-8"), "do not delete me\n")
                self.assertTrue((repo_a / "src").is_dir())
                # External state cache was cleared
                self.assertFalse(state_dir_a.exists())

                # Manual register_project still works for existing directories
                re_added = register_project(repo_a, name="Alpha Re-added", path=reg_path)
                self.assertEqual(re_added["name"], "Alpha Re-added")

                # Safety guard: clear_state_dir refuses to delete any state_dir outside allowed_state_root or overlapping repo
                rogue = register_project(repo_a, state_dir=repo_a, name="Rogue State", path=reg_path)
                res_rogue = remove_project(
                    rogue["id"],
                    clear_state_dir=True,
                    allowed_state_root=state_root,
                    path=reg_path,
                )
                self.assertTrue(res_rogue["removed"])
                self.assertFalse(res_rogue["state_cleared"])
                self.assertTrue(user_file.is_file())
            finally:
                if prev is None:
                    os.environ.pop("OPENFARM_PROJECTS_FILE", None)
                else:
                    os.environ["OPENFARM_PROJECTS_FILE"] = prev

    def test_auto_register_skips_ephemeral_tmp_when_default_registry_used(self) -> None:
        from openfarm_core.projects import default_projects_path

        with tempfile.TemporaryDirectory() as tmp:
            repo_tmp = Path(tmp) / "ephemeral-test-app"
            prev = os.environ.pop("OPENFARM_PROJECTS_FILE", None)
            default_reg = default_projects_path()
            existed_before = default_reg.exists()
            before_text = default_reg.read_text(encoding="utf-8") if existed_before else None
            try:
                initialize_project(
                    "ephemeral-test-app",
                    directory=repo_tmp,
                    language="python",
                    state_dir=Path(tmp) / "st",
                    secrets_dir=Path(tmp) / "sec",
                    no_git=True,
                )
                if not existed_before:
                    self.assertFalse(default_reg.exists())
                else:
                    self.assertEqual(default_reg.read_text(encoding="utf-8"), before_text)
            finally:
                if prev is not None:
                    os.environ["OPENFARM_PROJECTS_FILE"] = prev

    def test_cli_projects_list_add_edit_remove_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg_path = Path(tmp) / "projects.toml"
            repo = Path(tmp) / "cli-proj"
            repo.mkdir()
            (repo / "keep.txt").write_text("safe\n", encoding="utf-8")
            prev = os.environ.get("OPENFARM_PROJECTS_FILE")
            os.environ["OPENFARM_PROJECTS_FILE"] = str(reg_path)
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    self.assertEqual(cli_main(["--json", "projects", "add", str(repo), "--name", "CLI Proj"]), 0)
                added = json.loads(buf.getvalue())
                self.assertEqual(added["name"], "CLI Proj")

                buf2 = io.StringIO()
                with redirect_stdout(buf2):
                    self.assertEqual(cli_main(["--json", "projects", "edit", added["id"], "--name", "CLI Proj v2"]), 0)
                self.assertEqual(json.loads(buf2.getvalue())["name"], "CLI Proj v2")

                buf3 = io.StringIO()
                with redirect_stdout(buf3):
                    self.assertEqual(cli_main(["--json", "projects", "remove", added["id"]]), 0)
                self.assertTrue(json.loads(buf3.getvalue())["removed"])
                self.assertTrue((repo / "keep.txt").is_file())
            finally:
                if prev is None:
                    os.environ.pop("OPENFARM_PROJECTS_FILE", None)
                else:
                    os.environ["OPENFARM_PROJECTS_FILE"] = prev

    @unittest.skipUnless(textual_available(), "textual optional extra not installed")
    def test_textual_tui_switch_edit_and_delete_projects_nondestructively(self) -> None:
        from openfarm_core.projects import list_projects
        from openfarm_tui.app import OpenfarmApp

        with tempfile.TemporaryDirectory() as tmp:
            reg_path = Path(tmp) / "projects.toml"
            repo_a = Path(tmp) / "proj-one"
            repo_b = Path(tmp) / "proj-two"
            prev = os.environ.get("OPENFARM_PROJECTS_FILE")
            os.environ["OPENFARM_PROJECTS_FILE"] = str(reg_path)
            try:
                initialize_project(
                    "proj-one",
                    directory=repo_a,
                    language="python",
                    state_dir=Path(tmp) / "st1",
                    secrets_dir=Path(tmp) / "sec",
                    no_git=True,
                )
                initialize_project(
                    "proj-two",
                    directory=repo_b,
                    language="typescript",
                    state_dir=Path(tmp) / "st2",
                    secrets_dir=Path(tmp) / "sec",
                    no_git=True,
                )
                (repo_a / "precious.txt").write_text("keep\n", encoding="utf-8")

                async def _run() -> None:
                    app = OpenfarmApp(repo=repo_a, initial_view="projects")
                    async with app.run_test(size=(132, 42)) as pilot:
                        await pilot.pause(0.15)
                        # Switch active project from proj-one -> proj-two inside TUI
                        app.switch_active_project("proj-two")
                        await pilot.pause(0.1)
                        self.assertEqual(app.repo, repo_b.resolve())
                        self.assertIn("proj-two", str(app.query_one("#repo-title").render()))

                        # Edit proj-two name from TUI
                        app.apply_project_edit("proj-two", name="Project Two Renamed", idea="New idea")
                        await pilot.pause(0.1)

                        # Open New / Edit / Delete modals via keyboard and dismiss cleanly
                        await pilot.press("n")
                        await pilot.pause(0.05)
                        await pilot.press("escape")
                        await pilot.pause(0.05)
                        await pilot.press("e")
                        await pilot.pause(0.05)
                        await pilot.press("escape")
                        await pilot.pause(0.05)
                        await pilot.press("d")
                        await pilot.pause(0.05)
                        await pilot.press("escape")
                        await pilot.pause(0.05)

                        # Delete proj-one from Openfarm (non-destructive on disk!)
                        app.apply_project_remove("proj-one", clear_state_dir=False)
                        await pilot.pause(0.1)

                asyncio.run(_run())

                remaining = list_projects(path=reg_path)
                self.assertEqual([p["id"] for p in remaining], ["proj-two"])
                self.assertEqual(remaining[0]["name"], "Project Two Renamed")
                # proj-one files on disk are completely untouched
                self.assertTrue((repo_a / "precious.txt").is_file())
            finally:
                if prev is None:
                    os.environ.pop("OPENFARM_PROJECTS_FILE", None)
                else:
                    os.environ["OPENFARM_PROJECTS_FILE"] = prev


if __name__ == "__main__":
    unittest.main()
