"""Tests for Stage 2: Driver Mode (`openfarm drive`) & multi-harness `install-skill`."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openfarm_core.bootstrap import INSTALL_TARGETS, install_skill_bundle
from openfarm_core.cli import EXIT_OK, EXIT_REFUSED, EXIT_USAGE, main as cli_main
from openfarm_core.config import load_config
from openfarm_core.driver import DriverAgent, drive_run
from openfarm_core.git import Git
from openfarm_core.human_cli import Style, error_hint, format_human
from openfarm_core.packs.python import PythonPack
from openfarm_core.state import StateDir, read_json
from tests.test_python_e2e import GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy


def _run_cli(*args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_main(list(args))
    return code, out.getvalue(), err.getvalue()


def _write_mock_driver_cli(
    directory: Path,
    provider: str,
    *,
    mode: str = "drive",
    log_path: Path | None = None,
) -> Path:
    """Create an executable mock `claude`, `agy`, or `codex` driver binary."""
    bin_name = {"claude": "claude", "antigravity": "agy", "codex": "codex"}[provider]
    script = directory / bin_name
    log_str = str(log_path) if log_path is not None else ""
    code = f"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

provider = {provider!r}
mode = {mode!r}
log_file = {log_str!r}
args = sys.argv[1:]
if provider == "antigravity" and "-p" in args:
    p_idx = args.index("-p")
    prompt_raw = args[p_idx + 1] if p_idx + 1 < len(args) else ""
    stdin_raw = sys.stdin.read()
else:
    prompt_raw = sys.stdin.read()
    stdin_raw = prompt_raw

if log_file:
    entry = {{"args": args, "prompt": prompt_raw, "home": os.environ.get("HOME", "")}}
    lp = Path(log_file)
    prev = lp.read_text(encoding="utf-8") if lp.is_file() else ""
    lp.write_text(prev + json.dumps(entry) + "\\n", encoding="utf-8")

def emit_result(text: str, *, tokens_in: int = 40, tokens_out: int = 15, ansi_wrap: bool = False):
    if provider == "codex":
        print(json.dumps({{"type": "thread.started", "thread_id": "t-drv"}}))
        print(json.dumps({{
            "type": "item.completed",
            "item": {{"id": "i-1", "type": "agent_message", "text": text}},
        }}))
        if mode != "no-usage":
            print(json.dumps({{
                "type": "turn.completed",
                "usage": {{"input_tokens": tokens_in, "output_tokens": tokens_out}},
            }}))
        return
    if provider == "antigravity":
        payload = {{
            "is_error": False,
            "response": text,
        }}
        if mode != "no-usage":
            payload["usage"] = {{"promptTokenCount": tokens_in, "candidatesTokenCount": tokens_out}}
        out_str = json.dumps(payload)
    else:
        payload = {{
            "type": "result",
            "is_error": False,
            "result": text,
        }}
        if mode != "no-usage":
            payload["usage"] = {{
                "input_tokens": tokens_in,
                "cache_creation_input_tokens": 5,
                "cache_read_input_tokens": 3,
                "output_tokens": tokens_out,
            }}
        out_str = json.dumps(payload)
    if ansi_wrap:
        out_str = "\\x1b]0;title\\x07\\x1b[32m" + out_str + "\\x1b[0m"
    print(out_str)

if mode == "inspect":
    info = {{
        "args": args,
        "has_bare": "--bare" in args,
        "stdin": stdin_raw,
        "prompt": prompt_raw,
        "home": os.environ.get("HOME", ""),
        "codex_home": os.environ.get("CODEX_HOME", ""),
        "cwd": os.getcwd(),
        "anthropic_key": os.environ.get("ANTHROPIC_API_KEY", ""),
        "gemini_key": os.environ.get("GEMINI_API_KEY", ""),
        "google_key": os.environ.get("GOOGLE_API_KEY", ""),
        "openai_key": os.environ.get("OPENAI_API_KEY", ""),
    }}
    emit_result(json.dumps(info), tokens_in=50, tokens_out=20, ansi_wrap=True)
    raise SystemExit(0)

if mode == "no-usage":
    emit_result(json.dumps({{"ok": True}}))
    raise SystemExit(0)

# The driver's child may have no writable folder (the boundary), so the
# "then recover" fakes keep no count: they answer badly until asked to correct.
is_correction = "Previous JSON turn had issue" in prompt_raw

if mode == "bad-then-recover":
    if not is_correction:
        emit_result("NOT VALID JSON AT ALL")
        raise SystemExit(0)

if mode == "always-bad":
    emit_result("ALWAYS BROKEN JSON {{{{")
    raise SystemExit(0)

if mode == "refuse-then-recover":
    if not is_correction:
        emit_result(json.dumps({{"actions": [{{"action": "start", "card": "NO-SUCH-CARD"}}], "answers": []}}))
        raise SystemExit(0)

req = {{}}
for cand_start in [0, *[i + 1 for i, ch in enumerate(prompt_raw) if ch == "\\n" and i + 1 < len(prompt_raw) and prompt_raw[i + 1] == "{{"]]:
    try:
        # The turn's JSON state may be followed by the open questions' requests.
        cand, _end = json.JSONDecoder().raw_decode(prompt_raw[cand_start:])
        if isinstance(cand, dict) and ("run_id" in cand or "dossier" in cand):
            req = cand
            break
    except ValueError:
        continue

# Handle wave review prompt
if isinstance(req, dict) and "dossier" in req and "diff" in req:
    rev = {{
        "verdict": "clean",
        "note": f"Reviewed {{req.get('card_id')}} via mock {{provider}}",
        "review_tokens": 321,
    }}
    emit_result(json.dumps(rev), tokens_in=60, tokens_out=22)
    raise SystemExit(0)

actions = []
answers = []
if isinstance(req, dict):
    for card_item in req.get("pending_ready_cards") or []:
        cid = card_item.get("id")
        if cid:
            actions.append({{"action": "start", "card": cid}})
            break
    for q in req.get("open_questions") or []:
        seq = q.get("seq")
        op = q.get("op") or (q.get("expects") or {{}}).get("op")
        if op == "shape_and_plan":
            data = {{"verdict": "ready", "clarified_notes": f"Mock {{provider}} ready"}}
        elif op == "pre_review":
            data = {{"verdict": "approve", "reviewer_brief": f"Mock {{provider}} approved"}}
        elif op == "reflect_harness":
            data = {{"lessons": []}}
        elif op == "scaffold_contract":
            data = {{"edits": []}}
        elif op == "shape_and_scaffold":
            data = {{"verdict": "ready", "clarified_notes": f"Mock {{provider}} ready", "edits": []}}
        elif op == "coach":
            data = {{"root_cause": "test failure", "guidance": "check implementation"}}
        elif op == "adjudicate_checker":
            data = {{"verdict": "uphold", "rationale": "checker valid"}}
        elif op == "handback_triage":
            data = {{"action": "hint", "body": "follow Done assertions"}}
        else:
            data = {{"summary": "ok", "recommendations": []}}
        answers.append({{"seq": seq, "data": data, "body": f"mock {{provider}} {{op}}"}})

emit_result(json.dumps({{"actions": actions, "answers": answers}}), tokens_in=45, tokens_out=18, ansi_wrap=(provider != "codex"))
"""
    script.write_text(code, encoding="utf-8")
    script.chmod(0o755)
    return script


class InstallSkillMultiHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-stage2-skill-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_install_targets_codex_workspace_and_all(self) -> None:
        self.assertEqual(
            INSTALL_TARGETS,
            ("claude", "antigravity", "codex", "agents", "workspace", "all"),
        )

        # --target codex with explicit --dest installs straight to dest/skills & dest/openfarm-AGENTS.md
        codex_dest = self.tmp / "custom-codex"
        rc_c, out_c, err_c = _run_cli(
            "install-skill", "--target", "codex", "--dest", str(codex_dest), "--json"
        )
        self.assertEqual(rc_c, EXIT_OK, err_c)
        data_c = json.loads(out_c)
        self.assertTrue((codex_dest / "skills" / "openfarm" / "SKILL.md").is_file())
        self.assertTrue((codex_dest / "skills" / "openfarm-grill-me" / "SKILL.md").is_file())
        self.assertTrue((codex_dest / "skills" / "openfarm-initialize" / "SKILL.md").is_file())
        self.assertTrue((codex_dest / "openfarm-AGENTS.md").is_file())
        self.assertFalse((codex_dest / "rules").exists())
        self.assertEqual(
            data_c["installed"]["codex"]["plugin"],
            [str((codex_dest / "openfarm-AGENTS.md").resolve())],
        )

        # install_skill_bundle(target="codex", home=fake_home) writes to fake_home/.codex/
        fake_home = self.tmp / "fake-home"
        res_home = install_skill_bundle(target="codex", home=fake_home)
        self.assertTrue((fake_home / ".codex" / "skills" / "openfarm" / "SKILL.md").is_file())
        self.assertTrue((fake_home / ".codex" / "openfarm-AGENTS.md").is_file())
        self.assertIn("codex", res_home["installed"])

        # --target workspace writes both .agents/skills and .codex/skills plus .claude/commands
        ws = self.tmp / "ws"
        ws.mkdir()
        rc_w, out_w, err_w = _run_cli(
            "install-skill", "--target", "workspace", "--dest", str(ws), "--json"
        )
        self.assertEqual(rc_w, EXIT_OK, err_w)
        data_w = json.loads(out_w)
        self.assertTrue((ws / ".agents" / "skills" / "openfarm" / "SKILL.md").is_file())
        self.assertTrue((ws / ".codex" / "skills" / "openfarm" / "SKILL.md").is_file())
        self.assertTrue((ws / ".codex" / "skills" / "openfarm-grill-me" / "SKILL.md").is_file())
        self.assertTrue((ws / ".codex" / "skills" / "openfarm-initialize" / "SKILL.md").is_file())
        self.assertTrue((ws / ".claude" / "commands" / "openfarm-initialize.md").is_file())
        self.assertEqual(len(data_w["installed"]["workspace"]["skills"]), 16)

        # --target all includes claude, antigravity, codex, and agents
        all_home = self.tmp / "all-home"
        rc_a, out_a, err_a = _run_cli(
            "install-skill", "--target", "all", "--dest", str(all_home), "--json"
        )
        self.assertEqual(rc_a, EXIT_OK, err_a)
        data_a = json.loads(out_a)
        self.assertEqual(
            set(data_a["installed"].keys()),
            {"claude", "antigravity", "codex", "agents"},
        )
        self.assertTrue((all_home / ".codex" / "skills" / "openfarm" / "SKILL.md").is_file())
        self.assertTrue((all_home / ".codex" / "openfarm-AGENTS.md").is_file())
        self.assertFalse((all_home / ".codex" / "rules").exists())

    def test_cli_help_lists_22_commands_including_drive(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as caught:
                cli_main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        help_text = out.getvalue()
        self.assertIn("39 commands", help_text)
        self.assertIn("drive", help_text)
        self.assertIn("39 commands", error_hint("usage error", "no subcommand specified"))


class DriverAgentUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-drv-unit-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        self.work_dir = self.tmp / "work"
        self.work_dir.mkdir()

    def test_claude_antigravity_codex_flags_isolation_and_usage_parsing(self) -> None:
        fake_host_home = self.tmp / "host-home"
        (fake_host_home / ".claude").mkdir(parents=True)
        (fake_host_home / ".claude" / ".credentials.json").write_text('{"claudeAiOauth":{}}', encoding="utf-8")
        (fake_host_home / ".gemini" / "antigravity-cli").mkdir(parents=True)
        (fake_host_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").write_text("ag-tok\n", encoding="utf-8")
        (fake_host_home / ".codex").mkdir(parents=True)
        (fake_host_home / ".codex" / "auth.json").write_text('{"token":"cx-tok"}\n', encoding="utf-8")

        base_env = {
            "HOME": str(fake_host_home),
            "PATH": os.environ.get("PATH", "/usr/bin"),
            "ANTHROPIC_API_KEY": "sk-ant-leak-test",
            "GEMINI_API_KEY": "gk-leak-test",
            "GOOGLE_API_KEY": "ggk-leak-test",
            "OPENAI_API_KEY": "sk-oai-leak-test",
        }

        # Claude read-only flags & no --bare
        claude_bin = _write_mock_driver_cli(self.bin_dir, "claude", mode="inspect")
        with DriverAgent(
            agent="claude",
            cwd=self.work_dir,
            driver_bin=str(claude_bin),
            model="claude-sonnet-4-6",
            effort="high",
            env=base_env,
        ) as claude_drv:
            res = claude_drv.complete("hello claude", system_prompt="SYS")
        c_info = json.loads(res.text)
        self.assertFalse(c_info["has_bare"])
        for req_flag in (
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--disable-slash-commands",
            "--strict-mcp-config",
            "--permission-prompts",
            "none",
            "--permission-mode",
            "dontAsk",
            "--tools",
            "",
            "--model",
            "claude-sonnet-4-6",
            "--effort",
            "high",
            # The driver replaces Claude Code's own prompt (lean_prompt, v0.8.5).
            "--system-prompt",
            "SYS",
        ):
            self.assertIn(req_flag, c_info["args"])
        self.assertNotIn("--append-system-prompt", c_info["args"])
        self.assertNotEqual(c_info["home"], str(fake_host_home))
        self.assertFalse(Path(c_info["home"]).exists(), "isolated HOME must be cleaned up")
        self.assertEqual(c_info["anthropic_key"], "")
        self.assertEqual(res.tokens_in, 58)  # 50 + 5 + 3
        self.assertEqual(res.tokens_out, 20)

        # Antigravity read-only sandbox flags & usage parsing
        agy_bin = _write_mock_driver_cli(self.bin_dir, "antigravity", mode="inspect")
        with DriverAgent(
            agent="agy",
            cwd=self.work_dir,
            driver_bin=str(agy_bin),
            model="gemini-2.5-pro",
            effort="medium",
            env=base_env,
        ) as agy_drv:
            res_ag = agy_drv.complete("hello agy", system_prompt="SYS_AG")
        a_info = json.loads(res_ag.text)
        for req_flag in (
            "-p",
            "--output-format",
            "json",
            "--sandbox",
            "--disable-slash-commands",
            "--model",
            "gemini-2.5-pro",
            "--effort",
            "medium",
        ):
            self.assertIn(req_flag, a_info["args"])
        self.assertEqual(a_info["args"][-2], "-p")
        self.assertIn("SYS_AG", a_info["args"][-1])
        self.assertIn("hello agy", a_info["args"][-1])
        self.assertEqual(a_info["stdin"], "")
        self.assertNotEqual(a_info["home"], str(fake_host_home))
        self.assertEqual(a_info["gemini_key"], "")
        self.assertEqual(a_info["google_key"], "")
        self.assertEqual(res_ag.tokens_in, 50)
        self.assertEqual(res_ag.tokens_out, 20)

        # Codex read-only flags, CODEX_HOME isolation, and NDJSON turn.completed usage
        codex_bin = _write_mock_driver_cli(self.bin_dir, "codex", mode="inspect")
        with DriverAgent(
            agent="codex",
            cwd=self.work_dir,
            driver_bin=str(codex_bin),
            model="gpt-5-codex",
            effort="high",
            env=base_env,
        ) as cx_drv:
            res_cx = cx_drv.complete("hello codex", system_prompt="SYS_CX")
        x_info = json.loads(res_cx.text)
        for req_flag in (
            "exec",
            "--json",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "-s",
            "read-only",
            "-m",
            "gpt-5-codex",
            "-c",
            'model_reasoning_effort="high"',
            "-",
        ):
            self.assertIn(req_flag, x_info["args"])
        # The child works in an empty private folder of the driver's, not in `cwd`.
        self.assertEqual(x_info["args"][x_info["args"].index("-C") + 1], x_info["cwd"])
        self.assertNotEqual(Path(x_info["cwd"]), self.work_dir.resolve())
        # The system prompt is Codex's own instructions, not glued onto stdin.
        self.assertIn('instructions="SYS_CX"', x_info["args"])
        self.assertNotIn("SYS_CX", x_info["stdin"])
        self.assertIn("hello codex", x_info["stdin"])
        self.assertTrue(x_info["codex_home"].startswith(x_info["home"]))
        self.assertEqual(x_info["openai_key"], "")
        self.assertEqual(res_cx.tokens_in, 50)
        self.assertEqual(res_cx.tokens_out, 20)

        # Fallback token estimate when usage is omitted
        nousage_bin = _write_mock_driver_cli(self.bin_dir, "claude", mode="no-usage")
        with DriverAgent(agent="claude", cwd=self.work_dir, driver_bin=str(nousage_bin), env=base_env) as nu_drv:
            res_nu = nu_drv.complete("x" * 80)
        self.assertTrue(res_nu.estimated_usage)
        self.assertGreater(res_nu.tokens_in, 0)
        self.assertGreater(res_nu.tokens_out, 0)


class DriverModeEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy = build_python_toy()
        self.addCleanup(self.toy.cleanup)
        self.tmp = Path(tempfile.mkdtemp(prefix="of-drv-e2e-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _add_card_1(self) -> None:
        self.toy.write_card(
            "CARD-1",
            title="A full bundle refuses a push",
            category="pinned-bugfix",
            unit=".",
            touches=["tally/bounded.py"],
            tests=[{
                "name": "test_a_full_bundle_refuses_a_push",
                "file": "tests/test_full.py",
                "asserts": "push raises FullError",
            }],
            done_text="push on a full bundle raises `FullError`.",
        )
        self.toy.commit("add CARD-1")

    def _worker_script(self) -> Path:
        sp = self.tmp / "worker-script.json"
        sp.write_text(
            json.dumps({
                "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
                "CARD-1/implementer": GOOD_IMPLEMENTATION,
                "CARD-1/checker_pass1": "RESTATEMENT: full bundle refuses push with FullError",
                "CARD-1/checker_pass2": "{}",
            }),
            encoding="utf-8",
        )
        return sp

    def test_drive_managed_run_for_claude_agy_and_codex_mock_clis(self) -> None:
        sp = self._worker_script()

        for agent_arg, norm_provider in (
            ("claude", "claude"),
            ("agy", "antigravity"),
            ("codex", "codex"),
        ):
            with self.subTest(agent=agent_arg):
                # Reset card state for each subtest on a fresh toy copy? Better build fresh toy per subtest!
                toy = build_python_toy()
                self.addCleanup(toy.cleanup)
                toy.write_card(
                    "CARD-1",
                    title="A full bundle refuses a push",
                    category="pinned-bugfix",
                    unit=".",
                    touches=["tally/bounded.py"],
                    tests=[{
                        "name": "test_a_full_bundle_refuses_a_push",
                        "file": "tests/test_full.py",
                        "asserts": "push raises FullError",
                    }],
                    done_text="push on a full bundle raises `FullError`.",
                )
                toy.commit("add CARD-1")

                mock_bin = _write_mock_driver_cli(self.tmp / f"bin-{agent_arg}" if (self.tmp / f"bin-{agent_arg}").exists() else (lambda p: (p.mkdir(), p)[1])(self.tmp / f"bin-{agent_arg}"), norm_provider, mode="drive")
                rc, out, err = _run_cli(
                    "--repo", str(toy.root),
                    "--json",
                    "drive",
                    "--agent", agent_arg,
                    "--seat", "both",
                    "--cards", "1",
                    "--driver-bin", str(mock_bin),
                    "--script", str(sp),
                )
                self.assertEqual(rc, EXIT_OK, f"{agent_arg}: {err}\n{out}")
                payload = json.loads(out)
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["agent"], norm_provider)
                self.assertEqual(payload["seat"], "both")
                self.assertGreaterEqual(payload["driver_turns"], 2)
                self.assertGreater(payload["driver_tokens_in"], 0)
                self.assertGreater(payload["driver_tokens_out"], 0)
                self.assertTrue(any(a["action"] == "start" and a["card"] == "CARD-1" and not a.get("fallback") for a in payload["actions_taken"]))
                self.assertTrue(any(a["op"] == "shape_and_plan" and not a.get("fallback") for a in payload["answers_sent"]))
                self.assertTrue(any(a["op"] == "pre_review" and not a.get("fallback") for a in payload["answers_sent"]))
                self.assertEqual(payload["outcomes"][0]["final_state"], "in-review")

                # Verify board registered distinct coordinator + supervisor agent members
                board_members = read_json(
                    toy.state_dir / "runs" / payload["run_id"] / "board" / "members.json"
                )
                self.assertEqual(board_members["driver-coord"]["role"], "coordinator")
                self.assertEqual(board_members["driver-coord"]["owner_kind"], "agent")
                self.assertEqual(board_members["driver-sup"]["role"], "supervisor")
                self.assertEqual(board_members["driver-sup"]["owner_kind"], "agent")

    def test_drive_with_unified_script_and_human_output(self) -> None:
        self._add_card_1()
        combined = self.tmp / "combined-script.json"
        combined.write_text(
            json.dumps({
                "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
                "CARD-1/implementer": GOOD_IMPLEMENTATION,
                "CARD-1/checker_pass1": "RESTATEMENT: full bundle refuses push with FullError",
                "CARD-1/checker_pass2": "{}",
                "driver/plan": {"actions": [{"action": "start", "card": "CARD-1"}], "answers": []},
                "driver/question/shape_and_plan": {
                    "actions": [],
                    "answers": [{"seq": "auto", "data": {"verdict": "ready", "clarified_notes": "Scripted ready"}}],
                },
                "driver/question/pre_review": {
                    "actions": [],
                    "answers": [{"seq": "auto", "data": {"verdict": "approve", "reviewer_brief": "Scripted ok"}}],
                },
                "driver/question/reflect_harness": {
                    "actions": [],
                    "answers": [{"seq": "auto", "data": {"lessons": []}}],
                },
                "driver/wave/CARD-1": {
                    "verdict": "clean",
                    "note": "Scripted clean review",
                    "review_tokens": 111,
                },
            }),
            encoding="utf-8",
        )
        rc, out, err = _run_cli(
            "--repo", str(self.toy.root),
            "drive",
            "--agent", "claude",
            "--seat", "both",
            "--card", "CARD-1",
            "--wave",
            "--record-verdict",
            "--script", str(combined),
        )
        self.assertEqual(rc, EXIT_OK, err or out)
        self.assertIn("Driver Run", out)
        self.assertIn("in-review", out)
        self.assertIn("Wave Reviews (1)", out)
        self.assertIn("Scripted clean review", out)

        vfiles = list(self.toy.state_dir.glob("cards/CARD-1/*/verdict.json"))
        self.assertEqual(len(vfiles), 1)
        vdata = json.loads(vfiles[0].read_text(encoding="utf-8"))
        self.assertEqual(vdata["verdict"], "clean")
        # The scripted answer claims 111; the record carries the turn's own
        # (here guessed) tokens and says who decided.
        self.assertNotEqual(vdata["review_tokens"], 111)
        self.assertGreater(vdata["review_tokens"], 0)
        self.assertTrue(vdata["review_tokens_estimated"])
        self.assertEqual(vdata["by"], "driver/claude")

    def test_malformed_json_and_refusal_retry_and_safe_fallback(self) -> None:
        self._add_card_1()
        sp = self._worker_script()

        # 1. Recover on correction turn after malformed JSON
        bin_rec = self.tmp / "bin-recover"
        bin_rec.mkdir()
        mock_rec = _write_mock_driver_cli(bin_rec, "claude", mode="bad-then-recover")
        rc1, out1, err1 = _run_cli(
            "--repo", str(self.toy.root), "--json",
            "drive", "--agent", "claude", "--seat", "both", "--cards", "1",
            "--driver-bin", str(mock_rec), "--script", str(sp),
        )
        self.assertEqual(rc1, EXIT_OK, err1)
        p1 = json.loads(out1)
        self.assertTrue(p1["ok"])
        self.assertEqual(p1["outcomes"][0]["final_state"], "in-review")
        self.assertGreaterEqual(p1["driver_turns"], 3)

        # 2. Recover on correction turn after refused action
        toy_ref = build_python_toy()
        self.addCleanup(toy_ref.cleanup)
        toy_ref.write_card(
            "CARD-1",
            title="A full bundle refuses a push",
            category="pinned-bugfix",
            unit=".",
            touches=["tally/bounded.py"],
            tests=[{
                "name": "test_a_full_bundle_refuses_a_push",
                "file": "tests/test_full.py",
                "asserts": "push raises FullError",
            }],
            done_text="push on a full bundle raises `FullError`.",
        )
        toy_ref.commit("add CARD-1")
        bin_ref = self.tmp / "bin-refuse"
        bin_ref.mkdir()
        mock_ref = _write_mock_driver_cli(bin_ref, "codex", mode="refuse-then-recover")
        rc_ref, out_ref, err_ref = _run_cli(
            "--repo", str(toy_ref.root), "--json",
            "drive", "--agent", "codex", "--seat", "both", "--cards", "1",
            "--driver-bin", str(mock_ref), "--script", str(sp),
        )
        self.assertEqual(rc_ref, EXIT_OK, err_ref)
        p_ref = json.loads(out_ref)
        self.assertEqual(p_ref["outcomes"][0]["final_state"], "in-review")
        # Every turn ran: the fake works without a writable folder.
        self.assertEqual(p_ref["driver_errors"], [])

        # 3. Always malformed JSON -> 1 correction turn, then the run's own timeout rule
        # (`on_external_timeout = "fallback"` by default) for what the driver could not
        # answer: code starts the card, the supervisor's ops are skipped, and nothing is
        # recorded as a supervisor answer.
        toy_fb = build_python_toy()
        self.addCleanup(toy_fb.cleanup)
        toy_fb.write_card(
            "CARD-1",
            title="A full bundle refuses a push",
            category="pinned-bugfix",
            unit=".",
            touches=["tally/bounded.py"],
            tests=[{
                "name": "test_a_full_bundle_refuses_a_push",
                "file": "tests/test_full.py",
                "asserts": "push raises FullError",
            }],
            done_text="push on a full bundle raises `FullError`.",
        )
        toy_fb.commit("add CARD-1")
        bin_bad = self.tmp / "bin-always-bad"
        bin_bad.mkdir()
        mock_bad = _write_mock_driver_cli(bin_bad, "antigravity", mode="always-bad")
        rc2, out2, err2 = _run_cli(
            "--repo", str(toy_fb.root), "--json",
            "drive", "--agent", "antigravity", "--seat", "both", "--cards", "1",
            "--driver-bin", str(mock_bad), "--script", str(sp),
        )
        self.assertEqual(rc2, EXIT_OK, err2)
        p2 = json.loads(out2)
        self.assertTrue(p2["ok"])
        self.assertEqual(p2["outcomes"][0]["final_state"], "in-review")
        self.assertTrue(any(a.get("fallback") for a in p2["actions_taken"]))
        self.assertEqual(p2["answers_sent"], [])
        self.assertTrue(p2["timeouts"])
        self.assertTrue(all(t["policy"] == "fallback" for t in p2["timeouts"]))

    def test_wave_review_records_verdict_without_reading_never_touch_or_merging(self) -> None:
        # Add a never_touch file with a sentinel string and verify it is never exposed to driver prompts
        self.toy.write(
            "tally/invariants.py",
            'SECRET_INVARIANT_DO_NOT_EXPOSE = "seam-secret-999"\n',
        )
        cfg_path = self.toy.root / ".openfarm" / "config.toml"
        cfg_text = cfg_path.read_text(encoding="utf-8").replace(
            'never_touch = ["tally/seam.py"]',
            'never_touch = ["tally/seam.py", "tally/invariants.py"]',
        )
        self.assertIn("tally/invariants.py", cfg_text)
        cfg_path.write_text(cfg_text, encoding="utf-8")
        self.toy.commit("add never_touch invariant file")
        self._add_card_1()
        sp = self._worker_script()

        log_path = self.tmp / "driver-prompts.jsonl"
        bin_dir = self.tmp / "bin-wave"
        bin_dir.mkdir()
        mock_cli = _write_mock_driver_cli(bin_dir, "claude", mode="drive", log_path=log_path)

        git = Git(self.toy.root)
        main_before = git.head("refs/heads/main")

        rc, out, err = _run_cli(
            "--repo", str(self.toy.root), "--json",
            "drive",
            "--agent", "claude",
            "--seat", "both",
            "--cards", "1",
            "--driver-bin", str(mock_cli),
            "--script", str(sp),
            "--wave",
            "--record-verdict",
        )
        self.assertEqual(rc, EXIT_OK, err or out)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["reviews"]), 1)
        rev = payload["reviews"][0]
        self.assertEqual(rev["card"], "CARD-1")
        self.assertEqual(rev["verdict"], "clean")
        # Measured from the mock CLI's own usage (60 + 5 + 3 in, 22 out), not the 321 it claims.
        self.assertEqual(rev["review_tokens"], 90)
        self.assertFalse(rev["review_tokens_estimated"])
        self.assertTrue(rev["recorded"])
        vpath = Path(rev["verdict_path"])
        self.assertTrue(vpath.is_file())
        self.assertEqual(json.loads(vpath.read_text(encoding="utf-8"))["verdict"], "clean")

        # Never auto-merged onto main, and never pushed
        self.assertEqual(git.head("refs/heads/main"), main_before)
        branches = git.branches("openfarm/CARD-1-*")
        self.assertTrue(branches)
        self.assertGreater(git.count_commits(f"refs/heads/{branches[0]}", "refs/heads/main"), 0)

        # Verify driver prompts never saw never_touch contents
        logged_text = log_path.read_text(encoding="utf-8")
        self.assertNotIn("SECRET_INVARIANT_DO_NOT_EXPOSE", logged_text)
        self.assertNotIn("seam-secret-999", logged_text)

    def test_attach_to_active_run_and_validation_errors(self) -> None:
        self._add_card_1()
        sp = self._worker_script()

        # Enable external supervisor only in config; let CodeCoordinator run cards while driver drives supervisor
        cfg_path = self.toy.root / ".openfarm" / "config.toml"
        cfg_path.write_text(
            cfg_path.read_text(encoding="utf-8")
            + '\n[swarm]\nenabled = true\ncoordinator = "code"\nsupervisor = "external"\nexternal_wait = "10s"\n',
            encoding="utf-8",
        )
        self.toy.commit("enable external supervisor")

        run_id = "run-attach-sup"
        run_res: dict = {}

        def _bg_run() -> None:
            run_res["res"] = _run_cli(
                "--repo", str(self.toy.root), "--json",
                "run", "--card", "CARD-1", "--run-id", run_id, "--script", str(sp),
            )

        th = threading.Thread(target=_bg_run, daemon=True)
        th.start()

        ev = threading.Event()
        board_posts = self.toy.state_dir / "runs" / run_id / "board" / "board.jsonl"
        for _ in range(300):
            if board_posts.is_file() or not th.is_alive():
                break
            ev.wait(0.02)
        self.assertTrue(board_posts.is_file(), f"board never appeared: {run_res}")

        bin_dir = self.tmp / "bin-attach"
        bin_dir.mkdir()
        mock_cli = _write_mock_driver_cli(bin_dir, "codex", mode="drive")
        cfg = load_config(self.toy.root)
        dp = drive_run(
            self.toy.root,
            cfg,
            StateDir(self.toy.state_dir),
            agent="codex",
            seat="supervisor",
            run_id=run_id,
            driver_name="attach",
            driver_bin=str(mock_cli),
        )
        th.join(timeout=15.0)
        self.assertFalse(th.is_alive())
        rc_bg, out_bg, err_bg = run_res["res"]
        self.assertEqual(rc_bg, EXIT_OK, err_bg or out_bg)
        self.assertEqual(dp["run_id"], run_id)
        self.assertEqual(dp["seat"], "supervisor")
        self.assertEqual(dp["outcomes"][0]["final_state"], "in-review")
        self.assertEqual(dp["actions_taken"], [])
        self.assertGreaterEqual(len(dp["answers_sent"]), 2)
        self.assertTrue(all(not a.get("fallback") for a in dp["answers_sent"]))

        # Unknown --run refuses cleanly
        rc_unk, _, _ = _run_cli(
            "--repo", str(self.toy.root), "--json",
            "drive", "--run", "run-does-not-exist",
        )
        self.assertEqual(rc_unk, EXIT_REFUSED)

        # --spend 0 is a usage error
        rc_sp, _, _ = _run_cli(
            "--repo", str(self.toy.root), "--json",
            "drive", "--spend", "0",
        )
        self.assertEqual(rc_sp, EXIT_USAGE)


    def test_drive_cleans_up_run_lock_on_driver_error(self) -> None:
        self._add_card_1()
        sp = self._worker_script()
        bad_bin = self.tmp / "bin-fail"
        bad_bin.mkdir()
        fail_script = bad_bin / "claude"
        fail_script.write_text(
            "#!/usr/bin/env python3\nimport json\n"
            "print(json.dumps({'type': 'result', 'is_error': True, 'result': 'weekly quota hit'}))\n",
            encoding="utf-8",
        )
        fail_script.chmod(0o755)
        # A failed turn falls back for that turn only: the drive goes on, the
        # failures are reported, and nothing is answered in the supervisor's name.
        res = drive_run(
            self.toy.root,
            load_config(self.toy.root),
            StateDir(self.toy.state_dir),
            agent="claude",
            seat="both",
            cards=1,
            driver_bin=str(fail_script),
            script=sp,
        )
        self.assertTrue(res["driver_errors"])
        self.assertTrue(all(e["code"] == "driver-cli-failed" for e in res["driver_errors"]))
        self.assertIn("weekly quota hit", res["driver_errors"][0]["message"])
        self.assertEqual(res["answers_sent"], [])
        self.assertFalse((self.toy.state_dir / "run.lock").exists())
        cfg = load_config(self.toy.root)
        self.assertFalse(Path(cfg.kill_switch).exists())

    def test_python_pack_patterns_clean_on_stage2_files(self) -> None:
        pack = PythonPack()
        flagged = load_config(ROOT).flagged_patterns
        for rel in (
            "openfarm_core/driver.py",
            "openfarm_core/bootstrap.py",
            "openfarm_core/human_cli.py",
            "openfarm_core/tui.py",
            "tests/test_driver_mode.py",
        ):
            lines = (ROOT / rel).read_text(encoding="utf-8").splitlines()
            added = [
                {"path": rel, "line": idx, "text": line}
                for idx, line in enumerate(lines, start=1)
            ]
            prod = set() if rel.startswith("tests/") else {rel}
            found = pack.patterns(added, flagged, prod)
            banned = (
                [b for b in found["banned"] if b["pattern"] != "process-call"]
                if rel == "openfarm_core/driver.py"
                else found["banned"]
            )
            self.assertEqual(banned, [], f"banned in {rel}: {banned}")
            if rel in ("openfarm_core/driver.py", "openfarm_core/bootstrap.py", "openfarm_core/human_cli.py", "tests/test_driver_mode.py"):
                self.assertEqual(found["flagged"], [], f"flagged in {rel}: {found['flagged']}")


if __name__ == "__main__":
    unittest.main()
