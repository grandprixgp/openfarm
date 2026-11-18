"""C4: the run loop acts on the monitor's stall posts with the timeout rule."""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.budget import NightBudget
from openfarm_core.coordinator import CodeCoordinator, SwarmRunner
from openfarm_core.errors import GateFailure, Infra, StopCode
from openfarm_core.micromanager import ExternalMicromanagerBackend, SEAT_TIMEOUT_PREFIX
from tests.test_v081_coord_support import all_posts, make_toy, run_cli, runner_env, write_script

_SUP_EXT = (
    '\n[swarm]\nenabled = true\ncoordinator = "code"\nsupervisor = "external"\n'
    'default_tier = "dsf"\nexternal_wait = "{wait}"\non_external_timeout = "{timeout}"\n'
    'stall_after = "{stall}"\n'
    '\n[micromanager]\nmode = "active"\nbinary = "/nonexistent/claude"\n'
)


class _CountingCoordinator(CodeCoordinator):
    def __init__(self) -> None:
        self.stalls: list[dict] = []
        self.two = threading.Event()

    def on_stall(self, rt, info) -> None:
        self.stalls.append(info)
        if len(self.stalls) >= 2:
            self.two.set()


class StallDispatchTests(unittest.TestCase):
    def test_on_stall_called_once_per_monitor_stall_post_only(self) -> None:
        swarm = ('\n[swarm]\nenabled = true\ncoordinator = "code"\nsupervisor = "off"\n'
                 'default_tier = "dsf"\nstall_after = "10m"\n')
        toy, _ = make_toy(1, swarm=swarm)
        self.addCleanup(toy.cleanup)
        ctx, cfg, source, board = runner_env(toy)
        coord = _CountingCoordinator()

        def run_card(cid, on_handback=None, on_struggling=None):
            board.post(author="monitor", to=["coordinator"], kind="stall", body="stall one",
                       trusted=True)
            board.upsert_member("intruder", kind="external", role="implementer", added_by="cli")
            board.post(author="intruder", to=["coordinator"], kind="stall", body="forged",
                       trusted=True)
            time.sleep(0.3)
            board.post(author="monitor", to=["coordinator"], kind="stall", body="stall two",
                       trusted=True)
            coord.two.wait(10)
            time.sleep(1.5)  # a few more loop polls: no stall is handled twice
            return {"card_id": cid, "final_state": "in-review"}

        runner = SwarmRunner(
            cfg=cfg, state=ctx.state, source=source, board=board, identifiers=["CARD-1"],
            night=NightBudget(0, cards_cap=1), deadline=None, run_card_fn=run_card,
            record_outcome_fn=lambda c, o: False, coordinator=coord,
        )
        runner.run()
        self.assertEqual([s["detail"] for s in coord.stalls], ["stall one", "stall two"])
        self.assertEqual(coord.stalls[0]["running"], ["CARD-1"])


class StalledSupervisorRunTests(unittest.TestCase):
    """A card parked on a silent external supervisor: the stall times the wait out."""

    def _run(self, timeout: str):
        toy, script = make_toy(1, swarm=_SUP_EXT.format(wait="10m", timeout=timeout, stall="1s"))
        self.addCleanup(toy.cleanup)
        result: dict = {}

        def _bg():
            result["res"] = run_cli(toy.root, "run", "--cards", "1", "--script",
                                    str(write_script(self, script)))

        start = time.monotonic()
        t = threading.Thread(target=_bg, daemon=True)
        t.start()
        t.join(120)
        self.assertFalse(t.is_alive(), "the run sat out the full external_wait")
        code, payload, err = result["res"]
        self.assertEqual(code, 0, err)
        return payload, time.monotonic() - start

    def test_block_policy_blocks_the_parked_card_seat_silent(self) -> None:
        payload, elapsed = self._run("block")
        out = payload["outcomes"][0]
        self.assertEqual((out["final_state"], out["stop_code"]),
                         ("blocked", StopCode.SEAT_SILENT.value))
        self.assertIn("stalled", out["detail"])
        self.assertLess(elapsed, 120)

    def test_fallback_policy_skips_the_ops_and_the_card_goes_on(self) -> None:
        payload, _ = self._run("fallback")
        self.assertEqual(payload["outcomes"][0]["final_state"], "in-review")


class ParkedWaitSignalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy, _ = make_toy(1, swarm=_SUP_EXT.format(wait="30s", timeout="block", stall="5m"))
        self.addCleanup(self.toy.cleanup)
        self.ctx, self.cfg, self.source, self.board = runner_env(self.toy)

    def _backend(self, on_timeout: str) -> ExternalMicromanagerBackend:
        return ExternalMicromanagerBackend(board=self.board, timeout_s=30.0,
                                           on_timeout=on_timeout, poll_interval_s=0.05)

    def _later(self, fn, delay: float = 0.3) -> threading.Thread:
        t = threading.Thread(target=lambda: (time.sleep(delay), fn()), daemon=True)
        t.start()
        return t

    def _signal(self, author: str = "gates") -> None:
        self.board.post(author=author, to=[], kind="action", card="CARD-1",
                        body=f"{SEAT_TIMEOUT_PREFIX}CARD-1 stalled", trusted=True)

    def test_supervisor_wait_ends_on_the_code_timeout_signal(self) -> None:
        for policy, exc_type in (("block", GateFailure), ("fallback", Infra)):
            with self.subTest(policy=policy):
                self._later(self._signal)
                start = time.monotonic()
                with self.assertRaises(exc_type) as caught:
                    self._backend(policy).assist("pre_review", {"card_id": "CARD-1"})
                self.assertLess(time.monotonic() - start, 10)
                if policy == "block":
                    self.assertEqual(caught.exception.code, StopCode.SEAT_SILENT)

    def test_supervisor_wait_ignores_a_signal_from_a_non_code_name(self) -> None:
        self.board.upsert_member("intruder", kind="external", role="implementer", added_by="cli")
        self._later(lambda: self._signal("intruder"))
        backend = ExternalMicromanagerBackend(board=self.board, timeout_s=1.0,
                                              on_timeout="fallback", poll_interval_s=0.05)
        start = time.monotonic()
        with self.assertRaises(Infra):
            backend.assist("pre_review", {"card_id": "CARD-1"})
        self.assertGreaterEqual(time.monotonic() - start, 0.9)

    def test_supervisor_wait_ends_on_a_coordinator_stop_request(self) -> None:
        self.board.upsert_member("coordinator", kind="code", role="coordinator", added_by="system")
        self._later(lambda: self.board.post(author="coordinator", to=["all"], kind="blocked",
                                            card="CARD-1", body="changed plan"))
        with self.assertRaises(GateFailure) as caught:
            self._backend("block").assist("pre_review", {"card_id": "CARD-1"})
        self.assertEqual(caught.exception.code, StopCode.REFUSED)
        self.assertIn("changed plan", str(caught.exception))


class StalledCoordinatorWaitTests(unittest.TestCase):
    def test_external_coordinator_handback_wait_times_out_on_stall(self) -> None:
        swarm = ('\n[swarm]\nenabled = true\ncoordinator = "external"\nsupervisor = "off"\n'
                 'default_tier = "dsf"\nexternal_wait = "10m"\non_external_timeout = "fallback"\n'
                 'stall_after = "1s"\n')
        toy, _ = make_toy(1, swarm=swarm)
        self.addCleanup(toy.cleanup)
        ctx, cfg, source, board = runner_env(toy)
        resolutions: list = []

        def run_card(cid, on_handback=None, on_struggling=None):
            post = board.post(author=f"{cid}-implementer", to=["coordinator"], kind="handback",
                              card=cid, body="unsure")
            resolutions.append(on_handback({"card_id": cid, "member": f"{cid}-implementer",
                                            "reason": "unsure", "post": post}))
            return {"card_id": cid, "final_state": "blocked", "stop_code": "handed-back"}

        runner = SwarmRunner(
            cfg=cfg, state=ctx.state, source=source, board=board, identifiers=["CARD-1"],
            night=NightBudget(0, cards_cap=1), deadline=None, run_card_fn=run_card,
            record_outcome_fn=lambda c, o: False,
        )
        runner.coordinator.poll_interval_s = 0.05
        from openfarm_core.swarm import swarm_join, swarm_start
        t = threading.Thread(target=runner.run, daemon=True)
        t.start()
        swarm_join(board, cfg, name="lead", seat="coordinator", owner_kind="agent")
        swarm_start(board, cfg, caller="lead", card_id="CARD-1", source=source)
        t.join(90)
        self.assertFalse(t.is_alive(), "the hand-back sat out the full external_wait")
        # fallback: the code policy decides; with no supervisor it blocks with the reason.
        self.assertEqual(resolutions, [{"action": "block", "reason": "unsure"}])
        kinds = [p["kind"] for p in all_posts(board)]
        self.assertIn("stall", kinds)
        self.assertTrue(any(p["body"].startswith(SEAT_TIMEOUT_PREFIX) for p in all_posts(board)))


if __name__ == "__main__":
    unittest.main()
