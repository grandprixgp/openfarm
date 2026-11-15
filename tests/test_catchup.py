"""The catch-up trial merge: skipped when the base is still, blocking when it moves.

The gate only has something to do when the base moves while the card runs, so these
cases use a scripted backend that lands a commit on `main` at a chosen moment.
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
from openfarm_core.cli import Context, _do_run
from tests.toy import build_toy

ENABLED = os.environ.get("OPENFARM_E2E") == "1"

TEST = """\
use toy::fascicle::{Fascicle, FascicleError};

#[test]
fn a_full_fascicle_refuses_a_push() {
    let mut fascicle = Fascicle::new(1);
    fascicle.push(7).unwrap();
    assert_eq!(fascicle.push(8), Err(FascicleError::Full));
}
"""

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


class MovingBaseBackend:
    """A backend that lands a commit on the base at a chosen role call.

    This is the realistic shape of the problem: the base moves *while* a card is
    being worked, not before it starts.
    """

    def __init__(self, repo: Path, move_at_role: str, content: str,
                 path: str = "NOTES.md") -> None:
        self.repo = repo
        self.move_at = move_at_role
        self.content = content
        self.path = path
        self.moved = False
        self.calls: list[str] = []

    def hello(self) -> dict:
        return {"name": "moving-base", "versions": [1], "ops": ["complete"]}

    def _answers(self, role: str) -> str:
        if role == "test_writer":
            return "=== whole_file: tests/full.rs\n" + TEST
        if role == "implementer":
            return FIX
        if role == "checker_pass1":
            return "RESTATEMENT: a full fascicle refuses a push"
        return "{}"

    def complete(self, role: str, model: str, temperature: float,
                 messages: list, seed=None, max_tokens=4096,
                 response_format=None, card_id="", attempt=1) -> Completion:
        self.calls.append(role)
        if role == self.move_at and not self.moved:
            self._move_base()
        return Completion(content=self._answers(role), model="moving-base",
                          tokens_in=10, tokens_out=10)

    def _move_base(self) -> None:
        """Commit on `main` from a separate worktree, as another lane would.

        `main` is checked out in the toy's own tree, so the other lane works on a
        branch and `main` is then moved to it.
        """
        self.moved = True
        worktree = Path(tempfile.mkdtemp(prefix="other-lane-")) / "wt"
        subprocess.run(["git", "worktree", "add", "-b", "other-lane", str(worktree),
                        "main"], cwd=str(self.repo), check=True, capture_output=True)
        try:
            target = worktree / self.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.content)
            subprocess.run(["git", "add", "-A"], cwd=str(worktree), check=True,
                           capture_output=True)
            subprocess.run(["git", "-c", "user.name=other",
                            "-c", "user.email=other@localhost",
                            "commit", "-qm", "other lane: a commit"],
                           cwd=str(worktree), check=True, capture_output=True)
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(worktree),
                                  check=True, capture_output=True,
                                  text=True).stdout.strip()
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                           cwd=str(self.repo), capture_output=True)
        # `main` is checked out in the toy's own tree, so `git branch -f` refuses.
        # A separate worktree's checkout of it is then out of date, which is exactly
        # the situation the trial merge exists for.
        subprocess.run(["git", "update-ref", "refs/heads/main", head],
                       cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "reset", "-q", "--hard", "main"], cwd=str(self.repo),
                       check=True, capture_output=True)


def _register(case, toy):
    """Remember a toy so the case's tearDown removes it."""
    case._toys = getattr(case, "_toys", []) + [toy]
    return toy


@unittest.skipUnless(ENABLED, "set OPENFARM_E2E=1")
class CatchUpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy = build_toy()
        self._toys = [self.toy]
        self.toy.write_card(
            "CARD-1", title="A full fascicle refuses a push",
            category="pinned-bugfix", touches=["src/fascicle.rs"],
            tests=[{"name": "a_full_fascicle_refuses_a_push",
                    "file": "tests/full.rs",
                    "asserts": "push returns Err(FascicleError::Full)"}],
            done_text="push on a full fascicle returns `Err(FascicleError::Full)`.")
        self.toy.commit("toy: CARD-1")

    def tearDown(self) -> None:
        """Each toy carries a build target; /tmp here is a small tmpfs."""
        for toy in getattr(self, "_toys", []):
            toy.cleanup()
        self._toys = []

    def run_card(self, backend) -> dict:
        context = Context(self.toy.root)
        from openfarm_core.adapters.jsonl import JsonlTelemetry, TelemetrySink
        from openfarm_core.adapters.files import open_file_source

        source = open_file_source(context.cfg, context.state.root)
        source.prepare()
        run_id = "run-catchup"
        run_dir = context.state.create_run(run_id)
        telemetry = TelemetrySink(JsonlTelemetry(run_dir))
        from openfarm_core.cli import _run_card
        return _run_card(context, source, "CARD-1", run_id, telemetry, run_dir,
                         None, backend=backend)

    def test_catch_up_is_skipped_when_the_base_has_not_moved(self) -> None:
        backend = MovingBaseBackend(self.toy.root, move_at_role="never",
                                    content="unused\n")
        outcome = self.run_card(backend)
        self.assertEqual(outcome["final_state"], "in-review",
                         f"{outcome.get('stop_code')}: {outcome.get('detail')}")
        self.assertIn("skipped", self._dossier(outcome)["1_risk"]["catch_up"])

    def test_a_moved_base_is_trial_merged_and_stays_green(self) -> None:
        backend = MovingBaseBackend(self.toy.root, move_at_role="test_writer",
                                    content="a note from another lane\n")
        outcome = self.run_card(backend)
        self.assertEqual(outcome["final_state"], "in-review",
                         f"{outcome.get('stop_code')}: {outcome.get('detail')}")
        self.assertIn("green", self._dossier(outcome)["1_risk"]["catch_up"])

        # The trial merge commits nothing: the branch has no merge commit on it.
        log = subprocess.run(
            ["git", "log", "--oneline", "main..openfarm/CARD-1-a-full-fascicle"],
            cwd=str(self.toy.root), capture_output=True, text=True).stdout
        self.assertNotIn("Merge", log)

    def test_a_conflicting_base_blocks_with_stale_conflict(self) -> None:
        """The other lane changes the same method the card does."""
        conflicting = (
            "//! A fascicle, changed by another lane.\n"
            + self._fascicle_text().replace(
                "    /// Add a segment.\n",
                "    /// Add a segment, changed elsewhere.\n"))
        backend = MovingBaseBackend(self.toy.root, move_at_role="test_writer",
                                    content=conflicting,
                                    path="src/fascicle.rs")
        outcome = self.run_card(backend)
        self.assertEqual(outcome["final_state"], "blocked")
        self.assertEqual(outcome["stop_code"], "stale-conflict")

    def _fascicle_text(self) -> str:
        from tests.toy import FASCICLE_RS
        return FASCICLE_RS

    def _dossier(self, outcome: dict) -> dict:
        from openfarm_core.state import read_json
        path = outcome.get("dossier_path", "")
        self.assertTrue(path and Path(path).exists(), "the dossier should exist")
        return read_json(Path(path).with_suffix(".json")) or {}


if __name__ == "__main__":
    unittest.main()