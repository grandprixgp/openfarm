"""M4: a seat a live drive holds cannot be taken by reusing its member name.

A drive records itself on each seat member it joins: its pid, host and the
hash of a token it keeps in memory. A join under that name is the same holder
only with the token. Anyone else is refused while the holder runs, and may take
the seat over once the holder is provably gone (pid dead on this host), with a
note on the board. A member with no holder record keeps the old rule for a
person re-joining as the same owner kind. The owner kind of an active member
never changes through a re-join. A drive's end removes only the members it
still holds.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openfarm_core import driver as driver_mod
from openfarm_core.board import Board
from openfarm_core.cli import EXIT_OK, EXIT_REFUSED
from openfarm_core.cli import main as cli_main
from openfarm_core.config import load_config
from openfarm_core.driver import _end_drive, drive_run
from openfarm_core.errors import Refused
from openfarm_core.state import StateDir, _this_host
from openfarm_core.swarm import new_seat_holder, swarm_join
from tests.test_python_e2e import GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy


def _cfg():
    sw = type("_Sw", (), {"coordinator": "external", "supervisor": "external",
                          "max_members": 16})()
    return type("_Cfg", (), {"swarm": sw})()


def _dead_pid() -> int:
    """The pid of a process that has ended (and been reaped)."""
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


def _roster(board: Board) -> list[str]:
    return [p.get("body", "") for p in board.read(since=0, limit=None, advance_cursor=False)
            if p.get("kind") == "roster"]


class SeatHolderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v081d-seats-holder-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.board = Board(self.tmp / "board")
        self.board.ensure()
        self.cfg = _cfg()

    def _join(self, holder=None, *, kind: str = "agent", name: str = "d-sup") -> dict:
        return swarm_join(self.board, self.cfg, name=name, seat="supervisor",
                          owner_kind=kind, holder=holder)

    def _gone(self, holder: dict) -> dict:
        """The same holder, as if its process had ended."""
        return {**holder, "pid": _dead_pid()}

    def test_a_join_records_its_holder_but_never_its_token(self) -> None:
        holder = new_seat_holder()
        member = self._join(holder)
        rec = member["holder"]
        self.assertEqual((rec["pid"], rec["host"]), (holder["pid"], _this_host()))
        self.assertTrue(rec["since"])
        self.assertNotIn(holder["token"], (self.tmp / "board" / "members.json").read_text())

    def test_the_same_holder_may_join_again(self) -> None:
        holder = new_seat_holder()
        first = self._join(holder)
        again = self._join(holder)
        self.assertEqual(again["state"], "active")
        self.assertEqual(again["holder"], first["holder"])

    def test_another_holder_is_refused_while_the_first_runs(self) -> None:
        first = new_seat_holder()
        self._join(first)
        with self.assertRaises(Refused) as caught:
            self._join(new_seat_holder())
        self.assertEqual(caught.exception.code, "seat-occupied")
        self.assertIn(f"pid {first['pid']} on {_this_host()}", caught.exception.message)
        self.assertTrue(driver_mod.holds_seat(self.board.get_member("d-sup"), first))

    def test_a_person_joining_under_a_drivers_name_is_refused(self) -> None:
        first = new_seat_holder()
        self._join(first)
        for kind in ("human", "agent"):
            with self.subTest(kind=kind), self.assertRaises(Refused) as caught:
                self._join(None, kind=kind)
            self.assertEqual(caught.exception.code, "seat-occupied")
        member = self.board.get_member("d-sup")
        self.assertEqual(member["owner_kind"], "agent")
        self.assertTrue(driver_mod.holds_seat(member, first))

    def test_a_seat_whose_holder_is_gone_is_taken_over_and_the_board_says_so(self) -> None:
        first = new_seat_holder()
        self._join(self._gone(first))
        second = new_seat_holder()
        member = self._join(second)
        self.assertTrue(driver_mod.holds_seat(member, second))
        self.assertIn("took_over", member)
        last = _roster(self.board)[-1]
        self.assertTrue(last.startswith("d-sup took over seat supervisor (agent) from its holder "
                                        "(pid "), last)
        self.assertIn("which is no longer running", last)
        self.assertNotIn("took_over", self.board.get_member("d-sup"))

    def test_a_person_cannot_take_over_a_gone_drives_seat_as_another_owner_kind(self) -> None:
        self._join(self._gone(new_seat_holder()))
        with self.assertRaises(Refused) as caught:
            self._join(None, kind="human")
        self.assertEqual(caught.exception.code, "seat-occupied")
        self.assertEqual(self.board.get_member("d-sup")["owner_kind"], "agent")

    def test_a_holder_on_another_host_counts_as_running(self) -> None:
        holder = {**new_seat_holder(), "host": "some-other-host", "pid": _dead_pid()}
        self._join(holder)
        with self.assertRaises(Refused) as caught:
            self._join(new_seat_holder())
        self.assertEqual(caught.exception.code, "seat-occupied")

    def test_a_drive_ending_leaves_a_seat_another_holder_has_taken(self) -> None:
        first = new_seat_holder()
        self._join(self._gone(first))
        second = new_seat_holder()
        self._join(second)
        self.assertEqual(_end_drive(board=self.board, run_thread=None, ks_path=None,
                                    joined=["d-sup"], holder=first), [])
        member = self.board.get_member("d-sup")
        self.assertEqual(member["state"], "active")
        self.assertTrue(driver_mod.holds_seat(member, second))
        self.assertNotIn("d-sup left seat supervisor (drive ended)", _roster(self.board))
        # Its own end does remove it.
        self.assertEqual(_end_drive(board=self.board, run_thread=None, ks_path=None,
                                    joined=["d-sup"], holder=second), [])
        self.assertEqual(self.board.get_member("d-sup")["state"], "removed")

    def test_a_member_with_no_holder_record_keeps_the_old_rule_for_people(self) -> None:
        self._join(None, kind="human", name="pat")
        self.assertEqual(self._join(None, kind="human", name="pat")["state"], "active")
        for kind, holder in (("agent", None), ("human", new_seat_holder()),
                             ("agent", new_seat_holder())):
            with self.subTest(kind=kind, holder=bool(holder)), \
                    self.assertRaises(Refused) as caught:
                self._join(holder, kind=kind, name="pat")
            self.assertEqual(caught.exception.code, "seat-occupied")
        member = self.board.get_member("pat")
        self.assertEqual(member["owner_kind"], "human")
        self.assertNotIn("holder", member)

    def test_a_person_joining_by_command_under_a_drivers_name_is_refused(self) -> None:
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        toy.config.write_text(toy.config.read_text()
                              + '\n[swarm]\nenabled = true\nsupervisor = "external"\n')
        toy.commit("outside supervisor")
        board = Board(toy.state_dir / "runs" / "run-x" / "board",
                      state_dir=StateDir(toy.state_dir))
        board.ensure()
        holder = new_seat_holder()
        swarm_join(board, load_config(toy.root), name="driver-sup", seat="supervisor",
                   owner_kind="agent", holder=holder)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli_main(["--repo", str(toy.root), "--json", "swarm", "join", "--run", "run-x",
                           "--name", "driver-sup", "--seat", "supervisor"])
        self.assertEqual(rc, EXIT_REFUSED, out.getvalue() + err.getvalue())
        self.assertIn("is held by a running process", out.getvalue() + err.getvalue())
        member = board.get_member("driver-sup")
        self.assertEqual(member["owner_kind"], "agent")
        self.assertTrue(driver_mod.holds_seat(member, holder))


class TwoDrivesOneNameTests(unittest.TestCase):
    """The reviewer's case: a second drive under the same name while the first runs."""

    def setUp(self) -> None:
        self.toy = build_python_toy()
        self.addCleanup(self.toy.cleanup)
        self.toy.write_card(
            "CARD-1", title="A full bundle refuses a push", category="pinned-bugfix", unit=".",
            touches=["tally/bounded.py"],
            tests=[{"name": "test_a_full_bundle_refuses_a_push", "file": "tests/test_full.py",
                    "asserts": "push raises FullError"}],
            done_text="push on a full bundle raises `FullError`.",
        )
        self.toy.config.write_text(self.toy.config.read_text()
                                   + '\n[swarm]\nenabled = true\ncoordinator = "code"\n'
                                   'supervisor = "external"\nexternal_wait = "60s"\n')
        self.toy.commit("card and an outside supervisor")
        self.worker = Path(self.toy.base) / "worker.json"
        self.worker.write_text(json.dumps({
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
            "CARD-1/implementer": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: full bundle refuses push with FullError",
            "CARD-1/checker_pass2": "{}",
        }))
        self.driver = Path(self.toy.base) / "driver.json"
        answer = lambda data: {"actions": [], "answers": [{"seq": "auto", "data": data}]}
        self.driver.write_text(json.dumps({
            "driver/*": {"actions": [], "answers": []},
            "driver/question/shape_and_plan": answer({"verdict": "ready", "clarified_notes": "ok"}),
            "driver/question/pre_review": answer({"verdict": "approve", "reviewer_brief": "ok"}),
            "driver/question/reflect_harness": answer({"lessons": []}),
        }))

    def test_the_second_drive_is_refused_and_the_first_keeps_working(self) -> None:
        run_id = "run-two-drives"
        run_result: dict = {}

        def _run() -> None:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                run_result["rc"] = cli_main(["--repo", str(self.toy.root), "--json", "run",
                                             "--card", "CARD-1", "--run-id", run_id,
                                             "--script", str(self.worker)])
            run_result["err"] = err.getvalue()

        run_th = threading.Thread(target=_run, daemon=True)
        run_th.start()
        self.addCleanup(run_th.join, 60.0)
        posts = self.toy.state_dir / "runs" / run_id / "board" / "board.jsonl"
        ev = threading.Event()
        for _ in range(500):
            if posts.is_file() or not run_th.is_alive():
                break
            ev.wait(0.02)
        self.assertTrue(posts.is_file(), run_result)
        board = Board(posts.parent, state_dir=StateDir(self.toy.state_dir))

        def _drive() -> dict:
            return drive_run(self.toy.root, load_config(self.toy.root),
                             StateDir(self.toy.state_dir), agent="claude", seat="supervisor",
                             run_id=run_id, driver_name="d", script=self.driver)

        # The first drive takes no turn until the second has tried its seat.
        second_tried = threading.Event()
        real_complete = driver_mod.DriverAgent.complete
        first_thread: list[threading.Thread] = []

        def _held_back(agent, *args, **kwargs):
            if threading.current_thread() in first_thread:
                second_tried.wait(60.0)
            return real_complete(agent, *args, **kwargs)

        first_result: dict = {}

        def _first() -> None:
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    first_result["payload"] = _drive()
                except BaseException as exc:  # noqa: BLE001 - shown by the assertion below
                    first_result["error"] = exc

        with mock.patch.object(driver_mod.DriverAgent, "complete", _held_back):
            first_th = threading.Thread(target=_first, daemon=True)
            first_thread.append(first_th)
            first_th.start()
            self.addCleanup(first_th.join, 60.0)
            for _ in range(1000):
                member = board.get_member("d-sup")
                if (member is not None and member.get("state") == "active") \
                        or not first_th.is_alive():
                    break
                ev.wait(0.02)
            member = board.get_member("d-sup")
            self.assertEqual((member or {}).get("state"), "active", (member, first_result))

            try:
                with contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(Refused) as caught:
                    _drive()
                self.assertEqual(caught.exception.code, "seat-occupied")
                self.assertIn("held by a running process", caught.exception.message)
                with self.assertRaises(Refused) as human:
                    swarm_join(board, load_config(self.toy.root), name="d-sup",
                               seat="supervisor", owner_kind="human")
                self.assertEqual(human.exception.code, "seat-occupied")
                self.assertTrue(member.get("holder"), member)
                self.assertEqual(board.get_member("d-sup")["holder"], member["holder"])
                self.assertEqual(board.get_member("d-sup")["owner_kind"], "agent")
            finally:
                second_tried.set()
            first_th.join(60.0)

        self.assertFalse(first_th.is_alive())
        self.assertNotIn("error", first_result, first_result.get("error"))
        run_th.join(60.0)
        self.assertEqual(run_result.get("rc"), EXIT_OK, run_result.get("err", "")[-1500:])
        first = first_result["payload"]
        self.assertEqual(first["outcomes"][0]["final_state"], "in-review", first["outcomes"])
        self.assertTrue(first["answers_sent"])
        self.assertEqual(first["driver_errors"], [])
        self.assertEqual(first["timeouts"], [])
        roster = _roster(board)
        self.assertEqual(roster.count("d-sup joined seat supervisor (agent)"), 1, roster)
        self.assertEqual(roster.count("d-sup left seat supervisor (drive ended)"), 1, roster)

    def test_a_drive_takes_over_a_seat_whose_holder_is_gone(self) -> None:
        run_id = "run-takeover"
        board = Board(self.toy.state_dir / "runs" / run_id / "board",
                      state_dir=StateDir(self.toy.state_dir))
        board.ensure()
        # A drive that died without ending (kill -9, say) left its seat behind.
        gone = {**new_seat_holder(), "pid": _dead_pid()}
        swarm_join(board, load_config(self.toy.root), name="d-sup", seat="supervisor",
                   owner_kind="agent", holder=gone)
        run_result: dict = {}

        def _run() -> None:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                run_result["rc"] = cli_main(["--repo", str(self.toy.root), "--json", "run",
                                             "--card", "CARD-1", "--run-id", run_id,
                                             "--script", str(self.worker)])
            run_result["err"] = err.getvalue()

        run_th = threading.Thread(target=_run, daemon=True)
        run_th.start()
        self.addCleanup(run_th.join, 60.0)
        ev = threading.Event()
        for _ in range(500):
            if board.head_seq() > 1 or not run_th.is_alive():
                break
            ev.wait(0.02)
        with contextlib.redirect_stderr(io.StringIO()):
            res = drive_run(self.toy.root, load_config(self.toy.root),
                            StateDir(self.toy.state_dir), agent="claude", seat="supervisor",
                            run_id=run_id, driver_name="d", script=self.driver)
        run_th.join(60.0)
        self.assertEqual(run_result.get("rc"), EXIT_OK, run_result.get("err", "")[-1500:])
        self.assertEqual(res["outcomes"][0]["final_state"], "in-review", res["outcomes"])
        self.assertTrue(any(n.startswith("took over seat supervisor as d-sup from a holder "
                                         f"that is no longer running (pid {gone['pid']}")
                            for n in res["notes"]), res["notes"])
        roster = _roster(board)
        self.assertTrue(any(r.startswith("d-sup took over seat supervisor (agent)")
                            for r in roster), roster)
        self.assertEqual(roster.count("d-sup left seat supervisor (drive ended)"), 1, roster)


if __name__ == "__main__":
    unittest.main()
