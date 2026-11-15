"""A night: several cards claimed in one run, the queue, and the farm-wide stops.

The per-card flow is one thing; this is the loop around it — which card comes next,
what a night records, and what stops claiming for everyone.
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

from tests.toy import build_toy

ENABLED = os.environ.get("OPENFARM_E2E") == "1"
REPO_ROOT = Path(__file__).resolve().parent.parent
OPENFARM = REPO_ROOT / "openfarm"

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


def cli(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(OPENFARM), "--repo", str(repo), "--json", *args],
        capture_output=True, text=True)


def script(**entries) -> Path:
    path = Path(tempfile.mkdtemp()) / "script.json"
    path.write_text(json.dumps(entries))
    return path


def good_script(card_id: str) -> dict:
    return {
        f"{card_id}/test_writer": "=== whole_file: tests/full.rs\n" + TEST,
        f"{card_id}/implementer": FIX,
        f"{card_id}/checker_pass1": "RESTATEMENT: a full fascicle refuses a push",
        f"{card_id}/checker_pass2": "{}",
    }


def add_card(toy, card_id: str, title: str = "A full fascicle refuses a push",
             **overrides) -> None:
    payload = dict(
        title=title, category="pinned-bugfix", touches=["src/fascicle.rs"],
        tests=[{"name": "a_full_fascicle_refuses_a_push", "file": "tests/full.rs",
                "asserts": "push returns Err(FascicleError::Full)"}],
        done_text="push on a full fascicle returns `Err(FascicleError::Full)`.")
    payload.update(overrides)
    toy.write_card(card_id, **payload)


@unittest.skipUnless(ENABLED, "set OPENFARM_E2E=1")
class NightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy = build_toy()
        add_card(self.toy, "CARD-1")
        self.toy.commit("toy: CARD-1")

    def test_one_night_claims_and_drives_several_cards(self) -> None:
        for index in range(2, 4):
            add_card(self.toy, f"CARD-{index}",
                     title=f"A full fascicle refuses a push, number {index}")
        self.toy.commit("toy: three cards")

        answers = {}
        for index in (1, 2, 3):
            answers.update(good_script(f"CARD-{index}"))
        path = script(**answers)

        result = cli(self.toy.root, "run", "--cards", "3", "--script", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        states = [outcome.get("final_state") for outcome in payload["outcomes"]]
        self.assertEqual(states, ["in-review"] * 3, payload["outcomes"])

        # One card folder per card, one run record, and telemetry for each.
        run_id = payload["run_id"]
        run_dir = self.toy.state_dir / "runs" / run_id
        self.assertTrue((run_dir / "run.json").exists())
        attempts = (run_dir / "telemetry" / "attempts.jsonl").read_text()
        self.assertGreaterEqual(len(attempts.strip().splitlines()), 3)
        cards = (run_dir / "telemetry" / "cards.jsonl").read_text()
        self.assertEqual(len(cards.strip().splitlines()), 3)
        for index in (1, 2, 3):
            self.assertTrue(
                (self.toy.state_dir / "cards" / f"CARD-{index}" / run_id).exists())

    def test_the_card_cap_stops_the_night(self) -> None:
        add_card(self.toy, "CARD-2", title="A second push refusal")
        self.toy.commit("toy: two cards")
        answers = {}
        answers.update(good_script("CARD-1"))
        answers.update(good_script("CARD-2"))
        result = cli(self.toy.root, "run", "--cards", "1",
                     "--script", str(script(**answers)))
        payload = json.loads(result.stdout)
        self.assertEqual(len(payload["outcomes"]), 1)
        self.assertEqual(payload["outcomes"][0]["final_state"], "in-review")

    def test_a_dry_run_claims_nothing_and_calls_no_model(self) -> None:
        result = cli(self.toy.root, "run", "--dry-run", "--cards", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        outcome = payload["outcomes"][0]
        self.assertTrue(outcome["dry_run"])
        self.assertTrue(outcome["eligible"])

        # No card state was written at all.
        cards = self.toy.state_dir / "cards"
        self.assertFalse(cards.exists() and any(cards.iterdir()),
                         "a dry run must not touch a card")
        card_file = self.toy.state_dir / "cards-source" / "cards" / "CARD-1.md"
        self.assertIn('state = "ready"', card_file.read_text())

    def test_an_ineligible_card_is_released_not_blocked(self) -> None:
        add_card(self.toy, "CARD-SEAM", touches=["seam:gestures"])
        self.toy.commit("toy: a seam card")
        result = cli(self.toy.root, "run", "--card", "CARD-SEAM",
                     "--script", str(script(**{})))
        payload = json.loads(result.stdout)
        self.assertEqual(payload["outcomes"][0]["final_state"], "released")
        self.assertEqual(payload["outcomes"][0]["stop_code"], "ineligible")

        # Released means Ready again, not blocked.
        card_file = self.toy.state_dir / "cards-source" / "cards" / "CARD-SEAM.md"
        self.assertIn('state = "ready"', card_file.read_text())

    def test_a_paused_queue_refuses_a_new_run(self) -> None:
        cli(self.toy.root, "pause", "--reason", "a person said so")
        result = cli(self.toy.root, "run", "--cards", "1",
                     "--script", str(script(**good_script("CARD-1"))))
        self.assertEqual(result.returncode, 1)
        self.assertIn("paused", json.loads(result.stdout)["reason"])

        cli(self.toy.root, "resume")
        result = cli(self.toy.root, "run", "--cards", "1",
                     "--script", str(script(**good_script("CARD-1"))))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_kill_switch_refuses_a_new_run(self) -> None:
        cli(self.toy.root, "stop", "--reason", "a person said so")
        result = cli(self.toy.root, "run", "--cards", "1",
                     "--script", str(script(**good_script("CARD-1"))))
        self.assertEqual(result.returncode, 1)
        self.assertIn("kill switch", json.loads(result.stdout)["reason"])

        # Clearing it takes an explicit act.
        refused = cli(self.toy.root, "resume")
        self.assertEqual(refused.returncode, 1)
        cleared = cli(self.toy.root, "resume", "--clear-kill")
        self.assertEqual(cleared.returncode, 0, cleared.stderr)

    def test_a_second_run_is_refused_while_one_holds_the_lock(self) -> None:
        state = self.toy.state_dir
        from openfarm_core.state import StateDir
        holder = StateDir(state)
        self.assertTrue(holder.acquire_run_lock("run-1"))

        result = cli(self.toy.root, "run", "--cards", "1",
                     "--script", str(script(**good_script("CARD-1"))))
        self.assertEqual(result.returncode, 1)
        self.assertIn("lock", json.loads(result.stdout)["reason"])

        holder.release_run_lock("run-1")
        result = cli(self.toy.root, "run", "--cards", "1",
                     "--script", str(script(**good_script("CARD-1"))))
        self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(ENABLED, "set OPENFARM_E2E=1")
class BreakerTests(unittest.TestCase):
    def test_three_blocks_for_one_reason_pause_the_queue(self) -> None:
        toy = build_toy()
        # Three cards that will each be blocked for the same reason: a test that
        # passes on base proves nothing.
        for index in (1, 2, 3):
            toy.write_card(
                f"CARD-{index}", title=f"A card that proves nothing {index}",
                category="pinned-bugfix", touches=["src/fascicle.rs"],
                tests=[{"name": "a_full_fascicle_refuses_a_push",
                        "file": "tests/full.rs",
                        "asserts": "push returns Err(FascicleError::Full)"}],
                done_text="push returns `Err(FascicleError::Full)`.")
        toy.commit("toy: three unprovable cards")

        answers = {}
        for index in (1, 2, 3):
            answers[f"CARD-{index}/test_writer"] = (
                "=== whole_file: tests/full.rs\n"
                "use toy::fascicle::Fascicle;\n\n"
                "#[test]\n"
                "fn a_full_fascicle_refuses_a_push() {\n"
                "    let fascicle = Fascicle::new(1);\n"
                "    assert_eq!(fascicle.capacity(), 1);\n"
                "}\n")
        result = cli(toy.root, "run", "--cards", "3",
                     "--script", str(script(**answers)))
        payload = json.loads(result.stdout)
        codes = [outcome.get("stop_code") for outcome in payload["outcomes"]]
        self.assertEqual(codes, ["tests-unprovable"] * 3, payload["outcomes"])

        from openfarm_core.state import StateDir
        breaker = StateDir(toy.state_dir).read_breaker()
        self.assertTrue(breaker["tripped"], breaker)
        self.assertIn("tests-unprovable", breaker["reason"])

        # A tripped breaker refuses new runs until a person clears it.
        refused = cli(toy.root, "run", "--cards", "1")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("breaker", json.loads(refused.stdout)["reason"])


if __name__ == "__main__":
    unittest.main()
