"""Shared helpers for the v0.8.1 `coord` unit tests (no tests of its own).

Mirrors the review repro helpers: a toy Python repo with N one-function cards, a
scripted worker that solves each card first time, and a way to drive
`SwarmRunner` directly with a fake card thread.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.board import Board, board_dir_for
from openfarm_core.cli import main as cli_main
from openfarm_core.state import StateDir
from tests.test_python_e2e import build_python_toy

TIER_DSF = (
    '\n[tiers.dsf]\nmodel = "stub"\nefforts = ["low", "medium", "high", "max"]\n'
    'effort_param = "reasoning_effort"\n'
)


def run_cli(repo: Path, *args: str) -> tuple[int, dict, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli_main(["--repo", str(repo), "--json", *args])
        except SystemExit as exc:  # argparse errors
            code = exc.code
    raw = out.getvalue().strip()
    try:
        payload = json.loads(raw) if raw else {}
    except ValueError:
        payload = {"_raw": raw}
    return code, payload, err.getvalue()


def make_toy(
    n_cards: int = 2,
    *,
    swarm: str = "",
    tiers: str | None = TIER_DSF,
    concurrent_cards: int = 1,
    touches: dict | None = None,
    deps: dict | None = None,
):
    """A toy repo with cards CARD-1..CARD-n; returns `(toy, script)`."""
    toy = build_python_toy()
    names = [chr(ord("a") + i) for i in range(n_cards)]
    for nm in names:
        toy.write(f"tally/m_{nm}.py", f"def {nm}(x: int) -> int:\n    return x\n")
    toy.commit("add modules")
    cfg_path = toy.root / ".openfarm" / "config.toml"
    text = cfg_path.read_text(encoding="utf-8")
    if "concurrent_cards =" in text:
        text = text.replace("concurrent_cards = 1", f"concurrent_cards = {concurrent_cards}")
    else:
        text = text.replace(
            "concurrent_builds = 2",
            f"concurrent_builds = 2\nconcurrent_cards = {concurrent_cards}",
        )
    if tiers:
        text += tiers
    text += swarm
    cfg_path.write_text(text, encoding="utf-8")
    script: dict = {}
    for i, nm in enumerate(names, 1):
        cid = f"CARD-{i}"
        extra = {"depends": deps[cid]} if deps and cid in deps else {}
        toy.write_card(
            cid,
            title=f"Mod {nm}",
            category="pinned-bugfix",
            unit=".",
            touches=(touches or {}).get(cid, [f"tally/m_{nm}.py"]),
            tests=[{"name": f"test_{nm}", "file": f"tests/test_{nm}.py",
                    "asserts": f"{nm}(1)==2"}],
            done_text=f"`{nm}(x)` returns `x + 1`.",
            **extra,
        )
        script[f"{cid}/test_writer"] = (
            f"=== whole_file: tests/test_{nm}.py\nimport unittest\n"
            f"from tally.m_{nm} import {nm}\n"
            f"class T(unittest.TestCase):\n"
            f"    def test_{nm}(self): self.assertEqual({nm}(1), 2)\n"
        )
        script[f"{cid}/implementer"] = (
            f"=== whole_file: tally/m_{nm}.py\ndef {nm}(x: int) -> int:\n    return x + 1\n"
        )
        script[f"{cid}/checker_pass1"] = f"RESTATEMENT: {nm} returns x + 1"
        script[f"{cid}/checker_pass2"] = "{}"
    toy.commit("add cards")
    return toy, script


def write_script(testcase, script: dict) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="openfarm-coord-script-"))
    testcase.addCleanup(shutil.rmtree, tmp, True)
    path = tmp / "script.json"
    path.write_text(json.dumps(script), encoding="utf-8")
    return path


def board_for(toy, run_id: str) -> Board:
    state = StateDir(toy.state_dir)
    return Board(board_dir_for(state, run_id), state_dir=state)


def all_posts(board: Board) -> list[dict]:
    return board.read(as_name="*", since=0, limit=None, for_model=False)


def runner_env(toy, run_id: str = "coord-test"):
    """`(ctx, cfg, source, board)` for driving `SwarmRunner` directly."""
    from openfarm_core.cli import Context

    ctx = Context(toy.root)
    source = ctx.cards()
    source.prepare()
    board = Board(board_dir_for(ctx.state, run_id), state_dir=ctx.state)
    board.ensure()
    board.upsert_member("gates", kind="code", role="gates", added_by="system")
    board.upsert_member("monitor", kind="code", role="monitor", added_by="system")
    return ctx, ctx.cfg, source, board
