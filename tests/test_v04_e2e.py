"""v0.4 End-to-End integration tests: ACP micromanager, expanded cards, multi-step checkpoints, Phase A retries, real revert-check, and supervise CLI."""

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

from openfarm_core.state import read_json
from tests.toy import build_toy

ENABLED = os.environ.get("OPENFARM_E2E") == "1"
ROOT = Path(__file__).resolve().parent.parent
OPENFARM = ROOT / "openfarm"
STAND_IN = ROOT / "tests" / "acp_stand_in.py"

GOOD_TEST = """\
use toy::fascicle::{Fascicle, FascicleError};

#[test]
fn a_full_fascicle_refuses_a_push() {
    let mut fascicle = Fascicle::new(1);
    fascicle.push(7).unwrap();
    assert_eq!(fascicle.push(8), Err(FascicleError::Full));
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

SCAFFOLD_REMAINING_CAPACITY_EDIT = {
    "format": "search_replace",
    "path": "src/fascicle.rs",
    "search": (
        "    /// The capacity this fascicle was made with.\n"
        "    pub fn capacity(&self) -> usize {\n"
        "        self.capacity\n"
        "    }"
    ),
    "replace": (
        "    /// The capacity this fascicle was made with.\n"
        "    pub fn capacity(&self) -> usize {\n"
        "        self.capacity\n"
        "    }\n\n"
        "    /// How many segments can still fit.\n"
        "    pub fn remaining_capacity(&self) -> usize {\n"
        "        todo!(\"remaining_capacity\")\n"
        "    }"
    ),
}

TEST_REMAINING_CAPACITY = """\
use toy::fascicle::Fascicle;

#[test]
fn remaining_capacity_tracks_free_slots() {
    let mut fascicle = Fascicle::new(3);
    fascicle.push(10).unwrap();
    assert_eq!(fascicle.remaining_capacity(), 2);
}
"""

IMPL_REMAINING_CAPACITY = """\
=== search_replace: src/fascicle.rs
<<< SEARCH
    /// How many segments can still fit.
    pub fn remaining_capacity(&self) -> usize {
        todo!("remaining_capacity")
    }
>>> REPLACE
    /// How many segments can still fit.
    pub fn remaining_capacity(&self) -> usize {
        self.capacity.saturating_sub(self.segments.len())
    }
"""

TWO_STEP_TESTS = """\
use toy::fascicle::{Fascicle, FascicleError};

#[test]
fn step_one_rejects_over_capacity() {
    let mut fascicle = Fascicle::new(1);
    fascicle.push(7).unwrap();
    assert!(fascicle.push(8).is_err());
}

#[test]
fn step_two_returns_full_error() {
    let mut fascicle = Fascicle::new(1);
    fascicle.push(7).unwrap();
    assert_eq!(fascicle.push(8), Err(FascicleError::Full));
}
"""

# Step 1 fixes capacity refusal returning Err(OutOfRange), so step_one passes while step_two stays red!
IMPL_STEP_ONE_ONLY = """\
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
            return Err(FascicleError::OutOfRange);
        }
        self.segments.push(value);
        Ok(self.segments.len() - 1)
    }
"""

IMPL_STEP_TWO_FINISH = """\
=== search_replace: src/fascicle.rs
<<< SEARCH
            return Err(FascicleError::OutOfRange);
>>> REPLACE
            return Err(FascicleError::Full);
"""


def _write_temp_json(case: unittest.TestCase, name: str, payload: dict) -> Path:
    folder = Path(tempfile.mkdtemp(prefix="openfarm-toy-script-"))
    dirs = getattr(case, "_temp_dirs", [])
    dirs.append(folder)
    case._temp_dirs = dirs
    path = folder / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _run_cli(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(OPENFARM), "--repo", str(repo), "--json", *args],
        capture_output=True, text=True)


def _enable_acp_micromanager(toy, turns_path: Path, *, mode: str = "active",
                             extra_categories: list[str] | None = None,
                             revert_check: str | None = None) -> None:
    cfg_path = toy.root / ".openfarm" / "config.toml"
    text = cfg_path.read_text(encoding="utf-8")
    text = text.replace(
        'model = "scripted"',
        'model = "scripted"\nmicromanager = "claude-acp"',
    )
    if extra_categories:
        text = text.replace(
            'categories = ["pattern", "pinned-bugfix", "mechanical", "small-feature", "test-backfill"]',
            "categories = " + json.dumps([
                "pattern", "pinned-bugfix", "mechanical", "small-feature", "test-backfill",
                *extra_categories,
            ]),
        )
    if revert_check is not None:
        text = text.replace('revert_check = "required"', f'revert_check = "{revert_check}"')
    args_json = json.dumps([str(STAND_IN), "--mode", "script", "--script", str(turns_path)])
    mm_block = (
        "\n[micromanager]\n"
        f'mode = "{mode}"\n'
        f'binary = "{sys.executable}"\n'
        f"args = {args_json}\n"
        'tool_policy = "none"\n'
        "cost_per_1k_in = 0.003\n"
        "cost_per_1k_out = 0.015\n"
        "isolate_home = true\n"
    )
    cfg_path.write_text(text + mm_block, encoding="utf-8")
    toy.commit("toy: configure acp micromanager")


def _write_card_file(toy, card_id: str, *, title: str, state: str = "ready",
                     category: str = "pinned-bugfix", touches: list[str],
                     tests: list[dict], done_text: str, reference: str = "",
                     new_symbols: bool = False, context_files: list[str] | None = None,
                     stop_code: str = "", detail: str = "") -> Path:
    lines = [
        "+++",
        f'id = "{card_id}"',
        f'title = "{title}"',
        f'state = "{state}"',
        f'category = "{category}"',
        'unit = "toy"',
    ]
    if reference:
        lines.append(f'reference = "{reference}"')
    if new_symbols:
        lines.append("new_symbols = true")
    if context_files:
        lines.append("context_files = " + json.dumps(context_files))
    if stop_code:
        lines.append(f'stop_code = "{stop_code}"')
    if detail:
        lines.append(f'detail = "{detail}"')
    lines.append("touches = " + json.dumps(touches))
    for entry in tests:
        lines.extend([
            "",
            "[[tests]]",
            f'name = "{entry["name"]}"',
            f'file = "{entry["file"]}"',
            f'asserts = "{entry["asserts"]}"',
        ])
    lines.extend(["+++", "", "## Done when", done_text, ""])
    return toy.write(f"cards/{card_id}.md", "\n".join(lines))


def _cleanup_case_toys(case: unittest.TestCase) -> None:
    for toy in getattr(case, "_toys", []):
        toy.cleanup()
        if getattr(toy, "base", None) is not None:
            shutil.rmtree(toy.base, ignore_errors=True)
    case._toys = []
    for folder in getattr(case, "_temp_dirs", []):
        shutil.rmtree(folder, ignore_errors=True)
    case._temp_dirs = []


class CliSuperviseAndCircuitBreakerFastTests(unittest.TestCase):
    """v0.4 CLI supervise, circuit breaker, and card migration fast integration tests."""

    def setUp(self) -> None:
        self.toy = build_toy()
        self._toys = [self.toy]
        self._temp_dirs = []

    def tearDown(self) -> None:
        _cleanup_case_toys(self)

    def test_supervise_triage_and_prepare_queue_with_mm_script(self) -> None:
        _write_card_file(
            self.toy,
            "CARD-BLK",
            title="Blocked toy card",
            state="blocked",
            category="pinned-bugfix",
            touches=["src/fascicle.rs"],
            tests=[{"name": "a_full_fascicle_refuses_a_push", "file": "tests/full.rs",
                    "asserts": "Err(Full)"}],
            done_text="Return Full when capacity exceeded.",
            stop_code="attempts",
            detail="failed twice",
        )
        _write_card_file(
            self.toy,
            "CARD-RDY",
            title="Ready toy card",
            state="ready",
            category="pinned-bugfix",
            touches=["src/fascicle.rs"],
            tests=[{"name": "a_full_fascicle_refuses_a_push", "file": "tests/full.rs",
                    "asserts": "Err(Full)"}],
            done_text="Return Full when capacity exceeded.",
        )
        self.toy.commit("toy: add blocked and ready cards")

        mm_script = _write_temp_json(self, "mm_supervise.json", {
            "triage_blocked": {
                "summary": "CARD-BLK needs broader touches",
                "recommendations": [
                    {"card_id": "CARD-BLK", "action": "release", "reason": "needs split"},
                ],
                "findings": [
                    {"title": "Triage CARD-BLK follow-up", "body": "Split CARD-BLK into smaller units."},
                ],
                "_usage": {"tokens_in": 120, "tokens_out": 40, "spend": 0.012},
            },
            "prepare_queue": {
                "summary": "CARD-RDY looks good for overnight run",
                "recommendations": [
                    {"card_id": "CARD-RDY", "action": "keep"},
                ],
                "findings": [
                    {"title": "Queue note for CARD-RDY", "body": "Add edge case note."},
                ],
                "_usage": {"tokens_in": 90, "tokens_out": 30, "spend": 0.008},
            },
        })

        triage_res = _run_cli(self.toy.root, "supervise", "--triage-blocked",
                              "--mm-script", str(mm_script), "--apply")
        self.assertEqual(triage_res.returncode, 0, triage_res.stderr or triage_res.stdout)
        triage_payload = json.loads(triage_res.stdout)
        self.assertEqual(triage_payload["op"], "triage_blocked")
        self.assertIn("CARD-BLK", triage_payload["applied"]["released"])
        self.assertTrue(triage_payload["applied"]["findings"])
        self.assertEqual(triage_payload["micromanager"]["calls"], 1)

        prep_res = _run_cli(self.toy.root, "supervise", "--prepare-queue",
                            "--mm-script", str(mm_script))
        self.assertEqual(prep_res.returncode, 0, prep_res.stderr or prep_res.stdout)
        prep_payload = json.loads(prep_res.stdout)
        self.assertEqual(prep_payload["op"], "prepare_queue")
        self.assertEqual(prep_payload["summary"], "CARD-RDY looks good for overnight run")

    def test_supervise_works_with_real_acp_stand_in(self) -> None:
        turns = _write_temp_json(self, "acp_supervise_turns.json", {
            "turns": [
                {
                    "text": json.dumps({
                        "summary": "Queue verified via ACP",
                        "recommendations": [],
                        "findings": [],
                    }),
                    "usage": {"tokens_in": 200, "tokens_out": 50, "cost": 0.015},
                },
            ],
        })
        _enable_acp_micromanager(self.toy, turns)
        res = _run_cli(self.toy.root, "supervise", "--prepare-queue")
        self.assertEqual(res.returncode, 0, res.stderr or res.stdout)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["summary"], "Queue verified via ACP")
        self.assertEqual(payload["micromanager"]["calls"], 1)
        self.assertGreater(payload["micromanager"]["spend"], 0.0)

    def test_acp_l4_supervise_writes_distinct_timestamped_reports_and_artifacts(self) -> None:
        _write_card_file(
            self.toy,
            "CARD-SUP1",
            title="Ready card",
            state="ready",
            category="pinned-bugfix",
            touches=["src/fascicle.rs"],
            tests=[{"name": "a_full_fascicle_refuses_a_push", "file": "tests/full.rs",
                    "asserts": "Err(Full)"}],
            done_text="Refuse full push.",
        )
        self.toy.commit("toy: add CARD-SUP1")
        mm_script = _write_temp_json(self, "mm_sup_l4.json", {
            "prepare_queue": {
                "summary": "Prepared once",
                "recommendations": [],
                "findings": [],
                "_usage": {"tokens_in": 40, "tokens_out": 10, "spend": 0.002},
            },
        })

        r1 = _run_cli(self.toy.root, "supervise", "--prepare-queue", "--mm-script", str(mm_script))
        r2 = _run_cli(self.toy.root, "supervise", "--prepare-queue", "--mm-script", str(mm_script))
        self.assertEqual(r1.returncode, 0, r1.stderr or r1.stdout)
        self.assertEqual(r2.returncode, 0, r2.stderr or r2.stdout)
        p1 = json.loads(r1.stdout)
        p2 = json.loads(r2.stdout)
        self.assertTrue(p1.get("report_path"))
        self.assertTrue(p2.get("report_path"))
        self.assertNotEqual(p1["report_path"], p2["report_path"])
        sup_dir = self.toy.state_dir / "supervision"
        reports = sorted(sup_dir.glob("*-supervise.json"))
        self.assertEqual(len(reports), 2)
        artifact_dirs = sorted(p for p in sup_dir.iterdir() if p.is_dir())
        self.assertEqual(len(artifact_dirs), 2)
        self.assertNotEqual(artifact_dirs[0].name, artifact_dirs[1].name)


@unittest.skipUnless(ENABLED, "set OPENFARM_E2E=1 to run the cargo-backed tests")
class V04AcpAndOrchestrationE2ETests(unittest.TestCase):
    """Real cargo + real git + ACP stand-in end-to-end verification of v0.4 orchestration."""

    def setUp(self) -> None:
        self.toy = build_toy()
        self._toys = [self.toy]
        self._temp_dirs = []

    def tearDown(self) -> None:
        _cleanup_case_toys(self)

    def test_acp_full_flow_scaffold_finding_telemetry_and_dossier(self) -> None:
        turns = _write_temp_json(self, "mm_full_flow.json", {
            "turns": [
                # Turn 1: shape_and_plan
                {
                    "text": json.dumps({
                        "verdict": "ready",
                        "clarified_notes": "Implement remaining_capacity as saturating subtraction.",
                        "extra_context_files": ["src/lib.rs"],
                        "steps": [],
                        "findings": [
                            {"title": "Follow-up capacity overflow note",
                             "body": "Document saturating behavior for callers."},
                        ],
                    }),
                    "usage": {"tokens_in": 300, "tokens_out": 80, "cost": 0.021},
                },
                # Turn 2: scaffold_contract
                {
                    "text": json.dumps({
                        "edits": [SCAFFOLD_REMAINING_CAPACITY_EDIT],
                    }),
                    "usage": {"tokens_in": 250, "tokens_out": 90, "cost": 0.019},
                },
                # Turn 3: pre_review
                {
                    "text": json.dumps({
                        "verdict": "approve",
                        "reviewer_brief": "Contract scaffolded cleanly and saturation matches spec.",
                        "missed_clause": "",
                        "findings": [],
                    }),
                    "usage": {"tokens_in": 220, "tokens_out": 60, "cost": 0.014},
                },
            ],
        })
        _enable_acp_micromanager(self.toy, turns, mode="active", extra_categories=["feature"])

        _write_card_file(
            self.toy,
            "CARD-MM",
            title="Report remaining fascicle capacity",
            category="feature",
            reference="src/fascicle.rs",
            touches=["src/fascicle.rs"],
            tests=[{
                "name": "remaining_capacity_tracks_free_slots",
                "file": "tests/capacity.rs",
                "asserts": "remaining_capacity returns capacity minus len",
            }],
            done_text="Add `remaining_capacity(&self) -> usize` returning remaining slots.",
            new_symbols=True,
            context_files=["src/lib.rs"],
        )
        self.toy.commit("toy: add CARD-MM")

        script_path = _write_temp_json(self, "model_full.json", {
            "CARD-MM/test_writer": "=== whole_file: tests/capacity.rs\n" + TEST_REMAINING_CAPACITY,
            "CARD-MM/implementer": IMPL_REMAINING_CAPACITY,
            "CARD-MM/checker_pass1": "RESTATEMENT: remaining_capacity returns capacity minus len",
            "CARD-MM/checker_pass2": json.dumps({
                "traceability": [{"clause": "remaining slots", "code": "fascicle.rs", "test": "capacity.rs"}],
                "edge_cases": ["empty", "full"],
                "counter_tests": [],
            }),
        })

        res = _run_cli(self.toy.root, "run", "--card", "CARD-MM",
                       "--script", str(script_path), "--cards", "1")
        self.assertEqual(res.returncode, 0, res.stderr or res.stdout)
        payload = json.loads(res.stdout)
        outcome = payload["outcomes"][0]
        self.assertEqual(outcome["final_state"], "in-review", outcome)
        self.assertEqual(outcome["micromanager_calls"], 3)
        self.assertEqual(outcome["micromanager_tokens_in"], 770)
        self.assertEqual(outcome["micromanager_tokens_out"], 230)
        self.assertAlmostEqual(outcome["micromanager_spend"], 0.054, places=4)

        run_dir = list((self.toy.state_dir / "cards" / "CARD-MM").iterdir())[0]
        dossier = (run_dir / "dossier.md").read_text(encoding="utf-8")
        self.assertIn("Micromanager", dossier)
        self.assertIn("Contract scaffolded cleanly", dossier)
        dossier_json = read_json(run_dir / "dossier.json")
        self.assertEqual(dossier_json["6_checker"]["micromanager"]["calls"], 3)

        # CLI dossier --attempt 1 verifies M7 and gate metadata accuracy (M12)
        d_res = _run_cli(self.toy.root, "dossier", "CARD-MM", "--attempt", "1")
        self.assertEqual(d_res.returncode, 0, d_res.stderr or d_res.stdout)
        d_payload = json.loads(d_res.stdout)
        self.assertEqual(d_payload["attempt"], 1)
        self.assertIn("remaining_capacity", d_payload["9_diffs"]["attempt_diff"])
        # Finding from shape_and_plan landed on openfarm-cards branch and in card outcome
        self.assertIn("FIND-1", outcome["findings_filed"])
        find_text = subprocess.run(
            ["git", "show", "openfarm-cards:cards/FIND-1.md"],
            cwd=str(self.toy.root), capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("Follow-up capacity overflow note", find_text)

    def test_phase_a_h7_scaffold_preserved_and_h8_retried_with_coach(self) -> None:
        turns = _write_temp_json(self, "mm_phase_a_retry.json", {
            "turns": [
                # 1. shape_and_plan
                {
                    "text": json.dumps({
                        "verdict": "ready",
                        "clarified_notes": "",
                        "extra_context_files": [],
                        "steps": [],
                        "findings": [],
                    }),
                    "usage": {"tokens_in": 100, "tokens_out": 20, "cost": 0.01},
                },
                # 2. scaffold_contract
                {
                    "text": json.dumps({
                        "edits": [SCAFFOLD_REMAINING_CAPACITY_EDIT],
                    }),
                    "usage": {"tokens_in": 100, "tokens_out": 30, "cost": 0.01},
                },
                # 3. coach("test-writer") after Attempt 1 test passes on base
                {
                    "text": json.dumps({
                        "root_cause": "test asserted capacity instead of remaining_capacity",
                        "guidance": "Call fascicle.remaining_capacity() after push(10) and assert 2.",
                        "abort": False,
                        "abort_reason": "",
                    }),
                    "usage": {"tokens_in": 120, "tokens_out": 30, "cost": 0.01},
                },
                # 4. pre_review
                {
                    "text": json.dumps({
                        "verdict": "approve",
                        "reviewer_brief": "looks good after Phase A retry",
                        "missed_clause": "",
                        "findings": [],
                    }),
                    "usage": {"tokens_in": 100, "tokens_out": 20, "cost": 0.01},
                },
            ],
        })
        _enable_acp_micromanager(self.toy, turns, mode="active", extra_categories=["feature"])

        _write_card_file(
            self.toy,
            "CARD-H78",
            title="Report remaining fascicle capacity",
            category="feature",
            reference="src/fascicle.rs",
            touches=["src/fascicle.rs"],
            tests=[{
                "name": "remaining_capacity_tracks_free_slots",
                "file": "tests/capacity.rs",
                "asserts": "remaining_capacity returns 2 after 1 push in cap 3",
            }],
            done_text="Add `remaining_capacity(&self) -> usize` returning free capacity.",
            new_symbols=True,
        )
        self.toy.commit("toy: add CARD-H78")

        # Attempt 1 writes a bogus test that passes on base; H8 catches it, invokes coach, and retries Attempt 2!
        bad_matching_name_passes_on_base = """\
use toy::fascicle::Fascicle;

#[test]
fn remaining_capacity_tracks_free_slots() {
    let fascicle = Fascicle::new(3);
    assert_eq!(fascicle.capacity(), 3);
}
"""
        script_path = _write_temp_json(self, "model_h78.json", {
            "CARD-H78/test_writer/1": "=== whole_file: tests/capacity.rs\n" + bad_matching_name_passes_on_base,
            "CARD-H78/test_writer/2": "=== whole_file: tests/capacity.rs\n" + TEST_REMAINING_CAPACITY,
            "CARD-H78/implementer": IMPL_REMAINING_CAPACITY,
            "CARD-H78/checker_pass1": "RESTATEMENT: remaining_capacity returns 2 after 1 push in cap 3",
            "CARD-H78/checker_pass2": "{}",
        })

        res = _run_cli(self.toy.root, "run", "--card", "CARD-H78",
                       "--script", str(script_path), "--cards", "1")
        self.assertEqual(res.returncode, 0, res.stderr or res.stdout)
        outcome = json.loads(res.stdout)["outcomes"][0]
        self.assertEqual(outcome["final_state"], "in-review", outcome)
        # Attempt 2 test-writer retried after coach guidance & scaffold stayed intact!
        run_dir = list((self.toy.state_dir / "cards" / "CARD-H78").iterdir())[0]
        dossier = read_json(run_dir / "dossier.json")
        gates = dossier["2_gates"]
        self.assertTrue(any(g["name"] == "micromanager-scaffold" and g["result"] == "pass" for g in gates))
        coach_gates = [g for g in gates if g["name"] == "micromanager-coach"]
        self.assertTrue(coach_gates, "expected micromanager-coach gate in Phase A retry")
        coach_detail = read_json(Path(coach_gates[0]["output"])) or {}
        self.assertIn("fascicle.remaining_capacity()", json.dumps(coach_detail))

    def test_checkpointed_multi_step_phase_b_and_h1_prompt_divergence(self) -> None:
        mm_script = _write_temp_json(self, "mm_multistep.json", {
            "CARD-STEP/shape_and_plan": {
                "verdict": "ready",
                "clarified_notes": "Follow the 2-step split carefully.",
                "extra_context_files": ["src/lib.rs"],
                "steps": [
                    {"n": 1, "does": "Reject pushes beyond capacity",
                     "tests": ["step_one_rejects_over_capacity"], "touches": ["src/fascicle.rs"]},
                    {"n": 2, "does": "Return Err(Full) specifically when full",
                     "tests": ["step_two_returns_full_error"], "touches": ["src/fascicle.rs"]},
                ],
                "findings": [],
                "_usage": {"tokens_in": 100, "tokens_out": 40, "spend": 0.01},
            },
            "CARD-STEP/pre_review": {
                "verdict": "approve",
                "reviewer_brief": "Both steps landed cleanly.",
                "missed_clause": "",
                "findings": [],
                "_usage": {"tokens_in": 80, "tokens_out": 20, "spend": 0.01},
            },
        })

        self.toy.write_card(
            "CARD-STEP",
            title="Enforce capacity and return Err(Full)",
            category="pinned-bugfix",
            touches=["src/fascicle.rs"],
            tests=[
                {"name": "step_one_rejects_over_capacity", "file": "tests/two_step.rs",
                 "asserts": "second push returns Err"},
                {"name": "step_two_returns_full_error", "file": "tests/two_step.rs",
                 "asserts": "second push returns Err(Full)"},
            ],
            done_text="1. Reject pushes past capacity.\n2. Return Err(Full) specifically.",
        )
        self.toy.commit("toy: add CARD-STEP")

        script_path = _write_temp_json(self, "model_multistep.json", {
            "CARD-STEP/test_writer": "=== whole_file: tests/two_step.rs\n" + TWO_STEP_TESTS,
            "CARD-STEP/implementer/1": IMPL_STEP_ONE_ONLY,
            "CARD-STEP/implementer/2": IMPL_STEP_TWO_FINISH,
            "CARD-STEP/checker_pass1": "RESTATEMENT: full returns Err(Full) when capacity exceeded",
            "CARD-STEP/checker_pass2": "{}",
        })

        res = _run_cli(
            self.toy.root, "run", "--card", "CARD-STEP",
            "--script", str(script_path), "--mm-script", str(mm_script), "--cards", "1",
        )
        self.assertEqual(res.returncode, 0, res.stderr or res.stdout)
        outcome = json.loads(res.stdout)["outcomes"][0]
        self.assertEqual(outcome["final_state"], "in-review", outcome)
        self.assertEqual(outcome["depth"], 2)

        run_dir = list((self.toy.state_dir / "cards" / "CARD-STEP").iterdir())[0]
        brief1 = read_json(run_dir / "briefs" / "1-implementer.md")["brief"]
        brief2 = read_json(run_dir / "briefs" / "2-implementer.md")["brief"]
        self.assertIn("Reject pushes beyond capacity", brief1)
        self.assertIn("Return Err(Full) specifically", brief2)
        self.assertIn("passed step_one_rejects_over_capacity", brief2)

    def test_adjudicate_checker_dismiss_and_pre_review_bounce_and_block(self) -> None:
        cfg_path = self.toy.root / ".openfarm" / "config.toml"
        cfg_path.write_text(
            cfg_path.read_text(encoding="utf-8").replace(
                'counter_tests = "report"', 'counter_tests = "reject"'
            ),
            encoding="utf-8",
        )
        self.toy.commit("toy: set counter_tests=reject")

        # 1. Adjudicator dismisses false-reject counter test + pre_review approves
        mm_script = _write_temp_json(self, "mm_adj_bounce.json", {
            "CARD-ADJ/shape_and_plan": {
                "verdict": "ready", "clarified_notes": "",
                "extra_context_files": [], "steps": [], "findings": [],
            },
            "CARD-ADJ/adjudicate_checker": {
                "verdict": "dismiss_false_reject",
                "rationale": "Counter-test contradicts capacity invariant.",
                "fix_hint": "",
            },
            "CARD-ADJ/pre_review": {
                "verdict": "approve",
                "reviewer_brief": "False reject dismissed properly.",
                "missed_clause": "", "findings": [],
            },
        })
        self.toy.write_card(
            "CARD-ADJ",
            title="A full fascicle refuses a push",
            category="pinned-bugfix",
            touches=["src/fascicle.rs"],
            tests=[{"name": "a_full_fascicle_refuses_a_push", "file": "tests/full.rs",
                    "asserts": "Err(Full)"}],
            done_text="Refuse push when capacity is reached.",
        )
        self.toy.commit("toy: add CARD-ADJ")

        bogus_counter = {
            "name": "counter_contradiction",
            "file": "",
            "content": (
                "use toy::fascicle::Fascicle;\n"
                "#[test]\n"
                "fn counter_contradiction() {\n"
                "    let mut f = Fascicle::new(1);\n"
                "    f.push(1).unwrap();\n"
                "    assert!(f.push(2).is_ok());\n"
                "}\n"
            ),
        }
        script_adj = _write_temp_json(self, "model_adj.json", {
            "CARD-ADJ/test_writer": "=== whole_file: tests/full.rs\n" + GOOD_TEST,
            "CARD-ADJ/implementer": GOOD_IMPLEMENTATION,
            "CARD-ADJ/checker_pass1": "RESTATEMENT: full fascicle refuses push",
            "CARD-ADJ/checker_pass2": json.dumps({
                "traceability": [], "edge_cases": [], "counter_tests": [bogus_counter],
            }),
        })
        res = _run_cli(
            self.toy.root, "run", "--card", "CARD-ADJ",
            "--script", str(script_adj), "--mm-script", str(mm_script), "--cards", "1",
        )
        self.assertEqual(res.returncode, 0, res.stderr or res.stdout)
        outcome = json.loads(res.stdout)["outcomes"][0]
        self.assertEqual(outcome["final_state"], "in-review", outcome)
        self.assertEqual(outcome["counter_tests"]["failed"], 1, outcome)
        self.assertEqual(outcome["false_rejects_dismissed"], 1, outcome)

        # 2. Pre-review block in active mode triggers StopCode.SUPERVISOR_REJECT
        toy2 = build_toy()
        self._toys.append(toy2)
        mm_block = _write_temp_json(self, "mm_block.json", {
            "CARD-BLK2/shape_and_plan": {
                "verdict": "ready", "clarified_notes": "",
                "extra_context_files": [], "steps": [], "findings": [],
            },
            "CARD-BLK2/pre_review": {
                "verdict": "block",
                "reviewer_brief": "Missing safety check",
                "missed_clause": "must reject overflow",
                "findings": [],
            },
        })
        toy2.write_card(
            "CARD-BLK2",
            title="A full fascicle refuses a push",
            category="pinned-bugfix",
            touches=["src/fascicle.rs"],
            tests=[{"name": "a_full_fascicle_refuses_a_push", "file": "tests/full.rs",
                    "asserts": "Err(Full)"}],
            done_text="Refuse push when capacity is reached.",
        )
        toy2.commit("toy: add CARD-BLK2")
        script_clean = _write_temp_json(self, "model_block.json", {
            "CARD-BLK2/test_writer": "=== whole_file: tests/full.rs\n" + GOOD_TEST,
            "CARD-BLK2/implementer": GOOD_IMPLEMENTATION,
            "CARD-BLK2/checker_pass1": "RESTATEMENT: full fascicle refuses push",
            "CARD-BLK2/checker_pass2": "{}",
        })
        res_blk = _run_cli(
            toy2.root, "run", "--card", "CARD-BLK2",
            "--script", str(script_clean), "--mm-script", str(mm_block), "--cards", "1",
        )
        self.assertEqual(res_blk.returncode, 0, res_blk.stderr or res_blk.stdout)
        blk_outcome = json.loads(res_blk.stdout)["outcomes"][0]
        self.assertEqual(blk_outcome["final_state"], "blocked", blk_outcome)
        self.assertEqual(blk_outcome["stop_code"], "supervisor-reject")

    def test_pre_review_bounce_retries_phase_b_and_revert_check_cleans_worktrees(self) -> None:
        # Enable revert_check in toy config and test ACP bounce -> approve
        turns = _write_temp_json(self, "mm_bounce_turns.json", {
            "turns": [
                # 1. shape_and_plan
                {
                    "text": json.dumps({
                        "verdict": "ready", "clarified_notes": "",
                        "extra_context_files": [], "steps": [], "findings": [],
                    }),
                    "usage": {"tokens_in": 50, "tokens_out": 20, "cost": 0.005},
                },
                # 2. pre_review #1 -> bounce
                {
                    "text": json.dumps({
                        "verdict": "bounce",
                        "reviewer_brief": "Please clarify doc comment on push",
                        "missed_clause": "Update doc comment when refusing with Full",
                        "findings": [],
                    }),
                    "usage": {"tokens_in": 60, "tokens_out": 20, "cost": 0.005},
                },
                # 3. pre_review #2 -> approve
                {
                    "text": json.dumps({
                        "verdict": "approve",
                        "reviewer_brief": "Doc comment updated; ready for review.",
                        "missed_clause": "",
                        "findings": [],
                    }),
                    "usage": {"tokens_in": 60, "tokens_out": 20, "cost": 0.005},
                },
            ],
        })
        _enable_acp_micromanager(self.toy, turns, mode="active", revert_check="required")

        self.toy.write_card(
            "CARD-BNC",
            title="A full fascicle refuses a push",
            category="pinned-bugfix",
            touches=["src/fascicle.rs"],
            tests=[{"name": "a_full_fascicle_refuses_a_push", "file": "tests/full.rs",
                    "asserts": "Err(Full)"}],
            done_text="Refuse push when full.",
        )
        self.toy.commit("toy: add CARD-BNC")

        impl_v2 = GOOD_IMPLEMENTATION.replace(
            "/// Add a segment, or refuse with `Full`.",
            "/// Add a segment, or refuse with `FascicleError::Full`.",
        )
        script_path = _write_temp_json(self, "model_bounce.json", {
            "CARD-BNC/test_writer": "=== whole_file: tests/full.rs\n" + GOOD_TEST,
            "CARD-BNC/implementer/1": GOOD_IMPLEMENTATION,
            "CARD-BNC/implementer/2": impl_v2,
            "CARD-BNC/checker_pass1": "RESTATEMENT: refuse push when full",
            "CARD-BNC/checker_pass2": "{}",
        })

        res = _run_cli(self.toy.root, "run", "--card", "CARD-BNC",
                       "--script", str(script_path), "--cards", "1")
        self.assertEqual(res.returncode, 0, res.stderr or res.stdout)
        outcome = json.loads(res.stdout)["outcomes"][0]
        self.assertEqual(outcome["final_state"], "in-review", outcome)

        # Verify H1: attempt 2 received bounce diff + missed clause
        run_dir = list((self.toy.state_dir / "cards" / "CARD-BNC").iterdir())[0]
        brief2 = read_json(run_dir / "briefs" / "2-implementer.md")["brief"]
        self.assertIn("Update doc comment", brief2)
        self.assertIn("Current diff:", brief2)

        # Verify M1 real revert-check passed and cleaned up trial worktrees
        rc_gate = next(
            g for g in read_json(run_dir / "dossier.json")["2_gates"]
            if g["name"] == "revert-check"
        )
        self.assertEqual(rc_gate["result"], "pass")
        wt_list = subprocess.run(
            ["git", "worktree", "list"], cwd=str(self.toy.root),
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertNotIn("revert", wt_list)

    def test_acp_h2c_shared_night_mm_budget_cap_and_per_card_deltas_e2e(self) -> None:
        turns = _write_temp_json(self, "acp_budget_turns.json", {
            "turns": [
                {
                    "text": json.dumps({
                        "verdict": "ready",
                        "clarified_notes": "Card 1 shape ok",
                        "extra_context_files": [],
                        "steps": [],
                        "findings": [],
                    }),
                    "usage": {"tokens_in": 100, "tokens_out": 50, "cost": 0.012},
                },
                {
                    "text": json.dumps({
                        "verdict": "approve",
                        "reviewer_brief": "Card 1 approved",
                        "missed_clause": "",
                        "findings": [],
                    }),
                    "usage": {"tokens_in": 80, "tokens_out": 20, "cost": 0.008},
                },
                {
                    "text": json.dumps({
                        "verdict": "ready",
                        "clarified_notes": "Card 2 shape hits cap next",
                        "extra_context_files": [],
                        "steps": [],
                        "findings": [],
                    }),
                    "usage": {"tokens_in": 90, "tokens_out": 30, "cost": 0.010},
                },
                {
                    "text": json.dumps({
                        "verdict": "approve",
                        "reviewer_brief": "Should not run after spend cap",
                        "missed_clause": "",
                        "findings": [],
                    }),
                    "usage": {"tokens_in": 90, "tokens_out": 30, "cost": 0.050},
                },
            ],
        })
        _enable_acp_micromanager(self.toy, turns, mode="active", revert_check="off")
        cfg_path = self.toy.root / ".openfarm" / "config.toml"
        cfg_text = cfg_path.read_text(encoding="utf-8") + "spend_per_night = 0.025\n"
        cfg_path.write_text(cfg_text, encoding="utf-8")

        _write_card_file(
            self.toy,
            "CARD-B1",
            title="Budget card 1",
            category="pinned-bugfix",
            touches=["src/fascicle.rs"],
            tests=[{"name": "a_full_fascicle_refuses_a_push", "file": "tests/b1.rs",
                    "asserts": "Err(Full)"}],
            done_text="Refuse push when full.",
        )
        _write_card_file(
            self.toy,
            "CARD-B2",
            title="Budget card 2 hits shared night mm spend cap",
            category="pinned-bugfix",
            touches=["src/fascicle.rs"],
            tests=[{"name": "a_second_full_fascicle_refuses_a_push", "file": "tests/b2.rs",
                    "asserts": "Err(Full)"}],
            done_text="Refuse push when full on card 2.",
        )
        self.toy.commit("toy: add CARD-B1, CARD-B2 with mm spend_per_night")

        second_test = GOOD_TEST.replace(
            "fn a_full_fascicle_refuses_a_push()",
            "fn a_second_full_fascicle_refuses_a_push()",
        )
        script_path = _write_temp_json(self, "budget_script.json", {
            "CARD-B1/test_writer": "=== whole_file: tests/b1.rs\n" + GOOD_TEST,
            "CARD-B1/implementer": GOOD_IMPLEMENTATION,
            "CARD-B1/checker_pass1": "RESTATEMENT: refuse push",
            "CARD-B1/checker_pass2": "{}",
            "CARD-B2/test_writer": "=== whole_file: tests/b2.rs\n" + second_test,
            "CARD-B2/implementer": GOOD_IMPLEMENTATION,
            "CARD-B2/checker_pass1": "RESTATEMENT: refuse push 2",
            "CARD-B2/checker_pass2": "{}",
        })

        res = _run_cli(self.toy.root, "run", "--cards", "2", "--script", str(script_path))
        self.assertEqual(res.returncode, 0, res.stderr or res.stdout)
        run_json = json.loads(res.stdout)
        self.assertEqual(len(run_json["outcomes"]), 2)
        o1, o2 = run_json["outcomes"]
        self.assertEqual(o1["final_state"], "in-review", o1)
        # Card 2 degrades cleanly when shared night micromanager spend cap hits mid-card!
        self.assertEqual(o2["final_state"], "in-review", o2)
        self.assertAlmostEqual(o1["micromanager_spend"], 0.020, places=6)
        self.assertEqual(o1["micromanager_calls"], 2)
        self.assertAlmostEqual(o2["micromanager_spend"], 0.012, places=6)
        self.assertEqual(o2["micromanager_calls"], 1)
        self.assertAlmostEqual(run_json["micromanager_spend"], 0.032, places=6)

        run_dir_2 = list((self.toy.state_dir / "cards" / "CARD-B2").iterdir())[0]
        pre_gate = next(
            g for g in read_json(run_dir_2 / "dossier.json")["2_gates"]
            if g["name"] == "micromanager-pre-review"
        )
        self.assertEqual(pre_gate["result"], "fail")


if __name__ == "__main__":
    unittest.main()
