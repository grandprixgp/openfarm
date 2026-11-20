"""Tests for the stdlib TUI (`openfarm_core.tui`): snapshots, commands, safety, and refusals."""

from __future__ import annotations

import contextlib
import curses
import fcntl
import importlib
import io
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import sys
import tempfile
import termios
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adapters.files import FileCardSource
from openfarm_core.cards import Card, TestRef
from openfarm_core.cli import EXIT_OK, EXIT_USAGE, main as cli_main
from openfarm_core.errors import UsageError
from openfarm_core.git import Git
from openfarm_core.harness_global import GlobalLibrary
from openfarm_core.harness_memory import HarnessLibrary
from openfarm_core.packs.python import PythonPack
from openfarm_core.state import StateDir, write_json
from openfarm_core.tui import (
    VIEWS,
    TuiController,
    _handle_view_hotkey,
    _init_curses_theme,
    _render_curses_frame,
    collect_snapshot,
    dispatch_cli_command,
    read_evidence_file,
    render_snapshot,
    run_tui,
)

KEY_SENTINEL = "SUPER_SECRET_KEY_SENTINEL_948271"


def _make_temp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="openfarm-tui-"))


class PreConfigTuiTests(unittest.TestCase):
    """Snapshot rendering and ops helpers in a directory without `.openfarm/config.toml`."""

    def setUp(self) -> None:
        self.root = _make_temp_dir()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.repo = self.root / "empty_repo"
        self.repo.mkdir()
        (self.repo / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
        (self.repo / "demo.py").write_text("def ok() -> int:\n    return 1\n", encoding="utf-8")
        bench_task = self.repo / "bench" / "t1"
        bench_task.mkdir(parents=True)
        (bench_task / "card.md").write_text("# card\n", encoding="utf-8")
        (bench_task / "hidden.py").write_text("assert True\n", encoding="utf-8")
        (bench_task / "task.toml").write_text(
            'id = "py-sample"\nlanguage = "python"\ncategory = "small-feature"\n'
            'card = "card.md"\nhidden_tests = ["hidden.py"]\n',
            encoding="utf-8",
        )
        self.acp_home = self.root / "home"
        self.acp_home.mkdir()

    def test_all_seven_views_render_in_pre_config_directory(self) -> None:
        self.assertEqual(
            VIEWS,
            ("overview", "cards", "dossier", "board", "swarm", "harness", "ops"),
        )
        for view in VIEWS:
            text = render_snapshot(
                self.repo,
                view=view,
                acp_home=self.acp_home,
                claude_bin="",
                acp_bin="",
            )
            self.assertIn("openfarm tui", text)
            raw_json = render_snapshot(
                self.repo,
                view=view,
                as_json=True,
                acp_home=self.acp_home,
                claude_bin="",
                acp_bin="",
            )
            payload = json.loads(raw_json)
            self.assertEqual(payload["view"], view)
            self.assertEqual(payload["configured"], False)

        ops_snap = collect_snapshot(
            self.repo,
            view="ops",
            acp_home=self.acp_home,
            claude_bin="",
            acp_bin="",
        )
        self.assertEqual(ops_snap["data"]["bench_tasks"][0]["id"], "py-sample")
        self.assertEqual(ops_snap["data"]["pre_config_helpers"], ["init", "adopt", "install-skill"])

    def test_pre_config_command_dispatch_install_skill_dry_run_and_adopt(self) -> None:
        skill_dest = self.root / "skill-dest"
        res_skill = dispatch_cli_command(
            self.repo,
            None,
            ["install-skill", "--target", "workspace", "--dest", str(skill_dest), "--dry-run", "--json"],
        )
        self.assertEqual(res_skill["exit_code"], 0, res_skill["output"])
        self.assertEqual(res_skill["data"]["dry_run"], True)

        ext_state = self.root / "adopted-state"
        ext_key = self.root / "adopted.key"
        ext_key.write_text(KEY_SENTINEL, encoding="utf-8")
        git = Git(self.repo)
        git.init(branch="main")
        git.commit_all("pre-adopt commit")
        res_adopt = dispatch_cli_command(
            self.repo,
            None,
            f"adopt --lang python --state-dir {ext_state} --key-file {ext_key} --no-suite --json",
        )
        self.assertEqual(res_adopt["exit_code"], 0, res_adopt["output"])
        self.assertEqual((self.repo / ".openfarm" / "config.toml").is_file(), True)
        self.assertNotIn(KEY_SENTINEL, res_adopt["output"])

        init_dir = self.root / "init-proj"
        res_init = dispatch_cli_command(
            self.repo,
            None,
            ["init", "sample-app", "--dir", str(init_dir), "--state-dir", str(self.root / "init-state"), "--key-file", str(ext_key), "--no-git", "--json"],
        )
        self.assertEqual(res_init["exit_code"], 0, res_init["output"])
        self.assertEqual((init_dir / ".openfarm" / "config.toml").is_file(), True)
        self.assertNotIn(KEY_SENTINEL, res_init["output"])

    def test_invalid_view_raises_usage_error(self) -> None:
        with self.assertRaises(UsageError):
            render_snapshot(self.repo, view="nonexistent")

    def test_cli_tui_once_works_pre_config_and_with_json(self) -> None:
        out_txt = io.StringIO()
        with contextlib.redirect_stdout(out_txt):
            rc = cli_main(["--repo", str(self.repo), "tui", "--once", "--view", "ops"])
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("Ops —", out_txt.getvalue())

        out_json = io.StringIO()
        with contextlib.redirect_stdout(out_json):
            rc_j = cli_main(["tui", "--repo", str(self.repo), "--once", "--view", "overview", "--json"])
        self.assertEqual(rc_j, EXIT_OK)
        parsed = json.loads(out_json.getvalue())
        self.assertEqual(parsed["view"], "overview")
        self.assertEqual(parsed["configured"], False)

        out_bad = io.StringIO()
        err_bad = io.StringIO()
        with contextlib.redirect_stdout(out_bad), contextlib.redirect_stderr(err_bad):
            rc_bad = cli_main(["--repo", str(self.repo), "tui", "--once", "--refresh", "0"])
        self.assertEqual(rc_bad, EXIT_USAGE)


class InitializedRepoTuiTests(unittest.TestCase):
    """Full 7-view snapshots, interactive controller, commands, safety, and refusals."""

    def setUp(self) -> None:
        self.root = _make_temp_dir()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.repo = self.root / "repo"
        self.state_root = self.root / "state"
        self.global_harness = self.root / "global-harness"
        self.key_file = self.root / "worker.key"
        self.auth_file = self.root / "mm.auth"
        self.acp_home = self.root / "home"
        self.repo.mkdir()
        self.state_root.mkdir()
        self.acp_home.mkdir()
        self.key_file.write_text(KEY_SENTINEL + "\n", encoding="utf-8")
        self.auth_file.write_text("AUTH_SECRET_999\n", encoding="utf-8")

        (self.repo / ".openfarm").mkdir()
        (self.repo / "pkg").mkdir()
        (self.repo / "tests").mkdir()
        (self.repo / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
        (self.repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (self.repo / "pkg" / "core.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
        (self.repo / "pkg" / "other.py").write_text("def mul(a: int, b: int) -> int:\n    return a * b\n", encoding="utf-8")
        (self.repo / "tests" / "test_core.py").write_text(
            "import unittest\nfrom pkg.core import add\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertEqual(add(1, 2), 3)\n",
            encoding="utf-8",
        )
        config_text = f"""\
[project]
name = "demo"
lane = "farm"
base_branch = "main"
state_dir = "{self.state_root}"

[adapters]
cards = "files:cards/"
runner = "direct"
languages = ["python"]
model = "scripted"
micromanager = "off"

[model]
endpoint = "https://scripted.invalid/v1"
allowed_endpoints = ["https://scripted.invalid/v1"]
key_file = "{self.key_file}"
headers = {{ "x-cmd-zdr" = "1" }}
implementer = {{ model = "deepseek-v4.1-flash" }}

[policy]
never_touch = ["pkg/sealed.py"]
categories = ["small-feature", "pinned-bugfix"]

[harness]
enabled = true
global_dir = "{self.global_harness}"
"""
        (self.repo / ".openfarm" / "config.toml").write_text(config_text, encoding="utf-8")
        (self.repo / "cards").mkdir()
        self.git = Git(self.repo)
        self.git.init(branch="main")
        self.git.commit_all("initial repo")

        self.state = StateDir(self.state_root)
        self.source = FileCardSource(
            self.repo,
            "cards",
            worktree=self.state.root / "cards-source",
            base_branch="main",
        )
        self.source.prepare()

        self.source.add_card(
            Card(
                id="CARD-1",
                title="Fix add helper",
                state="in_review",
                done_text="add returns sum",
                touches=["pkg/core.py"],
                fields={
                    "category": "pinned-bugfix",
                    "tests": [TestRef(name="test_ok", file="tests/test_core.py", asserts="sum")],
                },
            )
        )
        self.source.add_card(
            Card(
                id="CARD-2",
                title="Extend core module",
                state="ready",
                done_text="`add(2, 3)` returns `5`.",
                touches=["pkg/core.py"],
                fields={
                    "category": "pinned-bugfix",
                    "tests": [TestRef(name="test_ok", file="tests/test_core.py", asserts="add(2, 3) == 5")],
                },
            )
        )
        self.source.add_card(
            Card(
                id="CARD-3",
                title="Extend other module",
                state="ready",
                done_text="extend other",
                touches=["pkg/other.py"],
                fields={"category": "small-feature"},
            )
        )

        # Create unmerged branch `openfarm/CARD-1-fix-add-helper` with a commit.
        self.branch_c1 = "openfarm/CARD-1-fix-add-helper"
        self.git.create_branch(self.branch_c1, "HEAD")
        wt = self.state.root / "tmp-wt-c1"
        self.git.worktree_add(wt, self.branch_c1)
        (wt / "pkg" / "core.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a + b + 0\n",
            encoding="utf-8",
        )
        Git(wt).commit_all("CARD-1 work")
        self.git.worktree_remove(wt)

        # Populate run, run.json with token split by_member/by_effort, board, roster, dossier, harness, bench.
        self.run_id = "run-20261117T000500Z"
        self.run_dir = self.state.create_run(self.run_id)
        write_json(
            self.run_dir / "run.json",
            {
                "run_id": self.run_id,
                "started_at": "2026-11-17T00:05:00Z",
                "finished_at": "2026-11-17T00:06:00Z",
                "spend": 0.0,
                "micromanager_spend": 0.0,
                "tokens": {
                    "worker": 1200,
                    "supervisor": 300,
                    "total": 1500,
                    "worker_share": 0.8,
                    "supervisor_share": 0.2,
                    "by_member": {"impl-1": 900, "checker-1": 300},
                    "by_effort": {"medium": 900, "high": 300},
                },
                "outcomes": [{"card": "CARD-1", "final_state": "in-review", "stop_code": ""}],
            },
        )
        self.state.acquire_run_lock(self.run_id)

        self.board_dir = self.run_dir / "board"
        (self.board_dir / "evidence").mkdir(parents=True)
        ev_file = self.board_dir / "evidence" / "brief-card-1.txt"
        ev_file.write_text("Brief evidence line 1\nBrief evidence line 2\n", encoding="utf-8")
        write_json(self.board_dir / "state.json", {"ended": False, "ended_at": "", "reason": ""})
        write_json(
            self.board_dir / "members.json",
            {
                "impl-1": {
                    "name": "impl-1",
                    "kind": "api",
                    "role": "implementer",
                    "card": "CARD-1",
                    "tier": "dsf",
                    "effort": "medium",
                    "state": "waiting",
                    "wait_since": "2026-11-17T00:05:10Z",
                    "tokens": {"in": 600, "out": 300},
                    "attempts": 2,
                    "last_read_seq": 1,
                },
                "sup-ext": {
                    "name": "sup-ext",
                    "kind": "external",
                    "role": "supervisor",
                    "card": "",
                    "tier": "",
                    "effort": "",
                    "state": "active",
                    "wait_since": "",
                    "tokens": {"in": 0, "out": 0},
                    "attempts": 0,
                    "last_read_seq": 0,
                    "owner_kind": "agent",
                },
            },
        )
        board_posts = [
            {
                "seq": 1,
                "at": "2026-11-17T00:05:01Z",
                "from": "coordinator",
                "to": ["impl-1"],
                "kind": "brief",
                "card": "CARD-1",
                "attempt": 1,
                "ref": None,
                "body": "Implement CARD-1 carefully",
                "evidence": [str(ev_file)],
            },
            {
                "seq": 2,
                "at": "2026-11-17T00:05:15Z",
                "from": "gates",
                "to": ["sup-ext"],
                "kind": "question",
                "card": "CARD-1",
                "attempt": 1,
                "ref": 1,
                "body": "Review counter-test triage",
                "evidence": [],
                "expects": {
                    "op": "adjudicate_checker",
                    "required": ["verdict"],
                    "verdicts": ["uphold", "dismiss_false_reject"],
                },
            },
        ]
        (self.board_dir / "board.jsonl").write_text(
            "\n".join(json.dumps(p) for p in board_posts) + "\n",
            encoding="utf-8",
        )

        # Populate dossier for CARD-1 with attempt 1 diff, 9_diffs, and harness summary.
        cdir = self.state.create_card("CARD-1", self.run_id)
        (cdir / "edits" / "1.diff").write_text("--- a/pkg/core.py\n+++ b/pkg/core.py\n+    return a + b + 0\n", encoding="utf-8")
        write_json(cdir / "card-record.json", {"card": "CARD-1", "final_state": "in-review"})
        write_json(
            cdir / "dossier.json",
            {
                "schema_version": 1,
                "card": "CARD-1",
                "run": self.run_id,
                "final_state": "in-review",
                "stop_code": "",
                "stop_detail": "",
                "1_risk": {
                    "card_hash_at_claim": "abc123",
                    "card_changed": False,
                    "files_changed": 1,
                    "lines_added": 1,
                    "lines_removed": 1,
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
                "9_diffs": {"test_diff": "", "production_diff": "+    return a + b + 0"},
                "harness": {
                    "revision": 1,
                    "state": "confirmed",
                    "cues_applied": ["h-0001"],
                    "lessons_added": [],
                    "probe_transition": "confirmed",
                    "efficiency_score": 0.91,
                },
                "team": [
                    {"member": "impl-1", "role": "implementer", "tier": "dsf", "effort": "medium", "attempts": 2, "tokens": 900}
                ],
                "handbacks": ["none"],
            },
        )

        # Populate local and global harness entries.
        hlib = HarnessLibrary(self.state.harness())
        hlib.record_card_outcome(
            card_meta={
                "id": "CARD-0",
                "language": "python",
                "category": "pinned-bugfix",
                "final_state": "in-review",
            },
            attempt_records=[
                {"attempt": 1, "outcome": "fail", "detail": "TypeError on None"},
                {"attempt": 2, "outcome": "pass"},
            ],
            failure="TypeError on None",
            reflect=False,
        )
        glib = GlobalLibrary(self.global_harness)
        glib.add(
            {
                "id": "h-global",
                "surface": "implementer_guidance",
                "role": "implementer",
                "language": "python",
                "kind": "pinned-bugfix",
                "failure_class": "type",
                "cue": "Handle None inputs explicitly",
                "lesson": "Guard None before dereference",
            },
            project="other-proj",
        )

        # Populate bench report inside state_dir/bench.
        bench_rep_dir = self.state.root / "bench" / "latest"
        bench_rep_dir.mkdir(parents=True)
        write_json(
            bench_rep_dir / "bench-report.json",
            {
                "dry_run": True,
                "summary": {
                    "arms": {
                        "farm": {
                            "runs": 1,
                            "passed": 1,
                            "pass_rate": 1.0,
                            "tokens": 100,
                            "tokens_per_pass": 100,
                            "worker_share": 1.0,
                        }
                    },
                    "categories": {"pinned-bugfix": {"farm": 1.0}},
                },
            },
        )

    def test_all_seven_views_render_with_exact_sections_and_no_key_leak(self) -> None:
        for view in VIEWS:
            text = render_snapshot(
                self.repo,
                view=view,
                acp_home=self.acp_home,
                claude_bin="",
                acp_bin="",
            )
            json_text = render_snapshot(
                self.repo,
                view=view,
                as_json=True,
                acp_home=self.acp_home,
                claude_bin="",
                acp_bin="",
            )
            self.assertNotIn(KEY_SENTINEL, text)
            self.assertNotIn(KEY_SENTINEL, json_text)
            self.assertNotIn("AUTH_SECRET_999", text)
            self.assertNotIn("AUTH_SECRET_999", json_text)

        ov = collect_snapshot(self.repo, view="overview")
        self.assertEqual(ov["data"]["active_run_id"], self.run_id)
        self.assertEqual(ov["data"]["last_run"]["tokens"]["by_member"], {"impl-1": 900, "checker-1": 300})
        self.assertEqual(ov["data"]["last_run"]["tokens"]["by_effort"], {"medium": 900, "high": 300})

        cards_snap = collect_snapshot(self.repo, view="cards")
        by_id = {c["id"]: c for c in cards_snap["data"]["cards"]}
        self.assertIn("CARD-2", by_id["CARD-1"]["collides_with"])
        self.assertNotIn("CARD-3", by_id["CARD-1"]["collides_with"])
        self.assertEqual(by_id["CARD-1"]["unmerged_commits"], 1)
        self.assertIn("pkg/core.py", by_id["CARD-1"]["diff_preview"])

        dos_text = render_snapshot(self.repo, view="dossier", card_id="CARD-1", attempt=1)
        self.assertIn("## Team and board", dos_text)
        self.assertIn("## 9. Diffs", dos_text)
        self.assertIn("## Harness efficiency", dos_text)
        dos_bad_attempt = collect_snapshot(self.repo, view="dossier", card_id="CARD-1", attempt=99)
        self.assertEqual(dos_bad_attempt["data"]["error"], "no gate ran on attempt 99")

        board_snap = collect_snapshot(
            self.repo, view="board", board_filter={"kind": "question", "card": "CARD-1"}
        )
        self.assertEqual(board_snap["data"]["run_source"], "active-lock")
        self.assertEqual(len(board_snap["data"]["posts"]), 1)
        self.assertEqual(board_snap["data"]["posts"][0]["seq"], 2)
        self.assertEqual(len(board_snap["data"]["waiting_members"]), 1)
        self.assertEqual(board_snap["data"]["waiting_members"][0]["name"], "impl-1")

        swarm_snap = collect_snapshot(self.repo, view="swarm")
        self.assertEqual(len(swarm_snap["data"]["members"]), 2)
        self.assertEqual(swarm_snap["data"]["members"][0]["tokens_total"], 900)

        harness_local = collect_snapshot(self.repo, view="harness", harness_global=False)
        self.assertEqual(harness_local["data"]["local"]["total_entries"], 1)
        harness_global = collect_snapshot(self.repo, view="harness", harness_global=True)
        self.assertEqual(harness_global["data"]["global"]["active"], 1)

        ops_snap = collect_snapshot(
            self.repo,
            view="ops",
            acp_home=self.acp_home,
            claude_bin="",
            acp_bin="",
        )
        self.assertEqual(ops_snap["data"]["latest_bench"]["dry_run"], True)
        self.assertEqual(ops_snap["data"]["acp"]["repo_workers"]["key_file_present"], True)

    def test_run_tui_once_and_curses_wrapper(self) -> None:
        buf = io.StringIO()
        code = run_tui(
            repo=self.repo,
            once=True,
            view="board",
            json_output=True,
            out=buf,
        )
        self.assertEqual(code, 0)
        parsed = json.loads(buf.getvalue())
        self.assertEqual(parsed["view"], "board")
        self.assertEqual(parsed["data"]["run_id"], self.run_id)

        with mock.patch("curses.wrapper", return_value=0) as wrapped:
            rc = run_tui(repo=self.repo, once=False, view="cards", force_curses=True)
        self.assertEqual(rc, 0)
        self.assertEqual(wrapped.call_count, 1)

    def test_board_evidence_containment_post_and_answer_validation(self) -> None:
        ok_ev = read_evidence_file(self.repo, None, "evidence/brief-card-1.txt", run_id=self.run_id)
        self.assertEqual(ok_ev["ok"], True)
        self.assertIn("Brief evidence line 1", ok_ev["text"])

        outside = self.root / "outside.txt"
        outside.write_text("SECRET_OUTSIDE", encoding="utf-8")
        bad_ev = read_evidence_file(self.repo, None, str(outside), run_id=self.run_id)
        self.assertEqual(bad_ev["ok"], False)
        self.assertEqual(bad_ev["refused"], True)
        self.assertIn("escapes state_dir", bad_ev["reason"])

        key_ev = read_evidence_file(self.repo, None, str(self.key_file), run_id=self.run_id)
        self.assertEqual(key_ev["ok"], False)
        self.assertNotIn(KEY_SENTINEL, json.dumps(key_ev))

        # Non-code member cannot post reserved kind `done`.
        bad_post = dispatch_cli_command(
            self.repo,
            None,
            ["board", "post", "--as", "sup-ext", "--to", "all", "--kind", "done", "--body", "done!"],
        )
        self.assertEqual(bad_post["exit_code"], 1)

        # Invalid answer verdict refused without posting.
        ctrl = TuiController(self.repo, run_id=self.run_id, view="board")
        bad_ans = ctrl.action_board_answer(
            2, as_name="sup-ext", json_data={"verdict": "approve"}
        )
        self.assertEqual(bad_ans["exit_code"], 1)
        snap_before = collect_snapshot(self.repo, view="board")
        self.assertEqual(snap_before["data"]["total_posts"], 2)

        # Valid answer accepted and posted.
        good_ans = ctrl.action_board_answer(2, as_name="sup-ext", json_data={"verdict": "uphold"})
        self.assertEqual(good_ans["exit_code"], 0, good_ans["output"])
        self.assertEqual(ctrl.snapshot["data"]["total_posts"], 3)
        self.assertEqual(ctrl.snapshot["data"]["unanswered_questions"], [])

    def test_refusal_done_on_unmerged_branch_then_succeeds_after_merge(self) -> None:
        refused = dispatch_cli_command(self.repo, None, "done CARD-1 --json")
        self.assertEqual(refused["exit_code"], 1)
        self.assertEqual(refused["data"]["done"], False)
        self.assertIn("not merged into main", refused["data"]["reason"])

        ok, msg = self.git.merge_no_commit(self.branch_c1)
        self.assertEqual(ok, True, msg)
        self.git.commit("merge CARD-1")

        accepted = dispatch_cli_command(self.repo, None, "done CARD-1 --note reviewed --json")
        self.assertEqual(accepted["exit_code"], 0, accepted["output"])
        self.assertEqual(accepted["data"]["done"], True)

    def test_refusal_swarm_start_without_coordinator_seat(self) -> None:
        # `sup-ext` holds supervisor seat, not coordinator -> must refuse.
        res_no_caller = dispatch_cli_command(self.repo, None, ["swarm", "start", "CARD-2"])
        self.assertEqual(res_no_caller["exit_code"], 1)
        self.assertIn("--as", res_no_caller["output"])

        res_wrong_role = dispatch_cli_command(
            self.repo, None, ["swarm", "start", "CARD-2", "--as", "sup-ext"]
        )
        self.assertEqual(res_wrong_role["exit_code"], 1)
        self.assertIn("coordinator", res_wrong_role["output"])

        # Joining coordinator when [swarm] coordinator is not external is also refused.
        res_join = dispatch_cli_command(
            self.repo, None, ["swarm", "join", "--name", "coord-ext", "--seat", "coordinator"]
        )
        self.assertEqual(res_join["exit_code"], 1)
        self.assertIn("not 'external'", res_join["output"])

    def test_controller_navigation_command_bar_and_harness_actions(self) -> None:
        ctrl = TuiController(self.repo, view="overview")
        self.assertEqual(ctrl.view, "overview")
        self.assertEqual(ctrl.handle_key("2"), True)
        self.assertEqual(ctrl.view, "cards")
        self.assertEqual(ctrl.handle_key("\t"), True)
        self.assertEqual(ctrl.view, "dossier")
        self.assertEqual(ctrl.handle_key("m"), True)
        self.assertEqual(ctrl.dossier_json_mode, True)
        self.assertEqual(ctrl.handle_key(curses.KEY_BTAB), True)
        self.assertEqual(ctrl.view, "cards")
        self.assertEqual(ctrl.handle_key("6"), True)
        self.assertEqual(ctrl.view, "harness")
        self.assertEqual(ctrl.handle_key("g"), True)
        self.assertEqual(ctrl.harness_global, True)

        self.assertEqual(ctrl.handle_key("?"), True)
        self.assertEqual(ctrl.help_open, True)
        self.assertEqual(ctrl.handle_key("?"), True)
        self.assertEqual(ctrl.help_open, False)

        res = ctrl.execute_command("status --json")
        self.assertEqual(res["exit_code"], 0)
        self.assertEqual(res["data"]["run"]["run_id"], self.run_id)
        self.assertEqual(ctrl.handle_key("j"), True)
        self.assertEqual(ctrl.handle_key(27), True)
        self.assertEqual(ctrl.modal_lines, [])

        res_pause = ctrl.action_pause("night check")
        self.assertEqual(res_pause["exit_code"], 0)
        self.assertEqual(ctrl.snapshot["status_bar"]["paused"], True)
        res_resume = ctrl.action_resume()
        self.assertEqual(res_resume["exit_code"], 0)
        self.assertEqual(ctrl.snapshot["status_bar"]["paused"], False)

        # Harness index and reset --yes through controller.
        res_idx = ctrl.action_harness("index")
        self.assertEqual(res_idx["exit_code"], 0)
        res_reset = ctrl.action_harness("reset", yes=True)
        self.assertEqual(res_reset["exit_code"], 0)
        self.assertEqual(ctrl.handle_key(27), True)
        self.assertEqual(ctrl.modal_lines, [])

        # Card actions: check (eligible and ineligible), verdict, escape, and nested tui refusal.
        res_check = ctrl.action_check("CARD-2")
        self.assertEqual(res_check["exit_code"], 0, res_check["output"])
        res_bad_check = ctrl.action_check("CARD-3")
        self.assertEqual(res_bad_check["exit_code"], 1)
        res_verdict = ctrl.action_verdict("clean", card_id="CARD-1", review_tokens=500, note="lgtm")
        self.assertEqual(res_verdict["exit_code"], 0, res_verdict["output"])
        res_escape = ctrl.action_escape("minor edge case", card_id="CARD-1")
        self.assertEqual(res_escape["exit_code"], 0, res_escape["output"])
        res_nested = ctrl.execute_command("tui")
        self.assertEqual(res_nested["exit_code"], 1)
        self.assertIn("nested", res_nested["output"])
        self.assertEqual(ctrl.handle_key(27), True)

        self.assertEqual(ctrl.handle_key("q"), False)

    def test_curses_loop_renders_frames_with_fake_stdscr(self) -> None:
        keys = [ord("2"), ord("4"), ord("5"), ord("?"), ord("?"), curses.ERR, ord("q")]
        drawn: list[str] = []

        class FakeStdscr:
            def erase(self) -> None:
                return None

            def keypad(self, flag: bool) -> None:
                return None

            def timeout(self, ms: int) -> None:
                return None

            def getmaxyx(self) -> tuple[int, int]:
                return (30, 120)

            def addnstr(self, y: int, x: int, text: str, n: int, attr: int = 0) -> None:
                drawn.append(text[:n])

            def refresh(self) -> None:
                return None

            def getch(self) -> int:
                return keys.pop(0) if keys else ord("q")

        def fake_wrapper(fn):
            return fn(FakeStdscr())

        with mock.patch("curses.wrapper", side_effect=fake_wrapper), mock.patch(
            "curses.curs_set", return_value=0
        ):
            rc = run_tui(repo=self.repo, once=False, view="overview", force_curses=True)
        self.assertEqual(rc, 0)
        joined = "\n".join(drawn)
        self.assertIn("openfarm tui", joined)
        self.assertIn("CARD-1", joined)
        self.assertIn("openfarm TUI Help", joined)

    def test_swarm_external_coordinator_full_lifecycle(self) -> None:
        cfg_path = self.repo / ".openfarm" / "config.toml"
        cfg_path.write_text(
            cfg_path.read_text(encoding="utf-8")
            + (
                '\n[tiers.dsf]\nmodel = "deepseek-v4.1-flash"\n'
                'efforts = ["low", "medium", "high", "max"]\n'
                'effort_param = "reasoning_effort"\n'
                '\n[swarm]\n'
                'enabled = true\n'
                'coordinator = "external"\n'
                'supervisor = "external"\n'
                'default_tier = "dsf"\n'
            ),
            encoding="utf-8",
        )

        join_res = dispatch_cli_command(
            self.repo,
            None,
            ["swarm", "join", "--name", "coord-1", "--seat", "coordinator", "--kind", "agent"],
        )
        self.assertEqual(join_res["exit_code"], 0, join_res["output"])

        start_res = dispatch_cli_command(
            self.repo,
            None,
            ["swarm", "start", "CARD-3", "--team", "implementer=dsf:medium", "--as", "coord-1"],
        )
        self.assertEqual(start_res["exit_code"], 0, start_res["output"])

        bad_effort = dispatch_cli_command(
            self.repo,
            None,
            ["swarm", "set", "CARD-3-implementer", "--tier", "dsf", "--effort", "invalid", "--as", "coord-1"],
        )
        self.assertEqual(bad_effort["exit_code"], 1)

        set_res = dispatch_cli_command(
            self.repo,
            None,
            ["swarm", "set", "CARD-3-implementer", "--tier", "dsf", "--effort", "high", "--as", "coord-1"],
        )
        self.assertEqual(set_res["exit_code"], 0, set_res["output"])

        hint_res = dispatch_cli_command(
            self.repo,
            None,
            ["swarm", "hint", "CARD-3-implementer", "--body", "Check edge cases", "--as", "coord-1"],
        )
        self.assertEqual(hint_res["exit_code"], 0, hint_res["output"])

        block_c3 = dispatch_cli_command(
            self.repo,
            None,
            ["swarm", "block", "CARD-3", "--reason", "needs review", "--as", "coord-1"],
        )
        self.assertEqual(block_c3["exit_code"], 0, block_c3["output"])

        block_res = dispatch_cli_command(
            self.repo,
            None,
            ["swarm", "block", "CARD-2", "--reason", "needs input", "--as", "coord-1"],
        )
        self.assertEqual(block_res["exit_code"], 0, block_res["output"])

        # CARD-3 was running, so its block is a stop request: its members stay
        # until the card's own final post (code, as `gates`) lands.
        from openfarm_core.board import Board, board_dir_for
        Board(
            board_dir_for(StateDir(self.state_root), self.run_id), state_dir=self.state_root
        ).post(
            "gates", ["coordinator"], "blocked", "CARD-3 blocked (refused): needs review",
            card="CARD-3", trusted=True,
        )

        rm_res = dispatch_cli_command(
            self.repo,
            None,
            ["swarm", "remove", "CARD-3-implementer", "--as", "coord-1"],
        )
        self.assertEqual(rm_res["exit_code"], 0, rm_res["output"])

        snap = collect_snapshot(self.repo, view="swarm")
        self.assertEqual(snap["data"]["swarm"]["coordinator"], "external")
        self.assertEqual(snap["data"]["tiers"][0]["name"], "dsf")

        # Also exercise `openfarm tui --once` CLI subcommand against all views + `--json`.
        for view in ("overview", "board", "swarm", "harness"):
            out_buf = io.StringIO()
            with contextlib.redirect_stdout(out_buf):
                rc = cli_main([
                    "tui", "--repo", str(self.repo), "--run", self.run_id,
                    "--once", "--view", view, "--json",
                ])
            self.assertEqual(rc, EXIT_OK)
            data = json.loads(out_buf.getvalue())
            self.assertEqual(data["view"], view)
            self.assertEqual(data["configured"], True)
            if view in ("board", "swarm"):
                self.assertEqual(data["data"]["run_id"], self.run_id)
            else:
                self.assertEqual(data["status_bar"]["active_run_id"], self.run_id)
            self.assertNotIn(KEY_SENTINEL, out_buf.getvalue())

    def test_python_pack_banned_pattern_check_is_clean_on_tui_and_tests(self) -> None:
        pack = PythonPack()
        for rel in ("openfarm_core/tui.py", "tests/test_tui.py"):
            path = Path(__file__).resolve().parent.parent / rel
            text = path.read_text(encoding="utf-8")
            added = [
                {"path": rel, "line": idx, "text": line}
                for idx, line in enumerate(text.splitlines(), start=1)
            ]
            test_files = {rel} if rel.startswith("tests/") else set()
            found = pack.patterns(added, [], test_files)
            self.assertEqual(found["banned"], [], f"banned patterns in {rel}: {found['banned']}")


class PrettifiedTuiThemeAndControllerTests(unittest.TestCase):
    """Tests for color themes, visual chrome, selection/search, responsive layout, and hotkeys."""

    def setUp(self) -> None:
        self._fixture = InitializedRepoTuiTests()
        self._fixture.setUp()
        self.addCleanup(self._fixture.doCleanups)
        self.repo = self._fixture.repo
        self.run_id = self._fixture.run_id

    def test_init_curses_theme_color_and_no_color_fallback(self) -> None:
        expected_keys = {
            "brand",
            "header",
            "tab_active",
            "tab_idle",
            "ok",
            "warn",
            "err",
            "info",
            "accent",
            "border",
            "selected",
            "key",
            "muted",
        }
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=False):
            no_color_theme = _init_curses_theme()
        self.assertEqual(set(no_color_theme.keys()), expected_keys)
        self.assertEqual(no_color_theme["header"], curses.A_REVERSE)
        self.assertEqual(no_color_theme["muted"], curses.A_DIM)

        with (
            mock.patch.dict(os.environ, {"NO_COLOR": ""}, clear=False),
            mock.patch("curses.has_colors", return_value=False),
        ):
            mono_theme = _init_curses_theme()
        self.assertEqual(set(mono_theme.keys()), expected_keys)
        self.assertEqual(mono_theme["selected"], curses.A_BOLD | curses.A_REVERSE)

        with (
            mock.patch.dict(os.environ, {"NO_COLOR": ""}, clear=False),
            mock.patch("curses.has_colors", return_value=True),
            mock.patch("curses.start_color"),
            mock.patch("curses.use_default_colors"),
            mock.patch("curses.init_pair"),
            mock.patch("curses.color_pair", side_effect=lambda idx: idx * 256),
        ):
            color_theme = _init_curses_theme()
        self.assertEqual(set(color_theme.keys()), expected_keys)
        self.assertNotEqual(color_theme["ok"], no_color_theme["ok"])

    def test_once_snapshots_include_unicode_chrome_bars_and_icons(self) -> None:
        ov_txt = render_snapshot(self.repo, view="overview")
        self.assertIn("openfarm tui v0.10.0", ov_txt)
        self.assertIn("╭─", ov_txt)
        self.assertIn("╰─", ov_txt)
        self.assertIn("█", ov_txt)
        self.assertIn("░", ov_txt)
        self.assertIn("? 1 question", ov_txt)

        cards_txt = render_snapshot(self.repo, view="cards")
        self.assertIn("◉ in_review", cards_txt)
        self.assertIn("✓ ready", cards_txt)
        self.assertIn("⚠ CARD-2", cards_txt)

        board_txt = render_snapshot(self.repo, view="board")
        self.assertIn("⏸ impl-1", board_txt)
        self.assertIn("? question", board_txt)

    def test_selection_search_esc_precedence_evidence_enter_and_ops_hotkeys(self) -> None:
        ctrl = TuiController(self.repo, run_id=self.run_id, view="cards")
        self.assertEqual(ctrl._selected_card_id(), "CARD-1")
        ctrl.handle_key("j")
        self.assertEqual(ctrl._selected_card_id(), "CARD-2")
        ctrl.handle_key("j")
        self.assertEqual(ctrl._selected_card_id(), "CARD-3")
        ctrl.handle_key("j")
        self.assertEqual(ctrl._selected_card_id(), "CARD-3")
        ctrl.handle_key(curses.KEY_PPAGE)
        self.assertEqual(ctrl._selected_card_id(), "CARD-1")

        ctrl.handle_key("]")
        self.assertEqual(ctrl.detail_scroll, 3)
        ctrl.handle_key("[")
        self.assertEqual(ctrl.detail_scroll, 0)

        ctrl.set_search("CARD-3")
        self.assertEqual(len(ctrl.filtered_items()), 1)
        self.assertEqual(ctrl._selected_card_id(), "CARD-3")

        # Esc priority: help -> modal -> search_query
        ctrl.handle_key("?")
        ctrl.open_modal("Note", ["line 1"])
        self.assertTrue(ctrl.help_open)
        ctrl.handle_key(27)
        self.assertFalse(ctrl.help_open)
        self.assertTrue(bool(ctrl.modal_lines))
        ctrl.handle_key(27)
        self.assertEqual(ctrl.modal_lines, [])
        self.assertEqual(ctrl.search_query, "CARD-3")
        ctrl.handle_key(27)
        self.assertEqual(ctrl.search_query, "")

        # Board Enter opens first evidence file in modal
        ctrl.set_view("board")
        self.assertTrue(_handle_view_hotkey(None, ctrl, ord("\r")))
        self.assertIn("Evidence:", ctrl.modal_title)
        self.assertIn("Brief evidence line 1", "\n".join(ctrl.modal_lines))
        ctrl.close_modal()

        # Ops hotkeys a, P, b work cleanly
        ctrl.set_view("ops")
        self.assertTrue(_handle_view_hotkey(None, ctrl, ord("a")))
        self.assertEqual(ctrl.last_command_result["exit_code"], 0)
        ctrl.close_modal()

        bench_task = self.repo / "bench" / "t1"
        bench_task.mkdir(parents=True)
        (bench_task / "card.md").write_text("# card\n", encoding="utf-8")
        (bench_task / "hidden.py").write_text("assert True\n", encoding="utf-8")
        (bench_task / "task.toml").write_text(
            'id = "py-sample"\nlanguage = "python"\ncategory = "small-feature"\n'
            'card = "card.md"\nhidden_tests = ["hidden.py"]\n',
            encoding="utf-8",
        )
        self.assertTrue(_handle_view_hotkey(None, ctrl, ord("b")))
        self.assertEqual(ctrl.last_command_result["exit_code"], 0)
        self.assertIn("py-sample", "\n".join(ctrl.modal_lines))
        ctrl.close_modal()

        # Bare `bench` and humanized subcommands in command bar render cleanly without ANSI leaks.
        for cmd in ("bench", "status", "swarm", "board", "harness status"):
            res = ctrl.execute_command(cmd)
            self.assertEqual(res["exit_code"], 0, f"{cmd}: {res['output']}")
            self.assertNotIn("\x1b[", res["output"])
            self.assertTrue(bool(ctrl.modal_lines))
            ctrl.close_modal()
        res_bench_json = ctrl.execute_command("bench --json")
        self.assertEqual(res_bench_json["exit_code"], EXIT_USAGE)
        self.assertEqual(res_bench_json["data"]["kind"], "usage error")
        ctrl.close_modal()

        with mock.patch("openfarm_core.cli.probe_acp", return_value={"ok": True, "action": "probe"}):
            self.assertTrue(_handle_view_hotkey(None, ctrl, ord("P")))
            self.assertEqual(ctrl.last_command_result["exit_code"], 0)
            self.assertIn('"action": "probe"', "\n".join(ctrl.modal_lines))

    def test_responsive_curses_frame_renders_at_wide_medium_and_narrow_sizes(self) -> None:
        for size in ((32, 120), (24, 90), (16, 60)):
            drawn: list[tuple[int, int, str]] = []

            class SizedStdscr:
                def erase(self) -> None:
                    return None

                def getmaxyx(self) -> tuple[int, int]:
                    return size

                def addnstr(self, y: int, x: int, text: str, n: int, attr: int = 0) -> None:
                    drawn.append((y, x, text[:n]))

                def refresh(self) -> None:
                    return None

            ctrl = TuiController(self.repo, run_id=self.run_id, view="overview")
            for v in VIEWS:
                ctrl.set_view(v)
                _render_curses_frame(SizedStdscr(), ctrl)
            ctrl.handle_key("?")
            _render_curses_frame(SizedStdscr(), ctrl)
            text = "\n".join(chunk for _, _, chunk in drawn)
            self.assertIn("openfarm tui", text)
            self.assertIn("openfarm TUI Help", text)
            self.assertIn("╭", text)
            self.assertIn("╰", text)


class LivePtyCursesTuiTests(unittest.TestCase):
    """End-to-end real-PTY curses tests across color, NO_COLOR, search, modals, and resize."""

    def setUp(self) -> None:
        self._fixture = InitializedRepoTuiTests()
        self._fixture.setUp()
        self.addCleanup(self._fixture.doCleanups)
        self.repo = self._fixture.repo
        self.global_harness = self._fixture.global_harness
        bench_task = self.repo / "bench" / "t1"
        bench_task.mkdir(parents=True)
        (bench_task / "card.md").write_text("# card\n", encoding="utf-8")
        (bench_task / "hidden.py").write_text("assert True\n", encoding="utf-8")
        (bench_task / "task.toml").write_text(
            'id = "py-sample"\nlanguage = "python"\ncategory = "small-feature"\n'
            'card = "card.md"\nhidden_tests = ["hidden.py"]\n',
            encoding="utf-8",
        )

    @staticmethod
    def _strip_ansi(raw: bytes) -> str:
        txt = raw.decode("utf-8", errors="replace")
        txt = re.sub(r"\x1b\[[0-9;?]*[mK]", "", txt)
        txt = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", " ", txt)
        txt = re.sub(r"\x1b\][^\x07]*\x07", "", txt)
        txt = re.sub(r"\x1b[()][AB012]|\x1b[=>]", "", txt)
        return txt

    @staticmethod
    def _drain_until(
        master_fd: int,
        chunks: list[bytes],
        markers: tuple[str, ...] = (),
        timeout_s: float = 5.0,
    ) -> str:
        ev = threading.Event()
        buf = b""
        elapsed = 0.0
        while elapsed < timeout_s:
            readable, _, _ = select.select([master_fd], [], [], 0.05)
            if readable:
                try:
                    data = os.read(master_fd, 65536)
                except OSError:
                    break
                if data:
                    buf += data
                    chunks.append(data)
                    if markers:
                        cleaned = LivePtyCursesTuiTests._strip_ansi(buf)
                        if all(m in cleaned for m in markers):
                            break
                    continue
            elif buf:
                cleaned = LivePtyCursesTuiTests._strip_ansi(buf)
                if not markers or all(m in cleaned for m in markers):
                    break
            ev.wait(0.02)
            elapsed += 0.07
        return LivePtyCursesTuiTests._strip_ansi(buf)

    def _spawn_pty_tui(self, extra_env: dict[str, str], *, remove_env: tuple[str, ...] = ()):
        master_fd, slave_fd = pty.openpty()
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 120, 0, 0))
        env = dict(os.environ)
        env.setdefault("OPENFARM_TUI_ENGINE", "curses")
        for key in remove_env:
            env.pop(key, None)
        env.update(extra_env)
        env["OPENFARM_GLOBAL_HARNESS"] = str(self.global_harness)
        popen_cls = getattr(importlib.import_module("subprocess"), "Popen")
        proc = popen_cls(
            [
                sys.executable,
                "-m",
                "openfarm_core.cli",
                "--repo",
                str(self.repo),
                "tui",
                "--refresh",
                "0.2",
            ],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave_fd)
        return proc, master_fd

    def test_live_pty_color_navigation_search_modals_and_resize(self) -> None:
        proc, master_fd = self._spawn_pty_tui(
            {"TERM": "xterm-256color"},
            remove_env=("NO_COLOR",),
        )
        chunks: list[bytes] = []
        try:
            init_txt = self._drain_until(master_fd, chunks, markers=("openfarm tui", "╭", "╰"))
            self.assertIn("openfarm tui", init_txt)
            self.assertIn("╭", init_txt)
            self.assertIn("╰", init_txt)

            steps: list[tuple[bytes, tuple[str, ...]]] = [
                (b"2", ("CARD-1",)),
                (b"j", ("CARD-2",)),
                (b"\r", ("Dossier",)),
                (b"4", ("Board",)),
                (b"5", ("Swarm",)),
                (b"6", ("Harness",)),
                (b"g", ("GLOBAL",)),
                (b"7", ("py-sample",)),
                (b"/py-sample\n", ("py-sample",)),
                (b"?", ("openfarm TUI Help",)),
                (b"\x1b", ()),
                (b":status\n", ("run-",)),
                (b"\x1b", ()),
            ]
            for seq, markers in steps:
                os.write(master_fd, seq)
                out = self._drain_until(master_fd, chunks, markers=markers)
                for marker in markers:
                    self.assertIn(marker, out)

            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 20, 68, 0, 0))
            os.kill(proc.pid, signal.SIGWINCH)
            threading.Event().wait(0.15)
            os.write(master_fd, b"1")
            resized_out = self._drain_until(master_fd, chunks, markers=("openfarm tui",))
            self.assertIn("openfarm tui", resized_out)

            os.write(master_fd, b"q")
            rc = proc.wait(timeout=5)
            self._drain_until(master_fd, chunks, timeout_s=0.3)
            self.assertEqual(rc, 0)
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=3)
            with contextlib.suppress(OSError):
                os.close(master_fd)

        full_text = self._strip_ansi(b"".join(chunks))
        self.assertNotIn("Traceback", full_text)
        self.assertNotIn(KEY_SENTINEL, full_text)
        self.assertNotIn("AUTH_SECRET_999", full_text)

    def test_live_pty_no_color_fallback_and_resize(self) -> None:
        proc, master_fd = self._spawn_pty_tui({"TERM": "xterm-256color", "NO_COLOR": "1"})
        chunks: list[bytes] = []
        try:
            init_txt = self._drain_until(master_fd, chunks, markers=("openfarm tui", "╭", "╰"))
            self.assertIn("openfarm tui", init_txt)
            self.assertIn("╭", init_txt)
            self.assertIn("╰", init_txt)

            os.write(master_fd, b"2")
            self.assertIn("CARD-1", self._drain_until(master_fd, chunks, markers=("CARD-1",)))
            os.write(master_fd, b"?")
            self.assertIn("openfarm TUI Help", self._drain_until(master_fd, chunks, markers=("openfarm TUI Help",)))
            os.write(master_fd, b"\x1b")
            self._drain_until(master_fd, chunks)

            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 20, 68, 0, 0))
            os.kill(proc.pid, signal.SIGWINCH)
            threading.Event().wait(0.15)
            os.write(master_fd, b"1")
            self._drain_until(master_fd, chunks, markers=("openfarm tui",))

            os.write(master_fd, b"q")
            rc = proc.wait(timeout=5)
            self._drain_until(master_fd, chunks, timeout_s=0.3)
            self.assertEqual(rc, 0)
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=3)
            with contextlib.suppress(OSError):
                os.close(master_fd)

        full_text = self._strip_ansi(b"".join(chunks))
        self.assertNotIn("Traceback", full_text)
        self.assertNotIn(KEY_SENTINEL, full_text)
        self.assertNotIn("AUTH_SECRET_999", full_text)


if __name__ == "__main__":
    unittest.main()
