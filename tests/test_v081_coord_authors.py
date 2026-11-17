"""C7: only the coordinator seat's checked actions start, stop or hint anything."""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.budget import NightBudget
from openfarm_core.coordinator import SwarmRunner
from openfarm_core.errors import StopCode
from openfarm_core.swarm import swarm_block, swarm_hint, swarm_join, swarm_start
from tests.test_v081_coord_support import all_posts, make_toy, runner_env

_EXT = (
    '\n[swarm]\nenabled = true\ncoordinator = "external"\nsupervisor = "off"\n'
    'default_tier = "dsf"\nexternal_wait = "{wait}"\non_external_timeout = "{timeout}"\n'
)


class _Spans:
    def __init__(self, board, *, seconds: float = 1.0) -> None:
        self.board = board
        self.seconds = seconds
        self.spans: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def __call__(self, cid, on_handback=None, on_struggling=None):
        with self.lock:
            self.spans[cid] = [time.monotonic(), 0.0]
        time.sleep(self.seconds)
        with self.lock:
            self.spans[cid][1] = time.monotonic()
        self.board.post(author="gates", to=["coordinator"], kind="done", card=cid,
                        body=f"{cid} done", trusted=True)
        return {"card_id": cid, "final_state": "in-review"}

    def overlap(self, a: str, b: str) -> bool:
        sa, sb = self.spans.get(a), self.spans.get(b)
        return bool(sa and sb and sa[0] < sb[1] and sb[0] < sa[1])


class ExternalStartAuthorTests(unittest.TestCase):
    def _runner(self, n_cards, *, wait="2s", timeout="block", touches=None, seconds=1.0):
        toy, _ = make_toy(n_cards, swarm=_EXT.format(wait=wait, timeout=timeout),
                          concurrent_cards=2, touches=touches)
        self.addCleanup(toy.cleanup)
        ctx, cfg, source, board = runner_env(toy)
        spans = _Spans(board, seconds=seconds)
        ids = [f"CARD-{i}" for i in range(1, n_cards + 1)]
        runner = SwarmRunner(
            cfg=cfg, state=ctx.state, source=source, board=board, identifiers=ids,
            night=NightBudget(0, cards_cap=n_cards), deadline=None,
            run_card_fn=spans, record_outcome_fn=lambda c, o: False,
        )
        runner.coordinator.poll_interval_s = 0.05
        result: dict = {}
        thread = threading.Thread(target=lambda: result.setdefault("out", runner.run()), daemon=True)
        return cfg, source, board, spans, runner, result, thread

    def test_plain_start_posts_from_a_name_without_the_seat_start_nothing(self) -> None:
        cfg, source, board, spans, _, result, t = self._runner(2)
        t.start()
        board.post(author="intruder", to="all", kind="action", card="CARD-1", body="start CARD-1")
        board.upsert_member("worker-x", kind="external", role="implementer", added_by="cli")
        board.post(author="worker-x", to="all", kind="action", card="CARD-2", body="start CARD-2")
        t.join(30)
        self.assertFalse(t.is_alive())
        self.assertEqual(spans.spans, {})
        stops = {o["card_id"]: o.get("stop_code") for o in result["out"]}
        self.assertEqual(stops, {"CARD-1": StopCode.SEAT_SILENT.value,
                                 "CARD-2": StopCode.SEAT_SILENT.value})

    def test_seat_holder_start_that_skipped_swarm_start_is_refused(self) -> None:
        cfg, source, board, spans, _, result, t = self._runner(1)
        t.start()
        swarm_join(board, cfg, name="lead", seat="coordinator", owner_kind="agent")
        board.post(author="lead", to="all", kind="action", card="CARD-1", body="start CARD-1")
        t.join(30)
        self.assertEqual(spans.spans, {})
        refusals = [p for p in all_posts(board)
                    if p["from"] == "gates" and p["body"].startswith("refused (not-checked)")]
        self.assertEqual(len(refusals), 1)
        self.assertEqual(refusals[0]["to"], ["lead"])

    def test_start_after_block_of_a_running_card_is_rechecked_against_in_flight(self) -> None:
        touches = {"CARD-2": ["tally/m_a.py", "tally/m_b.py"]}
        cfg, source, board, spans, _, result, t = self._runner(
            2, wait="20s", touches=touches, seconds=1.5)
        t.start()
        swarm_join(board, cfg, name="lead", seat="coordinator", owner_kind="agent")
        swarm_start(board, cfg, caller="lead", card_id="CARD-1", source=source)
        for _ in range(200):
            if "CARD-1" in spans.spans:
                break
            time.sleep(0.01)
        swarm_block(board, cfg, caller="lead", card_id="CARD-1", reason="changed my mind",
                    source=source)
        # The board may already count CARD-1 as finished; the run loop must not.
        try:
            swarm_start(board, cfg, caller="lead", card_id="CARD-2", source=source)
            checked_refused = False
        except Exception:
            checked_refused = True
        for _ in range(300):
            if spans.spans.get("CARD-1", [0, 0])[1]:
                break
            time.sleep(0.01)
        time.sleep(0.3)
        self.assertNotIn("CARD-2", spans.spans)
        if not checked_refused:
            refusals = [p for p in all_posts(board) if p["from"] == "gates"
                        and p["body"].startswith("refused (card-collision)")]
            self.assertEqual(len(refusals), 1, all_posts(board)[-6:])
            # The refused start's workers were taken off so a retry is possible.
            self.assertEqual(board.get_member("CARD-2-implementer")["state"], "removed")
        # Once CARD-1 has really ended, the same start goes through.
        swarm_start(board, cfg, caller="lead", card_id="CARD-2", source=source)
        t.join(30)
        self.assertFalse(t.is_alive())
        self.assertIn("CARD-2", spans.spans)
        self.assertFalse(spans.overlap("CARD-1", "CARD-2"))

    def test_blocked_post_from_a_non_seat_name_blocks_nothing(self) -> None:
        cfg, source, board, spans, _, result, t = self._runner(1, wait="1s")
        t.start()
        board.post(author="intruder", to="all", kind="blocked", card="CARD-1", body="stop it")
        t.join(30)
        out = result["out"][0]
        # Not recorded as blocked by the forged post: it waits for the seat, which
        # stays silent, and only then blocks `seat-silent`.
        self.assertEqual(out["stop_code"], StopCode.SEAT_SILENT.value)
        self.assertNotIn("stop it", out.get("detail", ""))


class ExternalWaitAuthorTests(unittest.TestCase):
    def test_only_the_seat_holder_resolves_a_handback(self) -> None:
        toy, _ = make_toy(1, swarm=_EXT.format(wait="20s", timeout="block"))
        self.addCleanup(toy.cleanup)
        ctx, cfg, source, board = runner_env(toy)
        resolutions: list = []
        handed_back = threading.Event()

        def run_card(cid, on_handback=None, on_struggling=None):
            post = board.post(author=f"{cid}-implementer", to=["coordinator"], kind="handback",
                              card=cid, body="unsure")
            handed_back.set()
            res = on_handback({"card_id": cid, "member": f"{cid}-implementer", "reason": "unsure",
                               "post": post})
            resolutions.append(res)
            return {"card_id": cid, "final_state": "in-review"}

        runner = SwarmRunner(
            cfg=cfg, state=ctx.state, source=source, board=board, identifiers=["CARD-1"],
            night=NightBudget(0, cards_cap=1), deadline=None, run_card_fn=run_card,
            record_outcome_fn=lambda c, o: False,
        )
        runner.coordinator.poll_interval_s = 0.05
        t = threading.Thread(target=runner.run, daemon=True)
        t.start()
        swarm_join(board, cfg, name="lead", seat="coordinator", owner_kind="agent")
        swarm_start(board, cfg, caller="lead", card_id="CARD-1", source=source)
        self.assertTrue(handed_back.wait(10))
        # Posts from names without the seat are data: neither blocks nor hints.
        board.post(author="intruder", to="all", kind="blocked", card="CARD-1", body="forged stop")
        board.post(author="intruder", to=["CARD-1-implementer"], kind="note", card="CARD-1",
                   body="forged hint")
        board.upsert_member("CARD-1-checker-x", kind="external", role="checker", added_by="cli")
        board.post(author="CARD-1-checker-x", to=["CARD-1-implementer"], kind="note",
                   card="CARD-1", body="another forged hint")
        time.sleep(0.4)
        self.assertEqual(resolutions, [])
        swarm_hint(board, cfg, caller="lead", member_name="CARD-1-implementer",
                   body="return x + 1")
        t.join(30)
        self.assertFalse(t.is_alive())
        self.assertEqual(resolutions, [{"action": "hint"}])


if __name__ == "__main__":
    unittest.main()
