"""C8: a model coordinator's wait on the supervisor seat does not hold its lock."""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adapters.openai_compat import ScriptedBackend
from openfarm_core.budget import NightBudget
from openfarm_core.coordinator import SwarmRunner
from openfarm_core.micromanager import ExternalMicromanagerBackend, Micromanager
from tests.test_v081_coord_support import all_posts, make_toy, runner_env


def _acts(*actions) -> dict:
    return {"content": json.dumps({"actions": list(actions)}), "tokens_in": 10, "tokens_out": 5}


class ModelCoordinatorLockTests(unittest.TestCase):
    def _setup(self, coordinator_tier: bool = True):
        swarm = (
            '\n[tiers.coord]\nmodel = "coord-model"\nefforts = ["low","medium","high","max"]\n'
            '\n[swarm]\nenabled = true\ncoordinator = "tier:coord"\nsupervisor = "external"\n'
            'default_tier = "dsf"\nexternal_wait = "6s"\non_external_timeout = "fallback"\n'
            '\n[micromanager]\nmode = "active"\nbinary = "/nonexistent/claude"\n'
        )
        toy, _ = make_toy(2, swarm=swarm, concurrent_cards=2)
        self.addCleanup(toy.cleanup)
        return runner_env(toy)

    def _run(self, backend_script: dict):
        ctx, cfg, source, board = self._setup()
        # Someone holds the external supervisor seat, but never answers.
        board.upsert_member("sup", kind="external", role="supervisor", added_by="cli",
                            owner_kind="agent")
        backend = ScriptedBackend(backend_script)
        timings: dict = {}
        parked = threading.Event()

        def _silent_supervisor() -> Micromanager:
            return Micromanager(ExternalMicromanagerBackend(
                board=board, timeout_s=6.0, on_timeout="fallback", poll_interval_s=0.05))

        def run_card(cid, on_handback=None, on_struggling=None):
            if cid == "CARD-1":
                start = time.monotonic()
                res = on_handback({"card_id": cid, "member": "CARD-1-implementer",
                                   "reason": "unsure", "board_role": "implementer",
                                   "micromanager": _silent_supervisor()})
                timings["handback"] = (time.monotonic() - start, res)
                return {"card_id": cid, "final_state": "blocked", "stop_code": "handed-back"}
            # CARD-2 struggles while CARD-1 is parked on the silent supervisor.
            for _ in range(500):
                if any(p["kind"] == "question" for p in all_posts(board)):
                    parked.set()
                    break
                time.sleep(0.01)
            start = time.monotonic()
            on_struggling({"card_id": cid, "member": "CARD-2-implementer",
                           "failed_at_effort": 2, "detail": "red test"})
            timings["struggling"] = time.monotonic() - start
            return {"card_id": cid, "final_state": "in-review"}

        runner = SwarmRunner(
            cfg=cfg, state=ctx.state, source=source, board=board,
            identifiers=["CARD-1", "CARD-2"], night=NightBudget(0, cards_cap=2), deadline=None,
            run_card_fn=run_card, record_outcome_fn=lambda c, o: False, backend=backend,
        )
        runner.run()
        self.assertTrue(parked.is_set())
        return timings

    def test_other_cards_events_are_decided_while_an_ask_waits(self) -> None:
        timings = self._run({
            "coordinator/1": _acts({"action": "start", "card": "CARD-1"},
                                   {"action": "start", "card": "CARD-2"}),
            "CARD-1/coordinator": _acts({"action": "ask", "card": "CARD-1", "body": "one or two?"}),
            "CARD-2/coordinator": _acts({"action": "wait", "reason": "let it retry"}),
            "coordinator": _acts({"action": "wait"}),
        })
        self.assertLess(timings["struggling"], 3.0, timings)
        waited, res = timings["handback"]
        self.assertGreaterEqual(waited, 5.0)  # the silent seat's full wait, then fallback
        self.assertEqual(res["action"], "block")

    def test_code_fallback_handback_triage_also_waits_outside_the_lock(self) -> None:
        bad = {"content": "not json", "tokens_in": 1, "tokens_out": 1}
        timings = self._run({
            "coordinator/1": _acts({"action": "start", "card": "CARD-1"},
                                   {"action": "start", "card": "CARD-2"}),
            # Two unreadable answers hand the hand-back to the code policy, which asks
            # the (silent) supervisor's seat.
            "CARD-1/coordinator": bad,
            "CARD-2/coordinator": _acts({"action": "wait", "reason": "let it retry"}),
            "coordinator": _acts({"action": "wait"}),
        })
        self.assertLess(timings["struggling"], 3.0, timings)
        self.assertEqual(timings["handback"][1]["action"], "block")


if __name__ == "__main__":
    unittest.main()
