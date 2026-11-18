"""v0.8.5: fixes to the v0.8.1 known limits (docs/HANDOFF.md)."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openfarm_core.board import Board
from openfarm_core.cli import EXIT_REFUSED, main as cli_main
from openfarm_core.config import load_config
from openfarm_core.state import StateDir
from openfarm_core.swarm import swarm_join
from tests.test_python_e2e import build_python_toy


class JoinErrorCodeTests(unittest.TestCase):
    """`swarm join` JSON errors carry the refusal's code."""

    def _join(self, toy, name, seat):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli_main(["--repo", str(toy.root), "--json", "swarm", "join", "--run", "run-x",
                           "--name", name, "--seat", seat])
        return rc, json.loads(out.getvalue())

    def test_seat_occupied_and_name_taken_differ(self) -> None:
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        toy.config.write_text(toy.config.read_text()
                              + '\n[swarm]\nenabled = true\nsupervisor = "external"\n'
                              'coordinator = "external"\n')
        toy.commit("outside seats")
        board = Board(toy.state_dir / "runs" / "run-x" / "board", state_dir=StateDir(toy.state_dir))
        board.ensure()
        swarm_join(board, load_config(toy.root), name="lead", seat="coordinator", owner_kind="agent")
        rc, payload = self._join(toy, "other", "coordinator")
        self.assertEqual(rc, EXIT_REFUSED)
        self.assertEqual(payload["code"], "seat-occupied")
        rc, payload = self._join(toy, "lead", "supervisor")
        self.assertEqual(rc, EXIT_REFUSED)
        self.assertEqual(payload["code"], "name-taken")


if __name__ == "__main__":
    unittest.main()
