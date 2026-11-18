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


import importlib.util

HAS_PYTEST = importlib.util.find_spec("pytest") is not None


@unittest.skipUnless(HAS_PYTEST, "the Python running the suite cannot import pytest")
class KnownFailingRowsTests(unittest.TestCase):
    """A parametrized named test with every case on `known_failing` is refused;
    one with only some cases there goes on as before."""

    def _run(self, known):
        from tests.test_python_e2e import GOOD_IMPLEMENTATION, run_cli, script
        from tests.test_v081b_packs_pytest import PARAM_TEST, PYTEST_PYPROJECT

        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        toy.write("pyproject.toml", PYTEST_PYPROJECT)
        toy.config.write_text(toy.config.read_text().replace(
            "known_failing = []", "known_failing = [" + ", ".join(f'"{k}"' for k in known) + "]"))
        toy.write_card(
            "CARD-1", title="A full bundle refuses a push", category="pinned-bugfix",
            unit=".", touches=["tally/bounded.py"],
            tests=[{"name": "test_a_full_bundle_refuses_a_push", "file": "tests/test_full.py"}],
            done_text="`Bounded(1).push(8)` after one push raises `FullError`.")
        toy.commit("tally: pytest, CARD-1")
        sp = script(**{
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + PARAM_TEST,
            "CARD-1/implementer": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: a full bundle refuses a push",
            "CARD-1/checker_pass2": "{}"})
        result = run_cli(toy.root, "run", "--card", "CARD-1", "--script", str(sp), "--cards", "1")
        return json.loads(result.stdout)["outcomes"][0]

    def test_every_case_known_failing_is_refused(self) -> None:
        rows = [f"test_a_full_bundle_refuses_a_push[{n}]" for n in (1, 2, 3)]
        outcome = self._run(rows)
        self.assertEqual(outcome["stop_code"], "ineligible", outcome)
        self.assertIn("known_failing list in every case", outcome.get("detail", ""))

    def test_some_cases_known_failing_is_not_refused_here(self) -> None:
        outcome = self._run(["test_a_full_bundle_refuses_a_push[1]"])
        self.assertNotIn("known_failing list in every case", outcome.get("detail", ""))


if __name__ == "__main__":
    unittest.main()
