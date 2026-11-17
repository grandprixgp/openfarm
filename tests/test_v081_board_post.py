"""v0.8.1 B1: `openfarm board post` is an outside voice, and only `trusted=True` is code."""

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

from openfarm_core.board import Board, board_dir_for
from openfarm_core.cli import EXIT_OK, EXIT_REFUSED, main as cli_main
from openfarm_core.errors import Refused
from openfarm_core.state import StateDir
from openfarm_core.swarm import (
    swarm_block,
    swarm_hint,
    swarm_join,
    swarm_remove,
    swarm_start,
    worker_member_name,
)
from tests.test_swarm_roster import _card, _make_config


class BoardPostCliTests(unittest.TestCase):
    """The CLI front door refuses code names, unknown names and steering kinds."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-v081-post-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.state_root = self.tmp / "state"
        self.state = StateDir(self.state_root)
        cfg_dir = self.repo / ".openfarm"
        cfg_dir.mkdir()
        (cfg_dir / "config.toml").write_text(
            f'[project]\nname = "demo"\nstate_dir = "{self.state_root}"\n\n'
            f'[adapters]\nlanguages = ["python"]\n\n'
            f'[model]\nimplementer = {{ model = "m" }}\n\n'
            f'[policy]\ncategories = ["pattern"]\n',
            encoding="utf-8",
        )
        self.state.create_run("run-p")
        self.board = Board(board_dir_for(self.state, "run-p"), state_dir=self.state)
        self.board.ensure()
        self.board.upsert_member("gates", kind="code", role="gates")
        self.board.upsert_member("coordinator", kind="code", role="coordinator")
        self.board.upsert_member(
            "sup", kind="external", role="supervisor", owner_kind="human"
        )
        self.board.upsert_member(
            "CARD-1-implementer", kind="scripted", role="implementer", card="CARD-1"
        )

    def _post(self, *args: str) -> tuple[int, dict]:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_main(
                ["--repo", str(self.repo), "--json", "board", "post", "--run", "run-p", *args]
            )
        raw = out.getvalue().strip()
        return code, (json.loads(raw) if raw else {})

    def test_code_member_names_cannot_post_from_the_cli(self) -> None:
        # `--as gates` wrote a `done` post before (the repro's "CARD-3 reached in-review").
        for kind in ("done", "gate", "note"):
            code, out = self._post(
                "--as", "gates", "--to", "coordinator", "--kind", kind,
                "--card", "CARD-3", "--body", "CARD-3 reached in-review",
            )
            self.assertEqual(code, EXIT_REFUSED, (kind, out))
        for name in ("coordinator", "monitor", "system"):
            code, out = self._post("--as", name, "--to", "all", "--body", "hi")
            self.assertEqual(code, EXIT_REFUSED, (name, out))
        self.assertEqual(self.board.status()["posts"], 0)

    def test_unknown_name_is_told_to_join_a_seat(self) -> None:
        code, out = self._post(
            "--as", "intruder", "--to", "all", "--kind", "note", "--body", "hello",
        )
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("swarm join", out.get("error", ""))
        self.assertEqual(self.board.status()["posts"], 0)

    def test_steering_and_code_kinds_are_refused_even_for_a_member(self) -> None:
        for kind in (
            "action", "blocked", "roster", "answer",
            "brief", "gate", "done", "handback", "stall",
        ):
            code, out = self._post(
                "--as", "sup", "--to", "all", "--kind", kind,
                "--card", "CARD-1", "--body", "start CARD-1",
            )
            self.assertEqual(code, EXIT_REFUSED, (kind, out))
        # an unregistered name gets the same refusals (the repro's `start CARD-1`)
        code, out = self._post(
            "--as", "intruder", "--to", "all", "--kind", "action",
            "--card", "CARD-1", "--body", "start CARD-1",
        )
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("swarm", out.get("error", ""))
        self.assertEqual(self.board.status()["posts"], 0)

    def test_outside_kinds_from_a_member_are_posted_untrusted(self) -> None:
        for kind in ("note", "question", "defect", "result"):
            code, out = self._post(
                "--as", "sup", "--to", "coordinator", "--kind", kind,
                "--body", "look \x1b[31mhere\x1b[0m",
            )
            self.assertEqual(code, EXIT_OK, (kind, out))
            self.assertEqual(out["post"]["from"], "sup")
            self.assertEqual(out["post"]["body"], "look here")
        code, out = self._post(
            "--as", "CARD-1-implementer", "--to", "coordinator", "--body", "a note",
        )
        self.assertEqual(code, EXIT_OK, out)

    def test_removed_member_cannot_post(self) -> None:
        self.board.update_member("sup", state="removed")
        code, out = self._post("--as", "sup", "--to", "all", "--body", "still here?")
        self.assertEqual(code, EXIT_REFUSED, out)
        self.assertEqual(self.board.status()["posts"], 0)


class TrustedFlagTests(unittest.TestCase):
    """`Board.post` treats a post as code only with `trusted=True`."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-v081-trust-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.board = Board(self.tmp / "board")
        self.board.ensure()
        self.board.upsert_member("gates", kind="code", role="gates")
        self.board.upsert_member("coordinator", kind="code", role="coordinator")

    def test_code_member_name_without_trusted_is_not_code(self) -> None:
        for kind in ("done", "gate"):
            with self.assertRaises(Refused) as caught:
                self.board.post("coordinator", "all", kind, "CARD-1 done", card="CARD-1")
            self.assertEqual(caught.exception.code, "reserved-kind")
        # a code member's untrusted text is cleaned like anyone else's
        post = self.board.post("coordinator", "all", "note", "```py\nx=1\n```ok")
        self.assertEqual(post["body"], "ok")
        # and posting as a fixed code name needs trusted=True whatever the kind
        for name in ("gates", "monitor", "system"):
            with self.assertRaises(Refused) as caught:
                self.board.post(name, "all", "blocked", "CARD-1 blocked", card="CARD-1")
            self.assertEqual(caught.exception.code, "code-author")
        done = self.board.post(
            "gates", "coordinator", "done", "CARD-1 reached in-review",
            card="CARD-1", trusted=True,
        )
        self.assertEqual(done["kind"], "done")


class CheckedSwarmCommandsStillPostTests(unittest.TestCase):
    """The checked swarm commands keep posting action, blocked, roster, note as a seat holder."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-v081-swarm-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.state = StateDir(self.tmp / "state")
        self.cfg = _make_config(self.repo, self.state.root)
        self.board = Board(board_dir_for(self.state, "run-s"), state_dir=self.state)
        self.board.ensure()

    def test_seat_holder_posts_every_checked_kind(self) -> None:
        swarm_join(self.board, self.cfg, name="lead", seat="coordinator", owner_kind="agent")
        cards = {"CARD-1": _card("CARD-1"), "CARD-2": _card("CARD-2")}
        start = swarm_start(self.board, self.cfg, caller="lead", card_id="CARD-1", cards_by_id=cards)
        impl = worker_member_name("CARD-1", "implementer")
        hint = swarm_hint(self.board, self.cfg, caller="lead", member_name=impl, body="edge")
        block = swarm_block(
            self.board, self.cfg, caller="lead", card_id="CARD-2",
            reason="not tonight", cards_by_id=cards,
        )
        self.board.post(
            "gates", "coordinator", "done", "CARD-1 reached in-review",
            card="CARD-1", trusted=True,
        )
        swarm_remove(self.board, self.cfg, caller="lead", member_name=impl, cards_by_id=cards)
        posts = self.board.read(as_name="*", since=0, limit=None, for_model=False)
        by_lead = [(p["kind"], p["card"]) for p in posts if p["from"] == "lead"]
        self.assertIn(("action", "CARD-1"), by_lead)
        self.assertIn(("note", "CARD-1"), by_lead)
        self.assertIn(("blocked", "CARD-2"), by_lead)
        self.assertIn(("roster", "CARD-1"), by_lead)
        self.assertEqual(start["card"], "CARD-1")
        self.assertEqual(hint["post"]["to"], [impl])
        self.assertFalse(block["running"])

    def test_outside_caller_must_be_an_external_seat_holder(self) -> None:
        # The code coordinator holds the seat by role, but a name typed from
        # outside proves nothing: with outside=True only a joined member passes.
        self.board.upsert_member("coordinator", kind="code", role="coordinator")
        cards = {"CARD-1": _card("CARD-1")}
        with self.assertRaises(Refused) as caught:
            swarm_start(
                self.board, self.cfg, caller="coordinator", card_id="CARD-1",
                cards_by_id=cards, outside=True,
            )
        self.assertEqual(caught.exception.code, "not-coordinator")
        # code's own call (outside=False) still works
        res = swarm_start(
            self.board, self.cfg, caller="coordinator", card_id="CARD-1", cards_by_id=cards,
        )
        self.assertEqual(res["action"], "start")


if __name__ == "__main__":
    unittest.main()
