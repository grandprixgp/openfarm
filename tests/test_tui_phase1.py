"""TUI redesign, Phase 1: the checks from the build spec (R1.x)."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from openfarm_core.adapters.files import FileCardSource
from openfarm_core.config import load_config
from openfarm_tui import textual_available
from tests.test_python_e2e import build_python_toy

HEADER_BRACKETS = "`[?date=YYYY-MM-DD]` and [b] and [/x] and [$y]"
BRACKETS = HEADER_BRACKETS + " and a\\"


class _RegistryIsolation(unittest.TestCase):
    """Keep the projects registry and provider profile out of the user's home."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        for var, name in (("OPENFARM_PROJECTS_FILE", "projects.toml"),
                          ("OPENFARM_PROVIDERS_FILE", "providers.toml")):
            prev = os.environ.get(var)
            os.environ[var] = str(self.tmp / name)
            self.addCleanup(self._restore, var, prev)

    @staticmethod
    def _restore(var: str, prev: str | None) -> None:
        if prev is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = prev


@unittest.skipUnless(textual_available(), "textual optional extra not installed")
class MarkupSafetyTests(_RegistryIsolation):
    """R1.1: text with square brackets opens in every tab."""

    def test_card_text_with_brackets_opens_in_every_view(self) -> None:
        from textual.widgets import TabbedContent

        from openfarm_tui.app import OpenfarmApp

        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        for cid in ("CARD-1", "CARD-2"):
            toy.write_card(
                cid,
                title=f"Add {HEADER_BRACKETS}",
                category="small-feature",
                touches=["tally/bounded.py"],
                tests=[{"name": "test_a", "file": "tests/test_a.py", "asserts": HEADER_BRACKETS}],
                done_text=f"## Done\n\n- The query string {BRACKETS} is parsed.",
            )
        toy.commit("add cards")
        cfg = load_config(toy.root)
        src = FileCardSource(toy.root, "cards", worktree=cfg.state_dir / "cards-source")
        src.prepare()
        claim = src.claim("CARD-1", holder="test-lane")
        src.move("CARD-1", claim["lease_id"], "in-review")
        src.finding("CARD-1", f"Finding {BRACKETS}", f"Body {BRACKETS}")

        async def _run() -> None:
            app = OpenfarmApp(repo=toy.root, initial_view="studio")
            async with app.run_test(size=(132, 42)) as pilot:
                await pilot.pause(0.15)
                for view in ("review", "findings", "engine", "projects", "providers", "studio"):
                    app.action_switch_view(view)
                    await pilot.pause(0.1)
                    for tabs in app.query(TabbedContent):
                        for pane in tabs.query("TabPane"):
                            tabs.active = pane.id
                            await pilot.pause(0.05)
                app.action_switch_view("review")
                await pilot.pause(0.05)
                self.assertIn("[?date=YYYY-MM-DD]", str(app.query_one("#review-tests").render()))
                app.action_switch_view("findings")
                app._open_full_finding_modal()
                await pilot.pause(0.1)
                await pilot.press("escape")
                app._open_dismiss_modal()
                await pilot.pause(0.1)
                await pilot.press("escape")
                app.notify(f"toast {BRACKETS} press [L]")
                await pilot.pause(0.1)

        asyncio.run(_run())

    def test_escape_survives_textual_markup(self) -> None:
        from textual.content import Content

        from openfarm_tui.markup import esc

        for raw in (BRACKETS, "[bold]x[/bold]", "arr[0] = []", "\\[b]", "[link=http://x]y"):
            shown = Content.from_markup(f"[bold]{esc(raw)}[/] tail").plain
            self.assertEqual(shown.replace("\\ ", "\\"), f"{raw} tail")


if __name__ == "__main__":
    unittest.main()
