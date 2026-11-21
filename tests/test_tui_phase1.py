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
from tests.storehelp import cards_dir
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
            reference="tally/bounded.py",
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
            self.assertFalse(list(cards_dir(toy.root).glob("W*.md")), "no template card by itself")

            app._arch_template_clicked()
            await settle()
            self.assertTrue((cards_dir(toy.root) / "W1-01.md").is_file())
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


def _log_text(app) -> str:
    """The activity log as one line of text (the log wraps long lines)."""
    return " ".join(" ".join(str(line.text) for line in app.query_one("#activity-log").lines).split())


@unittest.skipUnless(textual_available(), "textual optional extra not installed")
class RunPanelTests(_RegistryIsolation):
    """R1.7, R1.10, R1.11, R1.14, R1.16: the wave, its panel and the way back."""

    def _blocked_first_card(self):
        toy, src = _two_card_toy(self)
        claim = src.claim("CARD-1", holder="test-lane")
        src.move("CARD-1", claim["lease_id"], "blocked", stop_code="tests-unprovable")
        return toy, src

    def _drive(self, toy, body, view: str = "studio") -> None:
        from openfarm_tui.app import OpenfarmApp

        async def _run() -> None:
            app = OpenfarmApp(repo=toy.root, initial_view=view)
            async with app.run_test(size=(140, 44)) as pilot:
                await pilot.pause(0.15)

                async def settle() -> None:
                    await app.workers.wait_for_complete()
                    await pilot.pause(0.15)

                await body(app, pilot, settle)

        asyncio.run(_run())

    def test_the_tui_never_swaps_stdout_to_call_the_cli(self) -> None:
        import openfarm_tui.app as app_module

        source = Path(app_module.__file__).read_text()
        self.assertNotIn("redirect_stdout", source)
        self.assertNotIn("cli_main", source)

    def test_a_wave_runs_in_its_own_process_and_says_why_it_started_nothing(self) -> None:
        import sys

        toy, _src = self._blocked_first_card()

        async def body(app, pilot, settle) -> None:
            streams = (sys.stdout, sys.stderr)
            with mock.patch("openfarm_tui.app.subprocess.Popen", wraps=subprocess.Popen) as popen:
                app.start_nonblocking_wave()
                app._run_cli_quick(["status"])  # a second action while the wave is going
                await settle()
            cli_calls = [c.args[0] for c in popen.call_args_list if "openfarm_core.cli" in c.args[0]]
            self.assertTrue(any("run" in argv for argv in cli_calls), cli_calls)
            self.assertTrue(any("status" in argv for argv in cli_calls), cli_calls)
            self.assertEqual((sys.stdout, sys.stderr), streams)
            self.assertFalse(app._running_wave)
            log = _log_text(app)
            self.assertIn("CARD-2 waits for CARD-1 (blocked)", log)
            self.assertIn("ended without starting a task", log)
            self.assertNotIn("0 cards claimed", log)
            self.assertIn("CARD-2 waits for CARD-1 (blocked)", str(app.query_one("#run-queue-view").render()))

            # The way back: the button, and coming back to the Studio tab.
            self.assertEqual(app.query_one("#studio-switcher").current, "studio-run")
            self.assertTrue(app.query_one("#btn-run-back").display)
            app._run_back_clicked()
            self.assertEqual(app.query_one("#studio-switcher").current, "studio-plan")
            app.query_one("#studio-switcher").current = "studio-run"
            app.action_switch_view("review")
            app.action_switch_view("studio")
            self.assertEqual(app.query_one("#studio-switcher").current, "studio-plan")

        self._drive(toy, body)

    def test_run_only_buttons_and_keys_do_nothing_when_nothing_runs(self) -> None:
        from openfarm_core.state import StateDir

        toy, _src = self._blocked_first_card()
        state = StateDir(load_config(toy.root).state_dir)

        async def body(app, pilot, settle) -> None:
            for bid in ("#btn-pause", "#btn-stop", "#btn-hint", "#btn-resume", "#btn-daemon-stop"):
                self.assertTrue(app.query_one(bid).has_class("hidden"), bid)
            self.assertEqual(len(app.query("#btn-run-start")), 0, "Launch Wave is not repeated in the run panel")
            app.query_one("#plan-table").focus()
            await pilot.press("p", "s", "h")
            await settle()
            self.assertFalse(state.is_paused())
            self.assertFalse(state.kill_requested(load_config(toy.root).kill_switch))
            self.assertEqual(len(app.screen_stack), 1, "no Hint dialog")

            state.pause("left over")
            app.start_nonblocking_wave()
            await settle()
            self.assertIn("the queue is paused", _log_text(app))
            self.assertFalse(app.query_one("#btn-resume").has_class("hidden"))

        self._drive(toy, body)

    def test_a_phase_is_logged_once_and_a_finished_card_stops_looking_busy(self) -> None:
        toy, _src = self._blocked_first_card()

        async def body(app, pilot, settle) -> None:
            app.query_one("#studio-switcher").current = "studio-run"  # the log draws once it is on screen
            await pilot.pause(0.1)
            for _ in range(2):
                app._handle_run_event({"kind": "phase", "card_id": "CARD-9", "phase": "claim", "ts": "2026-11-20T10:00:00Z"})
            await pilot.pause(0.1)
            self.assertEqual(_log_text(app).count("CARD-9 → claim"), 1)
            app._handle_run_event({"kind": "card_done", "card_id": "CARD-9", "final_state": "in-review"})
            await pilot.pause(0.1)
            queue = str(app.query_one("#run-queue-view").render())
            self.assertIn("CARD-9 finished: in-review", queue)
            self.assertNotIn("▶", queue)

        self._drive(toy, body)


@unittest.skipUnless(textual_available(), "textual optional extra not installed")
class PlanViewTests(_RegistryIsolation):
    """R1.14: the plan says which cards wait, on what, and which can be built."""

    def test_a_card_behind_a_blocked_one_is_shown_waiting_with_its_dependency(self) -> None:
        toy, src = _two_card_toy(self)
        claim = src.claim("CARD-1", holder="test-lane")
        src.move("CARD-1", claim["lease_id"], "blocked", stop_code="tests-unprovable")

        async def body(app, pilot, settle) -> None:
            app.query_one("#studio-switcher").current = "studio-plan"
            await pilot.pause(0.1)
            table = app.query_one("#plan-table")
            rows = {str(table.get_row(k)[0]): [str(c) for c in table.get_row(k)]
                    for k in ("CARD-1", "CARD-2")}
            self.assertEqual(rows["CARD-1"][2], "blocked")
            self.assertEqual(rows["CARD-2"][2:4], ["waits", "CARD-1"])
            self.assertEqual(table.parent.border_title, "Wave Plan")
            self.assertIn("Token share: worker", str(app.query_one("#token-summary").render()))  # R1.16

            preflight = " ".join(str(app.query_one("#plan-preflight").render()).split())
            self.assertIn("0 can start now", preflight)
            self.assertIn("CARD-2 waits for CARD-1 (blocked)", preflight)

            app._selected_plan_id = "CARD-2"
            app.refresh_all_views()
            await pilot.pause(0.1)
            detail = " ".join(str(app.query_one("#plan-detail").render()).split())
            self.assertIn("waits for CARD-1 (blocked)", detail)
            self.assertIn("Depends on: CARD-1 (blocked)", detail)
            self.assertNotIn("eligible", detail)

        RunPanelTests._drive(self, toy, body)

    def test_a_card_that_fails_the_check_is_not_called_buildable(self) -> None:
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        toy.write_card(
            "CARD-3", title="Add part 3", category="not-a-category",
            touches=["tally/bounded.py"],
            tests=[{"name": "test_a", "file": "tests/test_a.py", "asserts": "returns 3"}],
            done_text="## Done\n\n- `push` returns 3 for a full bundle.", depends=[])
        toy.commit("add a card with a bad category")

        async def body(app, pilot, settle) -> None:
            app.query_one("#studio-switcher").current = "studio-plan"
            app._selected_plan_id = "CARD-3"
            app.refresh_all_views()
            await pilot.pause(0.1)
            self.assertEqual(str(app.query_one("#plan-table").get_row("CARD-3")[2]), "fix")
            detail = " ".join(str(app.query_one("#plan-detail").render()).split())
            self.assertIn("can't be built yet", detail)
            self.assertIn("not-a-category", detail)
            self.assertIn("1 can't be built yet", " ".join(str(app.query_one("#plan-preflight").render()).split()))

        RunPanelTests._drive(self, toy, body)


class IdeaScreenTests(_RegistryIsolation):
    """R1.11, R1.12: typing on a new project lands in the idea box, and the idea is kept."""

    def _open(self, toy, body, view: str = "auto") -> None:
        from openfarm_tui.app import OpenfarmApp

        async def _run() -> None:
            app = OpenfarmApp(repo=toy.root, initial_view=view)
            async with app.run_test(size=(140, 44)) as pilot:
                await pilot.pause(0.2)
                await body(app, pilot)

        asyncio.run(_run())

    def _toy(self):
        return ArchitectFailureTests._toy(self)

    def test_first_keys_go_to_the_idea_box_even_from_a_button(self) -> None:
        toy = self._toy()

        async def body(app, pilot) -> None:
            idea = app.query_one("#ta-idea")
            self.assertIs(app.focused, idea)
            await pilot.press("h", "i")
            self.assertEqual(idea.text, "hi")

            app.query_one("#btn-grill-ask").focus()
            await pilot.pause(0.05)
            await pilot.press("h", "e", "q", "r")
            await pilot.pause(0.1)
            self.assertEqual(idea.text, "hiheqr")
            self.assertIs(app.focused, idea)
            self.assertEqual(len(app.screen_stack), 1)  # no Hint dialog, and q did not quit

        self._open(toy, body)

    def test_the_idea_is_there_again_after_a_restart_in_the_same_folder(self) -> None:
        toy = self._toy()

        async def first(app, pilot) -> None:
            await pilot.press(*"a photo site")
            await pilot.pause(0.1)

        async def second(app, pilot) -> None:
            self.assertEqual(app.query_one("#ta-idea").text, "a photo site")
            app.query_one("#ta-idea").load_text("")
            await pilot.pause(0.1)

        async def third(app, pilot) -> None:
            self.assertEqual(app.query_one("#ta-idea").text, "")

        self._open(toy, first)
        self._open(toy, second)
        self._open(toy, third)
        dirty = subprocess.run(["git", "-C", str(toy.root), "status", "--porcelain"],
                               capture_output=True, text=True).stdout
        self.assertEqual(dirty, "")  # the draft is kept outside the folder

    def test_the_app_opens_where_the_next_step_is(self) -> None:
        toy, src = _two_card_toy(self)

        async def on_plan(app, pilot) -> None:
            self.assertEqual(app.query_one("#main-switcher").current, "studio")
            self.assertEqual(app.query_one("#studio-switcher").current, "studio-plan")

        self._open(toy, on_plan)

        claim = src.claim("CARD-1", holder="test-lane")
        src.move("CARD-1", claim["lease_id"], "in-review")

        async def on_review(app, pilot) -> None:
            self.assertEqual(app.query_one("#main-switcher").current, "review")

        self._open(toy, on_review)  # CARD-2 waits for CARD-1, so review is the only thing to do

        async def as_asked(app, pilot) -> None:
            self.assertEqual(app.query_one("#main-switcher").current, "engine")

        self._open(toy, as_asked, view="engine")

        async def on_run(app, pilot) -> None:
            self.assertEqual(app.query_one("#main-switcher").current, "studio")
            self.assertEqual(app.query_one("#studio-switcher").current, "studio-run")

        with mock.patch("openfarm_tui.app.daemon_status", return_value={"running": True, "pid": 4242}):
            self._open(toy, on_run)


class LayoutTests(_RegistryIsolation):
    """R1.15: nothing is cut off at 100 columns and up, and nothing jumps."""

    SCREENS = (("studio", "studio-grill"), ("studio", "studio-spec"), ("studio", "studio-plan"),
               ("studio", "studio-run"), ("review", None), ("findings", None), ("engine", None),
               ("projects", None), ("providers", None))

    @staticmethod
    def _cut_off(app) -> list[str]:
        from textual.widgets import Button

        out = []
        for button in app.screen.query(Button):
            if not all(node.display for node in button.ancestors_with_self):
                continue
            region = button.region
            boxes = [node.region for node in button.ancestors if node is not app.screen and hasattr(node, "region")]
            if not region.area or not all(box.contains_region(region) for box in boxes):
                out.append(str(button.id))
        return out

    def _open(self, toy, size, body) -> None:
        from openfarm_tui.app import OpenfarmApp

        async def _run() -> None:
            app = OpenfarmApp(repo=toy.root, initial_view="studio")
            async with app.run_test(size=size) as pilot:
                await pilot.pause(0.2)
                await body(app, pilot)

        asyncio.run(_run())

    def _toy(self, done_lines: int = 1):
        toy, src = _two_card_toy(self)
        if done_lines > 1:
            from openfarm_core.automode import update_card_with_eligibility
            update_card_with_eligibility(
                toy.root, "CARD-1",
                done_text="## Done\n\n" + "\n".join(f"- `push` returns {n} for bundle {n}." for n in range(done_lines)))
        claim = src.claim("CARD-1", holder="test-lane")
        src.move("CARD-1", claim["lease_id"], "blocked", stop_code="tests-unprovable")  # shows every review button
        return toy

    def test_no_button_is_cut_off_from_100_columns_up(self) -> None:
        toy = self._toy()
        for size in ((100, 44), (120, 44), (160, 50)):
            async def body(app, pilot, size=size) -> None:
                for view, sub in self.SCREENS:
                    app.action_switch_view(view)
                    if sub:
                        app.query_one("#studio-switcher").current = sub
                    await pilot.pause(0.15)
                    self.assertEqual(self._cut_off(app), [], f"{view} {sub or ''} at {size[0]} columns")

            self._open(toy, size, body)

    def test_the_launch_button_stays_in_view_under_a_long_done_text(self) -> None:
        toy = self._toy(done_lines=120)

        async def body(app, pilot) -> None:
            app.query_one("#studio-switcher").current = "studio-plan"
            table = app.query_one("#plan-table")
            table.move_cursor(row=table.get_row_index("CARD-1"))
            await pilot.pause(0.2)
            detail = app.query_one("#plan-detail")
            self.assertGreater(detail.region.height, app.size.height)  # the text is longer than the screen
            self.assertEqual(self._cut_off(app), [])
            launch = app.query_one("#btn-launch-wave").region
            self.assertTrue(app.screen.region.contains_region(launch))
            self.assertGreater(detail.parent.max_scroll_y, 0)  # and scrolls on its own

        self._open(toy, (100, 40), body)

    def test_the_progress_panel_is_one_size_and_moves_nothing(self) -> None:
        toy = self._toy()

        async def body(app, pilot) -> None:
            app.query_one("#studio-switcher").current = "studio-plan"
            await pilot.pause(0.15)
            table, launch = app.query_one("#plan-table"), app.query_one("#btn-launch-wave")
            before = (table.region.y, app.query_one("#studio-switcher").region.y)

            app._busy_button = "#btn-draft-plan"
            app._set_drafting_state(True, "Connecting to the Architect...")
            await pilot.pause(0.15)
            panel = app.query_one("#architect-panel")
            height = panel.region.height
            self.assertEqual((table.region.y, app.query_one("#studio-switcher").region.y), before)
            self.assertEqual(self._cut_off(app), [])
            self.assertLess(launch.region.bottom, panel.region.y + 1)  # the main button is still above it

            for n in range(40):
                app._log_architect(f"Writing card {n}...")
            await pilot.pause(0.15)
            self.assertEqual(panel.region.height, height)
            self.assertEqual((table.region.y, app.query_one("#studio-switcher").region.y), before)
            app._set_drafting_state(False, "")

        self._open(toy, (100, 44), body)


class FindingsTests(_RegistryIsolation):
    """R1.18: a finding says what it holds up, and a retried card's repeats are one row."""

    def _toy(self):
        toy, src = _two_card_toy(self)
        claim = src.claim("CARD-1", holder="test-lane")
        src.move("CARD-1", claim["lease_id"], "blocked", stop_code="tests-unprovable")
        # The titles the dogfood run's retries of one card really filed.
        for title in ("User-agent value is not specified", "Fixture is not scaffolded here",
                      "User-agent value is unspecified", "User-agent value is not specified"):
            src.finding("CARD-1", title, "The card does not say.")
        src.finding("CARD-2", "Test name promises more than the card", "Only the font is emitted.")
        return toy

    def test_repeats_fold_and_each_finding_says_what_it_holds_up(self) -> None:
        from openfarm_core.findings import list_findings

        toy = self._toy()
        found = list_findings(toy.root, load_config(toy.root), deduplicate=True)["findings"]
        by_title = {f["title"]: f for f in found}
        self.assertEqual(len(found), 3)
        agent = next(f for f in found if f["title"].startswith("User-agent"))
        self.assertEqual(agent["duplicate_count"], 3)
        self.assertEqual(by_title["Fixture is not scaffolded here"]["duplicate_count"], 1)
        self.assertEqual(agent["holds_up"], ["CARD-1", "CARD-2"])
        self.assertEqual(by_title["Test name promises more than the card"]["holds_up"], [])

    def test_the_inbox_shows_it_and_one_decision_covers_the_repeats(self) -> None:
        from openfarm_core.findings import list_findings

        toy = self._toy()

        async def body(app, pilot, settle) -> None:
            table = app.query_one("#findings-table")
            self.assertEqual(table.row_count, 3)
            agent = next(f for f in app._findings_items if f["title"].startswith("User-agent"))
            self.assertEqual(str(table.get_row(agent["id"])[2]), "blocks CARD-1")
            table.move_cursor(row=table.get_row_index(agent["id"]))
            await pilot.pause(0.1)
            detail = " ".join(str(app.query_one("#finding-detail").render()).split())
            self.assertIn("Holds up: CARD-1 (blocked), and CARD-2 waits for it", detail)
            self.assertIn("Filed 3 times by retries of CARD-1", detail)

            other = next(f for f in app._findings_items if f["from_card"] == "CARD-2")
            table.move_cursor(row=table.get_row_index(other["id"]))
            await pilot.pause(0.1)
            detail = " ".join(str(app.query_one("#finding-detail").render()).split())
            self.assertIn("Doesn't block anything (CARD-2 is ready)", detail)

            app.apply_finding_decision(agent["id"], "dismissed", reason="set in the card")
            await pilot.pause(0.1)
            self.assertEqual(table.row_count, 2)

        RunPanelTests._drive(self, toy, body, view="findings")
        counts = list_findings(toy.root, load_config(toy.root))["counts"]
        self.assertEqual((counts["waiting"], counts["dismissed"]), (2, 3))


class ReviewActionTests(_RegistryIsolation):
    """R1.9, R1.17: slow review actions show they run and how they end; a blocked card can be accepted."""

    def _drive(self, toy, body) -> None:
        from openfarm_tui.app import OpenfarmApp

        async def _run() -> None:
            app = OpenfarmApp(repo=toy.root, initial_view="review")
            async with app.run_test(size=(140, 44)) as pilot:
                await pilot.pause(0.15)

                async def settle() -> None:
                    await app.workers.wait_for_complete()
                    await pilot.pause(0.15)

                await body(app, pilot, settle)

        asyncio.run(_run())

    def _blocked_and_ready(self):
        toy, src = _two_card_toy(self)
        claim = src.claim("CARD-1", holder="test-lane")
        src.move("CARD-1", claim["lease_id"], "blocked", stop_code="tests-unprovable")
        return toy

    def test_frontier_review_shows_it_runs_opens_its_tab_and_is_kept(self) -> None:
        import threading

        toy = self._blocked_and_ready()
        release = threading.Event()
        calls: list[tuple] = []

        def fake_cli(*argv):
            calls.append(argv)
            release.wait(5)
            return 0, "Frontier review (card): agreed=clean (consensus=2/2)\n  [claude] clean: fine"

        async def body(app, pilot, settle) -> None:
            app._cli = fake_cli
            app._selected_review_id = ""
            app._run_frontier_review_bg()
            await pilot.pause(0.1)
            self.assertEqual(calls, [], "no card chosen: nothing is reviewed")
            self.assertIn("Which card?", " | ".join(n.message for n in app._notifications))

            app._selected_review_id = "CARD-1"
            app._run_frontier_review_bg()
            await pilot.pause(0.2)
            self.assertIn("Reviewing CARD-1 with Claude and Codex", str(app.query_one("#review-status").render()))
            self.assertTrue(app.query_one("#btn-freview").loading)
            release.set()
            await settle()
            self.assertEqual(calls, [("frontier-review", "--scope", "card", "CARD-1")])
            self.assertFalse(app.query_one("#btn-freview").loading)
            self.assertEqual(app.query_one("#review-tabs").active, "rev-tab-frontier")
            self.assertIn("agreed=clean", str(app.query_one("#review-frontier-out").render()))

        self._drive(toy, body)

        async def after_restart(app, pilot, settle) -> None:
            self.assertEqual(app._selected_review_id, "CARD-1")
            self.assertIn("agreed=clean", str(app.query_one("#review-frontier-out").render()))

        self._drive(toy, after_restart)

    def test_accepting_a_blocked_card_needs_a_note_and_lands_it(self) -> None:
        from textual.widgets import TextArea

        toy = self._blocked_and_ready()
        calls: list[tuple] = []

        def fake_cli(*argv):
            calls.append(argv)
            return 0, "CARD-1: merged into main and marked done"

        async def body(app, pilot, settle) -> None:
            app._cli = fake_cli
            accept = app.query_one("#btn-accept-blocked")
            self.assertEqual(app._selected_review_id, "CARD-1")
            self.assertFalse(accept.has_class("hidden"))

            app._open_accept_blocked_modal()
            await pilot.pause(0.1)
            app.screen._ok()  # no note
            await settle()
            self.assertEqual(calls, [])

            app._open_accept_blocked_modal()
            await pilot.pause(0.1)
            app.screen.query_one("#bounce-note", TextArea).text = "ran its tests by hand"
            app.screen._ok()
            await settle()
            self.assertEqual(calls, [("land", "CARD-1", "--accept-blocked", "--note", "ran its tests by hand")])
            self.assertIn("merged into main", str(app.query_one("#review-status").render()))

            app._render_review_card(next(c for c in app._review_cards if c.id == "CARD-2"), app._safe_cfg())
            self.assertTrue(accept.has_class("hidden"), "only a blocked card can be accepted this way")

        self._drive(toy, body)

    def test_a_failed_land_says_why_and_keeps_the_queue(self) -> None:
        toy = self._blocked_and_ready()

        async def body(app, pilot, settle) -> None:
            app._cli = lambda *argv: (2, "CARD-2 is in state 'ready', not 'in_review'")
            app._approved_to_land.append("CARD-2")
            app._land_approved_queue()
            await settle()
            self.assertIn("not 'in_review'", str(app.query_one("#review-status").render()))
            self.assertEqual(app._approved_to_land, ["CARD-2"])

        self._drive(toy, body)


if __name__ == "__main__":
    unittest.main()
