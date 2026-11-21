"""v0.10.2 tests: Workspace Discovery, Card Editor & Split, Findings v1.5 (dedup + bulk),
Touches Conflict Matrix, and `openfarm doctor [--fix]`."""

from __future__ import annotations

import asyncio
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from openfarm_core.adapters.files import FileCardSource
from openfarm_core.automode import draft_wave_plan, ensure_baseline_committed
from openfarm_core.bootstrap import initialize_project
from openfarm_core.cli import main as cli_main
from openfarm_core.config import load_config
from openfarm_core.git import Git
from openfarm_tui import textual_available


class ErgonomicsPreflightV0102Tests(unittest.TestCase):
    def test_workspace_discovery_classifies_configured_git_and_plain_folders(self) -> None:
        from openfarm_core.projects import discover_workspace_projects

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            # 1. Configured Openfarm repo
            p_cfg = root / "alpha-farm"
            initialize_project(
                "alpha-farm",
                directory=p_cfg,
                language="python",
                state_dir=Path(tmp) / "st-alpha",
                secrets_dir=Path(tmp) / "sec",
                no_git=True,
            )
            # 2. Plain git repo with package.json (TypeScript)
            p_git = root / "beta-web"
            p_git.mkdir()
            (p_git / "package.json").write_text('{"name":"beta-web"}\n', encoding="utf-8")
            Git(p_git).init(branch="main")
            # 3. Plain folder
            p_plain = root / "gamma-notes"
            p_plain.mkdir()
            (p_plain / "README.md").write_text("# Notes\n", encoding="utf-8")
            # 4. Hidden dir (must be ignored)
            (root / ".hidden").mkdir()

            found = discover_workspace_projects(root, registry_path=Path(tmp) / "projects.toml")
            by_name = {item["name"]: item for item in found}
            self.assertEqual(set(by_name), {"alpha-farm", "beta-web", "gamma-notes"})
            self.assertEqual(by_name["alpha-farm"]["kind"], "configured")
            self.assertEqual(by_name["beta-web"]["kind"], "git-repo")
            self.assertEqual(by_name["beta-web"]["detected_language"], "typescript")
            self.assertEqual(by_name["gamma-notes"]["kind"], "folder")

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli_main(["--json", "projects", "discover", "--dir", str(root)])
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["count"], 3)

    def test_card_editor_eligibility_and_split_card_into_two(self) -> None:
        from openfarm_core.automode import split_card_into_two, update_card_with_eligibility

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "edit-ts"
            initialize_project(
                "edit-ts",
                directory=repo,
                language="typescript",
                state_dir=Path(tmp) / "st",
                secrets_dir=Path(tmp) / "sec",
                commit=True,
            )
            (repo / "src" / "a.ts").write_text("export const A = 1;\n", encoding="utf-8")
            (repo / "src" / "b.ts").write_text("export const B = 2;\n", encoding="utf-8")
            Git(repo).commit_all("add a and b")

            draft_wave_plan(
                repo,
                answers=[{"id": "goal", "question": "Goal", "answer": "Build A and B"}],
                verify_baseline=False,
            )

            # Edit W1-01 with valid changes -> eligible=True and synced to openfarm-cards
            upd = update_card_with_eligibility(
                repo,
                "W1-01",
                title="Implement A helper",
                touches=["src/a.ts"],
                done_text="## Done\n\n- Export `helperA` in `src/a.ts` returning `1` or throwing an error.\n",
            )
            self.assertTrue(upd["eligible"])
            self.assertEqual(upd["card"]["title"], "Implement A helper")

            # Split W1-01 into W1-01a and W1-01b touching disjoint files
            split = split_card_into_two(
                repo,
                "W1-01",
                first_title="Part A",
                first_touches=["src/a.ts"],
                second_title="Part B",
                second_touches=["src/b.ts"],
            )
            self.assertEqual(len(split["cards"]), 2)
            self.assertTrue(all(c["eligible"] for c in split["cards"]))
            self.assertEqual([c["id"] for c in split["cards"]], ["W1-01a", "W1-01b"])
            self.assertEqual(split["cards"][1]["depends"], ["W1-01a"])

    def test_findings_v15_deduplicate_and_bulk_dismiss(self) -> None:
        from openfarm_core.findings import bulk_decide_findings, list_findings

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "find-repo"
            initialize_project(
                "find-repo",
                directory=repo,
                language="python",
                state_dir=Path(tmp) / "st",
                secrets_dir=Path(tmp) / "sec",
                commit=True,
            )
            cfg = load_config(repo)
            src = FileCardSource(repo, "cards", worktree=cfg.state_dir / "cards-source", base_branch=cfg.base_branch)
            src.prepare()
            src.finding("W1-01", "Missing input validation", "Empty string causes crash")
            src.finding("W1-01", "missing input validation", "Duplicate occurrence from retry")
            src.finding("W1-02", "Slow loop in parser", "Quadratic behavior")

            dedup = list_findings(repo, cfg, state_filter="waiting", deduplicate=True)
            self.assertEqual(len(dedup["findings"]), 2)
            dup_item = next(f for f in dedup["findings"] if f["from_card"] == "W1-01")
            self.assertEqual(dup_item["duplicate_count"], 2)

            bulk = bulk_decide_findings(repo, cfg, decision="dismissed", from_card="W1-01", reason="handled in W2")
            self.assertEqual(bulk["count"], 2)
            after = list_findings(repo, cfg, state_filter="waiting")
            self.assertEqual([f["from_card"] for f in after["findings"]], ["W1-02"])

    def test_touches_conflict_matrix_and_openfarm_doctor_fix(self) -> None:
        from openfarm_core.cards import Card
        from openfarm_core.doctor import compute_touches_conflict_matrix, run_doctor

        c1 = Card("W1-01", "A", touches=["src/a.ts", "src/shared.ts"])
        c2 = Card("W1-02", "B", touches=["src/b.ts"])
        c3 = Card("W1-03", "C", touches=["src/shared.ts"])
        matrix = compute_touches_conflict_matrix([c1, c2, c3])
        self.assertEqual(len(matrix["conflicts"]), 1)
        self.assertEqual({matrix["conflicts"][0]["a"], matrix["conflicts"][0]["b"]}, {"W1-01", "W1-03"})
        self.assertGreaterEqual(len(matrix["parallel_lanes"]), 2)

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "doc-repo"
            st = Path(tmp) / "st-doc"
            sec = Path(tmp) / "sec"
            initialize_project(
                "doc-repo",
                directory=repo,
                language="python",
                state_dir=st,
                secrets_dir=sec,
                commit=True,
            )
            cfg = load_config(repo)
            # Create a stale run.lock (dead PID on this host) and world-readable key file
            import platform
            (st / "run.lock").write_text(
                json.dumps({"run_id": "stale", "pid": 999999, "host": platform.node()}),
                encoding="utf-8",
            )
            kf = cfg.key_file
            assert kf is not None
            kf.write_text("sk-test-secret\n", encoding="utf-8")
            os.chmod(kf, 0o644)
            (repo / "keep_user.py").write_text("X = 1\n", encoding="utf-8")

            report = run_doctor(repo, fix=False)
            checks_by_id = {c["id"]: c for c in report["checks"]}
            self.assertEqual(checks_by_id["run_lock"]["status"], "warn")
            self.assertEqual(checks_by_id["key_permissions"]["status"], "warn")

            fixed = run_doctor(repo, fix=True)
            self.assertTrue(len(fixed["fixes_applied"]) >= 2)
            self.assertFalse((st / "run.lock").exists())
            self.assertEqual(stat.S_IMODE(kf.stat().st_mode), 0o600)
            self.assertTrue((repo / "keep_user.py").is_file())

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli_main(["--repo", str(repo), "--json", "doctor"])
            self.assertEqual(rc, 0)
            self.assertIn("checks", json.loads(buf.getvalue()))

    @unittest.skipUnless(textual_available(), "textual optional extra not installed")
    def test_textual_tui_v0102_discovery_card_editor_and_doctor(self) -> None:
        from openfarm_tui.app import OpenfarmApp

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "tui-v0102"
            prev_reg = os.environ.get("OPENFARM_PROJECTS_FILE")
            os.environ["OPENFARM_PROJECTS_FILE"] = str(Path(tmp) / "projects.toml")
            try:
                initialize_project(
                    "tui-v0102",
                    directory=repo,
                    language="typescript",
                    state_dir=Path(tmp) / "st",
                    secrets_dir=Path(tmp) / "sec",
                    commit=True,
                )
                draft_wave_plan(
                    repo,
                    answers=[{"id": "goal", "question": "Goal", "answer": "Photo gallery"}],
                    verify_baseline=False,
                )

                async def _run() -> None:
                    app = OpenfarmApp(repo=repo, initial_view="studio")
                    async with app.run_test(size=(132, 42)) as pilot:
                        await pilot.pause(0.15)
                        # Conflict matrix renders inside Plan preflight
                        preflight = str(app.query_one("#plan-preflight").render())
                        self.assertIn("parallel", preflight.lower())
                        # Switch to Engine -> Doctor tab exists
                        app.action_switch_view("engine")
                        await pilot.pause(0.1)
                        self.assertIsNotNone(app.query_one("#engine-doctor"))

                asyncio.run(_run())
            finally:
                if prev_reg is None:
                    os.environ.pop("OPENFARM_PROJECTS_FILE", None)
                else:
                    os.environ["OPENFARM_PROJECTS_FILE"] = prev_reg


if __name__ == "__main__":
    unittest.main()
