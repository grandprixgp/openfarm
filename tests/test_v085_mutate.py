"""v0.8.5: openfarm's own mutation runner for Python and TypeScript."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openfarm_core import mutate
from openfarm_core.errors import Invalid

MODULE = '''\
"""Clamp things."""


def clamp(x: int, lo: int = 0, hi: int = 10) -> int:
    """Keep x between lo and hi."""
    if x < lo:
        return lo
    return hi if x > hi else x
'''


class PythonMutantTests(unittest.TestCase):
    def test_operators_and_values_but_never_annotations_or_docstrings(self) -> None:
        found = mutate.python_mutants(MODULE, None)
        whats = {m["what"] for m in found}
        self.assertIn("Lt -> GtE", whats)
        self.assertIn("Gt -> LtE", whats)
        self.assertIn("return None", whats)
        self.assertIn("0 -> 1", whats)
        for m in found:
            compile(m["text"], "m.py", "exec")
            self.assertIn('"""Keep x between lo and hi."""', m["text"])
            self.assertIn("x: int", m["text"])

    def test_only_the_lines_asked_for(self) -> None:
        found = mutate.python_mutants(MODULE, {7})
        self.assertEqual({m["line"] for m in found}, {7})


class TypeScriptMutantTests(unittest.TestCase):
    def test_strings_comments_and_generics_are_left_alone(self) -> None:
        src = ('// a < b\nconst s = "x === y";\nconst t: Array<number> = [];\n'
               "export const f = (a: number) => a < 1 && a !== 0;\n")
        found = mutate.typescript_mutants(src, None)
        self.assertEqual(sorted(m["what"] for m in found), ["!== -> ===", "&& -> ||", "< -> <="])
        self.assertTrue(all(m["line"] == 4 for m in found))
        for m in found:
            self.assertIn('"x === y"', m["text"])
            self.assertIn("// a < b", m["text"])
            self.assertIn("Array<number>", m["text"])

    def test_true_and_false_only_as_words(self) -> None:
        found = mutate.typescript_mutants("const trueish = isTrue(true);\n", None)
        self.assertEqual([m["what"] for m in found], ["true -> false"])


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="of-v085-mutate-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "__init__.py").write_text("")
        (self.root / "pkg" / "m.py").write_text(MODULE)
        (self.root / "test_m.py").write_text(
            "import unittest\nfrom pkg.m import clamp\n\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_inside(self):\n        self.assertEqual(clamp(5), 5)\n"
            "    def test_low(self):\n        self.assertEqual(clamp(-1), 0)\n")
        self.out = self.root.parent / (self.root.name + "-out")
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)

    def _run(self, *extra, tests=("test_m",)):
        argv = [sys.executable, "-I", str(Path(mutate.__file__)), "--lang", "python",
                "--out", str(self.out),
                "--test-argv", json.dumps([sys.executable, "-m", "unittest", *tests]),
                *extra, "pkg/m.py"]
        return subprocess.run(argv, cwd=self.root, capture_output=True, text=True, timeout=300)

    def test_counts_missed_mutants_and_puts_the_file_back(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, mutate.EXIT_MISSED, result.stderr)
        self.assertEqual((self.root / "pkg" / "m.py").read_text(), MODULE)
        parsed = mutate.read_outcomes(self.out, result.stdout, result.stderr, result.returncode)
        self.assertGreater(parsed["killed"], 0)
        self.assertGreater(parsed["survived"], 0, "nothing tests the hi bound")
        self.assertEqual(parsed["judged"], parsed["killed"] + parsed["survived"])
        self.assertIn("mutants tested in", result.stdout)
        by_what = {o["what"]: o["outcome"] for o in
                   json.loads((self.out / "outcomes.json").read_text())["outcomes"]}
        # Same size as the `0 -> 1` mutant, written in the same second: judged on
        # its own code, not on a stale .pyc of the one before it.
        self.assertEqual(by_what["10 -> 11"], "missed")
        self.assertEqual(by_what["0 -> 1"], "caught")

    def test_a_failing_baseline_judges_nothing(self) -> None:
        (self.root / "test_m.py").write_text(
            "import unittest\n\n\nclass T(unittest.TestCase):\n"
            "    def test_no(self):\n        self.fail('red')\n")
        result = self._run()
        self.assertEqual(result.returncode, mutate.EXIT_BASELINE)
        with self.assertRaises(Invalid) as caught:
            mutate.read_outcomes(self.out, result.stdout, result.stderr, result.returncode)
        self.assertEqual(caught.exception.code, "mutation-not-run")

    def test_the_cap_is_kept_and_said(self) -> None:
        result = self._run("--max", "2")
        parsed = mutate.read_outcomes(self.out, result.stdout, result.stderr, result.returncode)
        self.assertEqual(parsed["total"], 2)
        self.assertTrue(parsed["capped"])

    def test_with_a_base_only_changed_lines(self) -> None:
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run([*git, "add", "-A"], cwd=self.root, check=True)
        subprocess.run([*git, "commit", "-qm", "base"], cwd=self.root, check=True)
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root,
                              capture_output=True, text=True).stdout.strip()
        text = MODULE.replace("    return hi if x > hi else x\n",
                              "    return hi if x >= hi else x\n")
        (self.root / "pkg" / "m.py").write_text(text)
        self.assertEqual(mutate.changed_lines(self.root, base, "pkg/m.py"), {8})
        self._run("--base", base)
        lines = {o["line"] for o in json.loads((self.out / "outcomes.json").read_text())["outcomes"]}
        self.assertEqual(lines, {8})
        self.assertEqual((self.root / "pkg" / "m.py").read_text(), text)


class PackTests(unittest.TestCase):
    def test_both_packs_now_give_a_mutation_command(self) -> None:
        from openfarm_core.packs.python import PythonPack
        from openfarm_core.packs.typescript import TypeScriptPack

        for pack, lang in ((PythonPack(ruff=False), "python"), (TypeScriptPack(), "typescript")):
            out = Path(tempfile.mkdtemp(prefix="of-v085-mut-out-"))
            self.addCleanup(shutil.rmtree, out, ignore_errors=True)
            pack.mutation_output = out
            pack.mutation_base = "abc123"
            argv = pack.commands("mutation", files=["a.py"], tests=["test_a"])["argv"]
            self.assertIn("--lang", argv)
            self.assertEqual(argv[argv.index("--lang") + 1], lang)
            self.assertEqual(argv[argv.index("--base") + 1], "abc123")
            self.assertEqual(argv[-1], "a.py")
            test_argv = json.loads(argv[argv.index("--test-argv") + 1])
            self.assertTrue(test_argv)


class FlowTests(unittest.TestCase):
    """A Python card with `[gates] mutation = "report"` gets a real kill rate."""

    def test_a_python_card_is_mutated(self) -> None:
        from tests.test_python_e2e import (GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy,
                                           run_cli, script)

        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        cfg = toy.root / ".openfarm" / "config.toml"
        text = cfg.read_text()
        self.assertIn('mutation = "off"', text)
        cfg.write_text(text.replace('mutation = "off"', 'mutation = "report"', 1))
        toy.write_card(
            "CARD-1", title="A full bundle refuses a push", category="pinned-bugfix",
            unit=".", touches=["tally/bounded.py"],
            tests=[{"name": "test_a_full_bundle_refuses_a_push", "file": "tests/test_full.py"}],
            done_text="`Bounded(1).push(8)` after one push raises `FullError`.")
        toy.commit("tally: CARD-1, mutation on")
        sp = script(**{
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
            "CARD-1/implementer": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: a full bundle refuses a push",
            "CARD-1/checker_pass2": "{}"})
        result = run_cli(toy.root, "run", "--card", "CARD-1", "--script", str(sp), "--cards", "1")
        outcome = json.loads(result.stdout)["outcomes"][0]
        self.assertEqual(outcome["final_state"], "in-review", outcome.get("detail"))
        run_dir = next((toy.state_dir / "cards" / "CARD-1").iterdir())
        gate = json.loads(next(run_dir.glob("gates/*/mutation.json")).read_text())
        self.assertNotIn("did not run", gate["detail"], gate)
        self.assertTrue(list(run_dir.glob("gates/mutation/*/outcomes.json")),
                        "the runner's result is kept with the card's evidence")
        self.assertEqual((toy.root / "tally" / "bounded.py").read_text().count("FullError"), 1,
                         "the base checkout was never mutated")


@unittest.skipUnless(shutil.which("node") and shutil.which("git"), "needs node and git")
class TypeScriptFlowTests(unittest.TestCase):
    """A TypeScript card with `mutation = "report"` is mutated by node's own tests."""

    def test_a_typescript_card_is_mutated(self) -> None:
        from tests.test_typescript_pack import GOOD_TS_IMPL, GOOD_TS_TEST, TypeScriptE2ETests

        helper = TypeScriptE2ETests("test_adopt_check_and_run_card_end_to_end")
        helper._cleanups = self._cleanups  # its temp folders go when this test ends
        root, state = helper._prepare_adopted_repo_with_card()
        cfg = root / ".openfarm" / "config.toml"
        text = cfg.read_text()
        self.assertIn('mutation = "off"', text)
        cfg.write_text(text.replace('mutation = "off"', 'mutation = "report"', 1))
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam",
                        "mutation on"], cwd=root, check=True)
        outcome = helper._run_card(root, GOOD_TS_TEST, GOOD_TS_IMPL)
        self.assertEqual(outcome["final_state"], "in-review", outcome.get("detail"))
        run_dir = next((state / "cards" / "CARD-1").iterdir())
        gate = json.loads(next(run_dir.glob("gates/*/mutation.json")).read_text())
        self.assertNotIn("did not run", gate["detail"], gate)
        found = list(run_dir.glob("gates/mutation/*/outcomes.json"))
        self.assertTrue(found)
        self.assertEqual(json.loads(found[0].read_text())["lang"], "typescript")


if __name__ == "__main__":
    unittest.main()
