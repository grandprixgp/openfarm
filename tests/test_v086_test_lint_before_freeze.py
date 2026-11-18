"""Pilot, 18 Nov 2026: a lint finding in the frozen tests lost card F1.

The test writer wrote `assert_eq!(x, true)`. The tests were proven and frozen,
the implementation was right, and then clippy (`-D warnings`) failed on the test
file, which the implementer may not touch. Every attempt failed the same way and
the card stopped as no-progress. The tests are now linted once before they
freeze, and a finding in a test file goes back to the test writer.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adapters.openai_compat import Completion
from openfarm_core.cli import Context, _run_card
from tests.toy import build_toy
from tests.test_flow_edges import FIX, TEST

ENABLED = os.environ.get("OPENFARM_E2E") == "1" and shutil.which("cargo-clippy") is not None

LINT_BAD_TEST = """\
use toy::fascicle::{Fascicle, FascicleError};

#[test]
fn a_full_fascicle_refuses_a_push() {
    let mut fascicle = Fascicle::new(1);
    fascicle.push(7).unwrap();
    assert_eq!(fascicle.push(8) == Err(FascicleError::Full), true);
}
"""


def _write(path: str, body: str) -> str:
    return f"=== whole_file: {path}\n{body}"


class QueueBackend:
    """Answers per role from a queue; the last answer repeats."""

    def __init__(self, answers: dict[str, list[str]]) -> None:
        self.answers = {k: list(v) for k, v in answers.items()}
        self.calls: list[str] = []

    def hello(self) -> dict:
        return {"name": "scripted", "versions": [1], "ops": ["complete"]}

    def complete(self, role, model, temperature, messages, seed=None, max_tokens=4096,
                 response_format=None, card_id="", attempt=1) -> Completion:
        self.calls.append(role)
        queue = self.answers.get(role) or self.answers.get("*") or [""]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        return Completion(content=answer, model="scripted", tokens_in=10, tokens_out=10)


@unittest.skipUnless(ENABLED, "set OPENFARM_E2E=1 with cargo clippy installed")
class TestLintBeforeFreezeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy = build_toy()
        self.toy.write_card(
            "CARD-1", title="A full fascicle refuses a push", category="pinned-bugfix",
            touches=["src/fascicle.rs"],
            tests=[{"name": "a_full_fascicle_refuses_a_push", "file": "tests/full.rs",
                    "asserts": "push returns Err(FascicleError::Full)"}],
            done_text="push on a full fascicle returns `Err(FascicleError::Full)`.")
        self.toy.commit("toy: CARD-1")

    def tearDown(self) -> None:
        self.toy.cleanup()

    def _run(self, backend) -> dict:
        from openfarm_core.adapters.files import open_file_source
        from openfarm_core.adapters.jsonl import JsonlTelemetry, TelemetrySink

        context = Context(self.toy.root)
        source = open_file_source(context.cfg, context.state.root)
        source.prepare()
        run_dir = context.state.create_run("run-1")
        return _run_card(context, source, "CARD-1", "run-1", TelemetrySink(JsonlTelemetry(run_dir)),
                         run_dir, None, backend=backend)

    def test_a_lint_finding_in_the_tests_goes_back_to_the_test_writer(self) -> None:
        backend = QueueBackend({
            "test_writer": [_write("tests/full.rs", LINT_BAD_TEST), _write("tests/full.rs", TEST)],
            "implementer": [FIX],
            "*": ["{}"],
        })
        outcome = self._run(backend)
        self.assertEqual(outcome.get("final_state"), "in-review", json.dumps(outcome)[:2000])
        self.assertEqual(backend.calls.count("test_writer"), 2)
        card_dir = Path(self.toy.state_dir) / "cards" / "CARD-1"
        first = sorted(card_dir.rglob("attempts/01-test-writer.json"))
        self.assertTrue(first, "the first test-writer attempt was recorded")
        rec = json.loads(first[0].read_text())
        self.assertEqual(rec["outcome"], "failed-gates")
        self.assertIn("tests/full.rs", rec["detail"])
        self.assertIn("bool_assert_comparison", rec["detail"])
        # Fixed by the retry, so nothing on file says the tests still fail lint.
        self.assertEqual(list(card_dir.rglob("test-lint*.json")), [])


UNFORMATTED_TEST = (
    "use toy::fascicle::{Fascicle, FascicleError};\n\n#[test]\n"
    "fn a_full_fascicle_refuses_a_push() {\n"
    "  let mut fascicle = Fascicle::new( 1 );\n"
    "  fascicle.push(7).unwrap();\n"
    "  assert_eq!(fascicle.push(8), Err(FascicleError::Full));\n}"  # no final newline
)


@unittest.skipUnless(ENABLED and shutil.which("rustfmt"), "set OPENFARM_E2E=1 with rustfmt")
class TestFormatBeforeFreezeTests(TestLintBeforeFreezeTests):
    """Pilot, 18 Nov 2026: every Rust test file froze without its final newline."""

    def test_a_lint_finding_in_the_tests_goes_back_to_the_test_writer(self) -> None:
        self.skipTest("covered by the parent class")

    def test_the_test_writers_files_are_formatted_before_they_freeze(self) -> None:
        import subprocess
        backend = QueueBackend({
            "test_writer": [_write("tests/full.rs", UNFORMATTED_TEST)],
            "implementer": [FIX],
            "*": ["{}"],
        })
        outcome = self._run(backend)
        self.assertEqual(outcome.get("final_state"), "in-review", json.dumps(outcome)[:2000])
        branch = subprocess.run(
            ["git", "-C", str(self.toy.root), "branch", "--list", "openfarm/CARD-1-*",
             "--format=%(refname:short)"], capture_output=True, text=True).stdout.split()[0]
        frozen = subprocess.run(["git", "-C", str(self.toy.root), "show", f"{branch}:tests/full.rs"],
                                capture_output=True, text=True).stdout
        self.assertTrue(frozen.endswith("}\n"), repr(frozen[-20:]))
        self.assertIn("    let mut fascicle = Fascicle::new(1);", frozen)
        check = subprocess.run(["rustfmt", "--check", "--edition", "2021"], input=frozen,
                               capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stdout)


if __name__ == "__main__":
    unittest.main()
