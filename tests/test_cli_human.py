"""Tests for the humanized CLI output, bare commands, help, hints, and gates."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import openfarm_core
from openfarm_core.board import Board, board_dir_for
from openfarm_core.cards import Card
from openfarm_core.cli import (
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_TROUBLE,
    EXIT_USAGE,
    main as cli_main,
)
from openfarm_core.config import load_config
from openfarm_core.git import Git
from openfarm_core.harness_global import GlobalLibrary
from openfarm_core.harness_memory import HarnessLibrary
from openfarm_core.human_cli import (
    Style,
    error_hint,
    format_bar,
    format_human,
    format_kv,
    format_table,
    visible_len,
)
from openfarm_core.micromanager import Micromanager, ScriptedMicromanager
from openfarm_core.packs.python import PythonPack
from openfarm_core.state import StateDir, write_json


class _FakeTty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _run_cli(*argv: str) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_main(list(argv))
    return code, out.getvalue(), err.getvalue()


def _make_demo_repo(tmp: Path, *, with_swarm: bool = True) -> tuple[Path, Path, Path]:
    repo = tmp / "repo"
    state_dir = tmp / "state"
    key_file = tmp / "worker.key"
    repo.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    key_file.write_text("KEY_SENTINEL_DO_NOT_LEAK\n", encoding="utf-8")

    git = Git(repo)
    git.init(branch="main")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "math_ops.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "tests" / "test_math.py").write_text(
        "import unittest\n"
        "from pkg.math_ops import add\n\n"
        "class MathTests(unittest.TestCase):\n"
        "    def test_add(self) -> None:\n"
        "        self.assertEqual(add(1, 2), 3)\n",
        encoding="utf-8",
    )

    swarm_block = ""
    if with_swarm:
        swarm_block = (
            '\n[tiers.dsf]\nmodel = "deepseek/deepseek-v4.1-flash"\n'
            'efforts = ["low", "medium", "high", "max"]\n'
            'effort_param = "reasoning_effort"\n'
            '\n[swarm]\n'
            'enabled = true\n'
            'coordinator = "external"\n'
            'supervisor = "external"\n'
            'default_tier = "dsf"\n'
        )

    (repo / ".openfarm").mkdir()
    (repo / ".openfarm" / "config.toml").write_text(
        f'[project]\nname = "demo"\nlane = "farm"\nbase_branch = "main"\n'
        f'state_dir = "{state_dir}"\n\n'
        f'[adapters]\ncards = "files:cards/"\nrunner = "direct"\n'
        f'languages = ["python"]\nmodel = "scripted"\n'
        f'micromanager = "off"\ntelemetry = "jsonl"\n\n'
        f'[model]\nkey_file = "{key_file}"\n'
        f'implementer = {{ model = "deepseek/deepseek-v4.1-flash", effort = "medium" }}\n\n'
        f'[policy]\ncategories = ["pattern", "pinned-bugfix"]\n\n'
        f'{swarm_block}'
        f'\n[harness]\nenabled = true\n',
        encoding="utf-8",
    )
    (repo / "cards").mkdir()
    (repo / "cards" / "CARD-1.md").write_text(
        "+++\n"
        "id = \"CARD-1\"\n"
        "title = \"Add subtract helper\"\n"
        "state = \"ready\"\n"
        "category = \"pattern\"\n"
        "unit = \".\"\n"
        "touches = [\"pkg/math_ops.py\"]\n"
        "reference = \"pkg/math_ops.py\"\n"
        "tests = [{ name = \"test_sub\", file = \"tests/test_sub.py\" }]\n"
        "+++\n"
        "`sub(5, 2)` returns `3`.\n",
        encoding="utf-8",
    )
    (repo / "cards" / "CARD-BAD.md").write_text(
        "+++\n"
        "id = \"CARD-BAD\"\n"
        "title = \"Bad card\"\n"
        "state = \"ready\"\n"
        "category = \"pattern\"\n"
        "unit = \".\"\n"
        "touches = []\n"
        "+++\n"
        "Does something properly.\n",
        encoding="utf-8",
    )
    git.commit_all("initial demo")
    return repo, state_dir, key_file


class StyleAndLayoutTests(unittest.TestCase):
    def test_style_tty_vs_no_color_and_dumb_term(self) -> None:
        pipe = io.StringIO()
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True):
            self.assertFalse(Style(pipe).enabled)
            self.assertEqual(Style(pipe).green("ok"), "ok")

            tty = _FakeTty()
            st = Style(tty)
            self.assertTrue(st.enabled)
            colored = st.green("ok")
            self.assertIn("\x1b[32m", colored)
            self.assertEqual(visible_len(colored), 2)

        with mock.patch.dict(os.environ, {"NO_COLOR": "1", "TERM": "xterm-256color"}, clear=True):
            tty = _FakeTty()
            st_no = Style(tty)
            self.assertFalse(st_no.enabled)
            self.assertEqual(st_no.red("fail"), "fail")

        with mock.patch.dict(os.environ, {"TERM": "dumb"}, clear=True):
            tty = _FakeTty()
            self.assertFalse(Style(tty).enabled)

    def test_table_alignment_with_ansi_and_bar(self) -> None:
        st = Style(_FakeTty(), enabled=True)
        plain_tbl = format_table(
            ["NAME", "STATE"],
            [["alpha", "✓ ok"], ["beta-long", "✗ fail"]],
            style=Style(enabled=False),
        )
        ansi_tbl = format_table(
            ["NAME", "STATE"],
            [["alpha", st.green("✓ ok")], ["beta-long", st.red("✗ fail")]],
            style=st,
        )
        plain_widths = [len(line) for line in plain_tbl.splitlines()]
        ansi_vis_widths = [visible_len(line) for line in ansi_tbl.splitlines()]
        self.assertEqual(plain_widths, ansi_vis_widths)

        kv = format_kv([("run_id", "r1"), ("spend", "$0.0000")], style=Style(enabled=False))
        self.assertIn("run_id:", kv)
        self.assertIn("spend:", kv)

        self.assertEqual(format_bar(0.5, width=8, style=Style(enabled=False)), "[████░░░░]")
        self.assertEqual(format_bar(-1.0, width=4, style=Style(enabled=False)), "[░░░░]")
        self.assertEqual(format_bar(2.0, width=4, style=Style(enabled=False)), "[████]")


class HumanCliCommandsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-cli-human-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo, self.state_dir, self.key_file = _make_demo_repo(self.tmp)
        self.state = StateDir(self.state_dir)

    def test_help_and_version_and_typo_suggestions(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as ver:
                cli_main(["--version"])
        self.assertEqual(ver.exception.code, 0)
        self.assertEqual(out.getvalue(), f"openfarm {openfarm_core.__version__}\n")

        help_out = io.StringIO()
        with contextlib.redirect_stdout(help_out):
            with self.assertRaises(SystemExit) as h_exit:
                cli_main(["--help"])
        self.assertEqual(h_exit.exception.code, 0)
        help_text = help_out.getvalue()
        for group in ("Setup & Auth", "Run & Monitor", "Review & Harness", "Swarm, Board & Bench"):
            self.assertIn(group, help_text)
        for cmd in ("init", "adopt", "install-skill", "acp", "check", "run", "status",
                    "pause", "resume", "stop", "tui", "dossier", "verdict", "done",
                    "release", "escape", "supervise", "harness", "swarm", "board", "bench"):
            self.assertIn(cmd, help_text)

        code, _, err = _run_cli("statsu")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("Did you mean: status?", err)

        code_b, _, err_b = _run_cli("board", "red")
        self.assertEqual(code_b, EXIT_USAGE)
        self.assertIn("Did you mean: read?", err_b)

        code_s, _, err_s = _run_cli("swarm", "statsu")
        self.assertEqual(code_s, EXIT_USAGE)
        self.assertIn("Did you mean:", err_s)
        self.assertIn("status", err_s)

        code_j, out_j, err_j = _run_cli("statsu", "--json")
        self.assertEqual(code_j, EXIT_USAGE)
        self.assertEqual(err_j, "")
        self.assertEqual(json.loads(out_j)["kind"], "usage error")

    def test_status_check_pause_resume_stop_and_hints(self) -> None:
        code, out, _ = _run_cli("--repo", str(self.repo), "status")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("✓ IDLE", out)
        self.assertIn("Next:", out)
        self.assertNotIn("KEY_SENTINEL_DO_NOT_LEAK", out)

        code_j, out_j, _ = _run_cli("--repo", str(self.repo), "status", "--json")
        self.assertEqual(code_j, EXIT_OK)
        self.assertEqual(json.loads(out_j)["paused"], False)

        # Pause, resume, stop, resume --clear-kill
        c_p, out_p, _ = _run_cli("--repo", str(self.repo), "pause", "--reason", "lunch")
        self.assertEqual(c_p, EXIT_OK)
        self.assertIn("⏸ Queue paused", out_p)

        c_st, out_st, _ = _run_cli("--repo", str(self.repo), "status")
        self.assertEqual(c_st, EXIT_OK)
        self.assertIn("⏸ PAUSED", out_st)

        c_k, out_k, _ = _run_cli("--repo", str(self.repo), "stop", "--reason", "manual")
        self.assertEqual(c_k, EXIT_OK)
        self.assertIn("✗ Kill switch armed", out_k)

        c_r_ref, out_r_ref, _ = _run_cli("--repo", str(self.repo), "resume")
        self.assertEqual(c_r_ref, EXIT_REFUSED)
        self.assertIn("✗ Resume refused", out_r_ref)
        self.assertIn("--clear-kill", out_r_ref)

        c_r_ok, out_r_ok, _ = _run_cli("--repo", str(self.repo), "resume", "--clear-kill")
        self.assertEqual(c_r_ok, EXIT_OK)
        self.assertIn("✓ Queue resumed", out_r_ok)

        # Check eligible and ineligible
        c_chk, out_chk, _ = _run_cli("--repo", str(self.repo), "check", "CARD-1")
        self.assertEqual(c_chk, EXIT_OK)
        self.assertIn("✓ CARD-1 is eligible", out_chk)
        self.assertIn("Next:", out_chk)

        c_bad, out_bad, _ = _run_cli("--repo", str(self.repo), "check", "CARD-BAD")
        self.assertEqual(c_bad, EXIT_REFUSED)
        self.assertIn("✗ CARD-BAD is not eligible", out_bad)
        self.assertIn("Rule Failures", out_bad)

        # Missing config stderr hint preserves line 1 and adds line 2 hint
        plain_dir = self.tmp / "unconfigured"
        plain_dir.mkdir()
        c_cfg, _, err_cfg = _run_cli("--repo", str(plain_dir), "status")
        self.assertEqual(c_cfg, EXIT_USAGE)
        lines = err_cfg.strip().splitlines()
        self.assertTrue(lines[0].startswith("openfarm: config error: no config at "))
        self.assertIn("hint:", lines[1])

    def test_run_dry_and_completed_dossier_verdict_done_escape_release(self) -> None:
        # Dry run
        c_dry, out_dry, _ = _run_cli(
            "--repo", str(self.repo), "run", "--dry-run", "--card", "CARD-1",
            "--run-id", "run-dry-1",
        )
        self.assertEqual(c_dry, EXIT_OK)
        self.assertIn("Run run-dry-1 [DRY RUN]", out_dry)
        self.assertIn("Token Split", out_dry)

        # Simulate a recorded run & card artifacts for dossier/verdict/done/escape/status --card
        card_dir = self.state.create_card("CARD-1", "run-1")
        write_json(card_dir / "card-record.json", {
            "run_id": "run-1",
            "card_id": "CARD-1",
            "final_state": "in-review",
            "stop_code": "",
            "attempts": 1,
        })
        write_json(card_dir / "dossier.json", {
            "schema_version": 1,
            "card": "CARD-1",
            "run": "run-1",
            "final_state": "in-review",
            "stop_code": "",
            "stop_detail": "",
            "1_risk": {
                "card_hash_at_claim": "abc123",
                "card_changed": False,
                "files_changed": 1,
                "lines_added": 1,
                "lines_removed": 0,
                "touches_check": "1 path(s), all inside touches",
                "existing_tests_changed": [],
                "banned_patterns": 0,
                "flagged_patterns": [],
                "unformatted": [],
                "catch_up": "clean",
            },
            "2_gates": [{"name": "tests", "result": "pass", "attempt": 1, "detail": "ok", "output": ""}],
            "3_known_failing": {"excluded": [], "config_list": []},
            "4_red_green": [],
            "5_struggle": {"attempts": 1, "test_writer_attempts": 1, "planner_split": False, "failed_attempts": []},
            "6_checker": {"restatement": {}, "restatement_mismatch": False, "counter_tests": {"written": 0, "dropped_uncompiled": 0, "passed": 0, "failed": 0, "not_run": 0}},
            "7_mutation": {"killed": 0, "total": 0, "survived": 0},
            "8_not_checked": ["coverage is off"],
            "9_diffs": {"test_diff": "", "production_diff": "+def sub(a, b): return a - b"},
        })
        # Also test refusing unmerged done (with branch 1 commit ahead of main)
        git = Git(self.repo)
        card = Card.from_dict({
            "id": "CARD-1",
            "title": "Add subtract helper",
            "state": "in_review",
            "category": "pattern",
            "touches": ["pkg/math_ops.py"],
            "done_text": "sub",
        })
        branch = f"openfarm/{card.id}-{card.slug()}"
        wt = self.tmp / "card1-wt"
        git.create_branch(branch, "HEAD")
        git.worktree_add(wt, branch)
        (wt / "pkg" / "math_ops.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a + b\n\n"
            "def sub(a: int, b: int) -> int:\n    return a - b\n",
            encoding="utf-8",
        )
        Git(wt).commit_all("implement sub")
        git.worktree_remove(wt)

        c_dos, out_dos, _ = _run_cli("--repo", str(self.repo), "dossier", "CARD-1", "--attempt", "99")
        self.assertEqual(c_dos, EXIT_REFUSED)
        self.assertIn("✗ Dossier unavailable for CARD-1", out_dos)

        c_dos_ok, out_dos_ok, _ = _run_cli("--repo", str(self.repo), "dossier", "CARD-1")
        self.assertEqual(c_dos_ok, EXIT_OK)
        self.assertIn("CARD-1", out_dos_ok)

        c_dos_json, out_dos_json, _ = _run_cli("--repo", str(self.repo), "dossier", "CARD-1", "--format", "json")
        self.assertEqual(c_dos_json, EXIT_OK)
        self.assertEqual(json.loads(out_dos_json)["card"], "CARD-1")

        c_ver, out_ver, _ = _run_cli(
            "--repo", str(self.repo), "verdict", "CARD-1", "clean",
            "--review-tokens", "500", "--minutes", "2.5", "--note", "solid",
        )
        self.assertEqual(c_ver, EXIT_OK)
        self.assertIn("✓ Recorded verdict 'clean' for CARD-1", out_ver)

        c_esc, out_esc, _ = _run_cli(
            "--repo", str(self.repo), "escape", "CARD-1", "--note", "negative input edge",
        )
        self.assertEqual(c_esc, EXIT_OK)
        self.assertIn("• Recorded escape on CARD-1", out_esc)

        c_st_card, out_st_card, _ = _run_cli("--repo", str(self.repo), "status", "--card", "CARD-1")
        self.assertEqual(c_st_card, EXIT_OK)
        self.assertIn("Card CARD-1 History", out_st_card)
        self.assertIn("in-review", out_st_card)

        c_done_bad, out_done_bad, _ = _run_cli("--repo", str(self.repo), "done", "CARD-1")
        self.assertEqual(c_done_bad, EXIT_REFUSED)
        self.assertIn("✗ Cannot mark CARD-1 done", out_done_bad)
        self.assertIn("git merge", out_done_bad)

        c_rel, out_rel, _ = _run_cli(
            "--repo", str(self.repo), "release", "CARD-1", "--reason", "requeue",
        )
        self.assertEqual(c_rel, EXIT_OK)
        self.assertIn("↩ Released CARD-1", out_rel)

        c_rel_bad, out_rel_bad, _ = _run_cli(
            "--repo", str(self.repo), "release", "CARD-MISSING", "--reason", "requeue",
        )
        self.assertEqual(c_rel_bad, EXIT_REFUSED)
        self.assertIn("✗ Release refused", out_rel_bad)

        # Render full run formatting with by_member and by_effort
        rendered_run = format_human("run", {
            "run_id": "run-fmt",
            "dry_run": False,
            "finished_at": "2026-11-17T09:00:00Z",
            "caps": {"spend": 1.0, "cards": 2},
            "spend": 0.0123,
            "micromanager_spend": 0.045,
            "spend_measured": True,
            "tokens": {
                "worker": 600,
                "supervisor": 400,
                "total": 1000,
                "worker_share": 0.6,
                "supervisor_share": 0.4,
                "by_member": {"CARD-1-implementer": 600, "supervisor": 400},
                "by_effort": {"medium": 600, "max": 400},
            },
            "outcomes": [{
                "card": "CARD-1",
                "final_state": "in-review",
                "stop_code": "",
                "attempts": 1,
                "detail": "all gates green",
            }],
            "notes": ["sample note"],
            "harness": {"enabled": True, "revision": 1, "state": "confirmed", "active_count": 1},
        }, style=Style(enabled=False))
        self.assertIn("Token Split", rendered_run)
        self.assertIn("60% worker", rendered_run)
        self.assertIn("CARD-1-implementer", rendered_run)
        self.assertIn("medium", rendered_run)
        self.assertIn("openfarm verdict CARD-1", rendered_run)

    def test_supervise_and_harness_local_and_global(self) -> None:
        mm_script = self.tmp / "mm.json"
        mm_script.write_text(json.dumps({
            "triage_blocked": {
                "verdict": "approve",
                "summary": "No blocked cards to triage.",
                "recommendations": [{"action": "wait", "card_id": "CARD-1", "reason": "none"}],
                "findings": [{"from_card": "TRIAGE", "title": "Clean queue", "body": "Ready"}],
            }
        }), encoding="utf-8")
        c_sup, out_sup, _ = _run_cli(
            "--repo", str(self.repo), "supervise", "--triage-blocked", "--mm-script", str(mm_script),
        )
        self.assertEqual(c_sup, EXIT_OK)
        self.assertIn("Supervision Report", out_sup)
        self.assertIn("No blocked cards to triage.", out_sup)
        self.assertIn("Micromanager Budget", out_sup)

        # Populate local + global harness
        cfg = load_config(self.repo)
        lib = HarnessLibrary(self.state_dir / "harness", config=cfg)
        mm = Micromanager(ScriptedMicromanager({
            "reflect_harness": {
                "lessons": [{
                    "title": "Keep imports explicit",
                    "surface": "implementer_guidance",
                    "role": "implementer",
                    "cue": "when editing pkg/",
                    "lesson": "Check exports cleanly.",
                    "proposed_patch": {"guidance": "Check exports cleanly."},
                    "confidence": 0.9,
                }]
            }
        }))
        lib.record_card_outcome(
            {"card_id": "CARD-1", "category": "pattern", "language": "python",
             "final_state": "in-review", "attempts": 2},
            [{"attempt": 1, "outcome": "fail", "error_signature": "E1"},
             {"attempt": 2, "outcome": "pass"}],
            micromanager=mm,
        )
        entries = lib.list_entries()
        self.assertTrue(entries)
        eid = entries[0]["id"]

        for sub in (
            ("harness", "status"),
            ("harness", "index"),
            ("harness", "show", eid),
            ("harness", "promote", eid),
            ("harness", "demote", eid),
            ("harness", "reset", "--yes"),
        ):
            code, out, _ = _run_cli("--repo", str(self.repo), *sub)
            self.assertEqual(code, EXIT_OK, f"{sub}: {out}")
            self.assertNotIn("\\n", out)

        gdir = self.tmp / "global-harness"
        (self.repo / ".openfarm" / "config.toml").write_text(
            (self.repo / ".openfarm" / "config.toml").read_text(encoding="utf-8")
            + f'global_dir = "{gdir}"\n',
            encoding="utf-8",
        )
        added = GlobalLibrary(gdir).add({
            "title": "Prefer pathlib",
            "surface": "implementer_guidance",
            "role": "implementer",
            "language": "python",
            "kind": "pattern",
            "failure_class": "paths",
            "tags": ["paths"],
            "cue": "when touching paths",
            "lesson": "Use Path.",
            "proposed_patch": "Use Path.",
            "confidence": 0.9,
        }, "alpha")
        gid = added["id"]
        c_gs, out_gs, _ = _run_cli("--repo", str(self.repo), "harness", "status", "--global")
        self.assertEqual(c_gs, EXIT_OK)
        self.assertIn("Global Harness Library", out_gs)
        self.assertIn(gid, out_gs)

        c_gd, out_gd, _ = _run_cli("--repo", str(self.repo), "harness", "demote", gid, "--global")
        self.assertEqual(c_gd, EXIT_OK)
        self.assertIn("retired", out_gd)

        c_gp, out_gp, _ = _run_cli("--repo", str(self.repo), "harness", "promote", gid, "--global")
        self.assertEqual(c_gp, EXIT_OK)
        self.assertIn("active", out_gp)

    def test_board_and_swarm_human_and_bare_subcommands(self) -> None:
        # Bare `swarm` without lock succeeds and shows seats + tiers, whereas `swarm status` refuses
        c_bare_sw, out_bare_sw, _ = _run_cli("--repo", str(self.repo), "swarm")
        self.assertEqual(c_bare_sw, EXIT_OK)
        self.assertIn("Swarm Configuration", out_bare_sw)
        self.assertIn("dsf", out_bare_sw)
        self.assertIn("deepseek/deepseek-v4.1-flash", out_bare_sw)

        c_sw_stat_nolock, out_sw_stat_nolock, err_sw = _run_cli("--repo", str(self.repo), "swarm", "status")
        self.assertEqual(c_sw_stat_nolock, EXIT_REFUSED)
        self.assertIn("no active run", err_sw)

        # Bare swarm/board/bench with --json MUST keep returning EXIT_USAGE = 2
        for bare in ("swarm", "board", "bench"):
            c_j, out_j, _ = _run_cli("--repo", str(self.repo), bare, "--json")
            self.assertEqual(c_j, EXIT_USAGE, f"{bare} --json: {out_j}")
            self.assertEqual(json.loads(out_j)["kind"], "usage error")

        # Now acquire run lock and test bare `swarm`, `board`, plus full swarm/board lifecycle
        self.assertTrue(self.state.acquire_run_lock("run-live"))
        self.addCleanup(self.state.release_run_lock, "run-live")
        self.state.create_run("run-live")
        board = Board(board_dir_for(self.state, "run-live"), state_dir=self.state)
        board.ensure()

        c_tiers, out_tiers, _ = _run_cli("--repo", str(self.repo), "swarm", "tiers")
        self.assertEqual(c_tiers, EXIT_OK)
        self.assertIn("Swarm Tiers", out_tiers)

        c_join, out_join, _ = _run_cli(
            "--repo", str(self.repo), "swarm", "join",
            "--name", "alice", "--seat", "coordinator", "--kind", "human",
        )
        self.assertEqual(c_join, EXIT_OK)
        self.assertIn("Joined seat 'coordinator' as 'alice'", out_join)

        c_join_s, out_join_s, _ = _run_cli(
            "--repo", str(self.repo), "swarm", "join",
            "--name", "bob", "--seat", "supervisor", "--kind", "human",
        )
        self.assertEqual(c_join_s, EXIT_OK)
        self.assertIn("Joined seat 'supervisor'", out_join_s)

        c_start, out_start, _ = _run_cli(
            "--repo", str(self.repo), "swarm", "start", "CARD-1",
            "--team", "implementer=dsf:high", "--as", "alice",
        )
        self.assertEqual(c_start, EXIT_OK)
        self.assertIn("Started CARD-1", out_start)

        c_set, out_set, _ = _run_cli(
            "--repo", str(self.repo), "swarm", "set", "CARD-1-implementer",
            "--effort", "max", "--reason", "boost", "--as", "alice",
        )
        self.assertEqual(c_set, EXIT_OK)
        self.assertIn("Updated CARD-1-implementer", out_set)

        c_hint, out_hint, _ = _run_cli(
            "--repo", str(self.repo), "swarm", "hint", "CARD-1-implementer",
            "--body", "watch negatives", "--as", "alice",
        )
        self.assertEqual(c_hint, EXIT_OK)
        self.assertIn("Sent hint #", out_hint)

        # Bare `openfarm swarm` with active run lock shows live Swarm Status
        c_sw_live, out_sw_live, _ = _run_cli("--repo", str(self.repo), "swarm")
        self.assertEqual(c_sw_live, EXIT_OK)
        self.assertIn("Swarm Status (run-live)", out_sw_live)
        self.assertIn("CARD-1-implementer", out_sw_live)

        # Board post, bare `board`, `board read --status`, question + answer, and `board wait` JSON!
        c_bp, out_bp, _ = _run_cli(
            "--repo", str(self.repo), "board", "post",
            "--as", "alice", "--to", "all", "--kind", "note", "--body", "hello swarm",
        )
        self.assertEqual(c_bp, EXIT_OK)
        self.assertIn("✓ Posted #", out_bp)

        c_bare_bd, out_bare_bd, _ = _run_cli("--repo", str(self.repo), "board")
        self.assertEqual(c_bare_bd, EXIT_OK)
        self.assertIn("Board run-live", out_bare_bd)
        self.assertIn("hello swarm", out_bare_bd)

        c_bstat, out_bstat, _ = _run_cli("--repo", str(self.repo), "board", "read", "--status")
        self.assertEqual(c_bstat, EXIT_OK)
        self.assertIn("Board Status (run-live)", out_bstat)
        self.assertIn("Roster", out_bstat)

        q = board.post(
            "alice", "bob", "question", "Proceed?",
            card="CARD-1", expects={"required": ["verdict"], "verdict": ["approve", "reject"]},
        )
        # `board wait` without `--json` MUST still output valid JSON
        c_wait, out_wait, _ = _run_cli(
            "--repo", str(self.repo), "board", "wait", "--as", "bob", "--since", "0", "--timeout", "1s",
        )
        self.assertEqual(c_wait, EXIT_OK)
        self.assertTrue(json.loads(out_wait)["woke"])

        ans_file = self.tmp / "ans.json"
        ans_file.write_text(json.dumps({"verdict": "approve"}), encoding="utf-8")
        c_ans, out_ans, _ = _run_cli(
            "--repo", str(self.repo), "board", "answer", str(q["seq"]),
            "--as", "bob", "--file", str(ans_file),
        )
        self.assertEqual(c_ans, EXIT_OK)
        self.assertIn(f"✓ Answered #{q['seq']}", out_ans)

        c_blk, out_blk, _ = _run_cli(
            "--repo", str(self.repo), "swarm", "block", "CARD-1",
            "--reason", "stop test", "--as", "alice",
        )
        self.assertEqual(c_blk, EXIT_OK)
        self.assertIn("Blocked CARD-1", out_blk)

        # Typing the code coordinator's name after --as proves nothing: from the
        # command line only a member that joined the seat may steer.
        board.upsert_member("coordinator", kind="code", role="coordinator", added_by="system")
        before = len(board.read(as_name="*", since=0, limit=None, for_model=False))
        c_fake, out_fake, err_fake = _run_cli(
            "--repo", str(self.repo), "swarm", "hint", "CARD-1-implementer",
            "--body", "do it my way", "--as", "coordinator",
        )
        self.assertNotEqual(c_fake, EXIT_OK)
        self.assertIn("joined the coordinator seat", out_fake + err_fake)
        notes = [
            p for p in board.read(as_name="*", since=0, limit=None, for_model=False)[before:]
            if p["kind"] == "note"
        ]
        self.assertEqual(notes, [])
        board.upsert_member(
            "coordinator", kind="code", role="coordinator", state="removed", added_by="system"
        )
        # CARD-1 was running, so the block is a stop request: its members stay
        # until the card's own final post (code, as `gates`) lands.
        board.post(
            "gates", ["coordinator"], "blocked", "CARD-1 blocked (refused): stop test",
            card="CARD-1", trusted=True,
        )

        c_rm, out_rm, _ = _run_cli(
            "--repo", str(self.repo), "swarm", "remove", "CARD-1-implementer", "--as", "alice",
        )
        self.assertEqual(c_rm, EXIT_OK)
        self.assertIn("Removed CARD-1-implementer", out_rm)

    def test_standalone_init_adopt_install_skill_acp_and_bench(self) -> None:
        # init
        init_dir = self.tmp / "new-app"
        c_init, out_init, _ = _run_cli(
            "init", "new-app", "--dir", str(init_dir),
            "--state-dir", str(self.tmp / "init-state"),
            "--secrets-dir", str(self.tmp / "init-secrets"),
            "--key-file", str(self.key_file),
            "--mutation", "off", "--no-git",
        )
        self.assertEqual(c_init, EXIT_OK)
        self.assertIn("✓ Initialized Openfarm project 'new-app'", out_init)
        self.assertIn("Next:", out_init)
        self.assertNotIn("KEY_SENTINEL_DO_NOT_LEAK", out_init)

        # adopt (in existing plain repo)
        ad_dir = self.tmp / "ad-repo"
        ad_dir.mkdir()
        Git(ad_dir).init(branch="main")
        (ad_dir / "pyproject.toml").write_text('[project]\nname = "ad"\nversion = "0.1.0"\n', encoding="utf-8")
        Git(ad_dir).commit_all("init")
        c_ad, out_ad, _ = _run_cli(
            "adopt", "--dir", str(ad_dir), "--state-dir", str(self.tmp / "ad-state"),
            "--key-file", str(self.key_file), "--no-suite",
        )
        self.assertEqual(c_ad, EXIT_OK)
        self.assertIn("✓ Adopted repository (python)", out_ad)
        self.assertNotIn("KEY_SENTINEL_DO_NOT_LEAK", out_ad)

        # install-skill --dry-run
        c_sk, out_sk, _ = _run_cli(
            "install-skill", "--target", "workspace", "--dest", str(self.tmp / "sk-ws"), "--dry-run",
        )
        self.assertEqual(c_sk, EXIT_OK)
        self.assertIn("[DRY RUN]", out_sk)
        self.assertIn("RUNTIME", out_sk)

        # acp status & configure (never leaks key)
        c_acp, out_acp, _ = _run_cli("--repo", str(self.repo), "acp", "status")
        self.assertEqual(c_acp, EXIT_OK)
        self.assertIn("Frontier Claude ACP Status", out_acp)
        self.assertNotIn("KEY_SENTINEL_DO_NOT_LEAK", out_acp)

        c_acpc, out_acpc, _ = _run_cli(
            "--repo", str(self.repo), "acp", "configure", "--mode", "advisory", "--auth", "oauth",
        )
        self.assertEqual(c_acpc, EXIT_OK)
        self.assertIn("Frontier Claude ACP Configured", out_acpc)
        self.assertNotIn("KEY_SENTINEL_DO_NOT_LEAK", out_acpc)

        # bench list & bare `openfarm bench`
        bench_dir = self.tmp / "bench"
        t1 = bench_dir / "t1"
        t1.mkdir(parents=True)
        (t1 / "card.md").write_text("# card\n", encoding="utf-8")
        (t1 / "hidden.py").write_text("assert True\n", encoding="utf-8")
        (t1 / "task.toml").write_text(
            'id = "py-sample"\nlanguage = "python"\ncategory = "pattern"\n'
            'card = "card.md"\nhidden_tests = ["hidden.py"]\n',
            encoding="utf-8",
        )
        c_bl, out_bl, _ = _run_cli("bench", "list", "--dir", str(bench_dir))
        self.assertEqual(c_bl, EXIT_OK)
        self.assertIn("Bench Tasks (1)", out_bl)
        self.assertIn("py-sample", out_bl)

        old_cwd = os.getcwd()
        self.addCleanup(os.chdir, old_cwd)
        os.chdir(self.tmp)
        c_bb, out_bb, _ = _run_cli("bench")
        self.assertEqual(c_bb, EXIT_OK)
        self.assertIn("py-sample", out_bb)

    def test_baseline_refusals_tty_bench_run_and_json_parity(self) -> None:
        # Baseline check in human mode (named test not-run -> eligible, shows Baseline section)
        c_chk, out_chk, _ = _run_cli("--repo", str(self.repo), "check", "CARD-1", "--baseline")
        self.assertEqual(c_chk, EXIT_OK)
        self.assertIn("✓ CARD-1 is eligible", out_chk)
        self.assertIn("Baseline", out_chk)
        self.assertIn("NAMED TEST", out_chk)

        # Run refused by kill switch, paused, and breaker in human & --json modes
        self.state.request_kill(reason="emergency")
        c_rk, out_rk, _ = _run_cli("--repo", str(self.repo), "run")
        self.assertEqual(c_rk, EXIT_REFUSED)
        self.assertIn("✗ Run refused:", out_rk)
        self.assertIn("openfarm resume --clear-kill", out_rk)

        c_rk_j, out_rk_j, _ = _run_cli("--repo", str(self.repo), "run", "--json")
        self.assertEqual(c_rk_j, EXIT_REFUSED)
        self.assertTrue(json.loads(out_rk_j)["refused"])
        self.state.clear_kill()

        self.state.write_breaker(True, ["CARD-1"], "3 attempts failures")
        c_sb, out_sb, _ = _run_cli("--repo", str(self.repo), "status")
        self.assertEqual(c_sb, EXIT_OK)
        self.assertIn("✗ BREAKER TRIPPED", out_sb)
        self.state.write_breaker(False, [])

        # Mock TTY stdout emits ANSI escapes while NO_COLOR=1 strips them completely
        root_repo = Path(__file__).resolve().parent.parent
        bench_out = self.tmp / "bench-report-out"
        c_br, out_br, _ = _run_cli(
            "bench", "run", "--dir", str(root_repo / "bench"),
            "--task", "py-bounded-full", "--dry-run", "--out", str(bench_out),
        )
        self.assertEqual(c_br, EXIT_OK)
        self.assertIn(f"report: {bench_out}", out_br)

        # acp probe formatted output
        probe_text = format_human("acp_probe", {
            "action": "probe",
            "oauth_ready": True,
            "resolved_transport": "claude-native-acp",
            "claude_cli": "/usr/bin/claude",
            "claude_agent_acp": None,
            "credentials_file_present": True,
            "oauth_token_env": False,
            "cli_auth": {"loggedIn": True},
            "protocol_version": 1,
            "auth_methods": [{"id": "oauth"}],
        }, style=Style(enabled=False))
        self.assertIn("Frontier Claude ACP Probe", probe_text)
        self.assertIn("Handshake protocol v1", probe_text)

        # Mock TTY vs NO_COLOR=1 directly on cli_main stdout
        tty_out = _FakeTty()
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True):
            with contextlib.redirect_stdout(tty_out):
                rc_tty = cli_main(["--repo", str(self.repo), "status"])
        self.assertEqual(rc_tty, EXIT_OK)
        self.assertIn("\x1b[", tty_out.getvalue())

        tty_nocolor = _FakeTty()
        with mock.patch.dict(os.environ, {"NO_COLOR": "1", "TERM": "xterm-256color"}, clear=True):
            with contextlib.redirect_stdout(tty_nocolor):
                rc_nc = cli_main(["--repo", str(self.repo), "status"])
        self.assertEqual(rc_nc, EXIT_OK)
        self.assertNotIn("\x1b[", tty_nocolor.getvalue())

        # --json parity across commands
        for cmd_argv in (
            ("--repo", str(self.repo), "check", "CARD-1", "--json"),
            ("--repo", str(self.repo), "pause", "--json"),
            ("--repo", str(self.repo), "resume", "--json"),
            ("--repo", str(self.repo), "harness", "status", "--json"),
            ("--repo", str(self.repo), "swarm", "tiers", "--json"),
            ("--repo", str(self.repo), "acp", "status", "--json"),
        ):
            rc_j, out_j, _ = _run_cli(*cmd_argv)
            self.assertEqual(rc_j, EXIT_OK, f"{cmd_argv}: {out_j}")
            self.assertIsInstance(json.loads(out_j), dict)

    def test_error_hints_helper_and_python_pack_gates(self) -> None:
        self.assertIn("openfarm adopt", error_hint("config error", "no config at /x"))
        self.assertIn("openfarm swarm tiers", error_hint("refused", "no active run holds the lock"))
        self.assertIn("openfarm done", error_hint("refused", "branch has 1 commit(s) not merged into main"))

        pack = PythonPack()
        root = Path(__file__).resolve().parent.parent
        flagged = load_config(root).flagged_patterns
        for rel in ("openfarm_core/human_cli.py", "tests/test_cli_human.py"):
            text = (root / rel).read_text(encoding="utf-8")
            added = [
                {"path": rel, "line": idx, "text": line}
                for idx, line in enumerate(text.splitlines(), start=1)
            ]
            prod_paths = set() if rel.startswith("tests/") else {rel}
            found = pack.patterns(added, flagged, prod_paths)
            self.assertEqual(found["banned"], [], f"banned patterns in {rel}: {found['banned']}")
            self.assertEqual(found["flagged"], [], f"flagged patterns in {rel}: {found['flagged']}")

        # Check all lines added/modified in cli.py (parser, emit helpers, main error/typo handling)
        cli_lines = (root / "openfarm_core" / "cli.py").read_text(encoding="utf-8").splitlines()
        cli_ranges = list(range(100, 145)) + list(range(1735, len(cli_lines) + 1))
        cli_added = [
            {"path": "openfarm_core/cli.py", "line": idx, "text": cli_lines[idx - 1]}
            for idx in cli_ranges
        ]
        cli_found = pack.patterns(cli_added, flagged, {"openfarm_core/cli.py"})
        self.assertEqual(cli_found["banned"], [], f"banned patterns in cli.py additions: {cli_found['banned']}")
        self.assertEqual(cli_found["flagged"], [], f"flagged patterns in cli.py additions: {cli_found['flagged']}")


if __name__ == "__main__":
    unittest.main()
