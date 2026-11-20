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


def _two_card_toy(case: unittest.TestCase):
    """A toy with CARD-1 and CARD-2, where CARD-2 depends on CARD-1."""
    toy = build_python_toy()
    case.addCleanup(toy.cleanup)
    for cid, deps in (("CARD-1", []), ("CARD-2", ["CARD-1"])):
        toy.write_card(
            cid,
            title=f"Add part {cid[-1]}",
            category="small-feature",
            touches=["tally/bounded.py"],
            tests=[{"name": "test_a", "file": "tests/test_a.py", "asserts": "returns 3"}],
            done_text="## Done\n\n- `push` returns 3 for a full bundle.",
            depends=deps,
        )
    toy.commit("add cards")
    cfg = load_config(toy.root)
    src = FileCardSource(toy.root, "cards", worktree=cfg.state_dir / "cards-source")
    src.prepare()
    return toy, src


class CardEditTests(_RegistryIsolation):
    """R1.2: Edit Card saves the copy the farm builds from, and keeps `depends`."""

    def test_an_edit_to_a_blocked_card_is_what_the_next_run_gets(self) -> None:
        from openfarm_core.automode import update_card_with_eligibility

        toy, src = _two_card_toy(self)
        claim = src.claim("CARD-1", holder="test-lane")
        src.move("CARD-1", claim["lease_id"], "blocked", stop_code="tests-unprovable")

        update_card_with_eligibility(
            toy.root, "CARD-1", done_text="## Done\n\n- `push` returns 4 for a full bundle.")
        self.assertIn("returns 4", src.read("CARD-1")["card"]["done_text"])
        self.assertEqual(src.read("CARD-1")["card"]["state"], "blocked")

        src.release("CARD-1", reason="retry")
        src.sync_ready_cards_from_base()  # what drafting more cards does later
        self.assertEqual(src.next()["cards"], ["CARD-1"])
        self.assertIn("returns 4", src.read("CARD-1")["card"]["done_text"])

    def test_a_card_being_built_is_refused_with_the_reason(self) -> None:
        from openfarm_core.automode import update_card_with_eligibility
        from openfarm_core.errors import Refused

        toy, src = _two_card_toy(self)
        src.claim("CARD-1", holder="test-lane")
        with self.assertRaises(Refused) as caught:
            update_card_with_eligibility(toy.root, "CARD-1", title="New title")
        self.assertIn("CARD-1 is building; stop it first", str(caught.exception))
        self.assertEqual(src.read("CARD-1")["card"]["title"], "Add part 1")

    @unittest.skipUnless(textual_available(), "textual optional extra not installed")
    def test_saving_the_form_unchanged_keeps_depends(self) -> None:
        from openfarm_tui.app import OpenfarmApp

        toy, src = _two_card_toy(self)

        async def _run() -> None:
            app = OpenfarmApp(repo=toy.root, initial_view="studio")
            async with app.run_test(size=(132, 42)) as pilot:
                await pilot.pause(0.15)
                app._selected_plan_id = "CARD-2"
                app._open_card_editor_modal()
                await pilot.pause(0.1)
                self.assertEqual(app.screen.query_one("#ce-depends").value, "CARD-1")
                app.screen._ok()
                await pilot.pause(0.2)

        asyncio.run(_run())
        self.assertEqual(src.read("CARD-2")["card"]["deps"], ["CARD-1"])
        self.assertIn('depends = ["CARD-1"]', (toy.root / "cards" / "CARD-2.md").read_text())


class _CardsBackend:
    """Stands in for the Architect: answers every prompt with the given card files."""

    def __init__(self, *card_ids: str) -> None:
        import json

        from openfarm_core.adapters.files import render_card_file

        self.prompts: list[str] = []
        files = {}
        for i, cid in enumerate(card_ids):
            header = {
                "id": cid, "title": f"Later part {cid}", "state": "ready", "category": "small-feature",
                "unit": "toy", "touches": ["tally/bounded.py"],
                "depends": [card_ids[i - 1]] if i else [],
                "tests": [{"name": "test_b", "file": "tests/test_b.py", "asserts": "returns 5"}],
            }
            files[f"cards/{cid}.md"] = render_card_file(header, "## Done\n\n- `push` returns 5 after a reset.\n")
        self.answer = json.dumps({"files": files})

    def ask(self, prompt: str, **_kw) -> str:
        self.prompts.append(prompt)
        return self.answer


class NewCardIdTests(_RegistryIsolation):
    """R1.3: a new plan or split never reuses a card ID or writes over a card file."""

    def _toy_with_done_cards(self):
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        ids = ["W1-01", "W1-02", "W1-03", "W1-04"]
        for cid in ids:
            toy.write_card(
                cid, title=f"First part {cid}", category="small-feature", touches=["tally/bounded.py"],
                tests=[{"name": "test_a", "file": "tests/test_a.py", "asserts": "returns 3"}],
                done_text="## Done\n\n- `push` returns 3 for a full bundle.")
        (toy.root / ".openfarm" / "GRILL_DECISIONS.md").write_text(
            "# Architect Grill Decisions\n\n## What we are building\n\nThe first idea, FIRST_IDEA_TOKEN\n")
        toy.commit("add cards")
        cfg = load_config(toy.root)
        src = FileCardSource(toy.root, "cards", worktree=cfg.state_dir / "cards-source")
        src.prepare()
        for cid in ids:
            claim = src.claim(cid, holder="test-lane")
            src.move(cid, claim["lease_id"], "in-review")
            src.mark_done(cid)
        return toy, src, ids

    def test_new_cards_get_new_ids_and_old_files_stay(self) -> None:
        from openfarm_core.automode import draft_wave_plan

        toy, src, ids = self._toy_with_done_cards()
        before = {cid: (toy.root / "cards" / f"{cid}.md").read_bytes() for cid in ids}
        backend = _CardsBackend("W1-01", "W1-02")

        res = draft_wave_plan(toy.root, idea="A later request, SECOND_IDEA_TOKEN", backend=backend,
                              verify_baseline=False)

        self.assertEqual(res["new_cards"], ["W1-05", "W1-06"])
        self.assertIn("Number new cards from W1-05", backend.prompts[0])
        self.assertEqual({cid: (toy.root / "cards" / f"{cid}.md").read_bytes() for cid in ids}, before)
        self.assertEqual([src.read(cid)["card"]["state"] for cid in ids], ["done"] * 4)
        second = src.read("W1-06")["card"]
        self.assertEqual((second["id"], second["deps"], second["state"]), ("W1-06", ["W1-05"], "ready"))
        decisions = (toy.root / ".openfarm" / "GRILL_DECISIONS.md").read_text()
        self.assertIn("FIRST_IDEA_TOKEN", decisions)
        self.assertIn("SECOND_IDEA_TOKEN", decisions)
        self.assertLess(decisions.index("FIRST_IDEA_TOKEN"), decisions.index("SECOND_IDEA_TOKEN"))

    def test_a_second_split_replaces_only_the_cards_nobody_started(self) -> None:
        from openfarm_core.automode import draft_wave_plan

        toy, src, ids = self._toy_with_done_cards()
        draft_wave_plan(toy.root, idea="more", backend=_CardsBackend("W1-05", "W1-06"), verify_baseline=False)
        claim = src.claim("W1-05", holder="test-lane")
        src.move("W1-05", claim["lease_id"], "blocked", stop_code="tests-unprovable")

        res = draft_wave_plan(toy.root, backend=_CardsBackend("W1-01", "W1-02"), verify_baseline=False)

        self.assertEqual(res["new_cards"], ["W1-07", "W1-08"])
        self.assertEqual(res["retired_cards"], ["W1-06"])
        states = {c.id: c.state for c in src.all_cards()}
        self.assertEqual((states["W1-05"], states["W1-06"], states["W1-07"]), ("blocked", "dropped", "ready"))
        self.assertEqual([states[cid] for cid in ids], ["done"] * 4)
        self.assertEqual([c["id"] for c in res["cards"]], ["W1-07", "W1-08"])
        self.assertEqual((toy.root / ".openfarm" / "GRILL_DECISIONS.md").read_text().count("more"), 1)


if __name__ == "__main__":
    unittest.main()
