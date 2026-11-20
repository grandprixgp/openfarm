"""TUI redesign, Phase 1: the checks from the build spec (R1.x)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_key_hints_on_buttons_are_shown(self) -> None:
        from textual.widgets import Button

        from openfarm_tui.app import OpenfarmApp
        from openfarm_tui.modals import BounceNoteModal

        toy = build_python_toy()
        self.addCleanup(toy.cleanup)

        async def _run() -> None:
            app = OpenfarmApp(repo=toy.root, initial_view="review")
            async with app.run_test(size=(132, 42)) as pilot:
                await pilot.pause(0.15)
                for bid, hint in (("#btn-approve", "[a]"), ("#btn-land", "[L]"), ("#btn-plan-edit", "[e]"),
                                  ("#btn-find-full", "[Enter]"), ("#btn-proj-scan", "[S]")):
                    self.assertIn(hint, str(app.query_one(bid, Button).label))
                self.assertIn("[m]", str(app.query_one("#review-frontier-out").render()))
                app.push_screen(BounceNoteModal("CARD-1", "t"))
                await pilot.pause(0.1)
                self.assertIn("[Esc]", str(app.screen.query_one("#cancel", Button).label))

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


class _Architect:
    """A stand-in Architect: each call takes the next reply; an exception is raised instead."""

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.on_chunk = None

    def ask(self, prompt: str, **_kw) -> str:
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if self.on_chunk is not None:
            for i in range(0, len(reply), 40):
                self.on_chunk(reply[i:i + 40])
        return reply

    def stream_to(self, on_chunk) -> None:
        self.on_chunk = on_chunk

    def close(self) -> None:
        pass


def _git_state(root: Path) -> tuple[str, str]:
    def run(*args: str) -> str:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True).stdout
    return run("rev-parse", "HEAD"), run("status", "--porcelain", "--", "cards")


@unittest.skipUnless(textual_available(), "textual optional extra not installed")
class ArchitectFailureTests(_RegistryIsolation):
    """R1.4, R1.19: an Architect that can't be asked, or doesn't answer, is a failure on screen."""

    def _toy(self):
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        toy.write("pyproject.toml", '[project]\nname = "toy"\nversion = "0"\n')  # so the Studio doesn't re-init it
        toy.commit("pyproject")
        return toy

    def _drive(self, toy, body, **patches) -> None:
        from textual.widgets import TextArea

        from openfarm_tui.app import OpenfarmApp

        async def _run() -> None:
            app = OpenfarmApp(repo=toy.root, initial_view="studio")
            async with app.run_test(size=(140, 44)) as pilot:
                await pilot.pause(0.15)
                app.query_one("#ta-idea", TextArea).text = "A photo of the day site"
                said: list[str] = []
                real = app._set_drafting_state
                app._set_drafting_state = lambda active, message="": (said.append(message), real(active, message))[1]

                async def settle() -> None:
                    await app.workers.wait_for_complete()
                    await pilot.pause(0.15)

                await body(app, settle, said)

        with mock.patch.multiple("openfarm_tui.app", **patches):
            asyncio.run(_run())

    def _assert_failure_shown(self, app, reason: str) -> None:
        self.assertIn(reason, str(app.query_one("#grill-status").render()))
        self.assertFalse(app.query_one("#btn-arch-retry").has_class("hidden"))
        self.assertFalse(app.query_one("#btn-arch-template").has_class("hidden"))
        self.assertFalse(app._drafting_plan)

    def test_a_backend_that_cannot_open_shows_the_reason_and_retry(self) -> None:
        toy = self._toy()
        before = _git_state(toy.root)

        async def body(app, settle, said) -> None:
            for press in (app._grill_ask_clicked, app._chunk_to_cards_clicked):
                press()
                await settle()
                self._assert_failure_shown(app, "Claude isn't signed in")
                self.assertEqual(_git_state(toy.root), before)
            self.assertEqual((app._grill_pending_question, app._grill_history), ("", []))
            self.assertFalse([m for m in said if "sking" in m], "nobody was asked, so nothing may say so")

        self._drive(toy, body,
                    open_architect_backend=mock.Mock(return_value=(None, "Claude isn't signed in")))

    def test_a_turn_that_times_out_is_a_failure_and_the_template_is_a_choice(self) -> None:
        from openfarm_core.errors import Infra

        toy = self._toy()
        before = _git_state(toy.root)
        timeout = Infra("acp-timeout", "ACP prompt turn timed out after 600.0s")

        async def body(app, settle, said) -> None:
            app._chunk_to_cards_clicked()
            await settle()
            self._assert_failure_shown(app, "timed out after 600.0s")
            self.assertEqual(_git_state(toy.root), before)
            self.assertFalse(list((toy.root / "cards").glob("W*.md")), "no template card by itself")

            app._arch_template_clicked()
            await settle()
            self.assertTrue((toy.root / "cards" / "W1-01.md").is_file())
            self.assertTrue(app.query_one("#btn-arch-retry").has_class("hidden"))
            self.assertIn("Plan ready (basic template)", " | ".join(n.message for n in app._notifications))
            self.assertIn("Basic template (the Architect is not asked): splitting chunk C1 into cards...", said)

        self._drive(toy, body,
                    open_architect_backend=mock.Mock(return_value=(_Architect(timeout), "Claude")))

    def test_retry_asks_again_and_shows_the_real_question(self) -> None:
        from openfarm_core.errors import Infra

        toy = self._toy()
        question = json.dumps({"ready_to_plan": False, "question": "Whose photos?", "options": ["Mine", "NASA's"]})
        opened = mock.Mock(side_effect=[(_Architect(Infra("acp-timeout", "no answer in time")), "Claude"),
                                        (_Architect(question), "Claude")])

        async def body(app, settle, said) -> None:
            app._grill_ask_clicked()
            await settle()
            self._assert_failure_shown(app, "no answer in time")
            app._arch_retry_clicked()
            await settle()
            shown = str(app.query_one("#grill-transcript").render())
            self.assertIn("Questions from the Architect", shown)
            self.assertIn("Whose photos?", shown)
            self.assertTrue(app.query_one("#btn-arch-retry").has_class("hidden"))
            self.assertEqual(opened.call_count, 2)
            self.assertIn("Claude: asking about your idea...", said)

        self._drive(toy, body, open_architect_backend=opened)

    def test_the_switch_says_what_off_means_and_off_asks_nobody(self) -> None:
        from textual.widgets import Switch

        toy = self._toy()
        opened = mock.Mock()

        async def body(app, settle, said) -> None:
            words = app.query_one("#sw-architect-words")
            self.assertIn("on: the Architect writes", str(words.render()))
            app.query_one("#sw-use-architect", Switch).value = False
            await settle()
            self.assertIn("off: the plan comes from a basic template", str(words.render()))
            self.assertIn("The plan will come from a basic template", " | ".join(n.message for n in app._notifications))
            app._grill_ask_clicked()
            await settle()
            self.assertEqual(opened.call_count, 0)
            self.assertIn("Basic questions (the Architect was not asked)", str(app.query_one("#grill-transcript").render()))
            self.assertFalse([m for m in said if "Asking" in m or "Connecting" in m])

        self._drive(toy, body, open_architect_backend=opened)


class ArchitectBackendTests(_RegistryIsolation):
    """R1.4, R1.5, R1.13: the core side of asking the Architect."""

    def test_an_empty_folder_uses_the_global_providers_and_the_full_timeout(self) -> None:
        from types import SimpleNamespace

        from openfarm_core.automode import open_architect_backend

        empty = self.tmp / "empty"
        empty.mkdir()
        worker = SimpleNamespace(timeout_s=600.0, close=lambda: None)
        with mock.patch("openfarm_core.automode.inspect_acp_status", return_value={"oauth_ready": True}), \
                mock.patch("openfarm_core.adapters.claude_worker.open_acp_worker", return_value=worker) as opened:
            backend, name = open_architect_backend(empty)
        self.assertIsNotNone(backend, name)
        self.assertEqual(name, "Claude")
        self.assertEqual(opened.call_args.kwargs["provider"], "claude")
        self.assertEqual(worker.timeout_s, 600.0, "no 180 s cap unless one is asked for")

    def test_a_reply_that_cannot_be_read_is_an_error_not_a_template(self) -> None:
        from openfarm_core.automode import architect_grill_turn, draft_spec_and_chunks, draft_wave_plan
        from openfarm_core.errors import Infra

        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        before = _git_state(toy.root)
        with self.assertRaises(Infra):
            architect_grill_turn(toy.root, idea="x", backend=_Architect("sorry, I can't"))
        with self.assertRaises(Infra):
            draft_spec_and_chunks(toy.root, idea="x", backend=_Architect("{}"))
        with self.assertRaises(Infra):
            draft_wave_plan(toy.root, idea="x", backend=_Architect('{"files": {}}'), verify_baseline=False)
        self.assertEqual(_git_state(toy.root), before)
        self.assertFalse(list((toy.root / "cards").glob("*.md")))

    def test_a_slow_split_reports_each_file_as_it_is_written(self) -> None:
        from openfarm_core.automode import draft_wave_plan

        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        answer = json.loads(_CardsBackend("W1-01", "W1-02").answer)
        answer["files"] = {".openfarm/PROJECT_SPEC.md": "# Spec\n", **answer["files"]}
        said: list[str] = []
        draft_wave_plan(toy.root, idea="x", backend=_Architect(json.dumps(answer)), verify_baseline=False,
                        on_progress=said.append)
        writing = [m for m in said if m.startswith("Writing")]
        self.assertEqual(writing, ["Writing .openfarm/PROJECT_SPEC.md...", "Writing card 1: W1-01...",
                                   "Writing card 2: W1-02..."])

    def test_the_worker_hands_text_on_as_it_arrives(self) -> None:
        from types import SimpleNamespace

        from openfarm_core.adapters.claude_worker import ClaudeAcpWorkerBackend

        class _Acp:
            _initialized = True

            def new_session(self, **_kw):
                return "s"

            def prompt(self, session_id, text, timeout_s, on_chunk=None):
                for part in ("he", "llo"):
                    on_chunk(part)
                return SimpleNamespace(text="hello", tokens_in=1, tokens_out=1, vendor_cost_usd=0.0,
                                       stop_reason="end_turn", latency_s=0.0)

            def close(self) -> None:
                pass

        backend = ClaudeAcpWorkerBackend(adapter=_Acp())
        self.addCleanup(backend.close)
        got: list[str] = []
        backend.on_chunk = got.append
        backend.complete("architect", "", 0.2, [{"role": "user", "content": "hi"}])
        self.assertEqual(got, ["he", "llo"])


if __name__ == "__main__":
    unittest.main()
