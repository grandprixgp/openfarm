"""The parts of the flow the happy path doesn't reach.

Planner, append_stub on a new-symbols card, counter-tests, extra gates with
`on_fail = "ineligible"`, and the circuit breaker.
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

from openfarm_core.adapters.openai_compat import Completion
from openfarm_core.cli import Context, _run_card
from tests.toy import build_toy

ENABLED = os.environ.get("OPENFARM_E2E") == "1"

FIX = """\
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

TEST = """\
use toy::fascicle::{Fascicle, FascicleError};

#[test]
fn a_full_fascicle_refuses_a_push() {
    let mut fascicle = Fascicle::new(1);
    fascicle.push(7).unwrap();
    assert_eq!(fascicle.push(8), Err(FascicleError::Full));
}
"""

#: A named test that calls a symbol the base does not have.
NEW_SYMBOL_TEST = """\
use toy::fascicle::Fascicle;

#[test]
fn a_fascicle_reports_its_remaining_room() {
    let mut fascicle = Fascicle::new(2);
    fascicle.push(7).unwrap();
    assert_eq!(fascicle.remaining(), 1);
}
"""

STUB = """\
=== append_stub: src/fascicle.rs
a_fascicle_reports_its_remaining_room
impl Fascicle {
    /// How many more segments fit.
    pub fn remaining(&self) -> usize {
        todo!()
    }
}
"""

FILL_IMPLEMENTATION = """\
=== search_replace: src/fascicle.rs
<<< SEARCH
impl Fascicle {
    /// How many more segments fit.
    pub fn remaining(&self) -> usize {
        todo!()
    }
>>> REPLACE
impl Fascicle {
    /// How many more segments fit.
    pub fn remaining(&self) -> usize {
        self.capacity - self.segments.len()
    }
"""


class ScriptedBackend:
    """A backend whose answers come from a dict, and which records its calls."""

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    def hello(self) -> dict:
        return {"name": "scripted", "versions": [1], "ops": ["complete"]}

    def complete(self, role: str, model: str, temperature: float, messages: list,
                 seed=None, max_tokens=4096, response_format=None,
                 card_id="", attempt=1) -> Completion:
        self.calls.append(role)
        answer = self.answers.get(role, self.answers.get("*", ""))
        return Completion(content=answer, model="scripted", tokens_in=10,
                          tokens_out=10)


def _register(case, toy):
    """Remember a toy so the case's tearDown removes it."""
    case._toys = getattr(case, "_toys", []) + [toy]
    return toy


@unittest.skipUnless(ENABLED, "set OPENFARM_E2E=1")
class FlowEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy = build_toy()
        self._toys = [self.toy]

    def tearDown(self) -> None:
        """Each toy carries a build target; /tmp here is a small tmpfs."""
        for toy in getattr(self, "_toys", []):
            toy.cleanup()
        self._toys = []

    def add_card(self, card_id: str = "CARD-1", **overrides) -> None:
        payload = dict(
            title="A full fascicle refuses a push", category="pinned-bugfix",
            touches=["src/fascicle.rs"],
            tests=[{"name": "a_full_fascicle_refuses_a_push",
                    "file": "tests/full.rs",
                    "asserts": "push returns Err(FascicleError::Full)"}],
            done_text="push on a full fascicle returns `Err(FascicleError::Full)`.")
        payload.update(overrides)
        self.toy.write_card(card_id, **payload)
        self.toy.commit(f"toy: {card_id}")

    def run_card(self, backend, card_id: str = "CARD-1") -> dict:
        from openfarm_core.adapters.files import open_file_source
        from openfarm_core.adapters.jsonl import JsonlTelemetry, TelemetrySink

        context = Context(self.toy.root)
        source = open_file_source(context.cfg, context.state.root)
        source.prepare()
        run_id = f"run-{card_id}"
        run_dir = context.state.create_run(run_id)
        telemetry = TelemetrySink(JsonlTelemetry(run_dir))
        return _run_card(context, source, card_id, run_id, telemetry, run_dir,
                         None, backend=backend)

    # -- planner -------------------------------------------------------------

    def test_the_planner_is_used_when_the_done_text_has_two_groups(self) -> None:
        self.add_card(
            touches=["src/fascicle.rs"],
            tests=[{"name": "a_full_fascicle_refuses_a_push",
                    "file": "tests/full.rs",
                    "asserts": "Err(Full)"},
                   {"name": "a_fascicle_reports_its_remaining_room",
                    "file": "tests/room.rs",
                    "asserts": "remaining is 1"}],
            done_text=(
                "- push returns `Err(FascicleError::Full)` on a full fascicle.\n"
                "- remaining returns the free room.\n"))
        backend = ScriptedBackend({
            "planner": json.dumps({"steps": [
                {"n": 1, "does": "honour capacity", "tests": [
                    "a_full_fascicle_refuses_a_push"]},
                {"n": 2, "does": "report room", "tests": [
                    "a_fascicle_reports_its_remaining_room"]}]}),
            "test_writer": "=== whole_file: tests/full.rs\n" + TEST,
            "implementer": FIX,
            "checker_pass1": "RESTATEMENT: full refuses a push and room is reported",
            "checker_pass2": "{}",
        })
        outcome = self.run_card(backend)
        self.assertIn("planner", backend.calls)
        self.assertTrue(outcome["planner_used"])

    def test_a_planner_that_cannot_split_blocks(self) -> None:
        self.add_card(
            tests=[{"name": "a_full_fascicle_refuses_a_push",
                    "file": "tests/full.rs", "asserts": "Err(Full)"},
                   {"name": "a_fascicle_reports_its_remaining_room",
                    "file": "tests/room.rs", "asserts": "remaining is 1"}],
            done_text=("- push returns `Err(FascicleError::Full)`.\n"
                       "- remaining returns the free room.\n"))
        backend = ScriptedBackend({
            "planner": "CANNOT_SPLIT",
            "test_writer": "=== whole_file: tests/full.rs\n" + TEST,
            "*": "{}",
        })
        outcome = self.run_card(backend)
        self.assertEqual(outcome["final_state"], "blocked")
        self.assertEqual(outcome["stop_code"], "cannot-split")

    # -- append_stub ---------------------------------------------------------

    def test_append_stub_lets_a_new_symbol_card_prove_itself(self) -> None:
        """The test writer adds a panicking stub so the test compiles on base."""
        self.add_card(
            title="A fascicle reports its remaining room",
            category="mechanical",
            touches=["src/fascicle.rs"],
            new_symbols=True,
            tests=[{"name": "a_fascicle_reports_its_remaining_room",
                    "file": "tests/room.rs",
                    "asserts": "remaining is 1"}],
            done_text="remaining returns `1` after one push into a fascicle of 2.")

        backend = ScriptedBackend({
            "test_writer": ("=== whole_file: tests/room.rs\n" + NEW_SYMBOL_TEST
                            + "\n" + STUB),
            "implementer": FILL_IMPLEMENTATION,
            "checker_pass1": "RESTATEMENT: remaining reports the free room",
            "checker_pass2": "{}",
        })
        outcome = self.run_card(backend)
        self.assertEqual(outcome["final_state"], "in-review",
                         f"{outcome.get('stop_code')}: {outcome.get('detail')}")

    def test_a_stub_on_a_card_without_new_symbols_is_refused(self) -> None:
        self.add_card()
        backend = ScriptedBackend({
            "test_writer": STUB,
            "implementer": FIX,
            "*": "{}",
        })
        outcome = self.run_card(backend)
        self.assertEqual(outcome["final_state"], "blocked")
        # A refusal the model cannot fix stops the card: retrying it would only
        # spend model calls to reach the same answer.
        self.assertEqual(outcome["stop_code"], "refused")

    # -- counter-tests -------------------------------------------------------

    def test_a_failing_counter_test_is_reported_not_rejected(self) -> None:
        """Counter-tests are report-only in the first pilot batch."""
        self.add_card()
        counter = json.dumps({
            "traceability": [],
            "edge_cases": ["capacity 0"],
            "counter_tests": [{
                "name": "a_counter_test",
                "file": "tests/counter.rs",
                "content": (
                    "use toy::fascicle::Fascicle;\n\n"
                    "#[test]\n"
                    "fn a_counter_test() {\n"
                    "    let fascicle = Fascicle::new(0);\n"
                    "    assert_eq!(fascicle.capacity(), 99);\n"
                    "}\n")}]})
        backend = ScriptedBackend({
            "test_writer": "=== whole_file: tests/full.rs\n" + TEST,
            "implementer": FIX,
            "checker_pass1": "RESTATEMENT: a full fascicle refuses a push",
            "checker_pass2": counter,
        })
        outcome = self.run_card(backend)
        self.assertEqual(outcome["final_state"], "in-review",
                         f"{outcome.get('stop_code')}: {outcome.get('detail')}")
        self.assertEqual(outcome["counter_tests"]["written"], 1)
        self.assertEqual(outcome["counter_tests"]["failed"], 1)

    def test_a_counter_test_that_does_not_compile_is_dropped(self) -> None:
        self.add_card()
        counter = json.dumps({
            "counter_tests": [{"name": "broken", "file": "tests/counter.rs",
                               "content": "this is not rust at all\n"}]})
        backend = ScriptedBackend({
            "test_writer": "=== whole_file: tests/full.rs\n" + TEST,
            "implementer": FIX,
            "checker_pass1": "RESTATEMENT: a full fascicle refuses a push",
            "checker_pass2": counter,
        })
        outcome = self.run_card(backend)
        self.assertEqual(outcome["final_state"], "in-review")
        self.assertEqual(outcome["counter_tests"]["dropped_uncompiled"], 1)

    # -- extra gates ---------------------------------------------------------

    def test_an_extra_gate_is_skipped_when_its_paths_are_untouched(self) -> None:
        self.add_card()
        config = self.toy.config.read_text()
        config = config.replace("[gates]", """[[gates.extra]]
name = "never-runs"
run = "false"
via = "runner"
paths = ["src/elsewhere/**"]
on_fail = "block"

[gates]""")
        self.toy.config.write_text(config)
        self.toy.commit("toy: an extra gate that should not run")
        backend = ScriptedBackend({
            "test_writer": "=== whole_file: tests/full.rs\n" + TEST,
            "implementer": FIX,
            "checker_pass1": "RESTATEMENT: a full fascicle refuses a push",
            "checker_pass2": "{}",
        })
        outcome = self.run_card(backend)
        self.assertEqual(outcome["final_state"], "in-review",
                         f"{outcome.get('stop_code')}: {outcome.get('detail')}")

    def test_an_extra_gate_that_fails_as_ineligible_releases_the_card(self) -> None:
        self.add_card()
        config = self.toy.config.read_text()
        config = config.replace("[gates]", """[[gates.extra]]
name = "always-fails"
run = "false"
via = "runner"
paths = ["src/**"]
on_fail = "ineligible"

[gates]""")
        self.toy.config.write_text(config)
        self.toy.commit("toy: an extra gate that always fails")
        backend = ScriptedBackend({
            "test_writer": "=== whole_file: tests/full.rs\n" + TEST,
            "implementer": FIX,
            "checker_pass1": "RESTATEMENT: a full fascicle refuses a push",
            "checker_pass2": "{}",
        })
        outcome = self.run_card(backend)
        self.assertEqual(outcome["final_state"], "released")
        self.assertEqual(outcome["stop_code"], "ineligible")


def _register(case, toy):
    """Remember a toy so the case's tearDown removes it."""
    case._toys = getattr(case, "_toys", []) + [toy]
    return toy


@unittest.skipUnless(ENABLED, "set OPENFARM_E2E=1")
class TestBackfillTests(unittest.TestCase):
    """A backfill card touches only test files and has no production work.

    This category was briefly unrunnable: the test writer's production check treated
    its test files as production, and the implementer phase then had nothing to do.
    """

    def setUp(self) -> None:
        self.toy = build_toy()
        self._toys = [self.toy]

    def tearDown(self) -> None:
        for toy in getattr(self, "_toys", []):
            toy.cleanup()
        self._toys = []

    def test_a_backfill_card_reaches_in_review(self) -> None:
        self.toy.write_card(
            "BACK-1", title="Pin the empty fascicle length",
            category="test-backfill",
            touches=["tests/existing.rs"],
            tests=[{"name": "an_empty_fascicle_has_length_zero",
                    "file": "tests/existing.rs", "asserts": "len() == 0"}],
            done_text="An empty fascicle has `len()` zero.")
        self.toy.commit("toy: BACK-1")

        backend = ScriptedBackend({
            "test_writer": (
                "=== whole_file: tests/existing.rs\n"
                "use toy::fascicle::Fascicle;\n\n"
                "#[test]\n"
                "fn an_empty_fascicle_has_length_zero() {\n"
                "    let fascicle = Fascicle::new(3);\n"
                "    assert_eq!(fascicle.len(), 0);\n"
                "}\n"),
            "checker_pass1": "RESTATEMENT: an empty fascicle has length zero",
            "checker_pass2": "{}",
        })
        from openfarm_core.adapters.files import open_file_source
        from openfarm_core.adapters.jsonl import JsonlTelemetry, TelemetrySink
        context = Context(self.toy.root)
        source = open_file_source(context.cfg, context.state.root)
        source.prepare()
        run_id = "run-backfill"
        run_dir = context.state.create_run(run_id)
        outcome = _run_card(context, source, "BACK-1", run_id,
                            TelemetrySink(JsonlTelemetry(run_dir)), run_dir, None,
                            backend=backend)
        # Mutation is a test-backfill card's only blocking proof, and no mutation
        # tool is installed on this machine. So the honest outcome is a block, not a
        # pass: a backfill card that cannot be proven must not reach review. The
        # dossier says which gate stopped it.
        self.assertEqual(outcome["final_state"], "blocked")
        self.assertIn(outcome["stop_code"], ("mutation-cap", "tests-unprovable"))
        self.assertEqual(outcome["mutation"]["total"], 0)

    def test_a_backfill_card_reaches_in_review_when_mutation_is_off(self) -> None:
        """With mutation off, the same card runs through — the rest of the flow works."""
        config = self.toy.config.read_text().replace(
            'mutation = "report"', 'mutation = "off"').replace(
            'mutation_required_for = ["test-backfill"]',
            'mutation_required_for = []')
        self.toy.config.write_text(config)
        self.toy.write_card(
            "BACK-2", title="Pin the empty fascicle length",
            category="test-backfill",
            touches=["tests/existing.rs"],
            tests=[{"name": "an_empty_fascicle_has_length_zero",
                    "file": "tests/existing.rs", "asserts": "len() == 0"}],
            done_text="An empty fascicle has `len()` zero.")
        self.toy.commit("toy: BACK-2")

        backend = ScriptedBackend({
            "test_writer": (
                "=== whole_file: tests/existing.rs\n"
                "use toy::fascicle::Fascicle;\n\n"
                "#[test]\n"
                "fn an_empty_fascicle_has_length_zero() {\n"
                "    let fascicle = Fascicle::new(3);\n"
                "    assert_eq!(fascicle.len(), 0);\n"
                "}\n"),
            "checker_pass1": "RESTATEMENT: an empty fascicle has length zero",
            "checker_pass2": "{}",
        })
        from openfarm_core.adapters.files import open_file_source
        from openfarm_core.adapters.jsonl import JsonlTelemetry, TelemetrySink
        context = Context(self.toy.root)
        source = open_file_source(context.cfg, context.state.root)
        source.prepare()
        run_id = "run-backfill-2"
        run_dir = context.state.create_run(run_id)
        outcome = _run_card(context, source, "BACK-2", run_id,
                            TelemetrySink(JsonlTelemetry(run_dir)), run_dir, None,
                            backend=backend)
        self.assertEqual(outcome["final_state"], "in-review",
                         f"{outcome.get('stop_code')}: {outcome.get('detail')}")


@unittest.skipUnless(ENABLED, "set OPENFARM_E2E=1")
class BudgetAndBreakerTests(unittest.TestCase):
    def tearDown(self) -> None:
        """Each toy carries a build target; /tmp here is a small tmpfs."""
        for toy in getattr(self, "_toys", []):
            toy.cleanup()
        self._toys = []

    def test_a_build_cap_blocks_with_its_own_code(self) -> None:
        toy = _register(self, build_toy())
        config = toy.config.read_text().replace(
            "model_build_calls_per_card = 10",
            "model_build_calls_per_card = 1")
        toy.config.write_text(config)
        toy.write_card(
            "CARD-1", title="A full fascicle refuses a push",
            category="pinned-bugfix", touches=["src/fascicle.rs"],
            tests=[{"name": "a_full_fascicle_refuses_a_push",
                    "file": "tests/full.rs",
                    "asserts": "push returns Err(FascicleError::Full)"}],
            done_text="push on a full fascicle returns `Err(FascicleError::Full)`.")
        toy.commit("toy: a one-build cap")

        from openfarm_core.adapters.files import open_file_source
        from openfarm_core.adapters.jsonl import JsonlTelemetry, TelemetrySink
        context = Context(toy.root)
        source = open_file_source(context.cfg, context.state.root)
        source.prepare()
        run_id = "run-cap"
        run_dir = context.state.create_run(run_id)
        backend = ScriptedBackend({
            "test_writer": "=== whole_file: tests/full.rs\n" + TEST,
            # An implementation that leaves the named test red, so the attempt's
            # suite and lint spend the cap.
            "implementer": "=== whole_file: src/fascicle.rs\n// nothing useful\n",
            "*": "{}",
        })
        outcome = _run_card(context, source, "CARD-1", run_id,
                            TelemetrySink(JsonlTelemetry(run_dir)), run_dir, None,
                            backend=backend)
        self.assertEqual(outcome["final_state"], "blocked")
        self.assertEqual(outcome["stop_code"], "build-cap")


if __name__ == "__main__":
    unittest.main()