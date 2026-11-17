"""C1: code removes a finished card's workers, whatever coordinator holds the seat."""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.budget import NightBudget
from openfarm_core.coordinator import SwarmRunner
from openfarm_core.errors import Refused
from openfarm_core.swarm import WORKER_ROLES, swarm_join, swarm_remove, swarm_start, worker_member_name
from tests.test_v081_coord_support import (
    all_posts,
    board_for,
    make_toy,
    run_cli,
    runner_env,
    write_script,
)


def _card_workers(board, card_id: str) -> list[dict]:
    return [
        m for m in board.list_members(include_removed=True)
        if m.get("card") == card_id and m.get("role") in WORKER_ROLES
    ]


class ModelCoordinatorRosterTests(unittest.TestCase):
    def test_start_only_model_coordinator_never_fills_the_roster(self) -> None:
        # 3 seat/code members + 4 workers per card: with max_members = 10 the second
        # `start` is refused unless the first card's workers were removed by code.
        swarm = (
            '\n[tiers.coord]\nmodel = "coord-model"\nefforts = ["low","medium","high","max"]\n'
            '\n[swarm]\nenabled = true\ncoordinator = "tier:coord"\nsupervisor = "off"\n'
            'default_tier = "dsf"\nmax_members = 10\n'
        )
        toy, script = make_toy(3, swarm=swarm)
        self.addCleanup(toy.cleanup)
        for k in range(1, 4):
            script[f"coordinator/{k}"] = {
                "content": json.dumps({"actions": [{"action": "start", "card": f"CARD-{k}"}]}),
                "tokens_in": 10, "tokens_out": 5,
            }
        script["coordinator"] = {
            "content": json.dumps({"actions": [{"action": "wait"}]}),
            "tokens_in": 10, "tokens_out": 5,
        }
        code, payload, err = run_cli(
            toy.root, "run", "--cards", "3", "--script", str(write_script(self, script))
        )
        self.assertEqual(code, 0, err)
        states = {o.get("card_id") or o.get("card"): o.get("final_state") for o in payload["outcomes"]}
        self.assertEqual(states, {f"CARD-{k}": "in-review" for k in range(1, 4)})

        board = board_for(toy, payload["run_id"])
        refusals = [p for p in all_posts(board) if "max-members" in str(p.get("body", ""))]
        self.assertEqual(refusals, [])
        for k in range(1, 4):
            workers = _card_workers(board, f"CARD-{k}")
            self.assertEqual(len(workers), 4)
            self.assertTrue(all(m["state"] == "removed" for m in workers), workers)
        roster_posts = [
            p for p in all_posts(board)
            if p["kind"] == "roster" and p["from"] == "gates" and p["body"].startswith("removed ")
        ]
        self.assertEqual(sorted(p["card"] for p in roster_posts), ["CARD-1", "CARD-2", "CARD-3"])
        # Those roster posts are addressed to nobody, so a model coordinator spends no turn.
        self.assertTrue(all(p["to"] == [] for p in roster_posts))
        # The finished card's members still count for the run's token report.
        self.assertIn("CARD-1-implementer", payload["tokens"]["by_member"])

    def test_model_remove_of_an_already_retired_member_is_not_a_bad_answer(self) -> None:
        swarm = (
            '\n[tiers.coord]\nmodel = "coord-model"\nefforts = ["low","medium","high","max"]\n'
            '\n[swarm]\nenabled = true\ncoordinator = "tier:coord"\nsupervisor = "off"\n'
            'default_tier = "dsf"\n'
        )
        toy, script = make_toy(2, swarm=swarm)
        self.addCleanup(toy.cleanup)
        script["coordinator/1"] = {"content": json.dumps({"actions": [{"action": "start", "card": "CARD-1"}]})}
        # On CARD-1's `finished` event the model also tidies up, which code already did.
        script["coordinator/2"] = {"content": json.dumps({"actions": [
            {"action": "remove", "member": "CARD-1-implementer"},
            {"action": "start", "card": "CARD-2"},
        ]})}
        script["coordinator"] = {"content": json.dumps({"actions": [{"action": "wait"}]})}
        code, payload, err = run_cli(
            toy.root, "run", "--cards", "2", "--script", str(write_script(self, script))
        )
        self.assertEqual(code, 0, err)
        states = [o.get("final_state") for o in payload["outcomes"]]
        self.assertEqual(states, ["in-review", "in-review"])
        board = board_for(toy, payload["run_id"])
        refusals = [p for p in all_posts(board) if str(p.get("body", "")).startswith("refused")]
        self.assertEqual(refusals, [])


class RunnerRosterTests(unittest.TestCase):
    def _fake_card(self, board):
        def run_card(cid, on_handback=None, on_struggling=None):
            board.post(author="gates", to=["coordinator"], kind="done", card=cid,
                       body=f"{cid} done", trusted=True)
            return {"card_id": cid, "final_state": "in-review"}
        return run_card

    def test_code_coordinator_run_loop_retires_workers(self) -> None:
        swarm = '\n[swarm]\nenabled = true\ncoordinator = "code"\nsupervisor = "off"\ndefault_tier = "dsf"\n'
        toy, _ = make_toy(2, swarm=swarm)
        self.addCleanup(toy.cleanup)
        ctx, cfg, source, board = runner_env(toy)
        runner = SwarmRunner(
            cfg=cfg, state=ctx.state, source=source, board=board,
            identifiers=["CARD-1", "CARD-2"], night=NightBudget(0, cards_cap=2),
            deadline=None, run_card_fn=self._fake_card(board),
            record_outcome_fn=lambda c, o: False,
        )
        outcomes = runner.run()
        self.assertEqual([o["final_state"] for o in outcomes], ["in-review", "in-review"])
        for cid in ("CARD-1", "CARD-2"):
            workers = _card_workers(board, cid)
            self.assertEqual(len(workers), 4)
            self.assertTrue(all(m["state"] == "removed" for m in workers))

    def test_external_coordinator_workers_retired_and_own_remove_fails_cleanly(self) -> None:
        swarm = (
            '\n[swarm]\nenabled = true\ncoordinator = "external"\nsupervisor = "off"\n'
            'default_tier = "dsf"\nexternal_wait = "20s"\non_external_timeout = "block"\n'
        )
        toy, _ = make_toy(1, swarm=swarm)
        self.addCleanup(toy.cleanup)
        ctx, cfg, source, board = runner_env(toy)
        finished = threading.Event()

        def run_card(cid, on_handback=None, on_struggling=None):
            board.post(author="gates", to=["coordinator"], kind="done", card=cid,
                       body=f"{cid} done", trusted=True)
            return {"card_id": cid, "final_state": "in-review"}

        runner = SwarmRunner(
            cfg=cfg, state=ctx.state, source=source, board=board,
            identifiers=["CARD-1"], night=NightBudget(0, cards_cap=1),
            deadline=None, run_card_fn=run_card, record_outcome_fn=lambda c, o: False,
        )
        runner.coordinator.poll_interval_s = 0.05
        result: dict = {}

        def _bg():
            result["outcomes"] = runner.run()
            finished.set()

        t = threading.Thread(target=_bg, daemon=True)
        t.start()
        swarm_join(board, cfg, name="lead", seat="coordinator", owner_kind="agent")
        swarm_start(board, cfg, caller="lead", card_id="CARD-1", source=source)
        self.assertTrue(finished.wait(30))
        t.join(5)
        self.assertEqual(result["outcomes"][0]["final_state"], "in-review")
        workers = _card_workers(board, "CARD-1")
        self.assertEqual(len(workers), 4)
        self.assertTrue(all(m["state"] == "removed" for m in workers))
        with self.assertRaises(Refused) as caught:
            swarm_remove(board, cfg, caller="lead",
                         member_name=worker_member_name("CARD-1", "implementer"),
                         source=source)
        self.assertEqual(caught.exception.code, "already-removed")
        # The seat holder itself is never retired.
        self.assertEqual(board.get_member("lead")["state"], "active")


if __name__ == "__main__":
    unittest.main()
