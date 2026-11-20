"""v0.13.0 tests: Conversational Frontier Architect Grill, nested-dir baseline commit fix,
stack-authentic prompts, Adaptive Model Router, Attempt Timeline & Phase-B Resume."""

from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from openfarm_core.automode import draft_wave_plan, ensure_baseline_committed
from openfarm_core.bootstrap import initialize_project
from openfarm_core.cli import main as cli_main
from openfarm_core.git import Git
from openfarm_tui import textual_available


class ConversationalArchitectRoutingTimelineV013Tests(unittest.TestCase):
    def test_ensure_baseline_committed_handles_untracked_nested_subdirectories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "nested-ts"
            initialize_project(
                "nested-ts",
                directory=repo,
                language="typescript",
                state_dir=Path(tmp) / "st",
                secrets_dir=Path(tmp) / "sec",
                commit=True,
            )
            nested = repo / "src" / "domain" / "photo.ts"
            nested.parent.mkdir(parents=True, exist_ok=True)
            nested.write_text("export const X = 1;\n", encoding="utf-8")
            # git status --porcelain reports untracked dir as "?? src/domain/"
            committed = ensure_baseline_committed(repo, extra_allowed=["src/domain/photo.ts"])
            self.assertTrue(committed)
            self.assertTrue(Git(repo).is_clean())

    def test_conversational_architect_grill_and_stack_authentic_typescript_prompt(self) -> None:
        from openfarm_core.automode import architect_grill_turn

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "photo-web"
            initialize_project(
                "photo-web",
                directory=repo,
                language="typescript",
                state_dir=Path(tmp) / "st",
                secrets_dir=Path(tmp) / "sec",
                commit=True,
            )

            # 1. Conversational grill asks 1 sharp contextual question
            def fake_grill_backend(prompt: str) -> str:
                self.assertIn("typescript", prompt.lower())
                self.assertNotIn("no silent data loss", prompt.lower())
                return json.dumps({
                    "ready_to_plan": False,
                    "question": "Where should the daily photo come from?",
                    "options": ["Local curated JSON feed + real HTTP server", "Live NASA APOD API"],
                    "recommended": "Local curated JSON feed + real HTTP server",
                })

            turn = architect_grill_turn(
                repo,
                idea="Stunning realtime photo of the day web app with recursive lore expansion",
                history=[],
                language="typescript",
                backend=fake_grill_backend,
            )
            self.assertFalse(turn["ready_to_plan"])
            self.assertIn("photo", turn["question"].lower())
            self.assertEqual(len(turn["options"]), 2)

            # 2. Stack-authentic Architect prompt for TypeScript NEVER forces Rust Result/no-network boilerplate
            seen_prompts: list[str] = []

            def fake_arch_backend(prompt: str) -> str:
                seen_prompts.append(prompt)
                return json.dumps({
                    "files": {
                        ".openfarm/PROJECT_SPEC.md": "# Photo Web App\n\nRunnable local TypeScript web server + HTML/CSS UI.\n",
                        "src/server.ts": "export function renderHomeHtml(title: string): string { return `<h1>${title}</h1>`; }\n",
                        "tests/server.test.ts": (
                            'import test from "node:test";\n'
                            'import assert from "node:assert/strict";\n'
                            'import { renderHomeHtml } from "../src/server.ts";\n'
                            'test("renders home html", () => { assert.equal(renderHomeHtml("Dawn"), "<h1>Dawn</h1>"); });\n'
                        ),
                        "cards/W1-01.md": (
                            "+++\n"
                            'id = "W1-01"\n'
                            'title = "Build adaptive palette CSS generator"\n'
                            'state = "ready"\n'
                            'category = "small-feature"\n'
                            'touches = ["src/palette.ts", "tests/palette.test.ts"]\n'
                            'reference = "src/server.ts"\n'
                            "new_symbols = true\n"
                            "depends = []\n"
                            'tests = [{ name = "generates_css_vars", file = "tests/palette.test.ts", asserts = "Returns CSS custom properties" }]\n'
                            "+++\n\n"
                            "## Done\n\n"
                            "- Export `buildPaletteCss` in `src/palette.ts` returning CSS variables.\n"
                        ),
                    }
                })

            plan = draft_wave_plan(
                repo,
                idea="Stunning realtime photo of the day web app with recursive lore expansion",
                grill_history=[{"question": turn["question"], "answer": "Local curated JSON feed + real HTTP server"}],
                backend=fake_arch_backend,
                verify_baseline=True,
            )
            self.assertTrue(plan["eligibility_ok"])
            self.assertTrue(plan["baseline_ok"])
            self.assertTrue((repo / "src" / "palette.ts").is_file())  # placeholder auto-created for new touch file!
            self.assertTrue(Git(repo).is_clean())  # nested files committed cleanly!
            self.assertEqual(len(seen_prompts), 1)
            self.assertNotIn("no silent data loss", seen_prompts[0].lower())
            self.assertNotIn("core domain model + first end-to-end command", seen_prompts[0].lower())
            self.assertIn("runnable", seen_prompts[0].lower())

    def test_adaptive_model_router_and_attempt_timeline_cli(self) -> None:
        from openfarm_core.routing import compute_routing_recommendations
        from openfarm_core.timeline import build_card_timeline

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "rt-app"
            st = Path(tmp) / "st-rt"
            initialize_project(
                "rt-app",
                directory=repo,
                language="typescript",
                state_dir=st,
                secrets_dir=Path(tmp) / "sec",
                commit=True,
            )
            # Seed synthetic card-record.json history across 2 cards
            c1_dir = st / "cards" / "W1-01" / "run-1"
            c1_dir.mkdir(parents=True)
            (c1_dir / "card-record.json").write_text(json.dumps({
                "card_id": "W1-01",
                "run_id": "run-1",
                "language": "typescript",
                "category": "small-feature",
                "final_state": "in_review",
                "attempts": [
                    {"role": "test_writer", "attempt": 1, "model": "deepseek/v4", "effort": "medium", "tokens_in": 4000, "tokens_out": 800, "ok": True},
                    {"role": "implementer", "attempt": 1, "model": "deepseek/v4", "effort": "medium", "tokens_in": 5000, "tokens_out": 1200, "ok": True},
                ],
                "gates": [
                    {"gate": "fail-before", "result": "pass", "detail": "2 named tests failed"},
                    {"gate": "pass-after", "result": "pass", "detail": "all passed"},
                ],
            }), encoding="utf-8")

            routing = compute_routing_recommendations(repo)
            self.assertEqual(routing["samples"], 1)
            self.assertTrue(routing["recommendations"])
            rec = routing["recommendations"][0]
            self.assertEqual(rec["category"], "small-feature")
            self.assertEqual(rec["recommended_effort"], "medium")

            tl = build_card_timeline(repo, "W1-01")
            self.assertEqual(tl["card_id"], "W1-01")
            self.assertEqual(len(tl["runs"]), 1)
            self.assertEqual(len(tl["runs"][0]["attempts"]), 2)

            buf1 = io.StringIO()
            with redirect_stdout(buf1):
                self.assertEqual(cli_main(["--repo", str(repo), "--json", "routing"]), 0)
            self.assertEqual(json.loads(buf1.getvalue())["samples"], 1)

            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                self.assertEqual(cli_main(["--repo", str(repo), "--json", "timeline", "W1-01"]), 0)
            self.assertEqual(json.loads(buf2.getvalue())["card_id"], "W1-01")

            # Also verify live card-record.json shape where `attempts` is an int and details live in attempts/*.json
            c2_dir = st / "cards" / "W1-02" / "run-2"
            (c2_dir / "attempts").mkdir(parents=True)
            (c2_dir / "card-record.json").write_text(json.dumps({
                "card_id": "W1-02",
                "run_id": "run-2",
                "category": "small-feature",
                "final_state": "in_review",
                "attempts": 1,
                "tooling": {"pack": "typescript"},
            }), encoding="utf-8")
            (c2_dir / "attempts" / "01-implementer.json").write_text(json.dumps({
                "role": "implementer",
                "attempt": 1,
                "model": "deepseek/deepseek-v4.1-flash",
                "tokens_in": 3200,
                "tokens_out": 900,
                "outcome": "pass",
                "gates": [{"name": "pass-after", "result": "pass", "detail": "all green"}],
            }), encoding="utf-8")
            rt2 = compute_routing_recommendations(repo)
            self.assertEqual(rt2["samples"], 2)
            tl2 = build_card_timeline(repo, "W1-02")
            self.assertEqual(len(tl2["runs"][0]["attempts"]), 1)
            self.assertTrue(tl2["runs"][0]["attempts"][0]["ok"])
            self.assertEqual(tl2["runs"][0]["gates"][0]["gate"], "pass-after")

    @unittest.skipUnless(textual_available(), "textual optional extra not installed")
    def test_textual_tui_v013_single_prompt_conversational_grill_and_spec_inspector(self) -> None:
        from openfarm_tui.app import OpenfarmApp

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "tui-v013"
            prev_reg = os.environ.get("OPENFARM_PROJECTS_FILE")
            os.environ["OPENFARM_PROJECTS_FILE"] = str(Path(tmp) / "projects.toml")
            try:
                initialize_project(
                    "tui-v013",
                    directory=repo,
                    language="typescript",
                    state_dir=Path(tmp) / "st",
                    secrets_dir=Path(tmp) / "sec",
                    commit=True,
                )

                async def _run() -> None:
                    app = OpenfarmApp(repo=repo, initial_view="studio")
                    async with app.run_test(size=(120, 34)) as pilot:
                        await pilot.pause(0.15)
                        # Single-prompt Conversational Architect Studio (no rigid Rust invariant/slice boxes!)
                        self.assertIsNotNone(app.query_one("#ta-idea"))
                        self.assertIsNotNone(app.query_one("#btn-grill-ask"))
                        self.assertIsNotNone(app.query_one("#btn-draft-spec"))
                        self.assertIsNotNone(app.query_one("#btn-draft-plan"))
                        self.assertIsNotNone(app.query_one("#chunks-table"))
                        self.assertIsNotNone(app.query_one("#ta-spec-editor"))
                        self.assertIsNotNone(app.query_one("#btn-chunk-to-cards"))
                        self.assertIsNotNone(app.query_one("#architect-panel"))
                        self.assertIsNotNone(app.query_one("#architect-log"))
                        app._set_drafting_state(True, "Splitting C1 into cards...")
                        await pilot.pause(0.05)
                        self.assertFalse(app.query_one("#architect-panel").has_class("hidden"))
                        self.assertTrue(app.query_one("#btn-chunk-to-cards").disabled)
                        app._set_drafting_state(False, "")
                        self.assertFalse(app.query_one("#btn-chunk-to-cards").disabled)
                        self.assertEqual(len(app.query("#ta-invariants")), 0)
                        self.assertEqual(len(app.query("#ta-slice")), 0)

                asyncio.run(_run())
            finally:
                if prev_reg is None:
                    os.environ.pop("OPENFARM_PROJECTS_FILE", None)
                else:
                    os.environ["OPENFARM_PROJECTS_FILE"] = prev_reg

    def test_spec_and_chunks_iterative_pipeline(self) -> None:
        from openfarm_core.automode import draft_spec_and_chunks, load_project_chunks, set_chunk_status

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "iter-app"
            initialize_project(
                "iter-app",
                directory=repo,
                language="typescript",
                state_dir=Path(tmp) / "st",
                secrets_dir=Path(tmp) / "sec",
                commit=True,
            )

            def fake_spec_backend(prompt: str) -> str:
                self.assertIn("chunks", prompt.lower())
                return json.dumps({
                    "spec_markdown": "# Photo of the Day\n\n## Vision\nAdaptive realtime web experience with recursive lore.\n",
                    "chunks": [
                        {"id": "C1", "title": "Core Daily Photo & Adaptive Theme", "goal": "Serve daily photo + adaptive palette"},
                        {"id": "C2", "title": "Recursive Expandable Lore", "goal": "Click any term to expand nested lore cards"},
                    ],
                })

            spec_res = draft_spec_and_chunks(
                repo,
                idea="Image of the day with realtime adaptive theme and recursive lore",
                grill_history=[{"question": "Image source?", "answer": "Curated local JSON + local web server"}],
                language="typescript",
                backend=fake_spec_backend,
            )
            self.assertEqual(len(spec_res["chunks"]), 2)
            self.assertEqual(spec_res["chunks"][0]["status"], "active")
            self.assertIn("Adaptive realtime web experience", (repo / ".openfarm" / "PROJECT_SPEC.md").read_text(encoding="utf-8"))

            # Mark C1 done -> C2 automatically becomes active for next iteration!
            updated = set_chunk_status(repo, "C1", "done")
            self.assertEqual(updated[0]["status"], "done")
            self.assertEqual(updated[1]["status"], "active")
            self.assertEqual(len(load_project_chunks(repo)), 2)

    def test_initialize_project_inherits_global_endpoint_and_zdr_header(self) -> None:
        from openfarm_core.config import load_config
        from openfarm_core.providers import save_global_providers

        with tempfile.TemporaryDirectory() as tmp:
            prov_file = Path(tmp) / "providers.toml"
            key_file = Path(tmp) / "worker.key"
            prev_prov = os.environ.get("OPENFARM_PROVIDERS_FILE")
            os.environ["OPENFARM_PROVIDERS_FILE"] = str(prov_file)
            try:
                save_global_providers(
                    worker_preset="custom",
                    endpoint="https://api.commandcode.ai/provider/v1",
                    model="deepseek/deepseek-v4.1-flash",
                    api_key="test-secret-key",
                    key_file=key_file,
                    zdr=True,
                    path=prov_file,
                )
                repo = Path(tmp) / "zdr-app"
                initialize_project(
                    "zdr-app",
                    directory=repo,
                    language="typescript",
                    state_dir=Path(tmp) / "st",
                    secrets_dir=Path(tmp) / "sec",
                    commit=True,
                )
                cfg = load_config(repo)
                self.assertEqual(cfg.endpoint, "https://api.commandcode.ai/provider/v1")
                self.assertEqual(cfg.model_headers.get("x-cmd-zdr"), "1")
                raw_toml = (repo / ".openfarm" / "config.toml").read_text(encoding="utf-8")
                self.assertIn("https://api.commandcode.ai/provider/v1", raw_toml)
                self.assertIn("x-cmd-zdr", raw_toml)
            finally:
                if prev_prov is None:
                    os.environ.pop("OPENFARM_PROVIDERS_FILE", None)
                else:
                    os.environ["OPENFARM_PROVIDERS_FILE"] = prev_prov


if __name__ == "__main__":
    unittest.main()
