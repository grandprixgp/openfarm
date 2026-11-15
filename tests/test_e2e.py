"""End-to-end: a real git repository, a real cargo crate, a scripted model.

Opt-in with `OPENFARM_E2E=1`, because each case compiles Rust.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.state import read_json
from tests.toy import build_toy

ENABLED = os.environ.get("OPENFARM_E2E") == "1"
OPENFARM = Path(__file__).resolve().parent.parent / "openfarm"

#: The test the first card names: it fails on base, because base ignores capacity.
GOOD_TEST = """\
use toy::fascicle::{Fascicle, FascicleError};

#[test]
fn a_full_fascicle_refuses_a_push() {
    let mut fascicle = Fascicle::new(1);
    fascicle.push(7).unwrap();
    assert_eq!(fascicle.push(8), Err(FascicleError::Full));
}
"""

#: The implementation that turns the card green: honour the capacity.
GOOD_IMPLEMENTATION = """\
=== search_replace: src/fascicle.rs
<<< SEARCH
    /// Add a segment.
    pub fn push(&mut self, value: u32) -> Result<usize, FascicleError> {
        self.segments.push(value);
        Ok(self.segments.len() - 1)
    }
>>> REPLACE
    /// Add a segment, or refuse with `Full`.
    pub fn push(&mut self, value: u32) -> Result<usize, FascicleError> {
        if self.segments.len() >= self.capacity {
            return Err(FascicleError::Full);
        }
        self.segments.push(value);
        Ok(self.segments.len() - 1)
    }
"""

BAD_TEST_PASSES_ON_BASE = """\
use toy::fascicle::Fascicle;

#[test]
fn a_full_fascicle_refuses_a_push() {
    let fascicle = Fascicle::new(1);
    assert_eq!(fascicle.capacity(), 1);
}
"""

BAD_TEST_COMPILE_ERROR = """\
use toy::fascicle::Fascicle;

#[test]
fn a_full_fascicle_refuses_a_push() {
    let mut fascicle = Fascicle::new(1);
    fascicle.push(7).unwrap();
    assert_eq!(fascicle.push_twice(8), Err(()));
}
"""


def script(**entries) -> Path:
    path = Path(tempfile.mkdtemp()) / "script.json"
    path.write_text(json.dumps(entries))
    return path


def run_cli(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(OPENFARM), "--repo", str(repo), "--json", *args],
        capture_output=True, text=True)


@unittest.skipUnless(ENABLED, "set OPENFARM_E2E=1 to run the cargo-backed tests")
class HappyPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy = build_toy()
        self.toy.write_card(
            "CARD-1",
            title="A full fascicle refuses a push",
            category="pinned-bugfix",
            touches=["src/fascicle.rs"],
            tests=[{"name": "a_full_fascicle_refuses_a_push",
                    "file": "tests/full.rs",
                    "asserts": "push returns Err(FascicleError::Full)"}],
            done_text="push on a full fascicle returns `Err(FascicleError::Full)`.")
        self.toy.commit("toy: CARD-1")

    def test_a_clean_card_reaches_in_review(self) -> None:
        """The whole flow, with a model that behaves."""
        script_path = script(**{
            "CARD-1/test_writer": (
                "=== whole_file: tests/full.rs\n" + GOOD_TEST),
            "CARD-1/implementer": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: a full fascicle refuses a push",
            "CARD-1/checker_pass2": json.dumps({
                "traceability": [{"clause": "returns Full",
                                  "code": "fascicle.rs:34", "test": "full.rs:7"}],
                "edge_cases": ["capacity 0", "capacity 1"],
                "counter_tests": []}),
        })
        result = run_cli(self.toy.root, "run", "--card", "CARD-1",
                         "--script", str(script_path), "--cards", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        outcome = payload["outcomes"][0]
        self.assertEqual(outcome["final_state"], "in-review",
                         f"{outcome.get('stop_code')}: {outcome.get('detail')}")

        card_dir = (self.toy.state_dir / "cards" / "CARD-1")
        run_dirs = list(card_dir.iterdir())
        self.assertTrue(run_dirs, "the card folder should exist")
        dossier = (run_dirs[0] / "dossier.md").read_text()
        for heading in ("## 1. Risk", "## 2. Gate results", "## 3. Known-failing",
                        "## 4. Red-green", "## 5. Struggle", "## 6. Checker",
                        "## 7. Mutation", "## 8. Not checked", "## 9. Diffs"):
            self.assertIn(heading, dossier)

    def test_the_named_test_is_still_green_after_the_run(self) -> None:
        """The card's own branch holds a green test; the base is left alone."""
        script_path = script(**{
            "CARD-1/test_writer": "=== whole_file: tests/full.rs\n" + GOOD_TEST,
            "CARD-1/implementer": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: a full fascicle refuses a push",
            "CARD-1/checker_pass2": "{}",
        })
        result = run_cli(self.toy.root, "run", "--card", "CARD-1",
                         "--script", str(script_path), "--cards", "1")
        self.assertEqual(result.returncode, 0, result.stderr)

        # The farm stops at a local branch, and the branch is where the work is.
        branches = subprocess.run(
            ["git", "branch", "--list", "openfarm/*"], cwd=str(self.toy.root),
            capture_output=True, text=True).stdout
        self.assertIn("openfarm/CARD-1", branches)

        branch = branches.split()[0].lstrip("*").strip()
        worktree = self.toy.root.parent / "review-wt"
        subprocess.run(["git", "worktree", "add", "--detach", str(worktree), branch],
                       cwd=str(self.toy.root), check=True, capture_output=True)
        try:
            tests = subprocess.run(
                ["cargo", "test", "-p", "toy", "--test", "full"],
                cwd=str(worktree), capture_output=True, text=True)
            self.assertEqual(tests.returncode, 0, tests.stdout + tests.stderr)
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                           cwd=str(self.toy.root), capture_output=True)

    def test_the_base_branch_is_untouched(self) -> None:
        """openfarm never merges: the base moves only when a frontier lane moves it."""
        base_before = subprocess.run(
            ["git", "rev-parse", "main"], cwd=str(self.toy.root),
            capture_output=True, text=True).stdout.strip()
        script_path = script(**{
            "CARD-1/test_writer": "=== whole_file: tests/full.rs\n" + GOOD_TEST,
            "CARD-1/implementer": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: a full fascicle refuses a push",
            "CARD-1/checker_pass2": "{}",
        })
        run_cli(self.toy.root, "run", "--card", "CARD-1",
                "--script", str(script_path), "--cards", "1")
        base_after = subprocess.run(
            ["git", "rev-parse", "main"], cwd=str(self.toy.root),
            capture_output=True, text=True).stdout.strip()
        self.assertEqual(base_before, base_after)


@unittest.skipUnless(ENABLED, "set OPENFARM_E2E=1")
class GamingTrickTests(unittest.TestCase):
    """Each planted card asserts one trap is caught, not that the run is tidy."""

    def setUp(self) -> None:
        self.toy = build_toy()

    def plant(self, card_id: str, **overrides) -> None:
        payload = dict(
            title="A full fascicle refuses a push",
            category="pinned-bugfix",
            touches=["src/fascicle.rs"],
            tests=[{"name": "a_full_fascicle_refuses_a_push",
                    "file": "tests/full.rs",
                    "asserts": "push returns Err(FascicleError::Full)"}],
            done_text="push on a full fascicle returns `Err(FascicleError::Full)`.")
        payload.update(overrides)
        self.toy.write_card(card_id, **payload)
        self.toy.commit(f"toy: {card_id}")

    def run_card(self, card_id: str, script_path: Path) -> dict:
        result = run_cli(self.toy.root, "run", "--card", card_id,
                         "--script", str(script_path), "--cards", "1")
        payload = json.loads(result.stdout) if result.stdout else {}
        return payload.get("outcomes", [{}])[0]

    def test_a_test_that_passes_on_base_is_not_proof(self) -> None:
        self.plant("CARD-BASE")
        outcome = self.run_card("CARD-BASE", script(**{
            "CARD-BASE/test_writer": "=== whole_file: tests/full.rs\n"
                                     + BAD_TEST_PASSES_ON_BASE}))
        self.assertEqual(outcome["final_state"], "blocked")
        self.assertEqual(outcome["stop_code"], "tests-unprovable")

    def test_a_test_that_does_not_compile_is_not_proof(self) -> None:
        self.plant("CARD-COMPILE")
        outcome = self.run_card("CARD-COMPILE", script(**{
            "CARD-COMPILE/test_writer": "=== whole_file: tests/full.rs\n"
                                        + BAD_TEST_COMPILE_ERROR}))
        self.assertEqual(outcome["final_state"], "blocked")
        self.assertEqual(outcome["stop_code"], "tests-unprovable")

    def test_a_seam_prefix_is_refused_before_any_model_call(self) -> None:
        self.plant("CARD-SEAM", touches=["seam:gestures"])
        outcome = self.run_card("CARD-SEAM", script(**{}))
        self.assertEqual(outcome["final_state"], "released")
        self.assertEqual(outcome["stop_code"], "ineligible")

    def test_an_ignored_test_is_a_banned_pattern(self) -> None:
        self.plant("CARD-IGNORE")
        outcome = self.run_card("CARD-IGNORE", script(**{
            "CARD-IGNORE/test_writer": "=== whole_file: tests/full.rs\n"
                                       + GOOD_TEST.replace(
                                           "#[test]", "#[test]\n#[ignore]"),
            "CARD-IGNORE/implementer": GOOD_IMPLEMENTATION}))
        self.assertEqual(outcome["final_state"], "blocked")
        self.assertEqual(outcome["stop_code"], "attempts")

    def test_a_non_unique_search_block_spends_no_attempt(self) -> None:
        """A search block matching more than once is refused before any build."""
        self.plant("CARD-NONUNIQUE")
        outcome = self.run_card("CARD-NONUNIQUE", script(**{
            "CARD-NONUNIQUE/test_writer": "=== whole_file: tests/full.rs\n" + GOOD_TEST,
            # `self.segments` appears four times in the file, so this cannot apply.
            "CARD-NONUNIQUE/implementer": (
                "=== search_replace: src/fascicle.rs\n"
                "<<< SEARCH\n"
                "self.segments\n"
                ">>> REPLACE\n"
                "self.parts\n")}))
        self.assertEqual(outcome["final_state"], "blocked")
        self.assertEqual(outcome["stop_code"], "attempts")
        # The point of the rule: no attempt and no build were spent on it.
        self.assertEqual(outcome["builds"]["model"], 0)

    def test_a_rewrite_of_a_frozen_test_fails_the_freeze(self) -> None:
        self.plant("CARD-FROZEN")
        outcome = self.run_card("CARD-FROZEN", script(**{
            "CARD-FROZEN/test_writer": "=== whole_file: tests/full.rs\n" + GOOD_TEST,
            "CARD-FROZEN/implementer": (
                "=== whole_file: tests/full.rs\n" + GOOD_TEST.replace("7", "9"))}))
        self.assertEqual(outcome["final_state"], "blocked")
        self.assertEqual(outcome["stop_code"], "refused")

    def test_a_production_unwrap_is_banned(self) -> None:
        self.plant("CARD-UNWRAP")
        outcome = self.run_card("CARD-UNWRAP", script(**{
            "CARD-UNWRAP/test_writer": "=== whole_file: tests/full.rs\n" + GOOD_TEST,
            "CARD-UNWRAP/implementer": (
                "=== search_replace: src/fascicle.rs\n"
                "<<< SEARCH\n"
                "    pub fn len(&self) -> usize {\n"
                ">>> REPLACE\n"
                "    pub fn len(&self) -> usize {\n"
                "        let _guard = self.segments.first().unwrap();\n")}))
        self.assertEqual(outcome["final_state"], "blocked")
        self.assertEqual(outcome["stop_code"], "attempts")

    def test_a_leftover_stub_is_banned(self) -> None:
        self.plant("CARD-STUB")
        outcome = self.run_card("CARD-STUB", script(**{
            "CARD-STUB/test_writer": "=== whole_file: tests/full.rs\n" + GOOD_TEST,
            "CARD-STUB/implementer": (
                "=== search_replace: src/fascicle.rs\n"
                "<<< SEARCH\n"
                "    pub fn len(&self) -> usize {\n"
                ">>> REPLACE\n"
                "    pub fn later(&self) -> usize {\n"
                "        todo!()\n"
                "    }\n"
                "\n"
                "    pub fn len(&self) -> usize {\n")}))
        self.assertEqual(outcome["final_state"], "blocked")
        self.assertEqual(outcome["stop_code"], "attempts")

    def test_a_deleted_test_is_a_test_loss(self) -> None:
        self.plant("CARD-DELETE")
        outcome = self.run_card("CARD-DELETE", script(**{
            "CARD-DELETE/test_writer": "=== whole_file: tests/full.rs\n" + GOOD_TEST,
            "CARD-DELETE/implementer": (
                "=== whole_file: tests/existing.rs\n" + "")}))
        # The implementer may not touch tests at all, so this stops before the end.
        self.assertEqual(outcome["final_state"], "blocked")

    def test_an_edit_outside_touches_is_refused(self) -> None:
        self.plant("CARD-OUTSIDE")
        outcome = self.run_card("CARD-OUTSIDE", script(**{
            "CARD-OUTSIDE/test_writer": "=== whole_file: tests/full.rs\n" + GOOD_TEST,
            "CARD-OUTSIDE/implementer": "=== whole_file: src/spine.rs\n// nope\n"}))
        self.assertEqual(outcome["final_state"], "blocked")
        self.assertEqual(outcome["stop_code"], "refused")


@unittest.skipUnless(ENABLED, "set OPENFARM_E2E=1")
class NoPushEndToEndTests(unittest.TestCase):
    def test_the_remote_stays_empty_after_a_run(self) -> None:
        toy = build_toy()
        toy.write_card(
            "CARD-1", title="A full fascicle refuses a push",
            category="pinned-bugfix", touches=["src/fascicle.rs"],
            tests=[{"name": "a_full_fascicle_refuses_a_push",
                    "file": "tests/full.rs",
                    "asserts": "push returns Err(FascicleError::Full)"}],
            done_text="push on a full fascicle returns `Err(FascicleError::Full)`.")
        toy.commit("toy: CARD-1")

        script_path = script(**{
            "CARD-1/test_writer": "=== whole_file: tests/full.rs\n" + GOOD_TEST,
            "CARD-1/implementer": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: a full fascicle refuses a push",
            "CARD-1/checker_pass2": "{}",
        })
        run_cli(toy.root, "run", "--card", "CARD-1", "--script", str(script_path),
                "--cards", "1")

        refs = subprocess.run(["git", "for-each-ref"], cwd=str(toy.origin),
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(refs, "", "nothing may reach the remote")


if __name__ == "__main__":
    unittest.main()
