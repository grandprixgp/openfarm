"""J7: a driver whose drive ended may take its seat back under the same name.

A drive's seat members are marked removed when it ends (`left_seat`). A second
`openfarm drive --run ID` with the same `--driver-name`, role and owner kind
joins again and takes the seat back if it is free; a seat someone else holds,
and a member the coordinator removed, are still refused. The roster shows the
rejoin.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
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
from openfarm_core.cli import EXIT_OK
from openfarm_core.cli import main as cli_main
from openfarm_core.config import load_config
from openfarm_core.driver import _end_drive, drive_run
from openfarm_core.errors import Refused
from openfarm_core.state import StateDir
from openfarm_core.swarm import new_seat_holder, swarm_join
from tests.test_python_e2e import GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy


def _cfg(external: str = "both"):
    sw = type("_Sw", (), {
        "coordinator": "external" if external in ("both", "coordinator") else "code",
        "supervisor": "external" if external in ("both", "supervisor") else "code",
        "max_members": 16})()
    return type("_Cfg", (), {"swarm": sw})()


def _roster(board: Board) -> list[str]:
    return [p.get("body", "") for p in board.read(since=0, limit=None, advance_cursor=False)
            if p.get("kind") == "roster"]


class SwarmRejoinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v081c-d2-rejoin-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.board = Board(self.tmp / "board")
        self.board.ensure()
        self.cfg = _cfg()
        self.holders: dict[str, dict] = {}

    # Each drive holds its seats with its own holder (M4, v0.8.1 seats unit): a
    # drive's end removes only the members it still holds.
    def _join(self, name: str, seat: str = "supervisor") -> dict:
        holder = new_seat_holder()
        self.holders[name] = holder
        return swarm_join(self.board, self.cfg, name=name, seat=seat, owner_kind="agent",
                          holder=holder)

    def _leave(self, name: str) -> None:
        self.assertEqual(_end_drive(board=self.board, run_thread=None, ks_path=None,
                                    joined=[name], holder=self.holders[name]), [])
        self.assertEqual(self.board.get_member(name)["state"], "removed")

    def test_a_driver_that_left_takes_its_seat_back(self) -> None:
        self._join("d-sup")
        self._leave("d-sup")
        member = self._join("d-sup")
        self.assertEqual((member["state"], member["role"], member["kind"]),
                         ("active", "supervisor", "external"))
        self.assertFalse(member.get("left_seat"))
        self.assertEqual(_roster(self.board)[-1], "d-sup rejoined seat supervisor (agent)")
        # And again: leaving and coming back works every time.
        self._leave("d-sup")
        self._join("d-sup")
        self.assertEqual(self.board.get_member("d-sup")["state"], "active")

    def test_a_seat_someone_else_holds_is_still_refused(self) -> None:
        self._join("d-sup")
        self._leave("d-sup")
        swarm_join(self.board, self.cfg, name="other", seat="supervisor", owner_kind="human")
        with self.assertRaises(Refused) as caught:
            swarm_join(self.board, self.cfg, name="d-sup", seat="supervisor", owner_kind="agent")
        self.assertEqual(caught.exception.code, "seat-occupied")
        self.assertEqual(self.board.get_member("d-sup")["state"], "removed")

    def test_another_role_kind_or_a_member_removed_by_others_stays_out(self) -> None:
        self._join("d-sup")
        self._leave("d-sup")
        for seat, kind in (("coordinator", "agent"), ("supervisor", "human")):
            with self.subTest(seat=seat, kind=kind):
                with self.assertRaises(Refused) as caught:
                    swarm_join(self.board, self.cfg, name="d-sup", seat=seat, owner_kind=kind)
                self.assertEqual(caught.exception.code, "name-taken")
        swarm_join(self.board, self.cfg, name="kicked", seat="coordinator", owner_kind="agent")
        self.board.update_member("kicked", state="removed")
        with self.assertRaises(Refused) as caught:
            swarm_join(self.board, self.cfg, name="kicked", seat="coordinator", owner_kind="agent")
        self.assertEqual(caught.exception.code, "name-taken")


class DriveTheSameRunTwiceTests(unittest.TestCase):
    """The reviewer's case: drive a live run, the drive stops, drive it again."""

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
        cfg = self.toy.root / ".openfarm" / "config.toml"
        cfg.write_text(cfg.read_text() + '\n[swarm]\nenabled = true\ncoordinator = "code"\n'
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

    def test_a_second_drive_with_the_same_name_takes_the_seat_back(self) -> None:
        run_id = "run-twice"
        result: dict = {}

        def _bg() -> None:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                result["rc"] = cli_main(["--repo", str(self.toy.root), "--json", "run",
                                         "--card", "CARD-1", "--run-id", run_id,
                                         "--script", str(self.worker)])
            result["err"] = err.getvalue()

        th = threading.Thread(target=_bg, daemon=True)
        th.start()
        self.addCleanup(th.join, 60.0)
        posts = self.toy.state_dir / "runs" / run_id / "board" / "board.jsonl"
        ev = threading.Event()
        for _ in range(500):
            if posts.is_file() or not th.is_alive():
                break
            ev.wait(0.02)
        self.assertTrue(posts.is_file(), result)

        def _drive() -> dict:
            return drive_run(self.toy.root, load_config(self.toy.root),
                             StateDir(self.toy.state_dir), agent="claude", seat="supervisor",
                             run_id=run_id, driver_name="d", script=self.driver)

        # The first drive stops at its first look at the board (a Ctrl-C, say).
        with mock.patch.object(driver_mod, "_parse_board_state",
                               side_effect=KeyboardInterrupt), \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(KeyboardInterrupt):
            _drive()
        board = Board(posts.parent, state_dir=StateDir(self.toy.state_dir))
        self.assertEqual(board.get_member("d-sup")["state"], "removed")
        self.assertTrue(th.is_alive(), "the run belongs to its own process and goes on")

        second = _drive()
        th.join(60.0)
        self.assertFalse(th.is_alive())
        self.assertEqual(result.get("rc"), EXIT_OK, result.get("err", "")[-1500:])
        self.assertEqual(second["outcomes"][0]["final_state"], "in-review", second["outcomes"])
        self.assertTrue(second["answers_sent"])
        self.assertTrue(all(not a.get("fallback") for a in second["answers_sent"]))
        roster = _roster(board)
        self.assertIn("d-sup rejoined seat supervisor (agent)", roster)
        self.assertEqual(roster.count("d-sup left seat supervisor (drive ended)"), 2, roster)


if __name__ == "__main__":
    unittest.main()
