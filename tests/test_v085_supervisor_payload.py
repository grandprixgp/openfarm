"""v0.8.5: what a supervisor is sent, and that every way of supervising sends the same.

Measured offline on the 12 bench tasks with scripted workers (18 Nov 2026), the
supervisor prompts went from 233,752 to 138,862 characters with the compact form;
pre-review alone from 11,166 to 4,399 characters a call.
"""

from __future__ import annotations

import json
import unittest

from openfarm_core.driver import (
    QUESTION_PROMPT_MAX_CHARS,
    _cap_question_prompt,
    render_driver_turn,
)
from openfarm_core.harness_memory import extract_contrast
from openfarm_core.micromanager import (
    _build_op_prompt,
    build_supervisor_prompt,
    compact_supervisor_payload,
)


def _gate(name, result="pass", detail="", evidence=None, path="/abs/state/gates/1/x.json"):
    return {"name": name, "result": result, "detail": detail,
            "evidence": evidence or {}, "detail_path": path}


class CompactPayloadTests(unittest.TestCase):
    def test_local_paths_never_reach_a_supervisor(self) -> None:
        out = compact_supervisor_payload({
            "_card_dir": "/abs/card", "gates": [_gate("suite", detail="green")]})
        self.assertNotIn("_card_dir", out)
        self.assertNotIn("/abs/", json.dumps(out))

    def test_a_gate_that_ran_twice_is_shown_once_as_its_last_run(self) -> None:
        out = compact_supervisor_payload({"gates": [
            _gate("suite", detail="first"), _gate("lint"), _gate("suite", detail="second")]})
        self.assertEqual([g["name"] for g in out["gates"]], ["lint", "suite"])
        self.assertEqual(out["gates"][1]["detail"], "second")

    def test_a_passing_gate_is_name_result_and_detail(self) -> None:
        out = compact_supervisor_payload({"gates": [
            _gate("fail-before", detail="1 failed on base",
                  evidence={"rows": {"t": [{"status": "FAILED"}]}})]})
        self.assertEqual(out["gates"], [
            {"name": "fail-before", "result": "pass", "detail": "1 failed on base"}])

    def test_a_failing_gate_keeps_its_evidence(self) -> None:
        out = compact_supervisor_payload({"gates": [
            _gate("lint", "fail", "2 problems", {"problems": ["E1", "E2"], "fixed": []})]})
        self.assertEqual(out["gates"][0]["evidence"], {"problems": ["E1", "E2"]})

    def test_checker_and_counter_test_evidence_is_kept_on_a_pass(self) -> None:
        out = compact_supervisor_payload({"gates": [
            _gate("checker-pass2", evidence={"checker": {"misses": "empty input", "catches": ""},
                                             "counter_tests": {"written": 0}}),
            _gate("counter-test-1", evidence={"ran": True})]})
        self.assertEqual(out["gates"][0]["evidence"],
                         {"checker": {"misses": "empty input"}, "counter_tests": {"written": 0}})
        self.assertEqual(out["gates"][1]["evidence"], {"ran": True})

    def test_everything_else_is_left_as_it_was(self) -> None:
        payload = {"card": {"id": "C-1", "deps": [], "done_text": "x"},
                   "full_diff": "diff --git a b\n+x\n", "touches": ["a.py"]}
        self.assertEqual(compact_supervisor_payload(payload), payload)

    def test_the_prompt_is_compact_json(self) -> None:
        prompt = _build_op_prompt("pre_review", {"card": {"id": "C-1"}, "touches": ["a"]})
        self.assertIn('{"card":{"id":"C-1"},"touches":["a"]}', prompt)
        self.assertEqual(build_supervisor_prompt("pre_review", {"card": {"id": "C-1"}}),
                         _build_op_prompt("pre_review", {"card": {"id": "C-1"}}))


class DriverTurnTests(unittest.TestCase):
    def test_a_question_is_carried_whole_after_the_state(self) -> None:
        request = "Operation: pre_review\n" + ("x" * 10_000) + "\nTHE END"
        text = render_driver_turn({"run_id": "r1", "open_questions": [
            {"seq": 7, "op": "pre_review", "card": "C-1", "prompt": request}]})
        state, end = json.JSONDecoder().raw_decode(text)
        self.assertEqual(state["open_questions"][0]["seq"], 7)
        self.assertNotIn("prompt", state["open_questions"][0])
        rest = text[end:]
        self.assertIn("=== QUESTION #7 (pre_review, C-1) ===", rest)
        self.assertIn("THE END", rest)
        self.assertIn("=== END QUESTION #7 ===", rest)

    def test_no_questions_is_just_the_state(self) -> None:
        text = render_driver_turn({"run_id": "r1", "open_questions": []})
        self.assertEqual(json.loads(text), {"run_id": "r1", "open_questions": []})

    def test_the_cap_is_far_above_a_pre_review_and_says_when_it_cuts(self) -> None:
        self.assertGreaterEqual(QUESTION_PROMPT_MAX_CHARS, 100_000)
        short = "y" * 12_000
        self.assertEqual(_cap_question_prompt(short), short)
        long = "z" * (QUESTION_PROMPT_MAX_CHARS + 50)
        capped = _cap_question_prompt(long)
        self.assertIn("cut the last 50 characters", capped)


class ReflectOnTests(unittest.TestCase):
    def test_a_clean_first_try_pass_is_kind_pass(self) -> None:
        contrast = extract_contrast({"id": "C-1", "final_state": "in-review"},
                                    [{"attempt": 1, "outcome": "pass", "status": "pass"}])
        self.assertEqual(contrast["outcome_kind"], "pass")

    def test_the_default_leaves_clean_passes_out(self) -> None:
        from openfarm_core.config import DEFAULT_HARNESS_REFLECT_ON, HarnessConfig

        self.assertNotIn("pass", DEFAULT_HARNESS_REFLECT_ON)
        self.assertEqual(HarnessConfig().reflect_on, DEFAULT_HARNESS_REFLECT_ON)
        for kind in ("retry_recovery", "fail", "counter_fail"):
            self.assertIn(kind, DEFAULT_HARNESS_REFLECT_ON)


class ReflectOnRecordTests(unittest.TestCase):
    """`record_card_outcome` asks for a reflection only on the kinds listed."""

    def _record(self, reflect_on, attempts):
        import tempfile
        from pathlib import Path

        from openfarm_core.config import HarnessConfig
        from openfarm_core.harness_memory import HarnessLibrary

        calls = []

        class _Sup:
            enabled = True

            def reflect_harness(self, *args, **kwargs):
                calls.append(args)
                return {"lessons": []}

        tmp = Path(tempfile.mkdtemp(prefix="of-v085-reflect-"))
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        cfg = HarnessConfig(dir=str(tmp), reflect_on=tuple(reflect_on))
        lib = HarnessLibrary(tmp, cfg)
        lib.record_card_outcome({"id": "C-1", "final_state": "in-review",
                                 "language": "python", "kind": "pinned-bugfix"},
                                attempts, micromanager=_Sup())
        return calls

    def test_a_clean_pass_is_not_reflected_on_by_default(self) -> None:
        from openfarm_core.config import DEFAULT_HARNESS_REFLECT_ON

        clean = [{"attempt": 1, "outcome": "pass", "status": "pass"}]
        self.assertEqual(self._record(DEFAULT_HARNESS_REFLECT_ON, clean), [])
        self.assertEqual(len(self._record(("pass",), clean)), 1)


if __name__ == "__main__":
    unittest.main()
