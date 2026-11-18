"""C6: a coordinator's block on a running card is a stop request, recorded once."""

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
from openfarm_core.errors import StopCode
from openfarm_core.swarm import swarm_block, swarm_join, swarm_start
from tests.test_v081_coord_support import make_toy, runner_env

_COORD_TIER = (
    '\n[tiers.coord]\nmodel = "coord-model"\nefforts = ["low","medium","high","max"]\n'
)


def _acts(*actions) -> dict:
    return {"content": json.dumps({"actions": list(actions)}), "tokens_in": 10, "tokens_out": 5}


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.lock = threading.Lock()

    def __call__(self, cid: str, outcome: dict) -> bool:
        with self.lock:
            self.calls.append((cid, dict(outcome)))
        return False

    def for_card(self, cid: str) -> list[dict]:
        with self.lock:
            return [o for c, o in self.calls if c == cid]


def _stoppable_card(board, *, timeout_s: float = 20.0):
    """A card thread that ends blocked `refused` once the coordinator asks it to stop."""
    def run(cid: str) -> dict:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            reason = board.stop_requested(cid)
            if reason:
                time.sleep(0.2)  # still working for a moment after the request
                board.post(author="gates", to=["coordinator"], kind="blocked", card=cid,
                           body=f"{cid} blocked (refused): {reason}", trusted=True)
                return {"card_id": cid, "final_state": "blocked", "stop_code": "refused",
                        "detail": reason, "from_thread": True}
            time.sleep(0.02)
        return {"card_id": cid, "final_state": "in-review", "from_thread": True}
    return run


class ModelCoordinatorBlockTests(unittest.TestCase):
    def test_block_on_running_card_is_a_stop_request_with_one_outcome(self) -> None:
        swarm = (
            _COORD_TIER + '\n[swarm]\nenabled = true\ncoordinator = "tier:coord"\n'
            'supervisor = "off"\ndefault_tier = "dsf"\n'
        )
        toy, _ = make_toy(3, swarm=swarm, concurrent_cards=2)
        self.addCleanup(toy.cleanup)
        ctx, cfg, source, board = runner_env(toy)
        backend = ScriptedBackend({
            "coordinator/1": _acts({"action": "start", "card": "CARD-1"},
                                   {"action": "start", "card": "CARD-2"}),
            "coordinator/2": _acts({"action": "block", "card": "CARD-2", "reason": "changed plan"},
                                   {"action": "block", "card": "CARD-3", "reason": "not tonight"},
                                   {"action": "hint", "member": "CARD-1-implementer",
                                    "body": "return x + 1"}),
            "coordinator": _acts({"action": "wait"}),
        })
        stoppable = _stoppable_card(board)
        hb_result: dict = {}

        def run_card(cid, on_handback=None, on_struggling=None):
            if cid == "CARD-1":
                # Wait until CARD-2 is really running, then hand back.
                for _ in range(500):
                    if any(p.get("body", "").startswith("claimed CARD-2")
                           for p in board.read(as_name="*", since=0, limit=None, for_model=False)):
                        break
                    time.sleep(0.01)
                hb_result["res"] = on_handback({"card_id": "CARD-1", "member": "CARD-1-implementer",
                                                "reason": "unsure", "board_role": "implementer"})
                return {"card_id": cid, "final_state": "in-review", "from_thread": True}
            board.post(author="gates", to=[], kind="note", card=cid, body=f"claimed {cid}",
                       trusted=True)
            return stoppable(cid)

        rec = _Recorder()
        night = NightBudget(0, cards_cap=3)
        runner = SwarmRunner(
            cfg=cfg, state=ctx.state, source=source, board=board,
            identifiers=["CARD-1", "CARD-2", "CARD-3"], night=night, deadline=None,
            run_card_fn=run_card, record_outcome_fn=rec, backend=backend,
        )
        outcomes = {o.get("card_id") or o.get("card"): o for o in runner.run()}

        self.assertEqual(hb_result["res"]["action"], "hint")
        # CARD-2: exactly one outcome, the thread's own, blocked `refused` with the reason.
        card2 = rec.for_card("CARD-2")
        self.assertEqual(len(card2), 1, rec.calls)
        self.assertTrue(card2[0].get("from_thread"))
        self.assertEqual(card2[0]["stop_code"], StopCode.REFUSED.value)
        self.assertEqual(card2[0]["detail"], "changed plan")
        self.assertTrue(outcomes["CARD-2"].get("from_thread"))
        # CARD-3 never started: recorded once, blocked `refused`, as before.
        card3 = rec.for_card("CARD-3")
        self.assertEqual(len(card3), 1, rec.calls)
        self.assertEqual((card3[0]["final_state"], card3[0]["stop_code"]),
                         ("blocked", StopCode.REFUSED.value))
        self.assertEqual(len(rec.for_card("CARD-1")), 1)
        self.assertEqual(len(rec.calls), 3)
        self.assertEqual(night.cards_claimed, 3)


class ExternalCoordinatorBlockTests(unittest.TestCase):
    def test_external_block_on_running_card_records_the_threads_outcome_once(self) -> None:
        swarm = (
            '\n[swarm]\nenabled = true\ncoordinator = "external"\nsupervisor = "off"\n'
            'default_tier = "dsf"\nexternal_wait = "20s"\non_external_timeout = "block"\n'
        )
        toy, _ = make_toy(1, swarm=swarm)
        self.addCleanup(toy.cleanup)
        ctx, cfg, source, board = runner_env(toy)
        rec = _Recorder()
        stoppable = _stoppable_card(board)
        runner = SwarmRunner(
            cfg=cfg, state=ctx.state, source=source, board=board,
            identifiers=["CARD-1"], night=NightBudget(0, cards_cap=1), deadline=None,
            run_card_fn=lambda cid, on_handback=None, on_struggling=None: stoppable(cid),
            record_outcome_fn=rec,
        )
        runner.coordinator.poll_interval_s = 0.05
        result: dict = {}
        t = threading.Thread(target=lambda: result.setdefault("out", runner.run()), daemon=True)
        t.start()
        swarm_join(board, cfg, name="lead", seat="coordinator", owner_kind="agent")
        swarm_start(board, cfg, caller="lead", card_id="CARD-1", source=source)
        time.sleep(0.5)
        swarm_block(board, cfg, caller="lead", card_id="CARD-1", reason="changed my mind",
                    source=source)
        t.join(30)
        self.assertFalse(t.is_alive())
        calls = rec.for_card("CARD-1")
        self.assertEqual(len(calls), 1, rec.calls)
        self.assertTrue(calls[0].get("from_thread"))
        self.assertEqual(calls[0]["detail"], "changed my mind")


if __name__ == "__main__":
    unittest.main()
