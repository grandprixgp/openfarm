"""Tests for Stage 3: swarm roster, collision checks, checked actions, and CLI."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adapters.files import FileCardSource
from openfarm_core.board import Board, board_dir_for
from openfarm_core.cards import Card
from openfarm_core.cli import EXIT_OK, EXIT_REFUSED, main as cli_main
from openfarm_core.config import Config
from openfarm_core.errors import Refused
from openfarm_core.git import Git
from openfarm_core.state import StateDir
from openfarm_core.swarm import (
    WORKER_ROLES,
    cards_collide,
    collision_reason,
    parse_team_spec,
    swarm_ask,
    swarm_block,
    swarm_hint,
    swarm_join,
    swarm_remove,
    swarm_set,
    swarm_start,
    swarm_status,
    swarm_tiers,
    swarm_wait,
    worker_member_name,
)


def _make_config(repo: Path, state_root: Path, *, extra_swarm: dict | None = None) -> Config:
    swarm_tbl = {
        "enabled": True,
        "coordinator": "external",
        "supervisor": "external",
        "default_tier": "dsf",
        "max_members": 24,
    }
    if extra_swarm:
        swarm_tbl.update(extra_swarm)
    return Config(
        {
            "project": {"name": "demo", "base_branch": "main", "state_dir": str(state_root)},
            "adapters": {"languages": ["python"], "model": "scripted"},
            "model": {
                "endpoint": "https://api.example.invalid/v1",
                "allowed_endpoints": ["https://api.example.invalid/v1"],
                "headers": {"x-cmd-zdr": "1"},
                "implementer": {"model": "deepseek/deepseek-v4.1-flash", "effort": "medium"},
                "checker": {"model": "deepseek/deepseek-v4.1-flash", "effort": "high"},
            },
            "tiers": {
                "dsf": {
                    "model": "deepseek/deepseek-v4.1-flash",
                    "efforts": ["low", "medium", "high", "max"],
                    "effort_param": "reasoning_effort",
                },
                "fast": {
                    "model": "deepseek/deepseek-fast",
                    "efforts": ["off", "low"],
                    "effort_param": "reasoning_effort",
                },
            },
            "swarm": swarm_tbl,
            "policy": {"categories": ["pattern"]},
            "budget": {"concurrent_cards": 2, "tokens_per_card": 10_000, "spend_per_night": 5.0},
        },
        repo,
    )


def _card(
    card_id: str,
    *,
    state: str = "ready",
    touches: list[str] | None = None,
    deps: list[str] | None = None,
) -> Card:
    return Card(
        id=card_id,
        title=f"Card {card_id}",
        state=state,
        done_text="returns `42`",
        touches=touches or [f"pkg/{card_id.lower()}.py"],
        deps=deps or [],
        fields={
            "category": "pattern",
            "tests": [{"name": "test_x", "file": f"tests/test_{card_id.lower()}.py"}],
        },
    )


class CollisionAndTeamParserTests(unittest.TestCase):
    def test_cards_collide_on_same_id_deps_and_overlapping_touches(self) -> None:
        c1 = _card("CARD-1", touches=["src/auth/*.py"])
        c2 = _card("CARD-2", touches=["src/auth/login.py"])
        c3 = _card("CARD-3", touches=["src/billing/*.py"], deps=["CARD-1"])
        c4 = _card("CARD-4", touches=["src/billing/tax.py"])

        self.assertTrue(cards_collide(c1, c1))
        self.assertTrue(cards_collide(c1, c2))
        self.assertIn("overlapping touches", collision_reason(c1, c2))
        self.assertTrue(cards_collide(c1, c3))
        self.assertTrue(cards_collide(c3, c1))
        self.assertIn("depends on", collision_reason(c3, c1))
        self.assertFalse(cards_collide(c1, c4))
        self.assertEqual(collision_reason(c1, c4), "")

    def test_parse_team_spec(self) -> None:
        self.assertEqual(parse_team_spec(None), {})
        self.assertEqual(
            parse_team_spec("implementer=dsf:high,test_writer=fast:low,checker=dsf"),
            {
                "implementer": {"tier": "dsf", "effort": "high"},
                "test-writer": {"tier": "fast", "effort": "low"},
                "checker": {"tier": "dsf"},
            },
        )
        for bad in ("implementer", "bogus=dsf:low", "implementer=dsf:a:b", "implementer="):
            with self.assertRaises(Refused, msg=bad):
                parse_team_spec(bad)


class SwarmActionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.state_root = self.tmp / "state"
        self.state = StateDir(self.state_root)
        self.cfg = _make_config(self.repo, self.state_root)
        self.board = Board(board_dir_for(self.state, "run-1"), state_dir=self.state)
        self.board.ensure()

    def test_join_seat_validation_and_occupancy(self) -> None:
        coord = swarm_join(self.board, self.cfg, name="alice", seat="coordinator", owner_kind="agent")
        self.assertEqual(coord["role"], "coordinator")
        self.assertEqual(coord["kind"], "external")
        self.assertEqual(coord["owner_kind"], "agent")

        # Duplicate seat holder refused
        with self.assertRaises(Refused) as caught:
            swarm_join(self.board, self.cfg, name="bob", seat="coordinator", owner_kind="human")
        self.assertEqual(caught.exception.code, "seat-occupied")

        # Idempotent re-join for same name succeeds
        again = swarm_join(self.board, self.cfg, name="alice", seat="coordinator", owner_kind="human")
        self.assertEqual(again["owner_kind"], "human")

        # Non-external seat refused
        code_cfg = _make_config(
            self.repo, self.state_root, extra_swarm={"coordinator": "code", "supervisor": "off"}
        )
        with self.assertRaises(Refused) as caught_nonext:
            swarm_join(self.board, code_cfg, name="charlie", seat="supervisor", owner_kind="human")
        self.assertEqual(caught_nonext.exception.code, "seat-not-external")

    def test_coordinator_ownership_required_and_refusals_posted_to_board(self) -> None:
        swarm_join(self.board, self.cfg, name="alice", seat="coordinator", owner_kind="agent")
        swarm_join(self.board, self.cfg, name="sup", seat="supervisor", owner_kind="human")
        cards = {"CARD-1": _card("CARD-1")}

        # Supervisor or unknown caller cannot execute coordinator actions
        for bad_caller in ("", "unknown", "sup"):
            with self.assertRaises(Refused) as caught:
                swarm_start(self.board, self.cfg, caller=bad_caller, card_id="CARD-1", cards_by_id=cards)
            self.assertEqual(caught.exception.code, "not-coordinator")

        # Refusals were posted back onto the board
        posts = self.board.read(as_name="*", since=0, limit=None)
        refusal_bodies = [p["body"] for p in posts if p["kind"] == "action"]
        self.assertTrue(any("not-coordinator" in b for b in refusal_bodies))

        # Supervisor cannot run set/hint/ask/block/remove/wait either
        swarm_start(self.board, self.cfg, caller="alice", card_id="CARD-1", cards_by_id=cards)
        impl_name = worker_member_name("CARD-1", "implementer")
        for fn, kwargs in (
            (swarm_set, {"member_name": impl_name, "effort": "high"}),
            (swarm_hint, {"member_name": impl_name, "body": "try x"}),
            (swarm_ask, {"body": "check?"}),
            (swarm_block, {"card_id": "CARD-1", "reason": "bad"}),
            (swarm_remove, {"member_name": impl_name, "finished_cards": {"CARD-1"}}),
            (swarm_wait, {"reason": "idle"}),
        ):
            with self.assertRaises(Refused) as caught:
                fn(self.board, self.cfg, caller="sup", **kwargs)
            self.assertEqual(caught.exception.code, "not-coordinator")

    def test_start_checks_ready_deps_collision_slots_tiers_and_limits(self) -> None:
        swarm_join(self.board, self.cfg, name="alice", seat="coordinator", owner_kind="agent")
        cards = {
            "CARD-1": _card("CARD-1", state="ready", touches=["src/a/*.py"]),
            "CARD-2": _card("CARD-2", state="ready", touches=["src/a/file.py"]),
            "CARD-3": _card("CARD-3", state="ready", touches=["src/b.py"], deps=["CARD-1"]),
            "CARD-4": _card("CARD-4", state="backlog", touches=["src/c.py"]),
            "CARD-5": _card("CARD-5", state="ready", touches=["src/d.py"]),
            "CARD-6": _card("CARD-6", state="ready", touches=["src/e.py"]),
        }

        # Bad tier or effort refused and posted
        with self.assertRaises(Refused) as bad_tier:
            swarm_start(
                self.board, self.cfg, caller="alice", card_id="CARD-1",
                team={"implementer": {"tier": "missing", "effort": "high"}}, cards_by_id=cards,
            )
        self.assertEqual(bad_tier.exception.code, "unknown-tier")

        with self.assertRaises(Refused) as bad_eff:
            swarm_start(
                self.board, self.cfg, caller="alice", card_id="CARD-1",
                team={"implementer": {"tier": "fast", "effort": "max"}}, cards_by_id=cards,
            )
        self.assertEqual(bad_eff.exception.code, "bad-effort")

        # Start CARD-1 with override for implementer and defaults for omitted roles
        started = swarm_start(
            self.board, self.cfg, caller="alice", card_id="CARD-1",
            team={"implementer": {"tier": "dsf", "effort": "high"}}, cards_by_id=cards,
        )
        self.assertEqual(set(started["team"]), set(WORKER_ROLES))
        self.assertEqual(started["team"]["implementer"]["effort"], "high")
        self.assertEqual(started["team"]["checker"]["effort"], "high")
        self.assertEqual(started["team"]["planner"]["tier"], "dsf")
        self.assertEqual(started["team"]["planner"]["effort"], "")

        # Cannot start CARD-1 twice
        with self.assertRaises(Refused) as dup:
            swarm_start(self.board, self.cfg, caller="alice", card_id="CARD-1", cards_by_id=cards)
        self.assertEqual(dup.exception.code, "already-running")

        # Colliding touches refused
        with self.assertRaises(Refused) as col:
            swarm_start(self.board, self.cfg, caller="alice", card_id="CARD-2", cards_by_id=cards)
        self.assertEqual(col.exception.code, "card-collision")

        # Unmet dep refused
        with self.assertRaises(Refused) as dep:
            swarm_start(self.board, self.cfg, caller="alice", card_id="CARD-3", cards_by_id=cards)
        self.assertEqual(dep.exception.code, "deps-not-done")

        # Non-ready card refused
        with self.assertRaises(Refused) as not_ready:
            swarm_start(self.board, self.cfg, caller="alice", card_id="CARD-4", cards_by_id=cards)
        self.assertEqual(not_ready.exception.code, "not-ready")

        # Budget exceeded refused
        with self.assertRaises(Refused) as over_budget:
            swarm_start(
                self.board, self.cfg, caller="alice", card_id="CARD-5",
                cards_by_id=cards, tokens_used=10_000,
            )
        self.assertEqual(over_budget.exception.code, "budget-exceeded")

        # Start CARD-5 cleanly (slot 2 of concurrent_cards=2), then CARD-6 hits slot limit
        swarm_start(self.board, self.cfg, caller="alice", card_id="CARD-5", cards_by_id=cards)
        with self.assertRaises(Refused) as slots:
            swarm_start(self.board, self.cfg, caller="alice", card_id="CARD-6", cards_by_id=cards)
        self.assertEqual(slots.exception.code, "slot-limit")

        # Verify refusal reasons are posted to caller
        alice_posts = self.board.read(as_name="alice", since=0, limit=None)
        bodies = "\n".join(p["body"] for p in alice_posts)
        self.assertIn("card-collision", bodies)
        self.assertIn("slot-limit", bodies)

    def test_set_hint_ask_block_remove_and_wait(self) -> None:
        swarm_join(self.board, self.cfg, name="alice", seat="coordinator", owner_kind="agent")
        swarm_join(self.board, self.cfg, name="sup", seat="supervisor", owner_kind="human")
        cards = {"CARD-1": _card("CARD-1")}
        swarm_start(self.board, self.cfg, caller="alice", card_id="CARD-1", cards_by_id=cards)
        impl = worker_member_name("CARD-1", "implementer")

        # Bad set refused and posted
        with self.assertRaises(Refused) as bad_set:
            swarm_set(self.board, self.cfg, caller="alice", member_name=impl, effort="turbo")
        self.assertEqual(bad_set.exception.code, "bad-effort")

        # Valid set updates tier/model/effort
        res_set = swarm_set(
            self.board, self.cfg, caller="alice", member_name=impl,
            tier="fast", effort="low", reason="faster retry",
        )
        self.assertEqual(res_set["member"]["tier"], "fast")
        self.assertEqual(res_set["member"]["model"], "deepseek/deepseek-fast")
        self.assertEqual(res_set["member"]["effort"], "low")

        # Hint posts note addressed to member
        hint = swarm_hint(self.board, self.cfg, caller="alice", member_name=impl, body="check edge")
        self.assertEqual(hint["post"]["kind"], "note")
        self.assertEqual(hint["post"]["to"], [impl])

        # Ask posts question to supervisor
        asked = swarm_ask(
            self.board, self.cfg, caller="alice", body="approve?", card="CARD-1",
            expects={"required": ["verdict"], "verdict": ["approve", "reject"]},
        )
        self.assertEqual(asked["post"]["kind"], "question")
        self.assertEqual(asked["post"]["to"], ["sup"])

        # Remove refused while card is not finished
        with self.assertRaises(Refused) as not_fin:
            swarm_remove(self.board, self.cfg, caller="alice", member_name=impl, cards_by_id=cards)
        self.assertEqual(not_fin.exception.code, "card-not-finished")

        # Wait records action without waking alice
        cursor_before = len(self.board.read(as_name="alice", since=0, limit=None))
        wait_res = swarm_wait(self.board, self.cfg, caller="alice", reason="waiting on card")
        self.assertEqual(wait_res["action"], "wait")
        self.assertEqual(len(self.board.read(as_name="alice", since=0, limit=None)), cursor_before)

        # Running cards reflects active card before block/finish
        status_running = swarm_status(self.board, self.cfg)
        self.assertEqual(status_running["running_cards"], ["CARD-1"])

        # Block posts blocked event, clearing slot before remove runs, and allowing remove
        blk = swarm_block(
            self.board, self.cfg, caller="alice", card_id="CARD-1",
            reason="needs owner", cards_by_id=cards,
        )
        self.assertEqual(blk["post"]["kind"], "blocked")
        removed = swarm_remove(self.board, self.cfg, caller="alice", member_name=impl)
        self.assertEqual(removed["member"]["state"], "removed")

        # Status & tiers
        status = swarm_status(self.board, self.cfg)
        self.assertEqual(status["running_cards"], [])
        tiers = swarm_tiers(self.cfg)
        self.assertEqual(tiers["default_tier"], "dsf")
        self.assertIn("fast", tiers["tiers"])


class SwarmCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.state_root = self.tmp / "state"
        self.state = StateDir(self.state_root)

        git = Git(self.repo)
        git._run(["init", "-q", "-b", "main"])
        (self.repo / "pkg").mkdir()
        (self.repo / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
        cfg_dir = self.repo / ".openfarm"
        cfg_dir.mkdir()
        (cfg_dir / "config.toml").write_text(
            f'[project]\nname = "demo"\nbase_branch = "main"\nstate_dir = "{self.state_root}"\n\n'
            f'[adapters]\nlanguages = ["python"]\nmodel = "scripted"\n\n'
            f'[model]\nimplementer = {{ model = "deepseek/deepseek-v4.1-flash", effort = "medium" }}\n\n'
            f'[tiers.dsf]\nmodel = "deepseek/deepseek-v4.1-flash"\n'
            f'efforts = ["low", "medium", "high", "max"]\n\n'
            f'[swarm]\nenabled = true\ncoordinator = "external"\nsupervisor = "external"\n\n'
            f'[policy]\ncategories = ["pattern"]\n',
            encoding="utf-8",
        )
        git.commit_all("init")

        source = FileCardSource(
            self.repo, "cards", worktree=self.state_root / "cards-source", base_branch="main"
        )
        source.prepare()
        source.add_card(_card("CARD-1", touches=["pkg/a.py"]))
        source.add_card(_card("CARD-2", touches=["pkg/a.py"]))

    def _run_cli(self, *args: str) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_main(["--repo", str(self.repo), *args])
        return code, out.getvalue(), err.getvalue()

    def test_swarm_cli_end_to_end(self) -> None:
        # `swarm tiers` works without an active run
        code, out, _ = self._run_cli("swarm", "tiers", "--json")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("dsf", json.loads(out)["tiers"])

        # `swarm status` refused when no run lock and no --run
        code, out, _ = self._run_cli("swarm", "status", "--json")
        self.assertEqual(code, EXIT_REFUSED)

        self.assertTrue(self.state.acquire_run_lock("run-cli"))
        self.addCleanup(self.state.release_run_lock, "run-cli")
        self.state.create_run("run-cli")
        Board(board_dir_for(self.state, "run-cli"), state_dir=self.state).ensure()

        code, out, _ = self._run_cli(
            "swarm", "join", "--name", "alice", "--seat", "coordinator", "--kind", "agent", "--json",
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(out)["member"]["role"], "coordinator")

        # Missing --as refused
        code, out, _ = self._run_cli("swarm", "start", "CARD-1", "--json")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertEqual(json.loads(out)["kind"], "refused")

        # Start CARD-1 with team spec
        code, out, _ = self._run_cli(
            "swarm", "start", "CARD-1", "--team", "implementer=dsf:high", "--as", "alice", "--json",
        )
        self.assertEqual(code, EXIT_OK)
        started = json.loads(out)
        self.assertEqual(started["team"]["implementer"]["effort"], "high")

        # Colliding CARD-2 refused
        code, out, _ = self._run_cli("swarm", "start", "CARD-2", "--as", "alice", "--json")
        self.assertEqual(code, EXIT_REFUSED)

        # Set and hint
        impl = worker_member_name("CARD-1", "implementer")
        code, out, _ = self._run_cli(
            "swarm", "set", impl, "--effort", "max", "--reason", "retry", "--as", "alice", "--json",
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(out)["member"]["effort"], "max")

        code, out, _ = self._run_cli(
            "swarm", "hint", impl, "--body", "focus on edge case", "--as", "alice", "--json",
        )
        self.assertEqual(code, EXIT_OK)

        # Remove refused while running, allowed after block
        code, _, _ = self._run_cli("swarm", "remove", impl, "--as", "alice", "--json")
        self.assertEqual(code, EXIT_REFUSED)

        code, out, _ = self._run_cli(
            "swarm", "block", "CARD-1", "--reason", "wrong spec", "--as", "alice", "--json",
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(out)["post"]["kind"], "blocked")

        code, out, _ = self._run_cli("swarm", "remove", impl, "--as", "alice", "--json")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(out)["member"]["state"], "removed")

        code, out, _ = self._run_cli("swarm", "status", "--json")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(out)["run_id"], "run-cli")


if __name__ == "__main__":
    unittest.main()
