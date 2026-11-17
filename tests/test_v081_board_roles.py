"""v0.8.1 B5: a role's own `[model]` entry survives the swarm being on."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.board import Board, board_dir_for
from openfarm_core.config import Config
from openfarm_core.state import StateDir
from openfarm_core.swarm import (
    parse_team_spec,
    swarm_join,
    swarm_set,
    swarm_start,
    swarm_status,
    worker_member_name,
)
from tests.test_swarm_roster import _card


def _config(repo: Path, state_root: Path, *, tiers: dict | None = None,
            swarm: dict | None = None, model: dict | None = None) -> Config:
    data = {
        "project": {"name": "demo", "base_branch": "main", "state_dir": str(state_root)},
        "adapters": {"languages": ["python"], "model": "scripted"},
        "model": {
            "endpoint": "https://api.example.invalid/v1",
            "allowed_endpoints": ["https://api.example.invalid/v1"],
            "headers": {"x-cmd-zdr": "1"},
            "implementer": {"model": "stub"},
            "checker": {"model": "independent-checker", "effort": "high"},
            **(model or {}),
        },
        "swarm": {"enabled": True, "coordinator": "external", "supervisor": "off",
                  **(swarm or {})},
        "policy": {"categories": ["pattern"]},
        "budget": {"concurrent_cards": 2},
    }
    if tiers is not None:
        data["tiers"] = tiers
    return Config(data, repo)


class RoleModelMembersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-v081-roles-",
                                         dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.state = StateDir(self.tmp / "state")
        self.cards = {"CARD-1": _card("CARD-1"), "CARD-2": _card("CARD-2")}

    def _board(self, cfg: Config, run_id: str = "run-r") -> Board:
        board = Board(board_dir_for(self.state, run_id), state_dir=self.state)
        board.ensure()
        swarm_join(board, cfg, name="lead", seat="coordinator", owner_kind="agent")
        return board

    def test_no_tiers_table_checker_keeps_its_model(self) -> None:
        cfg = _config(self.repo, self.state.root)
        board = self._board(cfg)
        res = swarm_start(board, cfg, caller="lead", card_id="CARD-1", cards_by_id=self.cards)
        team = res["team"]
        self.assertEqual(team["checker"]["model"], "independent-checker")
        self.assertEqual(team["checker"]["effort"], "high")
        self.assertEqual(team["implementer"]["model"], "stub")
        self.assertEqual(team["planner"]["model"], "stub")
        # effort_param for the checker comes from the tier that serves its model,
        # the same place the swarm-off path takes it from
        self.assertEqual(
            cfg.tiers[team["checker"]["tier"]].effort_param,
            cfg.effort_for_role("checker")[1],
        )
        # the start post and swarm status say which model is really called
        posts = board.read(as_name="*", since=0, limit=None, for_model=False)
        start = next(p for p in posts if p["kind"] == "action" and p["card"] == "CARD-1")
        self.assertTrue(start["body"].startswith("start CARD-1 "))
        self.assertIn("checker=independent-checker", start["body"])
        status = swarm_status(board, cfg)
        by_name = {m["name"]: m for m in status["members"]}
        self.assertEqual(
            by_name[worker_member_name("CARD-1", "checker")]["model"], "independent-checker"
        )

    def test_named_tier_replaces_the_role_model(self) -> None:
        cfg = _config(self.repo, self.state.root)
        board = self._board(cfg)
        res = swarm_start(
            board, cfg, caller="lead", card_id="CARD-1", cards_by_id=self.cards,
            team=parse_team_spec("checker=default:low"),
        )
        self.assertEqual(res["team"]["checker"]["model"], "stub")
        self.assertEqual(res["team"]["checker"]["effort"], "low")

    def test_set_effort_keeps_model_and_set_tier_replaces_it(self) -> None:
        cfg = _config(
            self.repo, self.state.root,
            tiers={
                "dsf": {"model": "stub", "efforts": ["low", "medium", "high", "max"]},
                "fast": {"model": "fast-model", "efforts": ["low", "high"]},
            },
            swarm={"default_tier": "dsf"},
        )
        board = self._board(cfg)
        swarm_start(board, cfg, caller="lead", card_id="CARD-1", cards_by_id=self.cards)
        checker = worker_member_name("CARD-1", "checker")
        self.assertEqual(board.get_member(checker)["model"], "independent-checker")
        after_effort = swarm_set(board, cfg, caller="lead", member_name=checker, effort="max")
        self.assertEqual(after_effort["member"]["model"], "independent-checker")
        self.assertEqual(after_effort["member"]["effort"], "max")
        after_tier = swarm_set(
            board, cfg, caller="lead", member_name=checker, tier="fast", effort="low"
        )
        self.assertEqual(after_tier["member"]["model"], "fast-model")
        self.assertEqual(after_tier["member"]["tier"], "fast")

    def test_tiers_table_role_model_wins_over_default_tier(self) -> None:
        # [tiers] present with default_tier: the implementer (the base model)
        # follows default_tier; a role with its own model keeps it, served by the
        # tier that names that model (here `chk`, so its effort_param applies).
        cfg = _config(
            self.repo, self.state.root,
            tiers={
                "dsf": {"model": "deepseek/flash", "efforts": ["low", "high", "max"]},
                "chk": {"model": "independent-checker", "efforts": ["high"],
                        "effort_param": "effort_level"},
            },
            swarm={"default_tier": "dsf"},
        )
        board = self._board(cfg)
        team = swarm_start(
            board, cfg, caller="lead", card_id="CARD-1", cards_by_id=self.cards
        )["team"]
        self.assertEqual(team["implementer"]["model"], "deepseek/flash")
        self.assertEqual(team["implementer"]["tier"], "dsf")
        self.assertEqual(team["checker"]["model"], "independent-checker")
        self.assertEqual(team["checker"]["tier"], "chk")
        self.assertEqual(cfg.tiers[team["checker"]["tier"]].effort_param, "effort_level")
        self.assertEqual(cfg.effort_for_role("checker"), ("high", "effort_level"))

    def test_role_without_its_own_model_takes_default_tier(self) -> None:
        cfg = _config(
            self.repo, self.state.root,
            tiers={"dsf": {"model": "deepseek/flash"}},
            swarm={"default_tier": "dsf"},
            model={"checker": {"model": "stub"}},  # same as implementer: no own model
        )
        board = self._board(cfg)
        team = swarm_start(
            board, cfg, caller="lead", card_id="CARD-1", cards_by_id=self.cards
        )["team"]
        for role in ("planner", "test-writer", "implementer", "checker"):
            self.assertEqual(team[role]["model"], "deepseek/flash", role)


class RoleModelRunTests(unittest.TestCase):
    """The repro's case I: the checker is called with its own model, swarm on or off."""

    def _run(self, swarm: str) -> list[str]:
        from tests.test_monitor import _run_cli
        from tests.test_python_e2e import build_python_toy

        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        toy.write("tally/m_a.py", "def a(x: int) -> int:\n    return x\n")
        toy.commit("add module")
        cfg_path = toy.root / ".openfarm" / "config.toml"
        text = cfg_path.read_text(encoding="utf-8")
        marker = 'checker     = { model = "stub"'
        self.assertIn(marker, text)
        text = text.replace(marker, 'checker     = { model = "independent-checker"')
        cfg_path.write_text(text + swarm, encoding="utf-8")
        toy.write_card(
            "CARD-1", title="Mod a", category="pinned-bugfix", unit=".",
            touches=["tally/m_a.py"],
            tests=[{"name": "test_a", "file": "tests/test_a.py", "asserts": "a(1)==2"}],
            done_text="`a(x)` returns `x + 1`.",
        )
        toy.commit("add cards")
        sp = toy.base / "script.json"
        sp.write_text(json.dumps({
            "CARD-1/test_writer": (
                "=== whole_file: tests/test_a.py\nimport unittest\nfrom tally.m_a import a\n"
                "class T(unittest.TestCase):\n    def test_a(self): self.assertEqual(a(1), 2)\n"
            ),
            "CARD-1/implementer": (
                "=== whole_file: tally/m_a.py\ndef a(x: int) -> int:\n    return x + 1\n"
            ),
            "CARD-1/checker_pass1": "RESTATEMENT: a returns x + 1",
            "CARD-1/checker_pass2": "{}",
        }), encoding="utf-8")
        code, payload, err = _run_cli(toy.root, "run", "--cards", "1", "--script", str(sp))
        self.assertIn("outcomes", payload, err)
        models = []
        for root, _dirs, files in os.walk(toy.state_dir):
            if "responses" not in root:
                continue
            for name in sorted(files):
                if name.startswith("checker-pass") or "-checker-pass" in name:
                    with open(os.path.join(root, name), encoding="utf-8") as handle:
                        models.append(json.load(handle).get("model"))
        return models

    def test_checker_model_with_swarm_on_matches_swarm_off(self) -> None:
        off = self._run("")
        on = self._run('\n[swarm]\nenabled = true\nsupervisor = "off"\n')
        self.assertTrue(off, "no checker call recorded with swarm off")
        self.assertTrue(on, "no checker call recorded with swarm on")
        self.assertEqual(set(off), {"independent-checker"})
        self.assertEqual(set(on), {"independent-checker"})


if __name__ == "__main__":
    unittest.main()
