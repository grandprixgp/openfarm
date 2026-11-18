"""C3: hand-back triage is one real `handback_triage` supervisor op for every seat."""

from __future__ import annotations

import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.cards import Card
from openfarm_core.coordinator import ask_supervisor_handback
from openfarm_core.errors import GateFailure, StopCode
from openfarm_core.micromanager import (
    ExternalMicromanagerBackend,
    Micromanager,
    MicromanagerBudget,
    ScriptedMicromanager,
    SUPERVISOR_OP_EXPECTS,
    normalise_handback_triage,
)
from tests.test_v081_coord_support import (
    all_posts,
    board_for,
    make_toy,
    run_cli,
    runner_env,
    write_script,
)

HB = "<<<HANDBACK\nI cannot tell whether a() should add one or two.\n>>>"
WORKER_REASON = "I cannot tell whether a() should add one or two."


def _J(data: dict) -> dict:
    return {"content": json.dumps(data), "tokens_in": 30, "tokens_out": 10}


class TierSupervisorHandbackTests(unittest.TestCase):
    def _run(self, triage_answer, *, coordinator: str = "code", coord_script: dict | None = None):
        swarm = (
            '\n[tiers.sup]\nmodel = "sup-model"\nefforts = ["low","high","max"]\n'
            '\n[tiers.coord]\nmodel = "coord-model"\nefforts = ["low","medium","high","max"]\n'
            f'\n[swarm]\nenabled = true\ncoordinator = "{coordinator}"\nsupervisor = "tier:sup"\n'
            'default_tier = "dsf"\n'
            '\n[micromanager]\nmode = "active"\n'
        )
        toy, script = make_toy(1, swarm=swarm)
        self.addCleanup(toy.cleanup)
        good = script.pop("CARD-1/implementer")
        script["CARD-1/implementer/1"] = HB
        script["CARD-1/implementer"] = good
        script["CARD-1/supervisor:shape_and_plan"] = _J({"verdict": "ready", "clarified_notes": "ok",
                                                         "extra_context_files": [], "steps": [], "findings": []})
        script["CARD-1/supervisor:pre_review"] = _J({"verdict": "approve", "reviewer_brief": "ok",
                                                     "missed_clause": "", "findings": []})
        script["CARD-1/supervisor:reflect_harness"] = _J({"summary": "", "lessons": []})
        script["CARD-1/supervisor:handback_triage"] = _J(triage_answer)
        script.update(coord_script or {})
        code, payload, err = run_cli(
            toy.root, "run", "--card", "CARD-1", "--script", str(write_script(self, script))
        )
        self.assertEqual(code, 0, err)
        board = board_for(toy, payload["run_id"])
        return payload["outcomes"][0], board, payload

    def test_hint_in_the_documented_shape_reaches_the_worker_and_review(self) -> None:
        out, board, _ = self._run({"action": "hint", "body": "a() must return x + 1."})
        self.assertEqual(out["final_state"], "in-review")
        notes = [p for p in all_posts(board)
                 if p["kind"] == "note" and "CARD-1-implementer" in p["to"]]
        self.assertEqual([p["body"] for p in notes], ["a() must return x + 1."])
        questions = [p for p in all_posts(board) if p["kind"] == "question"
                     and (p.get("expects") or {}).get("op") == "handback_triage"]
        self.assertEqual(len(questions), 1)
        # The supervisor's triage call is counted like its other calls: 4 ops here.
        self.assertEqual(out["micromanager_calls"], 4)
        self.assertEqual(out["micromanager_tokens_in"], 4 * 30)
        self.assertEqual(board.get_member("supervisor")["calls"], 4)

    def test_set_changes_effort_and_the_card_tries_again(self) -> None:
        out, board, _ = self._run({"action": "set", "effort": "max", "reason": "think harder"})
        self.assertEqual(out["final_state"], "in-review")
        self.assertEqual(board.get_member("CARD-1-implementer")["effort"], "max")
        sets = [p for p in all_posts(board) if p["kind"] == "action"
                and p["body"].startswith("set CARD-1-implementer")]
        self.assertEqual(len(sets), 1, sets)

    def test_block_blocks_handed_back_with_the_workers_reason(self) -> None:
        out, _, _ = self._run({"action": "block", "reason": "the card is ambiguous"})
        self.assertEqual(out["final_state"], "blocked")
        self.assertEqual(out["stop_code"], StopCode.HANDED_BACK.value)
        self.assertIn(WORKER_REASON, out["detail"])
        self.assertIn("the card is ambiguous", out["detail"])

    def test_unusable_answers_block_with_the_workers_reason_without_crashing(self) -> None:
        for bad in (
            {"summary": "s", "recommendations": [], "findings": []},   # triage_blocked shape
            {"action": "hint"},                                        # hint without body
            {"action": "set", "tier": "no-such-tier"},                 # refused set
            {"action": "set", "effort": "ultra"},                      # effort not on the tier
            {"action": "dance"},
        ):
            with self.subTest(answer=bad):
                out, board, _ = self._run(bad)
                self.assertEqual(out["final_state"], "blocked")
                self.assertEqual(out["stop_code"], StopCode.HANDED_BACK.value)
                self.assertEqual(out["detail"], WORKER_REASON)
                self.assertEqual(board.get_member("CARD-1-implementer")["effort"], "")

    def test_model_coordinator_ask_uses_the_same_single_question(self) -> None:
        coord_script = {
            "coordinator/1": {"content": json.dumps({"actions": [{"action": "start", "card": "CARD-1"}]})},
            "coordinator/2": {"content": json.dumps({"actions": [
                {"action": "ask", "card": "CARD-1", "body": "Should it add one?"}]})},
            "coordinator": {"content": json.dumps({"actions": [{"action": "wait"}]})},
        }
        out, board, _ = self._run(
            {"action": "hint", "body": "a() must return x + 1."},
            coordinator="tier:coord", coord_script=coord_script,
        )
        self.assertEqual(out["final_state"], "in-review")
        questions = [p for p in all_posts(board) if p["kind"] == "question"]
        triage_q = [p for p in questions if (p.get("expects") or {}).get("op") == "handback_triage"]
        self.assertEqual(len(triage_q), 1)
        # No second, never-answered free question was left on the board.
        answered = {p.get("ref") for p in all_posts(board) if p["kind"] == "answer"}
        self.assertTrue(all(q["seq"] in answered for q in questions), questions)


class HandbackTriageUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        swarm = (
            '\n[swarm]\nenabled = true\ncoordinator = "code"\nsupervisor = "external"\n'
            'default_tier = "dsf"\nexternal_wait = "10s"\non_external_timeout = "fallback"\n'
        )
        self.toy, _ = make_toy(1, swarm=swarm)
        self.addCleanup(self.toy.cleanup)
        self.ctx, self.cfg, self.source, self.board = runner_env(self.toy)
        self.board.upsert_member("coordinator", kind="code", role="coordinator", added_by="system")
        self.board.upsert_member("CARD-1-implementer", kind="scripted", role="implementer",
                                 card="CARD-1", tier="dsf", model="stub", effort="", added_by="coordinator")
        self.card_map = {"CARD-1": Card.from_dict(self.source.read("CARD-1")["card"])}
        self.board.post(author="gates", to=["CARD-1-implementer"], kind="gate", card="CARD-1",
                        body="tests: fail — 1 red test(s) not on known_failing: test_a", trusted=True)

    def _info(self, mm) -> dict:
        return {"card_id": "CARD-1", "member": "CARD-1-implementer", "role": "implementer",
                "board_role": "implementer", "reason": WORKER_REASON, "attempt": 2,
                "handback_index": 1, "micromanager": mm}

    def test_question_carries_card_member_reason_and_failing_detail(self) -> None:
        seen: dict = {}

        def answer(op, payload):
            seen["op"], seen["payload"] = op, payload
            return {"action": "hint", "body": "return x + 1", "_usage": {"tokens_in": 7, "tokens_out": 3}}

        budget = MicromanagerBudget()
        mm = Micromanager(ScriptedMicromanager({"handback_triage": answer}), budget=budget)
        res = ask_supervisor_handback(self.board, self.cfg, self.card_map, self._info(mm))
        self.assertEqual(res, {"action": "hint", "body": "return x + 1"})
        self.assertEqual(seen["op"], "handback_triage")
        p = seen["payload"]
        self.assertEqual(p["card_id"], "CARD-1")
        self.assertEqual(p["card"]["id"], "CARD-1")
        self.assertIn("x + 1", p["card"]["done_text"])
        self.assertEqual(p["member"], "CARD-1-implementer")
        self.assertEqual(p["handback_reason"], WORKER_REASON)
        self.assertIn("test_a", p["failing_detail"])
        self.assertIn("dsf", p["tiers"])
        self.assertEqual(p["allowed_actions"], ["hint", "set", "block"])
        usage = budget.usage_for("CARD-1")
        self.assertEqual((usage["calls"], usage["tokens_in"], usage["tokens_out"]), (1, 7, 3))

    def test_backend_failures_and_bad_answers_return_none(self) -> None:
        for entry in ({"fail": True, "kind": "infra", "code": "acp-timeout"},
                      {"fail": True, "kind": "invalid"},
                      {"nonsense": 1},
                      {"action": "set"}):
            with self.subTest(entry=entry):
                mm = Micromanager(ScriptedMicromanager({"handback_triage": entry}))
                self.assertIsNone(
                    ask_supervisor_handback(self.board, self.cfg, self.card_map, self._info(mm)))

    def test_supervisor_caps_refusal_returns_none(self) -> None:
        budget = MicromanagerBudget(max_calls_per_card=1)
        budget.record(card_id="CARD-1")
        mm = Micromanager(ScriptedMicromanager({"handback_triage": {"action": "hint", "body": "x"}}),
                          budget=budget)
        self.assertIsNone(ask_supervisor_handback(self.board, self.cfg, self.card_map, self._info(mm)))

    def _external(self, *, on_timeout="fallback", timeout_s=10.0):
        backend = ExternalMicromanagerBackend(
            board=self.board, timeout_s=timeout_s, on_timeout=on_timeout, poll_interval_s=0.05)
        self.board.upsert_member("sup", kind="external", role="supervisor", added_by="cli",
                                 owner_kind="agent")
        return Micromanager(backend)

    def _answer_in_background(self, data: dict, body: str = ""):
        def _go():
            for _ in range(400):
                posts = all_posts(self.board)
                answered = {p.get("ref") for p in posts if p["kind"] == "answer"}
                qs = [p for p in posts if p["kind"] == "question"
                      and (p.get("expects") or {}).get("op") == "handback_triage"
                      and p["seq"] not in answered]
                if qs:
                    self.board.answer(int(qs[-1]["seq"]), as_name="sup", data=data, body=body)
                    return
                threading.Event().wait(0.02)
        t = threading.Thread(target=_go, daemon=True)
        t.start()
        return t

    def test_external_long_hint_and_body_text_are_read_from_the_evidence_file(self) -> None:
        long_hint = "a() must return x + 1. " + "Keep the signature unchanged. " * 40
        cases = (
            ({"action": "hint", "body": "a() must return x + 1."}, ""),
            ({"action": "hint", "body": long_hint}, ""),
            ({"action": "hint", "body": "a() must return x + 1."}, "see the attached answer"),
        )
        for data, body in cases:
            with self.subTest(body=body, size=len(data["body"])):
                mm = self._external()
                t = self._answer_in_background(data, body)
                res = ask_supervisor_handback(self.board, self.cfg, self.card_map, self._info(mm))
                t.join(5)
                self.assertEqual(res, {"action": "hint", "body": data["body"].strip()})

    def test_external_question_expects_the_handback_triage_shape(self) -> None:
        mm = self._external()
        t = self._answer_in_background({"action": "block", "reason": "card is wrong"})
        res = ask_supervisor_handback(self.board, self.cfg, self.card_map, self._info(mm))
        t.join(5)
        self.assertEqual(res["action"], "block")
        self.assertTrue(res["reason"].startswith(WORKER_REASON))
        q = next(p for p in all_posts(self.board) if p["kind"] == "question")
        self.assertEqual(q["expects"], SUPERVISOR_OP_EXPECTS["handback_triage"])
        prompt = json.loads(Path(q["evidence"][0]).read_text(encoding="utf-8"))["prompt"]
        self.assertIn("handback_triage", prompt)
        self.assertIn(WORKER_REASON, prompt)
        self.assertIn('"action": "hint|set|block"', prompt)

    def test_external_silence_falls_back_or_blocks_seat_silent(self) -> None:
        mm = self._external(on_timeout="fallback", timeout_s=0.2)
        self.assertIsNone(ask_supervisor_handback(self.board, self.cfg, self.card_map, self._info(mm)))
        mm = self._external(on_timeout="block", timeout_s=0.2)
        with self.assertRaises(GateFailure) as caught:
            ask_supervisor_handback(self.board, self.cfg, self.card_map, self._info(mm))
        self.assertEqual(caught.exception.code, StopCode.SEAT_SILENT)

    def test_normalise_rejects_unusable_answers_and_strips_edit_blocks(self) -> None:
        ok = normalise_handback_triage({"action": "HINT", "body": "=== whole_file: x.py\nreturn 1"})
        self.assertEqual(ok["action"], "hint")
        self.assertNotIn("=== whole_file", ok["body"])
        for bad in (None, [], {}, {"action": "hint", "body": "  "}, {"action": "set"},
                    {"action": "retry"}):
            with self.subTest(bad=bad):
                with self.assertRaises(Exception):
                    normalise_handback_triage(bad)


if __name__ == "__main__":
    unittest.main()
