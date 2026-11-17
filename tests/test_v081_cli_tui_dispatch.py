"""U4: board and swarm commands typed in the TUI reach the real CLI handlers.

v0.8's TUI carried its own weaker copies of the board and swarm checks (and of
the glob-overlap test), reachable only if `board` or `swarm` were missing from
`cli.COMMANDS`. They are gone: the command bar runs the same `cli` code the
command line runs, and the cards view uses the swarm's own collision check.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core import cli as cli_mod  # noqa: E402
from openfarm_core import swarm as swarm_mod  # noqa: E402
from openfarm_core import tui  # noqa: E402
from openfarm_core.board import Board, board_dir_for  # noqa: E402
from openfarm_core.state import StateDir  # noqa: E402
from tests.test_v081_cli_toy import make_toy  # noqa: E402

RUN_ID = "run-tui-dispatch"

BOARD_LINES = {
    "read": f"board read --run {RUN_ID} --as '*'",
    "post": f"board post --run {RUN_ID} --as nobody --to all --body hi",
    "answer": f"board answer 1 --run {RUN_ID} --as nobody --file {{answer_file}}",
    "wait": f"board wait --run {RUN_ID} --as '*' --since 0 --timeout 0",
}
SWARM_LINES = {
    "status": f"swarm status --run {RUN_ID}",
    "tiers": "swarm tiers",
    "join": f"swarm join --run {RUN_ID} --name ext --seat coordinator",
    "start": f"swarm start CARD-1 --run {RUN_ID} --as nobody",
    "set": f"swarm set CARD-1-implementer --run {RUN_ID} --as nobody --effort low",
    "hint": f"swarm hint CARD-1-implementer --run {RUN_ID} --as nobody --body go",
    "block": f"swarm block CARD-1 --run {RUN_ID} --as nobody --reason stop",
    "remove": f"swarm remove CARD-1-implementer --run {RUN_ID} --as nobody",
}


class TuiDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy, _script = make_toy(1, extra_config='\n[swarm]\nenabled = true\n'
                                                      'coordinator = "external"\n')
        self.addCleanup(self.toy.cleanup)
        state = StateDir(self.toy.state_dir)
        board = Board(board_dir_for(state, RUN_ID), state_dir=state)
        board.ensure()
        board.upsert_member("gates", kind="code", role="gates", added_by="system")
        self.answer_file = self.toy.root.parent / "answer.json"
        self.answer_file.write_text("{}", encoding="utf-8")

    def _spy(self, name: str):
        real = cli_mod.COMMANDS[name]
        calls: list[str] = []

        def spy(ctx, args):
            calls.append(getattr(args, f"{name}_command", None) or "")
            return real(ctx, args)

        return spy, calls

    def test_every_board_and_swarm_command_reaches_the_cli_handler(self) -> None:
        board_spy, board_calls = self._spy("board")
        swarm_spy, swarm_calls = self._spy("swarm")
        with mock.patch.dict(cli_mod.COMMANDS, {"board": board_spy, "swarm": swarm_spy}):
            for sub, line in BOARD_LINES.items():
                line = line.format(answer_file=self.answer_file)
                with self.subTest(line=line):
                    before = len(board_calls)
                    tui.dispatch_cli_command(self.toy.root, None, line)
                    self.assertEqual(board_calls[before:], [sub])
            for sub, line in SWARM_LINES.items():
                with self.subTest(line=line):
                    before = len(swarm_calls)
                    tui.dispatch_cli_command(self.toy.root, None, line)
                    self.assertEqual(swarm_calls[before:], [sub])

    def test_the_real_seat_checks_answer(self) -> None:
        ctrl = tui.TuiController(self.toy.root, run_id=RUN_ID)
        # A name typed after --as proves nothing: the swarm's own seat check refuses
        # every steering command from a name that holds no coordinator seat.
        for sub in ("start", "block", "hint", "set", "remove"):
            with self.subTest(sub=sub):
                res = ctrl.execute_command(SWARM_LINES[sub])
                self.assertNotEqual(res["exit_code"], 0, res["output"])
                self.assertIn("refused", res["output"])
        board = Board(board_dir_for(StateDir(self.toy.state_dir), RUN_ID),
                      state_dir=StateDir(self.toy.state_dir))
        self.assertEqual(
            [p for p in board.read(as_name="*", since=0, limit=None, for_model=False)
             if p.get("from") == "nobody"], [])

    def test_the_fallback_copies_are_gone(self) -> None:
        for name in ("_fallback_validate_expects", "_fallback_board_post",
                     "_fallback_board_answer", "_dispatch_fallback_board",
                     "_dispatch_fallback_swarm", "_require_coordinator_seat",
                     "_validate_tier_effort", "_globs_overlap", "_nfa_globs_overlap",
                     "_compile_glob_tokens", "_cards_collide"):
            self.assertFalse(hasattr(tui, name), name)
        # The cards view's collision check is the swarm's own.
        self.assertIs(tui.cards_collide, swarm_mod.cards_collide)


if __name__ == "__main__":
    unittest.main()
