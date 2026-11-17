"""v0.8.1b checker K1-K4 on a real Python card: counter-tests really run.

The toy is the live bench task `py-slug-ends`, with its planted bug: `slugify`
trims only the trailing dash. The test writer and implementer answer from a
script, and so does the checker, in the second pass's new form. Every case runs the
real interpreter and real git.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adapters.direct import DirectRunner
from openfarm_core.adapters.files import open_card_source
from openfarm_core.adapters.jsonl import MemoryTelemetry
from openfarm_core.adapters.openai_compat import ScriptedBackend
from openfarm_core.board import Board, board_dir_for
from openfarm_core.briefs import BriefBook
from openfarm_core.config import Config, load_config
from openfarm_core.dossier import build_dossier, render_markdown
from openfarm_core.errors import StopCode
from openfarm_core.flow import CardRun
from openfarm_core.packs.base import pack_for
from openfarm_core.state import StateDir
from tests.test_python_e2e import CONFIG
from tests.test_v081b_checker_form import (COUNTER_TEST_HEADER, LIVE_ANSWER,
                                           counter_file, form_answer)
from tests.toy import ToyRepo, _run

SLUG_PY = '''\
"""Turn titles into URL slugs."""

import re


def slugify(text: str) -> str:
    """Lowercase `text` and join its runs of letters and digits with single dashes."""
    lowered = text.lower()
    return re.sub(r"[^a-z0-9]+", "-", lowered)
'''

EXISTING_TEST = '''\
import unittest

from textkit.slug import slugify


class SlugTests(unittest.TestCase):
    def test_words_are_joined_by_dashes(self):
        self.assertEqual(slugify("Hello World"), "hello-world")
'''

NAMED_TEST = '''\
import unittest

from textkit.slug import slugify


class SlugEndTests(unittest.TestCase):
    def test_punctuation_at_the_ends_leaves_no_dash(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")
'''

#: The live run's planted bug, exactly as its implementer answered.
BUGGY = '''\
=== search_replace: textkit/slug.py
<<< SEARCH
    lowered = text.lower()
    return re.sub(r"[^a-z0-9]+", "-", lowered)
>>> REPLACE
    lowered = text.lower()
    return re.sub(r"[^a-z0-9]+", "-", lowered).rstrip("-")
'''

#: The fix the checker asked for. A retry starts again from the base.
FIXED = BUGGY.replace('.rstrip("-")', '.strip("-")')

DONE = """\
- `slugify` never returns a slug that starts or ends with `-`: `slugify("Hello, World!")` \
is `"hello-world"` and `slugify("  --Draft--  ")` is `"draft"`.
- Text with no letters or digits gives `""`: `slugify("!!!")` is `""`.
- Runs of letters and digits inside the text are still joined by single dashes.
"""

#: The two counter-tests the live model wrote, put into the new form as whole files.
LIVE_ONE = "test_leading_and_trailing_punctuation_leaves_no_dashes"
LIVE_TWO = "test_leading_punctuation_before_word_leaves_no_dash"
LIVE_COUNTERS = [
    (LIVE_ONE, counter_file(LIVE_ONE, "  --Draft--  ", "draft")),
    (LIVE_TWO, counter_file(LIVE_TWO, "!!!Hello", "hello")),
]
#: A counter-test the buggy diff passes: `"!!!"` does give `""`.
PASSING = ("test_no_letters_gives_an_empty_slug",
           counter_file("test_no_letters_gives_an_empty_slug", "!!!", ""))
#: Not Python at all.
BROKEN = ("test_broken", "this is not python (\n")
#: Imports a module that does not exist, so unittest cannot collect it.
UNIMPORTABLE = ("test_unimportable",
                "import unittest\nfrom textkit.nowhere import slugify\n\n\n"
                "class T(unittest.TestCase):\n    def test_unimportable(self):\n"
                "        self.assertTrue(True)\n")
#: Runs, but its header names a test the file does not have.
MISNAMED = ("test_not_in_the_file",
            counter_file("test_something_else", "!!!Hello", "hello"))

HB = "<<<HANDBACK\nThe card assumes slugify trims, but the done text is unclear.\n>>>"


def build_slug_toy() -> ToyRepo:
    base = Path(tempfile.mkdtemp(prefix="openfarm-toy-"))
    root = base / "repo"
    state = base / "repo-state"
    root.mkdir()
    state.mkdir()
    repo = ToyRepo(root, state, base=base)
    repo.write("pyproject.toml", '[project]\nname = "textkit"\nversion = "0.1.0"\n')
    repo.write("textkit/__init__.py", "")
    repo.write("textkit/slug.py", SLUG_PY)
    repo.write("tests/__init__.py", "")
    repo.write("tests/test_slug.py", EXISTING_TEST)
    repo.write(".gitignore", "__pycache__/\n")
    config = CONFIG.format(state_dir=state, python=sys.executable)
    config = config.replace('never_touch = ["tally/seam.py"]', "never_touch = []")
    repo.write(".openfarm/config.toml", config)
    _run(["git", "init", "-q", "-b", "main"], root)
    repo.commit("textkit: base")
    repo.write_card(
        "CARD-1", title="A slug has no dash at either end", category="pinned-bugfix",
        unit=".", touches=["textkit/slug.py"],
        tests=[{"name": "test_punctuation_at_the_ends_leaves_no_dash",
                "file": "tests/test_slug_ends.py",
                "asserts": "slugify of Hello, World! is hello-world"}],
        done_text=DONE)
    repo.commit("textkit: CARD-1")
    return repo


def tree_state(worktree: Path) -> dict:
    """Every file in the worktree (ignored ones too) with its hash, HEAD, status."""
    files = {}
    for path in sorted(worktree.rglob("*")):
        rel = path.relative_to(worktree)
        if rel.parts[0] == ".git":
            continue
        if path.is_file():
            files[str(rel)] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir():
            files[str(rel) + "/"] = "dir"
    head = _run(["git", "rev-parse", "HEAD"], worktree).stdout.strip()
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], worktree).stdout.strip()
    status = _run(["git", "status", "--porcelain", "--ignored", "-uall"],
                  worktree).stdout
    return {"files": files, "head": head, "branch": branch, "status": status}


class _SlugHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.toy = build_slug_toy()
        self.addCleanup(self.toy.cleanup)
        self._seq = 0
        self.snapshots: list[tuple[dict, dict]] = []

    def card_run(self, script: dict, *, counter_tests: str = "report",
                 swarm_on: bool = False, on_handback=None) -> CardRun:
        self._seq += 1
        state = StateDir(self.toy.state_dir)
        data = dict(load_config(self.toy.root)._data)
        adapters = dict(data.get("adapters") or {})
        adapters.update({"model": "scripted", "micromanager": "off"})
        gates = dict(data.get("gates") or {})
        gates.update({"counter_tests": counter_tests, "planner": "off"})
        data.update({"adapters": adapters, "gates": gates})
        if swarm_on:
            data["tiers"] = {"fast": {"model": "stub-fast",
                                      "efforts": ["low", "medium", "high"],
                                      "effort_param": "reasoning_effort"}}
            data["swarm"] = {"enabled": True, "coordinator": "code",
                             "supervisor": "off", "default_tier": "fast",
                             "handbacks_per_card": 2}
        cfg = Config(data, self.toy.root)
        run_id = f"run-v081b-checker-{self._seq}"
        state.create_run(run_id)
        layout = state.create_card("CARD-1", run_id)
        board = None
        if swarm_on:
            board = Board(board_dir_for(state, run_id), state_dir=state)
            board.ensure()
        source = open_card_source(cfg, self.toy.state_dir)
        source.prepare()
        card = next(c for c in source.all_cards() if c.id == "CARD-1")
        worktree = self.toy.base / f"wt-{run_id}"
        branch = f"of/{run_id}/CARD-1"
        self.toy.git.create_branch(branch, "main")
        self.toy.git.worktree_add(worktree, branch)
        self.addCleanup(shutil.rmtree, worktree, True)
        pack = pack_for("python")
        pack.configure(worktree)
        book = BriefBook(worktree, cfg.never_touch, cfg.forbidden_files)
        book.pack = pack
        run = CardRun(card, cfg, state, layout, worktree, DirectRunner(worktree),
                      pack, ScriptedBackend(script), book, run_id,
                      MemoryTelemetry(), board=board, on_handback=on_handback)
        real = run._run_counter_tests

        def watched(tests):
            before = tree_state(run.worktree)
            try:
                return real(tests)
            finally:
                self.snapshots.append((before, tree_state(run.worktree)))

        run._run_counter_tests = watched
        return run

    @staticmethod
    def script(pass2, implementer=BUGGY, **extra) -> dict:
        script = {
            "CARD-1/test_writer": "=== whole_file: tests/test_slug_ends.py\n" + NAMED_TEST,
            "CARD-1/implementer": implementer,
            "CARD-1/checker_pass1": "RESTATEMENT: slugs have no dash at either end",
        }
        if isinstance(pass2, dict):
            script.update(pass2)
        else:
            script["CARD-1/checker_pass2"] = pass2
        script.update(extra)
        return script

    def gate(self, run: CardRun, name: str):
        found = [g for g in run.gates if g.name == name]
        self.assertTrue(found, f"no {name} gate in {[g.name for g in run.gates]}")
        return found[-1]

    def dossier(self, run: CardRun, result: dict) -> tuple[dict, str]:
        payload = build_dossier(run, result["final_state"])
        return payload, render_markdown(payload)

    def assert_worktree_untouched(self) -> None:
        self.assertTrue(self.snapshots, "no counter-test ran")
        for before, after in self.snapshots:
            self.assertEqual(before, after)

    def pass2_calls(self, run: CardRun) -> list[dict]:
        return [c for c in run.backend.calls if c["role"] == "checker_pass2"]


class CounterTestsRunTests(_SlugHarness):
    """K3 and K4 with the Python pack (stdlib unittest)."""

    def test_the_live_counter_tests_fail_on_the_planted_bug(self) -> None:
        run = self.card_run(self.script(form_answer(tests=LIVE_COUNTERS)))
        result = run.run()
        # report: recorded, and the card goes on.
        self.assertEqual(result["final_state"], "in-review", result.get("detail"))
        self.assertEqual(result["counter_tests"]["written"], 2)
        self.assertEqual(result["counter_tests"]["failed"], 2)
        self.assertEqual(result["counter_tests"]["unreadable"], 0)
        outcomes = run.checker_pass2["counter_results"]
        self.assertEqual([(r["name"], r["outcome"]) for r in outcomes],
                         [(LIVE_ONE, "failed"), (LIVE_TWO, "failed")])
        self.assertIn("'-draft' != 'draft'", outcomes[0]["output_head"])
        kept = json.loads((run.card_layout / "gates" / "counter-test-1.json").read_text())
        self.assertEqual(kept["outcome"], "failed")
        self.assertIn("-draft", kept["output"])
        self.assert_worktree_untouched()
        # Nothing the counter-tests did is on the branch.
        shown = _run(["git", "log", "--name-only", "--format=", "main..HEAD"],
                     run.worktree).stdout
        self.assertNotIn("openfarm_counter", shown)

        gate = self.gate(run, "checker-pass2")
        self.assertEqual(gate.result, "pass")
        self.assertIn("checker asks for changes", gate.detail)
        self.assertEqual(gate.evidence["checker"]["verdict"], "changes-requested")
        self.assertIn("trims only the right end", gate.evidence["checker"]["reasons"])

        payload, text = self.dossier(run, result)
        self.assertEqual(payload["6_checker"]["pass2"]["verdict"], "changes-requested")
        head = text.split("## 1. Risk")[0]
        self.assertIn("Checker, second pass: changes requested; counter-tests: "
                      "2 failed on the diff", head)
        self.assertIn("Second pass verdict: **changes requested**", text)
        self.assertIn(f"`{LIVE_ONE}` (tests/test_openfarm_counter_1.py): "
                      "**failed on the diff**", text)
        self.assertIn("'-draft' != 'draft'", text)
        self.assertIn("trims only the right end", text)
        self.assertIn("Traceability:", text)
        self.assertIn("Traced edge cases:", text)

    def test_the_brief_says_where_the_counter_tests_go_and_how_they_run(self) -> None:
        run = self.card_run(self.script(form_answer("accept")))
        run.run()
        brief = self.pass2_calls(run)[0]["messages"][0]["content"]
        self.assertIn("counter-test 1: tests/test_openfarm_counter_1.py", brief)
        self.assertIn("Python's stdlib `unittest`", brief)
        self.assertIn(f"{COUNTER_TEST_HEADER} <test name>", brief)

    def test_passed_failed_and_dropped_are_each_counted(self) -> None:
        run = self.card_run(self.script(form_answer(tests=[LIVE_COUNTERS[0], PASSING])))
        result = run.run()
        self.assertEqual(result["final_state"], "in-review", result.get("detail"))
        summary = result["counter_tests"]
        self.assertEqual((summary["written"], summary["failed"], summary["passed"]),
                         (2, 1, 1))
        self.assert_worktree_untouched()
        _, text = self.dossier(run, result)
        self.assertIn(f"`{PASSING[0]}` (tests/test_openfarm_counter_2.py): passed", text)

    def test_counter_tests_that_cannot_compile_or_be_collected_are_dropped(self) -> None:
        run = self.card_run(self.script(form_answer(tests=[BROKEN, UNIMPORTABLE])))
        result = run.run()
        self.assertEqual(result["final_state"], "in-review", result.get("detail"))
        summary = result["counter_tests"]
        self.assertEqual((summary["written"], summary["dropped_uncompiled"],
                          summary["failed"], summary["passed"]), (2, 2, 0, 0))
        outcomes = run.checker_pass2["counter_results"]
        self.assertEqual([r["outcome"] for r in outcomes], ["dropped", "dropped"])
        self.assert_worktree_untouched()
        _, text = self.dossier(run, result)
        self.assertIn("`test_broken` (tests/test_openfarm_counter_1.py): dropped, "
                      "it did not compile or could not be collected", text)
        self.assertIn("changes requested, but no counter-test fails on the diff", text)

    def test_a_counter_test_the_runner_does_not_report_is_not_run(self) -> None:
        run = self.card_run(self.script(form_answer(tests=[MISNAMED])))
        result = run.run()
        self.assertEqual(result["counter_tests"]["not_run"], 1)
        _, text = self.dossier(run, result)
        self.assertIn("the runner reported no test named 'test_not_in_the_file'", text)
        self.assert_worktree_untouched()

    def test_reject_bounces_the_card_with_the_counter_test_output(self) -> None:
        run = self.card_run(self.script(
            form_answer(tests=LIVE_COUNTERS), implementer=BUGGY,
            **{"CARD-1/implementer/2": FIXED}),
            counter_tests="reject")
        result = run.run()
        self.assertEqual(result["final_state"], "in-review", result.get("detail"))
        impl = [c for c in run.backend.calls if c["role"] == "implementer"]
        self.assertEqual(len(impl), 2)
        bounce_brief = impl[1]["messages"][0]["content"]
        self.assertIn(LIVE_ONE, bounce_brief)
        self.assertIn("-draft", bounce_brief)
        self.assertIn(".strip(\"-\")",
                      (run.worktree / "textkit" / "slug.py").read_text())
        # The same two counter-tests, run again on the fixed diff, pass.
        self.assertEqual([r["outcome"] for r in run.checker_pass2["counter_results"]],
                         ["passed", "passed"])
        self.assert_worktree_untouched()

    def test_reject_blocks_a_card_whose_counter_tests_still_fail(self) -> None:
        run = self.card_run(self.script(form_answer(tests=LIVE_COUNTERS)),
                            counter_tests="reject")
        result = run.run()
        self.assertEqual(result["final_state"], "blocked")
        self.assertNotEqual(result["stop_code"], "")
        self.assertGreaterEqual(result["counter_tests"]["failed"], 1)
        self.assert_worktree_untouched()

    def test_counter_tests_in_the_old_json_form_still_run(self) -> None:
        old = json.dumps({"traceability": [], "edge_cases": [], "counter_tests": [
            {"name": name, "file": "", "content": content}
            for name, content in LIVE_COUNTERS]})
        run = self.card_run(self.script(old))
        result = run.run()
        self.assertEqual(result["final_state"], "in-review", result.get("detail"))
        self.assertEqual(result["counter_tests"]["failed"], 2)
        # A counter-test run leaves no `__pycache__` entry, no file, no commit.
        self.assert_worktree_untouched()

    def test_a_verdict_alone_never_blocks_a_card(self) -> None:
        run = self.card_run(self.script(form_answer(tests=[PASSING])),
                            counter_tests="reject")
        result = run.run()
        self.assertEqual(result["final_state"], "in-review", result.get("detail"))
        self.assertIn("no counter-test fails on the diff",
                      self.gate(run, "checker-pass2").detail)


class UnreadableSecondPassTests(_SlugHarness):
    """K1: an answer that cannot be read is asked for once more, then said plainly."""

    def test_the_live_answer_is_asked_for_again_and_the_second_answer_is_used(self) -> None:
        run = self.card_run(self.script({
            "CARD-1/checker_pass2/2": LIVE_ANSWER,
            "CARD-1/checker_pass2/3": form_answer(tests=LIVE_COUNTERS)}))
        result = run.run()
        self.assertEqual(result["final_state"], "in-review", result.get("detail"))
        calls = self.pass2_calls(run)
        self.assertEqual([c["attempt"] for c in calls], [2, 3])
        again = calls[1]["messages"][0]["content"]
        self.assertIn("could not be read: it has test code in code fences", again)
        self.assertIn(f"{COUNTER_TEST_HEADER} <test name>", again)
        self.assertEqual(result["counter_tests"]["failed"], 2)
        self.assertTrue(run.checker_pass2["asked_again"])
        self.assertTrue((run.card_layout / "responses" / "checker-pass2-again.json").exists())

    def test_report_records_plainly_that_nothing_was_checked(self) -> None:
        run = self.card_run(self.script(LIVE_ANSWER))
        result = run.run()
        self.assertEqual(result["final_state"], "in-review", result.get("detail"))
        self.assertEqual(len(self.pass2_calls(run)), 2)
        self.assertEqual(result["counter_tests"]["unreadable"], 1)
        self.assertEqual(result["counter_tests"]["written"], 0)
        gate = self.gate(run, "checker-pass2")
        self.assertIn("could not be read", gate.detail)
        self.assertIn("nothing was checked", gate.detail)
        self.assertFalse(gate.evidence["checker"]["readable"])
        payload, text = self.dossier(run, result)
        self.assertIn("Checker, second pass: could not be read", text.split("## 1.")[0])
        self.assertIn("Second pass: **could not be read**", text)
        self.assertTrue(any("could not be read" in line
                            for line in payload["8_not_checked"]))

    def test_reject_fails_the_checker_gate_when_nothing_could_be_read(self) -> None:
        run = self.card_run(self.script("I looked, it seems fine."),
                            counter_tests="reject")
        result = run.run()
        self.assertEqual(result["final_state"], "blocked")
        self.assertEqual(result["stop_code"], StopCode.CHECKER_REJECT.value)
        self.assertIn("could not be read", result["detail"])
        self.assertEqual(self.gate(run, "checker-pass2").result, "fail")

    def test_the_old_empty_answer_is_still_read_once(self) -> None:
        run = self.card_run(self.script("{}"))
        result = run.run()
        self.assertEqual(result["final_state"], "in-review", result.get("detail"))
        self.assertEqual(len(self.pass2_calls(run)), 1)
        self.assertEqual(result["counter_tests"]["written"], 0)
        _, text = self.dossier(run, result)
        self.assertIn("no verdict stated (old answer form)", text)


class SwarmHandbackStillFirstTests(_SlugHarness):
    def test_a_hand_back_on_the_second_ask_is_read_before_anything_else(self) -> None:
        run = self.card_run(self.script({
            "CARD-1/checker_pass2/2": LIVE_ANSWER,
            "CARD-1/checker_pass2/3": HB}), swarm_on=True)
        result = run.run()
        self.assertEqual(result["final_state"], "blocked")
        self.assertEqual(result["stop_code"], StopCode.HANDED_BACK.value)
        self.assertEqual([h["role"] for h in run.handbacks], ["checker_pass2"])
        self.assertFalse([g for g in run.gates if g.name == "checker-pass2"])

    def test_a_hand_back_is_read_before_the_form(self) -> None:
        run = self.card_run(self.script(HB), swarm_on=True)
        result = run.run()
        self.assertEqual(result["stop_code"], StopCode.HANDED_BACK.value)
        self.assertEqual(len(self.pass2_calls(run)), 1)


if __name__ == "__main__":
    unittest.main()
