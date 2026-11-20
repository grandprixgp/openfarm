"""v0.10.0 Pillar 6 tests: Textual Workbench (all screens, Findings Inbox UI, CommandPalette, curses fallback)."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openfarm_core.adapters.files import FileCardSource
from openfarm_core.config import load_config
from openfarm_core.findings import list_findings
from openfarm_tui import textual_available
from tests.test_python_e2e import build_python_toy


class TextualWorkbenchV010Tests(unittest.TestCase):
    def _seed_repo_with_cards_and_findings(self):
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        toy.write_card(
            "CARD-1",
            title="Implement loader",
            category="small-feature",
            touches=["tally/bounded.py"],
            tests=[{"name": "test_a", "file": "tests/test_a.py"}],
            done_text="## Done\n\n- Loader returns expected item.",
        )
        toy.write_card(
            "CARD-2",
            title="Implement formatter",
            category="small-feature",
            touches=["tally/bounded.py"],
            tests=[{"name": "test_b", "file": "tests/test_b.py"}],
            done_text="## Done\n\n- Formatter returns expected text.",
        )
        toy.commit("add cards")
        cfg = load_config(toy.root)
        src = FileCardSource(toy.root, "cards", worktree=cfg.state_dir / "cards-source")
        src.prepare()
        # Move CARD-1 to in-review so Review inbox has a live card to inspect
        claim = src.claim("CARD-1", holder="test-lane")
        src.move("CARD-1", claim["lease_id"], "in-review")
        f1 = src.finding("CARD-1", "Missing null guard in loader", "Empty input raises KeyError instead of ValueError.")["id"]
        f2 = src.finding("CARD-1", "Dead helper in bounded.py", "Old helper _unused can be removed.")["id"]
        f3 = src.finding("CARD-2", "Negative zero formatting", "Ensure -0.0 formats as 0.0.")["id"]
        return toy, cfg, (f1, f2, f3)

    @unittest.skipUnless(textual_available(), "textual optional extra not installed")
    def test_workbench_renders_all_views_findings_banner_and_review_findings(self) -> None:
        from openfarm_tui.app import OpenfarmApp

        toy, _cfg, (f1, f2, f3) = self._seed_repo_with_cards_and_findings()

        async def _run() -> None:
            app = OpenfarmApp(repo=toy.root, initial_view="studio")
            async with app.run_test(size=(132, 42)) as pilot:
                await pilot.pause(0.15)
                # Studio shows "needs you" findings banner & zero '$' spend in chrome
                banner = str(app.query_one("#studio-findings-banner").render())
                self.assertIn("3 waiting in total", banner)
                self.assertNotIn("$", str(app.query_one("#token-summary").render()))
                self.assertNotIn("$", str(app.query_one("#econ-stats").render()))

                # Switch to Review (2): CARD-1 dossier lists findings filed by CARD-1
                await pilot.press("2")
                await pilot.pause(0.15)
                rev_findings = str(app.query_one("#review-card-findings").render())
                self.assertIn(f1, rev_findings)
                self.assertIn("Missing null guard", rev_findings)

                # Switch to Findings Inbox (3), test Enter (full modal) & '/' filter
                await pilot.press("3")
                await pilot.pause(0.15)
                hdr = str(app.query_one("#findings-header").render())
                self.assertIn("3 waiting", hdr)
                self.assertIn("0 picked", hdr)
                await pilot.press("enter")
                await pilot.pause(0.1)
                await pilot.press("escape")
                await pilot.pause(0.1)
                await pilot.press("slash")
                await pilot.pause(0.05)
                self.assertEqual(app.focused.id, "findings-filter")
                await pilot.press("escape")
                await pilot.pause(0.05)

                # Switch to Engine (4)
                await pilot.press("4")
                await pilot.pause(0.1)

                # Open Ctrl+K Command Palette
                await pilot.press("ctrl+k")
                await pilot.pause(0.1)

        asyncio.run(_run())

    @unittest.skipUnless(textual_available(), "textual optional extra not installed")
    def test_findings_inbox_decisions_w_x_c_persist(self) -> None:
        from openfarm_tui.app import OpenfarmApp

        toy, cfg, (f1, f2, f3) = self._seed_repo_with_cards_and_findings()

        async def _run() -> None:
            app = OpenfarmApp(repo=toy.root, initial_view="findings")
            async with app.run_test(size=(132, 42)) as pilot:
                await pilot.pause(0.15)
                # Newest first: f3 is selected first -> press 'w' to pick for next wave
                await pilot.press("w")
                await pilot.pause(0.15)
                # Now f2 is top waiting -> dismiss via helper method (simulating modal submit)
                app.apply_finding_decision(f2, "dismissed", reason="dead code already removed")
                await pilot.pause(0.1)
                # Fold f1 into CARD-2
                app.apply_finding_decision(f1, "folded", target_card="CARD-2")
                await pilot.pause(0.1)

        asyncio.run(_run())

        summary = list_findings(toy.root, cfg, state_filter="all")
        self.assertEqual(summary["counts"]["waiting"], 0)
        self.assertEqual(summary["counts"]["picked"], 1)
        self.assertEqual(summary["counts"]["dismissed"], 1)
        self.assertEqual(summary["counts"]["folded"], 1)

    @unittest.skipUnless(textual_available(), "textual optional extra not installed")
    def test_global_providers_view_saves_profile(self) -> None:
        from openfarm_core.providers import load_global_providers
        from openfarm_tui.app import OpenfarmApp

        with tempfile.TemporaryDirectory() as tmp:
            prov_path = Path(tmp) / "providers.toml"
            key_path = Path(tmp) / "worker.key"
            repo = Path(tmp) / "empty-app"
            repo.mkdir()
            prev = os.environ.get("OPENFARM_PROVIDERS_FILE")
            os.environ["OPENFARM_PROVIDERS_FILE"] = str(prov_path)
            try:
                async def _run() -> None:
                    app = OpenfarmApp(repo=repo, initial_view="providers")
                    async with app.run_test(size=(132, 42)) as pilot:
                        await pilot.pause(0.15)
                        app.save_providers_form(
                            frontier="claude",
                            preset="ollama",
                            endpoint="http://127.0.0.1:11434/v1",
                            model="qwen2.5-coder:32b",
                            api_key="",
                            zdr=False,
                            key_file=key_path,
                        )
                        await pilot.pause(0.1)

                asyncio.run(_run())
                loaded = load_global_providers(prov_path)
                self.assertTrue(loaded["configured"])
                self.assertEqual(loaded["worker"]["model"], "qwen2.5-coder:32b")
            finally:
                if prev is None:
                    os.environ.pop("OPENFARM_PROVIDERS_FILE", None)
                else:
                    os.environ["OPENFARM_PROVIDERS_FILE"] = prev

    def test_curses_fallback_when_force_curses_or_env_set(self) -> None:
        from openfarm_core.tui import run_tui

        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        called_curses = []

        with mock.patch("openfarm_core.tui.curses.wrapper", side_effect=lambda fn: (called_curses.append(True), 0)[1]):
            rc = run_tui(repo=toy.root, once=False, force_curses=True)
        self.assertEqual(rc, 0)
        self.assertEqual(len(called_curses), 1)


if __name__ == "__main__":
    unittest.main()
