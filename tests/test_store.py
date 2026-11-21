"""R2.1: the cards branch is the one home for cards, the spec, the goals and the decisions log."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openfarm_core.adapters.files import render_card_file
from openfarm_core.bootstrap import initialize_project
from openfarm_core.config import load_config
from openfarm_core.errors import Refused
from openfarm_core.store import Store, log_decisions, read_doc

REPO = Path(__file__).resolve().parent.parent


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


def _header(cid: str, **extra) -> dict:
    return {"id": cid, "title": f"Task {cid}", "state": "ready", "category": "small-feature",
            "touches": ["tally/core.py"], "reference": "tally/core.py", "depends": [],
            "tests": [{"name": f"test_{cid.lower().replace('-', '_')}", "file": "tests/test_core.py"}], **extra}


BODY = "## Done\n\n- `tally.core.total([1, 2])` returns `3` and raises `ValueError` on an empty list.\n"


class _Project(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"OPENFARM_PROJECTS_FILE": str(base / "projects.json"),
                                               "GIT_AUTHOR_NAME": "Ada", "GIT_COMMITTER_NAME": "Ada",
                                               "GIT_AUTHOR_EMAIL": "ada@example.invalid",
                                               "GIT_COMMITTER_EMAIL": "ada@example.invalid"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = base / "tally"
        initialize_project("tally", directory=self.root, idea="A tally counter", language="python",
                           state_dir=base / "state", secrets_dir=base / "secrets", commit=True)
        _git(self.root, "config", "user.name", "Ada")
        _git(self.root, "config", "user.email", "ada@example.invalid")
        (self.root / "tally").mkdir(exist_ok=True)
        (self.root / "tally" / "core.py").write_text("def total(xs):\n    return sum(xs)\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "core")
        self.cfg = load_config(self.root)
        self.store = Store(self.root, self.cfg)


class HomeTests(_Project):
    def test_a_new_project_keeps_nothing_of_openfarms_plan_on_main(self) -> None:
        tracked = _git(self.root, "ls-files").splitlines()
        self.assertFalse([p for p in tracked if p.startswith("cards/")], tracked)
        self.assertNotIn(".openfarm/PROJECT_SPEC.md", tracked)
        self.assertTrue(self.store.src.migrated)
        self.assertIn("A tally counter", read_doc(self.root, self.cfg, "spec"))
        self.assertTrue(str(self.store.doc_path("spec")).startswith(str(self.cfg.state_dir)))

    def test_cards_and_documents_never_commit_to_main(self) -> None:
        head = _git(self.root, "rev-parse", "HEAD")
        self.store.change_card("W1-01", "create", header=_header("W1-01"), body=BODY, why="first task")
        self.store.write_doc("spec", "# Tally\n\nA counter.\nIt counts.\n", why="spec written")
        self.store.log_decision("idea", "A tally counter")
        self.assertEqual(_git(self.root, "rev-parse", "HEAD"), head)
        self.assertEqual(_git(self.root, "status", "--porcelain").strip(), "")
        self.assertEqual(_git(self.root, "log", "--format=%s").count("commit openfarm spec and cards"), 0)

    def test_nothing_reads_cards_from_main_once_migrated(self) -> None:
        """Only `legacy_base_cards` may build the path to main's cards folder."""
        pattern = re.compile(r"""(root|repo|self\.repo|self\.root|target_dir)\s*/\s*["']cards["']""")
        hits = []
        for path in sorted([*(REPO / "openfarm_core").rglob("*.py"), *(REPO / "openfarm_tui").rglob("*.py")]):
            rel = path.relative_to(REPO).as_posix()
            # bench builds its own throwaway repos; state.py's folder is run records in the state dir
            if rel in ("openfarm_core/bench.py", "openfarm_core/state.py"):
                continue
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if pattern.search(line):
                    hits.append(f"{rel}:{number}: {line.strip()}")
        self.assertEqual(hits, [], "\n".join(hits))
        # and the one reader is switched off by the migrated mark
        (self.root / "cards").mkdir()
        (self.root / "cards" / "W9-01.md").write_text(render_card_file(_header("W9-01"), BODY))
        self.assertIsNone(self.store.src.legacy_base_cards())
        self.assertNotIn("W9-01", self.store.states())


class ChangeTests(_Project):
    def test_every_change_lands_in_the_cards_history(self) -> None:
        self.store.change_card("W1-01", "create", header=_header("W1-01"), body=BODY, why="first task")

        def _retitle(header: dict, body: str) -> tuple[dict, str]:
            header["title"] = "Count things"
            return header, body

        self.store.change_card("W1-01", "edit", apply=_retitle, why="clearer name")
        history = self.store.history("W1-01")
        self.assertEqual([h["kind"] for h in history], ["create", "edit"])
        self.assertEqual(history[1]["by"], "person/Ada")
        self.assertEqual(history[1]["why"], "clearer name")
        self.assertEqual(history[1]["changed"]["title"], ["Task W1-01", "Count things"])
        self.assertTrue(history[1]["at"])

    def test_an_id_is_never_used_twice_even_after_a_drop(self) -> None:
        first = self.store.next_card_id(1)
        self.store.change_card(first, "create", header=_header(first), body=BODY)
        self.store.change_card(first, "drop", why="not needed")
        second = self.store.next_card_id(1)
        self.assertEqual((first, second), ("W1-01", "W1-02"))
        self.assertEqual(self.store.src.reserve_id("W1-01"), "W1-03")  # asked for a used one
        self.assertEqual(self.store.src.reserve_id("W1-09"), "W1-09")
        self.assertEqual(self.store.next_card_id(1), "W1-10")
        with self.assertRaises(Refused):
            self.store.change_card("W1-01", "create", header=_header("W1-01"), body=BODY)

    def test_a_card_that_fails_the_checks_is_parked_with_its_reasons(self) -> None:
        bad = _header("W1-01", depends=["W1-77"])
        outcome = self.store.change_card("W1-01", "create", header=bad, body=BODY)
        self.assertFalse(outcome["ok"])
        header, _body = self.store.read("W1-01")
        self.assertEqual(header["state"], "backlog")
        self.assertIn("W1-77, which doesn't exist", " ".join(header["preflight"]))

        def _fix(h: dict, b: str) -> tuple[dict, str]:
            h["depends"] = []
            return h, b

        self.assertTrue(self.store.change_card("W1-01", "edit", apply=_fix, why="no such task")["ok"])
        header, _body = self.store.read("W1-01")
        self.assertEqual(header["state"], "ready")
        self.assertNotIn("preflight", header)


class DecisionsTests(_Project):
    def test_the_log_only_grows_and_the_idea_stays_first(self) -> None:
        self.store.log_decision("idea", "A tally counter for the web")
        self.store.log_decision("answer", "Postgres", question="Where is it stored?")
        _path, text = log_decisions(self.root, self.cfg, [("iteration", "", "I only see the photo")])
        self.assertLess(text.index("A tally counter for the web"), text.index("Postgres"))
        self.assertLess(text.index("Postgres"), text.index("I only see the photo"))
        self.assertIn("· The idea", text)
        self.assertIn("· Where is it stored?", text)
        self.assertIn("· Something's wrong", text)
        self.assertFalse(self.store.log_decision("answer", "Postgres", question="Where is it stored?"))
        self.assertEqual(self.store.read_doc("decisions").count("Postgres"), 1)


class MigrationTests(_Project):
    """A project from before: cards and the spec committed on main, copies on the cards branch."""

    def setUp(self) -> None:
        super().setUp()
        self.store.change_card("W1-01", "create", header=_header("W1-01"), body=BODY)
        self.store.change_card("W1-02", "create", header=_header("W1-02"), body=BODY)
        meta = self.store.src.meta()
        meta.pop("migrated", None)
        self.store.src.save_meta(meta, "test: as it was before")
        (self.root / "cards").mkdir()
        (self.root / "cards" / "W1-01.md").write_text(render_card_file(_header("W1-01"), BODY))
        (self.root / "cards" / "W1-02.md").write_text(
            render_card_file(_header("W1-02", title="Edited on main"), BODY))
        (self.root / "cards" / "W1-03.md").write_text(render_card_file(_header("W1-03"), BODY))
        (self.root / ".openfarm" / "PROJECT_SPEC.md").write_text("# Tally\n\nThe old spec.\nOn main.\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "chore: commit openfarm spec and cards")
        self.store = Store(self.root, self.cfg)

    def test_differences_are_shown_and_the_user_picks(self) -> None:
        self.assertFalse(self.store.src.migrated)
        status = self.store.migration()
        self.assertTrue(status["needed"])
        self.assertEqual([d["id"] for d in status["differences"]], ["W1-02"])
        self.assertIn("Edited on main", status["differences"][0]["main"])
        self.assertIn("Task W1-02", status["differences"][0]["state"])
        with self.assertRaises(Refused) as caught:
            self.store.migrate()
        self.assertEqual(caught.exception.code, "migration-choice-needed")
        self.assertTrue((self.root / "cards" / "W1-02.md").is_file())  # nothing lost by default

        before = int(_git(self.root, "rev-list", "--count", "HEAD"))
        result = self.store.migrate({"W1-02": "main"})
        self.assertTrue(result["migrated"])
        self.assertEqual(int(_git(self.root, "rev-list", "--count", "HEAD")), before + 1)  # one commit
        self.assertFalse((self.root / "cards").exists())
        tracked = _git(self.root, "ls-files").splitlines()
        self.assertFalse([p for p in tracked if p.startswith("cards/") or p.endswith("PROJECT_SPEC.md")])
        self.assertEqual(self.store.read("W1-02")[0]["title"], "Edited on main")
        self.assertIn("W1-03", self.store.states())  # a card only main had comes along
        self.assertIn("The old spec.", self.store.read_doc("spec"))
        self.assertFalse(self.store.migration()["needed"])

    def test_keeping_the_cards_branch_text(self) -> None:
        self.store.migrate({"W1-02": "state"})
        self.assertEqual(self.store.read("W1-02")[0]["title"], "Task W1-02")

    def test_the_spec_is_read_from_main_until_the_move(self) -> None:
        self.assertIn("The old spec.", read_doc(self.root, self.cfg, "spec"))


class CommandLineTests(_Project):
    """`openfarm card` reaches the same store, and the farm's own commands leave history too."""

    def _cli(self, *argv: str) -> tuple[int, str]:
        import contextlib
        import io

        from openfarm_core.cli import main

        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(["--repo", str(self.root), *argv])
        return code, out.getvalue()

    def test_add_show_history_and_skip(self) -> None:
        card = self.root.parent / "task.md"
        card.write_text(render_card_file(_header("W1-01"), BODY))
        code, out = self._cli("card", "add", str(card), "--why", "first task")
        self.assertEqual((code, out.strip()), (0, "W1-01: added, ready"))
        self.assertEqual(_git(self.root, "status", "--porcelain").strip(), "")  # nothing landed on main

        code, out = self._cli("card", "add", str(card))  # the same file again gets the next number
        self.assertIn("W1-02: added", out)
        self.assertIn('title = "Task W1-01"', self._cli("card", "show", "W1-02")[1])

        self.assertEqual(self._cli("card", "skip", "W1-02", "--why", "a copy")[0], 0)
        history = self._cli("card", "history", "W1-02")[1]
        self.assertIn("person/Ada  create  added by hand", history)
        self.assertIn("person/Ada  skip  a copy", history)
        self.assertEqual(self.store.read("W1-02")[0]["state"], "dropped")

    def test_a_task_that_fails_the_checks_says_why_when_added(self) -> None:
        card = self.root.parent / "task.md"
        card.write_text(render_card_file(_header("W1-01", depends=["W1-77"]), BODY))
        code, out = self._cli("card", "add", str(card))
        self.assertIn("W1-01: added, backlog", out)
        self.assertIn("not ready (depends): it waits for W1-77, which doesn't exist", out)

    def test_release_is_in_the_history(self) -> None:
        self.store.change_card("W1-01", "create", header=_header("W1-01"), body=BODY)
        claim = self.store.src.claim("W1-01", holder="lane")
        self.store.src.move("W1-01", claim["lease_id"], "blocked", stop_code="attempts")
        self.assertEqual(self._cli("release", "W1-01", "--reason", "try again")[0], 0)
        last = self.store.history("W1-01")[-1]
        self.assertEqual((last["kind"], last["why"], last["by"]), ("release", "try again", "person/Ada"))


class MigrateCommandTests(MigrationTests):
    def test_the_command_shows_both_versions_and_waits_for_a_choice(self) -> None:
        code, out = CommandLineTests._cli(self, "card", "migrate")
        self.assertNotEqual(code, 0)
        self.assertIn("W1-02 differs.", out)
        self.assertIn("--- on main ---", out)
        self.assertIn("Edited on main", out)
        self.assertIn("--take W1-02=main|state", out)
        self.assertTrue((self.root / "cards").is_dir())
        self.assertEqual(CommandLineTests._cli(self, "card", "migrate", "--take", "W1-02=state")[0], 0)
        self.assertFalse((self.root / "cards").exists())
        self.assertEqual(Store(self.root, self.cfg).read("W1-02")[0]["title"], "Task W1-02")


if __name__ == "__main__":
    unittest.main()
