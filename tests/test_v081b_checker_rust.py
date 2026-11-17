"""v0.8.1b checker K3 with the Rust pack: counter-tests run under a real `cargo test`.

These need `cargo` (and clippy, which the card's lint gate runs) and skip without
them. They do not need `OPENFARM_E2E`: each case is one small crate.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.cli import Context, _run_card
from openfarm_core.flow import CardRun
from tests.test_v081b_checker_flow import tree_state
from tests.test_v081b_checker_form import form_answer
from tests.toy import build_toy

HAVE_CARGO = shutil.which("cargo") is not None and shutil.which("rustfmt") is not None

NAMED_TEST = """\
use toy::fascicle::{Fascicle, FascicleError};

#[test]
fn a_full_fascicle_refuses_a_push() {
    let mut fascicle = Fascicle::new(1);
    fascicle.push(7).unwrap();
    assert_eq!(fascicle.push(8), Err(FascicleError::Full));
}
"""

#: Refuses, but only after it has stored the segment: the done text says a refused
#: push leaves the fascicle as it was. The named test cannot see it.
BUGGY = """\
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
        self.segments.push(value);
        if self.segments.len() > self.capacity {
            return Err(FascicleError::Full);
        }
        Ok(self.segments.len() - 1)
    }
"""

FIXED = """\
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

UNCHANGED = ("a_refused_push_leaves_the_fascicle_unchanged", """\
use toy::fascicle::Fascicle;

#[test]
fn a_refused_push_leaves_the_fascicle_unchanged() {
    let mut fascicle = Fascicle::new(1);
    fascicle.push(7).unwrap();
    let _ = fascicle.push(8);
    assert_eq!(fascicle.len(), 1);
}
""")

WITHIN = ("a_push_within_capacity_returns_its_index", """\
use toy::fascicle::Fascicle;

#[test]
fn a_push_within_capacity_returns_its_index() {
    let mut fascicle = Fascicle::new(2);
    assert_eq!(fascicle.push(7), Ok(0));
    assert_eq!(fascicle.push(8), Ok(1));
}
""")

DOES_NOT_COMPILE = ("a_counter_test_that_calls_nothing", """\
use toy::fascicle::Fascicle;

#[test]
fn a_counter_test_that_calls_nothing() {
    assert_eq!(Fascicle::new(1).no_such_method(), 0);
}
""")


@unittest.skipUnless(HAVE_CARGO, "needs cargo and rustfmt on PATH")
class RustCounterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy = build_toy()
        self.addCleanup(self.toy.cleanup)
        config = self.toy.config.read_text()
        config = config.replace('mutation = "report"', 'mutation = "off"')
        self.toy.config.write_text(config)
        self.toy.write_card(
            "CARD-1", title="A full fascicle refuses a push", category="pinned-bugfix",
            touches=["src/fascicle.rs"],
            tests=[{"name": "a_full_fascicle_refuses_a_push", "file": "tests/full.rs",
                    "asserts": "push returns Err(FascicleError::Full)"}],
            done_text=("push on a full fascicle returns `Err(FascicleError::Full)` and "
                       "leaves the fascicle exactly as it was."))
        self.toy.commit("toy: CARD-1")
        self.snapshots: list[tuple[dict, dict]] = []

    def set_counter_mode(self, mode: str) -> None:
        config = self.toy.config.read_text()
        self.toy.config.write_text(
            config.replace('counter_tests = "report"', f'counter_tests = "{mode}"'))
        self.toy.commit(f"toy: counter_tests = {mode}")

    def run_card(self, script: dict) -> tuple[dict, dict, list]:
        from openfarm_core.adapters.files import open_file_source
        from openfarm_core.adapters.jsonl import JsonlTelemetry, TelemetrySink
        from openfarm_core.adapters.openai_compat import ScriptedBackend

        real = CardRun._run_counter_tests
        snapshots = self.snapshots

        def watched(run, tests):
            before = tree_state(run.worktree)
            try:
                return real(run, tests)
            finally:
                snapshots.append((before, tree_state(run.worktree)))

        context = Context(self.toy.root)
        source = open_file_source(context.cfg, context.state.root)
        source.prepare()
        run_dir = context.state.create_run("run-rust-counter")
        telemetry = TelemetrySink(JsonlTelemetry(run_dir))
        backend = ScriptedBackend(script)
        with mock.patch.object(CardRun, "_run_counter_tests", watched), \
                contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()):
            outcome = _run_card(context, source, "CARD-1", "run-rust-counter",
                                telemetry, run_dir, None, backend=backend)
        card_dir = self.toy.state_dir / "cards" / "CARD-1" / "run-rust-counter"
        dossier = json.loads((card_dir / "dossier.json").read_text())
        return outcome, dossier, backend.calls

    def script(self, pass2: str, **extra) -> dict:
        script = {
            "CARD-1/test_writer": "=== whole_file: tests/full.rs\n" + NAMED_TEST,
            "CARD-1/implementer": BUGGY,
            "CARD-1/checker_pass1": "RESTATEMENT: a full fascicle refuses a push",
            "CARD-1/checker_pass2": pass2,
        }
        script.update(extra)
        return script

    def assert_worktree_untouched(self) -> None:
        self.assertTrue(self.snapshots, "no counter-test ran")
        for before, after in self.snapshots:
            self.assertEqual(before, after)

    def test_failing_and_passing_counter_tests_under_cargo(self) -> None:
        outcome, dossier, calls = self.run_card(
            self.script(form_answer(tests=[UNCHANGED, WITHIN])))
        self.assertEqual(outcome["final_state"], "in-review",
                         f"{outcome.get('stop_code')}: {outcome.get('detail')}")
        summary = outcome["counter_tests"]
        self.assertEqual((summary["written"], summary["failed"], summary["passed"],
                          summary["dropped_uncompiled"]), (2, 1, 1, 0))
        results = dossier["6_checker"]["pass2"]["counter_results"]
        self.assertEqual([(r["name"], r["outcome"]) for r in results],
                         [(UNCHANGED[0], "failed"), (WITHIN[0], "passed")])
        self.assertEqual(results[0]["file"], "tests/openfarm_counter_1.rs")
        self.assertIn("left: 2", results[0]["output_head"])
        self.assert_worktree_untouched()
        brief = [c for c in calls if c["role"] == "checker_pass2"][0]["messages"][0]["content"]
        self.assertIn("cargo test --test", brief)
        self.assertIn("`use toy::...;`", brief)
        self.assertIn("counter-test 1: tests/openfarm_counter_1.rs", brief)

    def test_reject_drops_what_does_not_compile_and_bounces_on_a_failure(self) -> None:
        self.set_counter_mode("reject")
        outcome, dossier, calls = self.run_card(self.script(
            form_answer(tests=[DOES_NOT_COMPILE, UNCHANGED]),
            **{"CARD-1/implementer/2": FIXED}))
        self.assertEqual(outcome["final_state"], "in-review",
                         f"{outcome.get('stop_code')}: {outcome.get('detail')}")
        implementer = [c for c in calls if c["role"] == "implementer"]
        self.assertEqual(len(implementer), 2)
        self.assertIn(UNCHANGED[0], implementer[1]["messages"][0]["content"])
        # The last checker pass ran on the fixed diff.
        results = dossier["6_checker"]["pass2"]["counter_results"]
        self.assertEqual([(r["name"], r["outcome"]) for r in results],
                         [(DOES_NOT_COMPILE[0], "dropped"), (UNCHANGED[0], "passed")])
        self.assertEqual(outcome["counter_tests"]["dropped_uncompiled"], 1)
        self.assert_worktree_untouched()


if __name__ == "__main__":
    unittest.main()
