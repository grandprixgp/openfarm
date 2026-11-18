"""v0.8.5: a worker role thinks at `effort` on its first attempt and at
`retry_effort` on every attempt after it (owner, 18 Nov: DeepSeek workers on
high first, max on retries up to the cap)."""

from __future__ import annotations

import json
import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openfarm_core.config import Config, ConfigError
from openfarm_core.briefs import Brief
from openfarm_core.roles import Roles as RoleRunner


def _cfg(**implementer) -> Config:
    data = tomllib.loads((ROOT / ".openfarm" / "config.toml").read_text())
    data["model"]["implementer"] = {"model": "deepseek/deepseek-v4.1-flash", **implementer}
    return Config(data, ROOT)


class ConfigTests(unittest.TestCase):
    def test_retries_take_the_retry_effort(self) -> None:
        cfg = _cfg(effort="high", retry_effort="max")
        self.assertEqual(cfg.effort_for_role("implementer", 1), ("high", "reasoning_effort"))
        self.assertEqual(cfg.effort_for_role("implementer", 2), ("max", "reasoning_effort"))
        self.assertEqual(cfg.effort_for_role("implementer", 5), ("max", "reasoning_effort"))
        self.assertEqual(cfg.effort_for_role("implementer"), ("high", "reasoning_effort"))

    def test_no_retry_effort_keeps_the_effort(self) -> None:
        cfg = _cfg(effort="high")
        self.assertEqual(cfg.effort_for_role("implementer", 3), ("high", "reasoning_effort"))

    def test_a_retry_effort_needs_a_first_one_and_a_known_name(self) -> None:
        with self.assertRaises(ConfigError):
            _cfg(retry_effort="max")
        with self.assertRaises(ConfigError):
            _cfg(effort="high", retry_effort="ludicrous")

    def test_the_dsf_workers_here_run_high_then_max(self) -> None:
        data = tomllib.loads((ROOT / ".openfarm" / "config.toml").read_text())
        cfg = Config(data, ROOT)
        for role in ("test_writer", "implementer", "checker", "planner"):
            self.assertEqual(cfg.effort_for_role(role, 1)[0], "high", role)
            self.assertEqual(cfg.effort_for_role(role, 2)[0], "max", role)


class _Backend:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        from types import SimpleNamespace

        return SimpleNamespace(content="ok", tokens_in=1, tokens_out=1, cost=0.0,
                               finish_reason="stop")


class RunnerTests(unittest.TestCase):
    def test_each_attempt_sends_its_effort(self) -> None:
        backend = _Backend()
        runner = RoleRunner(_cfg(effort="high", retry_effort="max"), backend, card_id="C-1")
        for attempt in (1, 2, 3):
            runner.call("implementer", Brief("implementer", "x"), attempt=attempt)
        self.assertEqual([c["effort"] for c in backend.calls], ["high", "max", "max"])

    def test_a_coordinator_set_effort_wins(self) -> None:
        backend = _Backend()
        runner = RoleRunner(_cfg(effort="high", retry_effort="max"), backend, card_id="C-1")
        runner.call("implementer", Brief("implementer", "x"), attempt=2, effort="medium")
        self.assertEqual(backend.calls[0]["effort"], "medium")


class FlowTests(unittest.TestCase):
    """A real (scripted) run: the retry goes out at max, the first try at high."""

    def test_the_second_attempt_runs_at_max(self) -> None:
        from tests.test_python_e2e import (GOOD_IMPLEMENTATION, GOOD_TEST,
                                           build_python_toy, run_cli, script)

        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        cfg_path = toy.root / ".openfarm" / "config.toml"
        text = cfg_path.read_text()
        text = text.replace(
            'implementer = { model = "stub", temperature = 0.3, vary_seed = true }',
            'implementer = { model = "stub", temperature = 0.3, vary_seed = true, '
            'effort = "high", retry_effort = "max" }')
        self.assertIn('retry_effort = "max"', text)
        cfg_path.write_text(text)
        toy.write_card(
            "CARD-1", title="A full bundle refuses a push", category="pinned-bugfix",
            unit=".", touches=["tally/bounded.py"],
            tests=[{"name": "test_a_full_bundle_refuses_a_push", "file": "tests/test_full.py"}],
            done_text="`Bounded(1).push(8)` after one push raises `FullError`.")
        toy.commit("tally: CARD-1 and retry effort")
        no_op = ("=== search_replace: tally/bounded.py\n<<< SEARCH\n"
                 "        self.segments.append(value)\n>>> REPLACE\n"
                 "        self.segments.append(value)  # still no check\n")
        model = script(**{
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
            "CARD-1/implementer/1/high": no_op,
            "CARD-1/implementer/2/max": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: a full bundle refuses a push",
            "CARD-1/checker_pass2": "{}"})
        result = run_cli(toy.root, "run", "--card", "CARD-1", "--cards", "1",
                         "--script", str(model))
        outcome = json.loads(result.stdout)["outcomes"][0]
        self.assertEqual(outcome["final_state"], "in-review", outcome.get("detail"))
        run_dir = next((toy.state_dir / "cards" / "CARD-1").iterdir())
        efforts = {p.name: json.loads(p.read_text()).get("effort")
                   for p in (run_dir / "responses").glob("*-implementer.json")}
        self.assertEqual(efforts, {"1-implementer.json": "high", "2-implementer.json": "max"})


class SwarmTests(unittest.TestCase):
    """With a board: the member shows the retry effort, and the coordinator's
    escalation never steps a retry down from it."""

    def test_retries_stay_at_max_on_the_board(self) -> None:
        from openfarm_core.board import Board, board_dir_for
        from openfarm_core.state import StateDir
        from tests.test_coordinator_code import _run_cli
        from tests.test_python_e2e import GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy

        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        cfg_path = toy.root / ".openfarm" / "config.toml"
        text = cfg_path.read_text().replace(
            'implementer = { model = "stub", temperature = 0.3, vary_seed = true }',
            'implementer = { model = "stub", temperature = 0.3, vary_seed = true, '
            'effort = "high", retry_effort = "max" }')
        text += ('\n[tiers.dsf]\nmodel = "stub"\n'
                 'efforts = ["low", "medium", "high", "xhigh", "max"]\n'
                 'effort_param = "reasoning_effort"\n'
                 '\n[swarm]\nenabled = true\ncoordinator = "code"\nsupervisor = "off"\n'
                 'default_tier = "dsf"\nescalate_after = 1\n')
        cfg_path.write_text(text)
        toy.write_card(
            "CARD-1", title="A full bundle refuses a push", category="pinned-bugfix",
            unit=".", touches=["tally/bounded.py"],
            tests=[{"name": "test_a_full_bundle_refuses_a_push", "file": "tests/test_full.py"}],
            done_text="`Bounded(1).push(8)` after one push raises `FullError`.")
        toy.commit("tally: CARD-1 and retry effort")

        def bad(n):
            return ("=== search_replace: tally/bounded.py\n<<< SEARCH\n"
                    "        self.segments.append(value)\n>>> REPLACE\n"
                    f"        self.segments.append(value)  # try {n}\n")
        sp = Path(toy.base) / "script.json"
        sp.write_text(json.dumps({
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
            "CARD-1/implementer/1/high": bad(1),
            "CARD-1/implementer/2/max": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: a full bundle refuses a push",
            "CARD-1/checker_pass2": "{}"}))
        code, payload, err = _run_cli(toy.root, "run", "--cards", "1", "--script", str(sp))
        self.assertEqual(code, 0, err)
        out = payload["outcomes"][0]
        self.assertEqual(out["final_state"], "in-review", out.get("detail"))
        by_eff = payload["tokens"]["by_effort"]
        self.assertGreater(by_eff.get("high", 0), 0)
        self.assertGreater(by_eff.get("max", 0), 0)
        self.assertNotIn("xhigh", by_eff)
        state = StateDir(toy.state_dir)
        board = Board(board_dir_for(state, payload["run_id"]), state_dir=state)
        posts = board.read(as_name="*", since=0, limit=None, for_model=False)
        self.assertFalse([p for p in posts if p["kind"] == "action"
                          and "effort=xhigh" in p["body"]], "no step down to xhigh")


if __name__ == "__main__":
    unittest.main()
