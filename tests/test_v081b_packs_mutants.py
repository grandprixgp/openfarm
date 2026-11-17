"""The mutation gate reads the real cargo-mutants (v0.8.1 fix wave 2, P2).

cargo-mutants 27 prints text whatever `--json` says (that flag is for `--list`), and
the old parser read a real run of 7 caught and 6 missed out of 13 as 1 killed, 6
survived, 7 total. It also wrote `mutants.out/` into the card's worktree. The pack
now passes `--output` (a folder outside the worktree), reads
`mutants.out/outcomes.json`, falls back to the closing summary line, and reads the
exit code for what cargo-mutants documents it to mean.

The `Real*` cases run the installed cargo-mutants and skip themselves without it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.errors import Invalid
from openfarm_core.gates.checks import FAIL, PASS, mutation_gate
from openfarm_core.packs.rust import RustPack

HAS_MUTANTS = (shutil.which("cargo") is not None
               and shutil.which("cargo-mutants") is not None)

#: What cargo-mutants 27.1.0 printed for the tinymut crate below (live notes).
REAL_STDOUT = """\
Found 13 mutants to test
ok       Unmutated baseline
MISSED   src/lib.rs:3:10: replace > with >= in clamp_add
MISSED   src/lib.rs:11:5: replace is_even -> bool with true
MISSED   src/lib.rs:11:5: replace is_even -> bool with false
MISSED   src/lib.rs:11:11: replace == with != in is_even
MISSED   src/lib.rs:11:7: replace % with / in is_even
MISSED   src/lib.rs:11:7: replace % with + in is_even
13 mutants tested: 6 missed, 7 caught
"""

#: The top level of the `outcomes.json` the same run wrote (per-mutant entries cut).
REAL_OUTCOMES = {"total_mutants": 13, "missed": 6, "caught": 7, "timeout": 0,
                 "unviable": 0, "success": 0, "cargo_mutants_version": "27.1.0",
                 "outcomes": []}

TINYMUT_TOML = """\
[package]
name = "tinymut"
version = "0.1.0"
edition = "2021"

[dependencies]
"""

TINYMUT_LIB = """\
pub fn clamp_add(a: i32, b: i32, max: i32) -> i32 {
    let s = a + b;
    if s > max {
        max
    } else {
        s
    }
}

pub fn is_even(n: i32) -> bool {
    n % 2 == 0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn adds() {
        assert_eq!(clamp_add(1, 2, 10), 3);
    }

    #[test]
    fn clamps() {
        assert_eq!(clamp_add(8, 5, 10), 10);
    }
}
"""

#: `i += 1` mutated to `i *= 1` never ends: cargo-mutants' timeout case.
LOOPING_LIB = """\
pub fn steps(n: u32) -> u32 {
    let mut i = 0;
    let mut k = 0;
    while i < n {
        i += 1;
        k += 2;
    }
    k
}

#[cfg(test)]
mod tests {
    #[test]
    fn four() {
        assert_eq!(super::steps(2), 4);
    }
}
"""


class MutationCommandTests(unittest.TestCase):
    def test_the_run_has_no_json_flag_and_one_file_flag_each(self) -> None:
        pack = RustPack()
        argv = pack.commands("mutation", files=["src/a.rs", "src/b.rs"])["argv"]
        self.assertEqual(argv[:2], ["cargo", "mutants"])
        self.assertNotIn("--json", argv)
        self.assertIn("--no-times", argv)
        self.assertEqual(argv[argv.index("--colors") + 1], "never")
        self.assertEqual(argv[-4:], ["--file", "src/a.rs", "--file", "src/b.rs"])
        out = Path(argv[argv.index("--output") + 1])
        self.assertTrue(out.is_dir())
        pack.read_mutation(REAL_STDOUT, "", 2)
        self.assertFalse(out.exists(), "a temporary output folder is removed")

    def test_the_flow_folder_gets_a_fresh_numbered_run(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="openfarm-mut-"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        pack = RustPack()
        pack.mutation_output = base / "gates" / "mutation"
        first = pack.commands("mutation", files=["src/a.rs"])["argv"]
        out1 = Path(first[first.index("--output") + 1])
        self.assertEqual(out1, base / "gates" / "mutation" / "1")
        (out1 / "mutants.out").mkdir()
        (out1 / "mutants.out" / "outcomes.json").write_text(json.dumps(REAL_OUTCOMES))
        self.assertEqual(pack.read_mutation("", "", 2)["killed"], 7)
        self.assertTrue(out1.is_dir(), "the card's evidence folder is kept")
        second = pack.commands("mutation", files=["src/a.rs"])["argv"]
        out2 = Path(second[second.index("--output") + 1])
        self.assertEqual(out2, base / "gates" / "mutation" / "2")
        # A run that wrote nothing never reads the first run's file.
        with self.assertRaises(Invalid):
            pack.read_mutation("", "", 2)


class MutationParseTests(unittest.TestCase):
    def test_the_real_summary_line(self) -> None:
        parsed = RustPack().parse_mutation(REAL_STDOUT)
        self.assertEqual((parsed["killed"], parsed["survived"], parsed["total"]),
                         (7, 6, 13))
        self.assertEqual(parsed["judged"], 13)
        self.assertEqual((parsed["timeout"], parsed["unviable"]), (0, 0))
        self.assertFalse(parsed["capped"])

    def test_every_outcome_word(self) -> None:
        parsed = RustPack().parse_mutation(
            "20 mutants tested in 1m 2s: 3 missed, 9 caught, 5 unviable, "
            "2 timeouts, 1 succeeded\n")
        self.assertEqual(parsed["total"], 20)
        self.assertEqual((parsed["killed"], parsed["survived"], parsed["unviable"],
                          parsed["timeout"]), (9, 3, 5, 2))
        self.assertEqual(parsed["judged"], 12)

    def test_one_mutant(self) -> None:
        parsed = RustPack().parse_mutation("1 mutant tested: 1 caught\n")
        self.assertEqual((parsed["killed"], parsed["total"], parsed["judged"]),
                         (1, 1, 1))

    def test_caught_mutants_are_not_counted_from_lines(self) -> None:
        # Only missed mutants get a line; the old parser counted the summary
        # line's "caught" as one kill and the missed lines as survivors.
        parsed = RustPack().parse_mutation(
            "Found 3 mutants to test\nok       Unmutated baseline\n"
            "MISSED   src/lib.rs:1:1: replace f with g\n"
            "3 mutants tested: 1 missed, 2 caught\n")
        self.assertEqual((parsed["killed"], parsed["survived"]), (2, 1))

    def test_outcomes_json_wins_over_the_text(self) -> None:
        pack = RustPack()
        argv = pack.commands("mutation", files=["src/lib.rs"])["argv"]
        out = Path(argv[argv.index("--output") + 1]) / "mutants.out"
        out.mkdir()
        (out / "outcomes.json").write_text(json.dumps(
            dict(REAL_OUTCOMES, caught=5, missed=4, timeout=3, unviable=1)))
        parsed = pack.read_mutation(REAL_STDOUT, "", 3)
        self.assertEqual(parsed["source"], "outcomes.json")
        self.assertEqual((parsed["killed"], parsed["survived"], parsed["timeout"],
                          parsed["unviable"], parsed["total"], parsed["judged"]),
                         (5, 4, 3, 1, 13, 9))

    def test_the_old_json_object_shape_is_gone(self) -> None:
        # No cargo-mutants version prints this for a run; it was a guess.
        with self.assertRaises(Invalid):
            RustPack().parse_mutation(
                '{"total": 10, "killed": 8, "survived": 2, "capped": false}')

    def test_text_without_a_summary_is_unreadable(self) -> None:
        with self.assertRaises(Invalid) as caught:
            RustPack().parse_mutation("Found 13 mutants to test\nkilled caught\n")
        self.assertEqual(caught.exception.code, "unreadable-mutation")
        with self.assertRaises(Invalid) as caught:
            RustPack().parse_mutation("")
        self.assertEqual(caught.exception.code, "empty-mutation")


class MutationExitCodeTests(unittest.TestCase):
    def test_results(self) -> None:
        for code in (0, 2, 3):
            with self.subTest(code=code):
                pack = RustPack()
                pack.commands("mutation", files=["src/lib.rs"])
                self.assertEqual(pack.read_mutation(REAL_STDOUT, "", code)["total"], 13)

    def test_the_tool_not_having_run(self) -> None:
        for code, words in ((1, "usage error"), (4, "baseline failed"),
                            (70, "internal error"), (101, "does not document"),
                            (-9, "does not document")):
            with self.subTest(code=code):
                pack = RustPack()
                argv = pack.commands("mutation", files=["src/lib.rs"])["argv"]
                out = Path(argv[argv.index("--output") + 1])
                # Even a summary-looking line does not make a failed baseline a result.
                with self.assertRaises(Invalid) as caught:
                    pack.read_mutation(REAL_STDOUT, "ERROR baseline", code)
                self.assertEqual(caught.exception.code, "mutation-not-run")
                self.assertIn(words, caught.exception.message)
                self.assertFalse(out.exists())


class MutationGateTests(unittest.TestCase):
    def test_the_real_run_reads_54_percent(self) -> None:
        gate = mutation_gate(RustPack().parse_mutation(REAL_STDOUT), 0.5,
                             "required", "pattern")
        self.assertEqual(gate.result, PASS)
        self.assertIn("54%", gate.detail)
        self.assertIn("(7/13)", gate.detail)

    def test_unjudged_mutants_are_out_of_the_rate(self) -> None:
        parsed = {"killed": 8, "survived": 2, "timeout": 5, "unviable": 5,
                  "total": 20, "judged": 10, "capped": False}
        gate = mutation_gate(parsed, 0.8, "required", "pattern")
        self.assertEqual(gate.result, PASS)
        self.assertIn("80%", gate.detail)
        self.assertIn("5 timed out", gate.detail)
        self.assertIn("5 unviable", gate.detail)
        self.assertEqual(gate.evidence["judged"], 10)
        self.assertEqual(gate.evidence["timeout"], 5)

    def test_nothing_judged_blocks_a_blocking_card(self) -> None:
        parsed = {"killed": 0, "survived": 0, "timeout": 2, "unviable": 3,
                  "total": 5, "judged": 0, "capped": False}
        gate = mutation_gate(parsed, 0.8, "report", "test-backfill")
        self.assertEqual(gate.result, FAIL)
        self.assertIn("no mutant could be judged", gate.detail)
        self.assertEqual(mutation_gate(parsed, 0.8, "report", "pattern").result,
                         PASS)

    def test_a_pack_without_judged_keeps_the_old_rate(self) -> None:
        gate = mutation_gate({"killed": 9, "survived": 1, "total": 10},
                             0.8, "required", "pattern")
        self.assertEqual(gate.result, PASS)
        self.assertNotIn("judged", gate.evidence)


def _crate(lib: str, extra_test: str = "") -> Path:
    base = Path(tempfile.mkdtemp(prefix="openfarm-mutcrate-"))
    root = base / "crate"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(TINYMUT_TOML)
    (root / "src" / "lib.rs").write_text(lib + extra_test)
    (root / ".gitignore").write_text("/target\n")
    return root


@unittest.skipUnless(HAS_MUTANTS, "cargo or cargo-mutants is not on PATH")
class RealCargoMutantsTests(unittest.TestCase):
    """The pack's own argv and reader against the installed cargo-mutants."""

    def _run(self, root: Path, **env) -> tuple[RustPack, subprocess.CompletedProcess]:
        self.addCleanup(shutil.rmtree, root.parent, ignore_errors=True)
        pack = RustPack()
        pack.configure(root)
        argv = pack.commands("mutation", files=["src/lib.rs"])["argv"]
        run_env = dict(os.environ, CARGO_TARGET_DIR=str(root.parent / "target"),
                       **env)
        result = subprocess.run(argv, cwd=root, capture_output=True, text=True,
                                env=run_env, timeout=900)
        return pack, result

    def _assert_nothing_left(self, root: Path) -> None:
        self.assertEqual(sorted(p.name for p in root.iterdir()),
                         [".gitignore", "Cargo.toml", "src"])

    def test_caught_and_missed_are_counted_for_what_they_are(self) -> None:
        root = _crate(TINYMUT_LIB)
        pack, result = self._run(root)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        parsed = pack.read_mutation(result.stdout, result.stderr, result.returncode)
        self.assertEqual(parsed["source"], "outcomes.json")
        self.assertEqual((parsed["killed"], parsed["survived"], parsed["total"]),
                         (7, 6, 13))
        # The text fallback agrees with the file.
        text = RustPack().parse_mutation(result.stdout)
        self.assertEqual((text["killed"], text["survived"], text["total"]),
                         (7, 6, 13))
        self._assert_nothing_left(root)

    def test_a_failing_baseline_is_the_tool_not_having_run(self) -> None:
        root = _crate(TINYMUT_LIB, "\n#[test]\nfn red() {\n    assert!(is_even(3));\n}\n")
        pack, result = self._run(root)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        with self.assertRaises(Invalid) as caught:
            pack.read_mutation(result.stdout, result.stderr, result.returncode)
        self.assertEqual(caught.exception.code, "mutation-not-run")
        self.assertIn("baseline failed", caught.exception.message)
        self._assert_nothing_left(root)

    def test_a_timed_out_mutant_is_neither_caught_nor_missed(self) -> None:
        root = _crate(LOOPING_LIB)
        pack, result = self._run(root, CARGO_MUTANTS_MINIMUM_TEST_TIMEOUT="2")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        parsed = pack.read_mutation(result.stdout, result.stderr, result.returncode)
        self.assertGreaterEqual(parsed["timeout"], 1)
        self.assertEqual(parsed["judged"], parsed["killed"] + parsed["survived"])
        self.assertEqual(parsed["total"], parsed["judged"] + parsed["timeout"]
                         + parsed["unviable"])
        self._assert_nothing_left(root)


#: The toy's existing test kept, plus backfill tests for `Spine`.
STRONG_SPINE_TESTS = """\
use toy::fascicle::Fascicle;
use toy::spine::Spine;

#[test]
fn an_empty_fascicle_has_length_zero() {
    let fascicle = Fascicle::new(3);
    assert_eq!(fascicle.len(), 0);
}

#[test]
fn a_spine_counts_and_returns_its_segments() {
    let mut spine = Spine::new();
    assert!(spine.is_empty());
    assert_eq!(spine.len(), 0);
    assert_eq!(spine.push(7), 0);
    assert_eq!(spine.push(9), 1);
    assert_eq!(spine.len(), 2);
    assert!(!spine.is_empty());
    assert_eq!(spine.get(0), Some(7));
    assert_eq!(spine.get(1), Some(9));
    assert_eq!(spine.get(2), None);
}
"""

WEAK_SPINE_TESTS = """\
use toy::fascicle::Fascicle;
use toy::spine::Spine;

#[test]
fn an_empty_fascicle_has_length_zero() {
    let fascicle = Fascicle::new(3);
    assert_eq!(fascicle.len(), 0);
}

#[test]
fn a_spine_counts_and_returns_its_segments() {
    let _spine = Spine::new();
}
"""


@unittest.skipUnless(HAS_MUTANTS, "cargo or cargo-mutants is not on PATH")
class RealMutationGateCardTests(unittest.TestCase):
    """A scripted `test-backfill` card whose only proof is the real mutation gate."""

    def _card(self, tests: str) -> tuple[dict, object]:
        from tests.test_python_e2e import run_cli, script
        from tests.toy import build_toy

        toy = build_toy()
        self.addCleanup(toy.cleanup)
        toy.write_card(
            "BACK-1", title="Pin what a spine counts",
            category="test-backfill",
            touches=["tests/existing.rs", "src/spine.rs"],
            tests=[{"name": "a_spine_counts_and_returns_its_segments",
                    "file": "tests/existing.rs",
                    "asserts": "push, len, is_empty and get agree"}],
            done_text="Every `Spine` method is pinned by a test.")
        toy.commit("toy: BACK-1")
        script_path = script(**{
            "BACK-1/test_writer": "=== whole_file: tests/existing.rs\n" + tests,
            "BACK-1/checker_pass1": "RESTATEMENT: every Spine method is pinned",
            "BACK-1/checker_pass2": "{}",
        })
        result = run_cli(toy.root, "run", "--card", "BACK-1",
                         "--script", str(script_path), "--cards", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["outcomes"][0], toy

    def _mutation_gate(self, toy) -> dict:
        run_dirs = list((toy.state_dir / "cards" / "BACK-1").iterdir())
        found = sorted(run_dirs[0].glob("gates/*/mutation*.json"))
        self.assertTrue(found, "the mutation gate was recorded")
        return json.loads(found[-1].read_text()), run_dirs[0]

    def _assert_no_mutants_out_in_worktrees(self, toy) -> None:
        for path in toy.state_dir.rglob("mutants.out"):
            self.assertIn("/gates/mutation/", str(path),
                          f"mutants.out outside the card's gate folder: {path}")
        self.assertFalse((toy.root / "mutants.out").exists())

    def test_strong_tests_reach_review_with_the_real_counts(self) -> None:
        outcome, toy = self._card(STRONG_SPINE_TESTS)
        self.assertEqual(outcome["final_state"], "in-review",
                         f"{outcome.get('stop_code')}: {outcome.get('detail')}")
        gate, run_dir = self._mutation_gate(toy)
        self.assertEqual(gate["result"], "pass")
        evidence = gate["evidence"]
        self.assertGreater(evidence["total"], 0)
        self.assertEqual(evidence["judged"] + evidence["timeout"]
                         + evidence["unviable"], evidence["total"])
        self.assertGreaterEqual(evidence["kill_rate"], 0.8)
        outcomes = json.loads(
            (run_dir / "gates" / "mutation" / "1" / "mutants.out"
             / "outcomes.json").read_text())
        self.assertEqual(outcomes["total_mutants"], evidence["total"])
        self.assertEqual(outcomes["caught"], evidence["killed"])
        self._assert_no_mutants_out_in_worktrees(toy)

    def test_weak_tests_block_on_the_real_kill_rate(self) -> None:
        outcome, toy = self._card(WEAK_SPINE_TESTS)
        self.assertEqual(outcome["final_state"], "blocked")
        self.assertEqual(outcome["stop_code"], "tests-unprovable",
                         outcome.get("detail"))
        gate, run_dir = self._mutation_gate(toy)
        self.assertEqual(gate["result"], "fail")
        evidence = gate["evidence"]
        outcomes = json.loads(
            (run_dir / "gates" / "mutation" / "1" / "mutants.out"
             / "outcomes.json").read_text())
        self.assertEqual((evidence["killed"], evidence["survived"], evidence["total"]),
                         (outcomes["caught"], outcomes["missed"],
                          outcomes["total_mutants"]))
        self.assertGreater(evidence["survived"], 0)
        self.assertIn("kill rate", gate["detail"])
        self._assert_no_mutants_out_in_worktrees(toy)


if __name__ == "__main__":
    unittest.main()
