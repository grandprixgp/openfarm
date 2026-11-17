"""C5: an external coordinator that joins after the code fallback acted still steers."""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.budget import NightBudget
from openfarm_core.coordinator import CodeCoordinator, SwarmRunner
from openfarm_core.swarm import swarm_block, swarm_start
from tests.test_v081_coord_support import all_posts, make_toy, runner_env

_EXT = (
    '\n[swarm]\nenabled = true\ncoordinator = "external"\nsupervisor = "off"\n'
    'default_tier = "dsf"\nexternal_wait = "{wait}"\non_external_timeout = "fallback"\n'
)


class LateJoinTests(unittest.TestCase):
    def test_late_external_holder_start_is_launched_beside_the_code_fallback(self) -> None:
        toy, _ = make_toy(3, swarm=_EXT.format(wait="30s"), concurrent_cards=2)
        self.addCleanup(toy.cleanup)
        ctx, cfg, source, board = runner_env(toy)
        # The code fallback already acted once: its member holds the coordinator role.
        CodeCoordinator().ensure_member(board)
        started: list[str] = []
        lock = threading.Lock()

        def run_card(cid, on_handback=None, on_struggling=None):
            with lock:
                started.append(cid)
            time.sleep(0.3)
            board.post(author="gates", to=["coordinator"], kind="done", card=cid,
                       body=f"{cid} done", trusted=True)
            return {"card_id": cid, "final_state": "in-review"}

        runner = SwarmRunner(
            cfg=cfg, state=ctx.state, source=source, board=board,
            identifiers=["CARD-1", "CARD-2", "CARD-3"], night=NightBudget(0, cards_cap=3),
            deadline=None, run_card_fn=run_card, record_outcome_fn=lambda c, o: False,
        )
        runner.coordinator.poll_interval_s = 0.05
        result: dict = {}
        t = threading.Thread(target=lambda: result.setdefault("out", runner.run()), daemon=True)
        t.start()
        # Unit `board` lets this join through `swarm_join`; register the member the
        # way a successful late join does, so this test holds at the run-loop level.
        board.upsert_member("lead", kind="external", role="coordinator", state="active",
                            added_by="cli", owner_kind="agent")
        self.assertEqual(board.seat_of("lead"), "coordinator")
        self.assertEqual(board.seat_of("coordinator"), "coordinator")
        start = time.monotonic()
        swarm_start(board, cfg, caller="lead", card_id="CARD-2", source=source)
        for _ in range(300):
            if "CARD-2" in started:
                break
            time.sleep(0.01)
        # Launched at once on the external holder's checked start, long before the
        # fallback's 30s idle timeout could start anything.
        self.assertEqual(started, ["CARD-2"])
        self.assertLess(time.monotonic() - start, 5)
        for _ in range(300):
            if any(p["kind"] == "done" and p["card"] == "CARD-2" for p in all_posts(board)):
                break
            time.sleep(0.01)
        swarm_start(board, cfg, caller="lead", card_id="CARD-1", source=source)
        swarm_block(board, cfg, caller="lead", card_id="CARD-3", reason="not tonight",
                    source=source)
        t.join(30)
        self.assertFalse(t.is_alive())
        out = {o.get("card_id") or o.get("card"): o for o in result["out"]}
        self.assertEqual(out["CARD-1"]["final_state"], "in-review")
        self.assertEqual(out["CARD-2"]["final_state"], "in-review")
        self.assertEqual((out["CARD-3"]["final_state"], out["CARD-3"]["detail"]),
                         ("blocked", "not tonight"))
        self.assertEqual(sorted(started), ["CARD-1", "CARD-2"])

    def test_code_fallback_posts_still_steer_after_a_late_join(self) -> None:
        toy, _ = make_toy(1, swarm=_EXT.format(wait="30s"))
        self.addCleanup(toy.cleanup)
        ctx, cfg, source, board = runner_env(toy)
        CodeCoordinator().ensure_member(board)
        board.upsert_member("lead", kind="external", role="coordinator", state="active",
                            added_by="cli", owner_kind="agent")
        # A stop request written by the code fallback's member counts as the seat's.
        board.post(author="coordinator", to=["all"], kind="blocked", card="CARD-1",
                   body="fallback stop")
        self.assertEqual(board.stop_requested("CARD-1"), "fallback stop")


if __name__ == "__main__":
    unittest.main()
