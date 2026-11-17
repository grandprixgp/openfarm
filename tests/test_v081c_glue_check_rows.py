"""v0.8.1c glue, G8: `check --baseline` reads named tests the way fail-before does.

The same test file is put on base, then judged twice: once by `check --baseline`
and once by a scripted run whose test writer writes that same file, so its
fail-before gate sees the same rows. Where fail-before refuses the card, check
must not call it eligible, and where fail-before passes, check must pass it too.

The difference found: a named test that is on base but skipped, or marked as an
expected failure, in every row (`ignored`) was eligible in check, while fail-before
refuses it ("an ignored test never ran on base, so it proves nothing").

The pytest cases run the real pytest and skip themselves when the Python running
the suite cannot import it (use `/var/tmp/openfarm-v081/tools/venv/bin/python`);
the unittest case runs everywhere.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.human_cli import Style, format_human_strict
from tests.test_python_e2e import GOOD_IMPLEMENTATION, build_python_toy, run_cli, script
from tests.test_v081b_packs_pytest import PYTEST_PYPROJECT

HAS_PYTEST = importlib.util.find_spec("pytest") is not None

HEAD = "import pytest\n\nfrom tally.bounded import Bounded, FullError\n\n\n"
FAILS = ("    bundle = Bounded(1)\n    bundle.push(1)\n"
         "    with pytest.raises(FullError):\n        bundle.push(2)\n")
PASSES = "    assert Bounded(c).capacity == c\n"
PARAM = "@pytest.mark.parametrize('c', [1, 2])\ndef test_x(c):\n"

#: name -> (the card's test file on base, known_failing, fail-before's result)
PYTEST_CASES = {
    "every case skipped": (HEAD + "@pytest.mark.skip\n" + PARAM + PASSES, [], "fail"),
    "every case xfail": (HEAD + "@pytest.mark.xfail(strict=False)\n" + PARAM + FAILS,
                         [], "fail"),
    "a case passing and a case skipped": (
        HEAD + "@pytest.mark.parametrize('c', [1, pytest.param(2, marks=pytest.mark.skip)])\n"
        "def test_x(c):\n" + PASSES, [], "fail"),
    "every case passing": (HEAD + PARAM + PASSES, [], "fail"),
    "one case known failing, one passing": (
        HEAD + PARAM + "    if c == 1:\n" + "\n".join("    " + line for line in
                                                     FAILS.splitlines()) + "\n",
        ["test_x[1]"], "fail"),
    "every case known failing": (HEAD + PARAM + FAILS, ["test_x[1]", "test_x[2]"], "pass"),
}

UNITTEST_SKIPPED = '''\
import unittest

from tally.bounded import Bounded, FullError


class FullTests(unittest.TestCase):
    @unittest.skip("not yet")
    def test_x(self):
        bundle = Bounded(1)
        bundle.push(7)
        with self.assertRaises(FullError):
            bundle.push(8)
'''


class _Toy:
    def _toy(self, content: str, known: list[str], pytest_runner: bool):
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        if pytest_runner:
            toy.write("pyproject.toml", PYTEST_PYPROJECT)
        if known:
            config = toy.root / ".openfarm" / "config.toml"
            config.write_text(config.read_text().replace(
                "known_failing = []", "known_failing = " + json.dumps(known)))
        toy.write("tests/test_full.py", content)
        toy.commit("tally: the card's test is already on base")
        toy.write_card(
            "CARD-1", title="A full bundle refuses a push", category="pinned-bugfix",
            unit=".", touches=["tally/bounded.py"],
            tests=[{"name": "test_x", "file": "tests/test_full.py",
                    "asserts": "push raises FullError"}],
            done_text="`Bounded(1).push(8)` after one push raises `FullError`.")
        toy.commit("tally: CARD-1")
        return toy

    def _check(self, toy) -> dict:
        result = run_cli(toy.root, "check", "CARD-1", "--baseline")
        return json.loads(result.stdout)

    def _fail_before(self, toy, content: str) -> dict:
        path = script(**{
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + content,
            "CARD-1/implementer": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: a full bundle refuses a push",
            "CARD-1/checker_pass2": "{}",
        })
        self.addCleanup(shutil.rmtree, path.parent, True)
        result = run_cli(toy.root, "run", "--card", "CARD-1", "--script", str(path),
                         "--cards", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = next((toy.state_dir / "cards" / "CARD-1").iterdir())
        found = sorted(run_dir.glob("gates/*/fail-before*.json"))
        self.assertTrue(found, "no fail-before gate recorded")
        return json.loads(found[-1].read_text())


class UnittestIgnoredOnBaseTests(_Toy, unittest.TestCase):
    def test_a_skipped_named_test_on_base_is_not_eligible(self) -> None:
        toy = self._toy(UNITTEST_SKIPPED, [], pytest_runner=False)
        payload = self._check(toy)
        baseline = payload["baseline"]
        self.assertEqual(baseline["named"], {"test_x": "ignored"})
        self.assertFalse(baseline["ok"], baseline)
        self.assertEqual(baseline["ignored_on_base"], ["test_x"])
        self.assertIn("skipped or expected to fail on base", baseline["error"])
        self.assertFalse(payload["eligible"])
        text = format_human_strict("check", payload, style=Style(enabled=False))
        self.assertIn("skipped or expected to fail, proves nothing", text)
        # fail-before refuses the same file.
        self.assertEqual(self._fail_before(toy, UNITTEST_SKIPPED)["result"], "fail")

    def test_the_human_note_for_a_test_not_on_base_yet(self) -> None:
        payload = {"card": "CARD-1", "eligible": True, "failures": [],
                   "baseline": {"ok": True, "named": {"test_x": "not-run",
                                                      "test_y": "mixed"}}}
        text = format_human_strict("check", payload, style=Style(enabled=False))
        self.assertIn("not on base yet", text)
        self.assertIn("fails in some rows only, proves nothing", text)


@unittest.skipUnless(HAS_PYTEST, "the Python running the suite cannot import pytest")
class PytestRowsAgreeWithFailBeforeTests(_Toy, unittest.TestCase):
    def test_check_and_fail_before_agree_on_parameter_rows(self) -> None:
        for label, (content, known, gate_result) in PYTEST_CASES.items():
            with self.subTest(label):
                toy = self._toy(content, known, pytest_runner=True)
                baseline = self._check(toy)["baseline"]
                gate = self._fail_before(toy, content)
                self.assertEqual(gate["result"], gate_result, gate["detail"])
                self.assertEqual(baseline["ok"], gate_result == "pass", baseline)
                rows = gate["evidence"]["rows"]["test_x"]
                statuses = {row["status"] for row in rows}
                want = statuses.pop() if len(statuses) == 1 else "mixed"
                self.assertEqual(baseline["named"], {"test_x": want})


if __name__ == "__main__":
    unittest.main()
