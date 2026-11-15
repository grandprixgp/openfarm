"""Tests for ACP v1 client (openfarm_core.adapters.acp) and Micromanager."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adapters.acp import (
    _ACTIVE_ISOLATED_HOMES,
    _DISALLOWED_NONE,
    _DISALLOWED_READ_ONLY,
    AcpResult,
    ClaudeAcpAdapter,
    _cleanup_all_isolated_homes,
    _sanitised_env,
    strip_control_sequences,
)
from openfarm_core.cards import Card
from openfarm_core.config import load_config
from openfarm_core.edits import FORMAT_SEARCH_REPLACE, FORMAT_WHOLE_FILE, parse_edits
from openfarm_core.errors import AdapterError, Infra, Invalid, Refused
from openfarm_core.micromanager import (
    ClaudeAcpMicromanager,
    CmdMicromanager,
    Micromanager,
    MicromanagerBudget,
    NullMicromanager,
    ScriptedMicromanager,
    open_micromanager,
    parse_json_answer,
)
from openfarm_core.roles import Restatement

STAND_IN = Path(__file__).resolve().parent / "acp_stand_in.py"


def _stand_in_cmd(*extra: str) -> list[str]:
    return [sys.executable, str(STAND_IN), *extra]


def _sample_card() -> Card:
    return Card(
        id="CARD-42",
        title="Add guard to parser",
        state="ready",
        done_text="parse() returns Err(Empty) on empty input",
        touches=["src/lib.rs", "tests/parser_test.rs"],
        fields={
            "category": "pinned-bugfix",
            "tests": [
                {"name": "test_empty_returns_err", "file": "tests/parser_test.rs"},
                {"name": "test_valid_input", "file": "tests/parser_test.rs"},
            ],
            "test_edits": ["tests/parser_test.rs"],
        },
    )


class StripControlSequencesTests(unittest.TestCase):
    def test_strips_csi_c1_and_closed_osc(self) -> None:
        raw = "\x1b[31mred\x1b[0m \x9b1mbold\x9b0m \x1b]8;;https://example.com\x07link\x1b]0;t\x1b\\"
        self.assertEqual(strip_control_sequences(raw), "red bold link")

    def test_unterminated_osc_does_not_truncate_subsequent_json(self) -> None:
        raw = '\x1b]8;;http://x{"verdict":"ready"}'
        cleaned = strip_control_sequences(raw)
        self.assertIn('{"verdict":"ready"}', cleaned)
        self.assertEqual(parse_json_answer(cleaned), {"verdict": "ready"})

    def test_lone_esc_prefix_preserves_following_text(self) -> None:
        self.assertEqual(strip_control_sequences("\x1bhello\x1b]world"), "helloworld")


class SanitisedEnvAndHomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-test-env-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_strips_forbidden_env_and_copies_credentials_with_0600(self) -> None:
        fake_home = self.tmp / "orig-home"
        (fake_home / ".claude").mkdir(parents=True)
        creds = fake_home / ".claude" / ".credentials.json"
        creds.write_text('{"token":"secret"}\n', encoding="utf-8")

        base_env = {
            "PATH": os.environ.get("PATH", "/usr/bin"),
            "HOME": str(fake_home),
            "CLAUDECODE": "1",
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_CODE_EXTRA": "xyz",
            "ANTHROPIC_AUTH_TOKEN": "sk-secret",
            "AI_AGENT": "1",
            "KEEP_ME": "yes",
        }
        env, iso = _sanitised_env(base_env=base_env, isolate_home=True)
        self.assertIsNotNone(iso)
        assert iso is not None
        try:
            self.assertNotIn("CLAUDECODE", env)
            self.assertNotIn("CLAUDE_CODE_ENTRYPOINT", env)
            self.assertNotIn("CLAUDE_CODE_EXTRA", env)
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
            self.assertNotIn("AI_AGENT", env)
            self.assertEqual(env["KEEP_ME"], "yes")
            self.assertEqual(env["HOME"], str(iso))

            self.assertEqual(stat.S_IMODE(iso.stat().st_mode), 0o700)
            settings = json.loads((iso / ".claude" / "settings.json").read_text())
            self.assertEqual(settings["hooks"], {})
            self.assertTrue(settings["disableAllHooks"])
            deny = settings["permissions"]["deny"]
            self.assertIn("Read(.openfarm/**)", deny)
            self.assertIn("Write(.openfarm/**)", deny)
            self.assertIn("Read(.claude/**)", deny)
            self.assertIn("Edit(.claude/**)", deny)
            copied = iso / ".claude" / ".credentials.json"
            self.assertTrue(copied.exists())
            self.assertEqual(stat.S_IMODE(copied.stat().st_mode), 0o600)
        finally:
            shutil.rmtree(iso, ignore_errors=True)

    def test_auth_file_creates_0700_api_key_helper(self) -> None:
        auth = self.tmp / "api.key"
        auth.write_text("sk-my-test-key\n", encoding="utf-8")
        env, iso = _sanitised_env(base_env={"HOME": str(self.tmp)}, isolate_home=True, auth_file=auth)
        self.assertIsNotNone(iso)
        assert iso is not None
        try:
            settings = json.loads((iso / ".claude" / "settings.json").read_text())
            helper = Path(settings["apiKeyHelper"])
            self.assertTrue(helper.exists())
            self.assertEqual(stat.S_IMODE(helper.stat().st_mode), 0o700)
            import subprocess

            out = subprocess.run([str(helper)], capture_output=True, text=True, check=True).stdout
            self.assertEqual(out, "sk-my-test-key")
        finally:
            shutil.rmtree(iso, ignore_errors=True)

    def test_isolated_home_cleaned_up_on_close_and_context_exit(self) -> None:
        orig_claudecode = os.environ.get("CLAUDECODE")
        orig_cc_test = os.environ.get("CLAUDE_CODE_TEST_VAR")
        os.environ["CLAUDECODE"] = "1"
        os.environ["CLAUDE_CODE_TEST_VAR"] = "leaked"
        try:
            iso_path: Path | None = None
            with ClaudeAcpAdapter(_stand_in_cmd("--mode", "normal"), cwd=self.tmp) as acp:
                init = acp.initialize()
                self.assertEqual(init["protocolVersion"], 1)
                iso_path = acp._isolated_home
                self.assertIsNotNone(iso_path)
                assert iso_path is not None
                self.assertTrue(iso_path.exists())
                # Verify child saw isolated HOME, not parent HOME.
                self.assertEqual(init["agentCapabilities"]["home"], str(iso_path))
            assert iso_path is not None
            self.assertFalse(iso_path.exists())
        finally:
            if orig_claudecode is None:
                os.environ.pop("CLAUDECODE", None)
            else:
                os.environ["CLAUDECODE"] = orig_claudecode
            if orig_cc_test is None:
                os.environ.pop("CLAUDE_CODE_TEST_VAR", None)
            else:
                os.environ["CLAUDE_CODE_TEST_VAR"] = orig_cc_test

    def test_isolated_home_atexit_registry_and_custom_deny_rules(self) -> None:
        env, iso = _sanitised_env(
            base_env={"HOME": str(self.tmp)},
            isolate_home=True,
            never_touch=["secrets/**"],
            forbidden_files=["private/**"],
        )
        self.assertIsNotNone(iso)
        assert iso is not None
        self.assertIn(iso, _ACTIVE_ISOLATED_HOMES)
        settings = json.loads((iso / ".claude" / "settings.json").read_text())
        self.assertTrue(settings["disableAllHooks"])
        deny = settings["permissions"]["deny"]
        self.assertIn("Read(secrets/**)", deny)
        self.assertIn("Edit(private/**)", deny)
        _cleanup_all_isolated_homes()
        self.assertFalse(iso.exists())
        self.assertNotIn(iso, _ACTIVE_ISOLATED_HOMES)
        self.assertEqual(env["HOME"], str(iso))


class AcpProtocolAndSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-test-acp-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "src").mkdir()
        (self.tmp / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
        (self.tmp / "secrets").mkdir()
        (self.tmp / "secrets" / "key.pem").write_text("SECRET\n", encoding="utf-8")
        (self.tmp / ".openfarm").mkdir()
        (self.tmp / ".openfarm" / "config.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

    def test_normal_session_chunks_usage_and_cost_separation(self) -> None:
        with ClaudeAcpAdapter(
            _stand_in_cmd("--mode", "normal", "--tokens-in", "300", "--tokens-out", "75", "--cost", "0.042"),
            cwd=self.tmp,
        ) as acp:
            hello = acp.hello()
            self.assertEqual(hello["versions"], [1])
            self.assertIn("session/prompt", hello["ops"])
            acp.initialize()
            sid = acp.new_session(
                cwd=self.tmp,
                system_prompt="Stay focused.",
                model="claude-test",
                effort="high",
                tool_policy="none",
            )
            res = acp.prompt(sid, "hello")
            self.assertIsInstance(res, AcpResult)
            self.assertEqual(res.stop_reason, "end_turn")
            self.assertEqual(res.tokens_in, 300)
            self.assertEqual(res.tokens_out, 75)
            self.assertAlmostEqual(res.vendor_cost_usd, 0.042, places=6)
            data = json.loads(res.text)
            self.assertEqual(data["echo"], "hello")
            opts = data["meta"]["claudeCode"]["options"]
            self.assertEqual(opts["model"], "claude-test")
            self.assertEqual(opts["effort"], "high")
            self.assertIn("Bash", opts["disallowedTools"])
            self.assertIn("WebFetch", opts["disallowedTools"])
            self.assertEqual(data["meta"]["systemPrompt"]["append"], "Stay focused.")

    def test_hostile_permission_policy_none_rejects_spoofed_allow_always(self) -> None:
        with ClaudeAcpAdapter(
            _stand_in_cmd("--mode", "hostile-perm", "--spoof-id", "--perm-kind", "read", "--perm-path", "src/main.rs"),
            cwd=self.tmp,
        ) as acp:
            sid = acp.new_session(cwd=self.tmp, tool_policy="none")
            res = acp.prompt(sid, "go")
            payload = json.loads(res.text)
            outcome = payload["perm_response"]["result"]["outcome"]
            # Must pick kind=="reject_once" (optionId="opt-reject-once"), NOT spoofed allow_always!
            self.assertEqual(outcome, {"outcome": "selected", "optionId": "opt-reject-once"})
            self.assertEqual(len(res.permission_events), 1)
            self.assertEqual(res.permission_events[0]["decision"], "reject_once")

    def test_hostile_permission_policy_none_cancels_without_reject_once(self) -> None:
        with ClaudeAcpAdapter(
            _stand_in_cmd("--mode", "hostile-perm", "--omit-reject-once", "--perm-kind", "execute"),
            cwd=self.tmp,
        ) as acp:
            sid = acp.new_session(cwd=self.tmp, tool_policy="none")
            res = acp.prompt(sid, "go")
            outcome = json.loads(res.text)["perm_response"]["result"]["outcome"]
            self.assertEqual(outcome, {"outcome": "cancelled"})

    def test_read_only_permission_containment_and_symlinks(self) -> None:
        outside = self.tmp.parent / f"outside-{os.getpid()}.txt"
        outside.write_text("outside\n", encoding="utf-8")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))

        link_out = self.tmp / "link_out"
        link_out.symlink_to(outside)
        link_secret = self.tmp / "link_secret"
        link_secret.symlink_to(self.tmp / "secrets" / "key.pem")

        cases = [
            ("read", "src/main.rs", "opt-allow-once", "allow_once"),
            ("search", "src", "opt-allow-once", "allow_once"),
            ("execute", "src/main.rs", "opt-reject-once", "reject_once"),
            ("edit", "src/main.rs", "opt-reject-once", "reject_once"),
            ("read", "../outside.txt", "opt-reject-once", "reject_once"),
            ("read", "/etc/passwd", "opt-reject-once", "reject_once"),
            ("read", "secrets/key.pem", "opt-reject-once", "reject_once"),
            ("read", ".openfarm/config.toml", "opt-reject-once", "reject_once"),
            ("read", "link_out", "opt-reject-once", "reject_once"),
            ("read", "link_secret", "opt-reject-once", "reject_once"),
        ]
        for kind, path, expected_opt, expected_decision in cases:
            with self.subTest(kind=kind, path=path):
                with ClaudeAcpAdapter(
                    _stand_in_cmd("--mode", "hostile-perm", "--perm-kind", kind, "--perm-path", path),
                    cwd=self.tmp,
                    never_touch=["secrets/**"],
                    allowed_root=self.tmp,
                ) as acp:
                    sid = acp.new_session(cwd=self.tmp, tool_policy="read-only")
                    res = acp.prompt(sid, "go")
                    outcome = json.loads(res.text)["perm_response"]["result"]["outcome"]
                    self.assertEqual(outcome, {"outcome": "selected", "optionId": expected_opt})
                    self.assertEqual(res.permission_events[0]["decision"], expected_decision)

    def test_hostile_fs_and_unknown_methods_return_minus_32601(self) -> None:
        with ClaudeAcpAdapter(_stand_in_cmd("--mode", "hostile-fs"), cwd=self.tmp) as acp:
            sid = acp.new_session(cwd=self.tmp, tool_policy="none")
            res = acp.prompt(sid, "go")
            fs_resps = json.loads(res.text)["fs_responses"]
            for mname in ("fs/write_text_file", "fs/read_text_file", "terminal/create", "mystery/op"):
                self.assertEqual(fs_resps[mname]["error"]["code"], -32601)
            self.assertEqual(len(res.rejected_methods), 4)

    def test_duplicate_incoming_request_id_returns_minus_32600(self) -> None:
        with ClaudeAcpAdapter(_stand_in_cmd("--mode", "dup-id"), cwd=self.tmp) as acp:
            sid = acp.new_session(cwd=self.tmp)
            res = acp.prompt(sid, "go")
            data = json.loads(res.text)
            self.assertEqual(data["first"]["result"], {"action": "decline"})
            self.assertEqual(data["second"]["error"]["code"], -32600)

    def test_elicitation_declined_immediately(self) -> None:
        with ClaudeAcpAdapter(_stand_in_cmd("--mode", "elicitation"), cwd=self.tmp) as acp:
            sid = acp.new_session(cwd=self.tmp)
            res = acp.prompt(sid, "go")
            data = json.loads(res.text)
            self.assertEqual(data["elicitation"]["result"], {"action": "decline"})

    def test_session_load_ignores_replay_and_rejects_requests_with_32600(self) -> None:
        with ClaudeAcpAdapter(_stand_in_cmd("--mode", "replay"), cwd=self.tmp) as acp:
            acp.initialize()
            acp.resume_session("sess-existing", cwd=self.tmp, tool_policy="read-only")
            self.assertFalse(acp._replaying_history)
            res = acp.prompt("sess-existing", "go")
            self.assertNotIn("REPLAYED_HISTORY_NOISE", res.text)
            data = json.loads(res.text)
            self.assertEqual(data["replay_rejection_code"], -32600)
            self.assertTrue(data["fresh"])

    def test_read_only_new_session_disallowed_tools(self) -> None:
        with ClaudeAcpAdapter(_stand_in_cmd("--mode", "normal"), cwd=self.tmp) as acp:
            sid = acp.new_session(cwd=self.tmp, tool_policy="read-only")
            res = acp.prompt(sid, "hello")
            opts = json.loads(res.text)["meta"]["claudeCode"]["options"]
            self.assertEqual(opts["disallowedTools"], list(_DISALLOWED_READ_ONLY))
            for tool in ("WebFetch", "WebSearch", "Task", "TodoWrite", "NotebookRead"):
                self.assertIn(tool, opts["disallowedTools"])

    def test_none_new_session_disallows_read_glob_grep_and_task(self) -> None:
        with ClaudeAcpAdapter(_stand_in_cmd("--mode", "normal"), cwd=self.tmp) as acp:
            sid = acp.new_session(cwd=self.tmp, tool_policy="none")
            res = acp.prompt(sid, "hello")
            opts = json.loads(res.text)["meta"]["claudeCode"]["options"]
            self.assertEqual(opts["disallowedTools"], list(_DISALLOWED_NONE))
            for tool in ("Read", "Glob", "Grep", "LS", "Task", "TodoWrite", "NotebookRead"):
                self.assertIn(tool, opts["disallowedTools"])

    def test_unterminated_osc_in_agent_chunk_preserves_json(self) -> None:
        with ClaudeAcpAdapter(_stand_in_cmd("--mode", "unterminated-osc"), cwd=self.tmp) as acp:
            sid = acp.new_session(cwd=self.tmp)
            res = acp.prompt(sid, "go")
            parsed = parse_json_answer(res.text)
            self.assertEqual(parsed["verdict"], "ready")
            self.assertEqual(parsed["clarified_notes"], "osc survived")

    def test_timeout_sends_cancel_and_terminates_process(self) -> None:
        with ClaudeAcpAdapter(
            _stand_in_cmd("--mode", "hang", "--hang-seconds", "30"),
            cwd=self.tmp,
        ) as acp:
            sid = acp.new_session(cwd=self.tmp)
            with self.assertRaises(Infra) as caught:
                acp.prompt(sid, "hang", timeout_s=0.25)
            self.assertEqual(caught.exception.code, "acp-timeout")
            self.assertIsNotNone(acp._proc)
            assert acp._proc is not None
            self.assertIsNotNone(acp._proc.poll())

    def test_cancel_check_sends_cancel_and_raises_refused(self) -> None:
        checks = {"n": 0}

        def kill_switch() -> bool:
            checks["n"] += 1
            return checks["n"] >= 3

        with ClaudeAcpAdapter(
            _stand_in_cmd("--mode", "hang", "--hang-seconds", "30", "--ack-cancel"),
            cwd=self.tmp,
        ) as acp:
            sid = acp.new_session(cwd=self.tmp)
            assert acp._proc is not None
            pid = acp._proc.pid
            with self.assertRaises(Refused) as caught:
                acp.prompt(sid, "hang", timeout_s=10.0, cancel_check=kill_switch)
            self.assertEqual(caught.exception.code, "acp-cancelled")
            self.assertIsNotNone(acp._proc.poll())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_oversized_line_raises_acp_oversized(self) -> None:
        with ClaudeAcpAdapter(_stand_in_cmd("--mode", "oversized"), cwd=self.tmp) as acp:
            sid = acp.new_session(cwd=self.tmp)
            with self.assertRaises(Infra) as caught:
                acp.prompt(sid, "big", timeout_s=5.0)
            self.assertEqual(caught.exception.code, "acp-oversized")

    def test_concurrent_prompt_on_same_adapter_refused(self) -> None:
        with ClaudeAcpAdapter(
            _stand_in_cmd("--mode", "hang", "--hang-seconds", "2", "--ack-cancel"),
            cwd=self.tmp,
        ) as acp:
            sid = acp.new_session(cwd=self.tmp)
            started = threading.Event()
            err_holder: list[Exception] = []

            def run_first() -> None:
                started.set()
                try:
                    acp.prompt(sid, "first", timeout_s=1.0)
                except Exception as exc:  # noqa: BLE001
                    err_holder.append(exc)

            t = threading.Thread(target=run_first, daemon=True)
            t.start()
            started.wait(timeout=1.0)
            # Wait briefly until _turn_lock is acquired.
            deadline = time.monotonic() + 1.0
            while not acp._turn_lock.locked() and time.monotonic() < deadline:
                time.sleep(0.01)
            with self.assertRaises(Refused) as caught:
                acp.prompt(sid, "second", timeout_s=1.0)
            self.assertEqual(caught.exception.code, "acp-concurrent-prompt")
            t.join(timeout=3.0)
            self.assertTrue(len(err_holder) == 1)

    def test_stdin_blocking_timeout_with_2mb_payload_terminates_and_reaps(self) -> None:
        with ClaudeAcpAdapter(
            _stand_in_cmd("--mode", "block-stdin-prompt", "--hang-seconds", "30"),
            cwd=self.tmp,
        ) as acp:
            sid = acp.new_session(cwd=self.tmp)
            assert acp._proc is not None
            pid = acp._proc.pid
            started = time.monotonic()
            with self.assertRaises(Infra) as caught:
                acp.prompt(sid, "X" * (2 * 1024 * 1024 + 64), timeout_s=0.25)
            elapsed = time.monotonic() - started
            self.assertEqual(caught.exception.code, "acp-stdin-timeout")
            self.assertLess(elapsed, 4.0)
            self.assertIsNotNone(acp._proc.poll())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_stdin_write_cancel_check_terminates_and_reaps(self) -> None:
        checks = {"n": 0}

        def cancel_on_second() -> bool:
            checks["n"] += 1
            return checks["n"] >= 2

        with ClaudeAcpAdapter(
            _stand_in_cmd("--mode", "block-stdin-prompt", "--hang-seconds", "30"),
            cwd=self.tmp,
        ) as acp:
            sid = acp.new_session(cwd=self.tmp)
            assert acp._proc is not None
            pid = acp._proc.pid
            with self.assertRaises(Refused) as caught:
                acp.prompt(
                    sid,
                    "Y" * (2 * 1024 * 1024),
                    timeout_s=5.0,
                    cancel_check=cancel_on_second,
                )
            self.assertEqual(caught.exception.code, "acp-cancelled")
            self.assertIsNotNone(acp._proc.poll())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_cumulative_usage_cost_and_prompt_response_tokens_across_turns(self) -> None:
        script_path = self.tmp / "cum_usage.json"
        script_path.write_text(
            json.dumps(
                {
                    "turns": [
                        {
                            "text": json.dumps({"turn": 1}),
                            "usage_updates": [
                                {"used": 1000, "size": 200000, "cost": {"amount": 0.1, "currency": "USD"}},
                                {"used": 1200, "size": 200000, "cost": {"amount": 0.2, "currency": "USD"}},
                                {"used": 1500, "size": 200000, "cost": {"amount": 0.3, "currency": "USD"}},
                            ],
                            "usage": {"tokens_in": 110, "tokens_out": 40},
                        },
                        {
                            "text": json.dumps({"turn": 2}),
                            "usage_updates": [
                                {"used": 2000, "size": 200000, "cost": {"amount": 0.45, "currency": "USD"}},
                            ],
                            "usage": {"tokens_in": 200, "tokens_out": 50},
                        },
                        {
                            "text": json.dumps({"turn": 3}),
                            "usage_updates": [
                                {"inputTokens": 77, "outputTokens": 22},
                            ],
                            "omit_result_usage": True,
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        with ClaudeAcpAdapter(
            _stand_in_cmd("--mode", "script", "--script", str(script_path)),
            cwd=self.tmp,
        ) as acp:
            sid = acp.new_session(cwd=self.tmp)
            r1 = acp.prompt(sid, "t1")
            self.assertEqual((r1.tokens_in, r1.tokens_out), (110, 40))
            self.assertAlmostEqual(r1.vendor_cost_usd, 0.3, places=6)

            r2 = acp.prompt(sid, "t2")
            self.assertEqual((r2.tokens_in, r2.tokens_out), (200, 50))
            self.assertAlmostEqual(r2.vendor_cost_usd, 0.15, places=6)

            r3 = acp.prompt(sid, "t3")
            self.assertEqual((r3.tokens_in, r3.tokens_out), (77, 22))
            self.assertAlmostEqual(r3.vendor_cost_usd, 0.0, places=6)

    def test_malformed_usage_raises_invalid_adapter_error_not_value_error(self) -> None:
        bad_cases = [
            {"usage_updates": [{"cost": {"amount": "not-a-float"}}]},
            {"usage_updates": [{"inputTokens": "not-an-int"}]},
            {"usage_updates": [{"inputTokens": True}]},
            {"usage_updates": [{"cost": {"amount": -1.5}}]},
            {"usage_updates": [], "result_usage": "bad-shape"},
            {"usage_updates": [], "result_usage": {"inputTokens": "NaN-bad"}},
        ]
        for idx, entry in enumerate(bad_cases):
            with self.subTest(case=idx):
                script_path = self.tmp / f"bad_usage_{idx}.json"
                script_path.write_text(json.dumps({"turns": [entry]}), encoding="utf-8")
                with ClaudeAcpAdapter(
                    _stand_in_cmd("--mode", "script", "--script", str(script_path)),
                    cwd=self.tmp,
                ) as acp:
                    sid = acp.new_session(cwd=self.tmp)
                    with self.assertRaises(Invalid) as caught:
                        acp.prompt(sid, "go")
                    self.assertIsInstance(caught.exception, AdapterError)
                    self.assertEqual(caught.exception.code, "acp-bad-usage")
                    assert acp._proc is not None
                    self.assertIsNotNone(acp._proc.poll())

    def test_nul_tilde_unscoped_search_and_claude_dir_denied_in_readonly(self) -> None:
        (self.tmp / ".claude").mkdir()
        (self.tmp / ".claude" / "settings.json").write_text("{}", encoding="utf-8")

        cases = [
            ("read", "src/\x00evil.rs", "nul-path"),
            ("read", "~/secret.txt", "tilde-path"),
            ("read", "~root/.ssh/id_rsa", "tilde-path"),
            ("read", ".claude/settings.json", "forbidden-file"),
        ]
        for idx, (kind, path, expected_reason) in enumerate(cases):
            with self.subTest(kind=kind, path=path):
                script_path = self.tmp / f"perm_case_{idx}.json"
                script_path.write_text(
                    json.dumps(
                        {
                            "turns": [
                                {
                                    "requests": [
                                        {
                                            "jsonrpc": "2.0",
                                            "id": f"perm-case-{idx}",
                                            "method": "session/request_permission",
                                            "params": {
                                                "sessionId": "sess-standin-1",
                                                "toolCall": {
                                                    "kind": kind,
                                                    "locations": [{"path": path}],
                                                    "rawInput": {"file_path": path},
                                                },
                                                "options": [
                                                    {"optionId": "opt-allow-once", "kind": "allow_once"},
                                                    {"optionId": "opt-reject-once", "kind": "reject_once"},
                                                ],
                                            },
                                        }
                                    ],
                                    "text": "done",
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                with ClaudeAcpAdapter(
                    _stand_in_cmd("--mode", "script", "--script", str(script_path)),
                    cwd=self.tmp,
                    allowed_root=self.tmp,
                ) as acp:
                    self.assertEqual(acp._is_path_allowed_readonly(path), (False, expected_reason))
                    sid = acp.new_session(cwd=self.tmp, tool_policy="read-only")
                    res = acp.prompt(sid, "go")
                    self.assertEqual(res.permission_events[0]["decision"], "reject_once")
                    self.assertEqual(res.permission_events[0]["reason"], expected_reason)

        # Unscoped search (empty paths) must also be denied in read-only mode.
        script_path = self.tmp / "unscoped_search.json"
        script_path.write_text(
            json.dumps(
                {
                    "turns": [
                        {
                            "requests": [
                                {
                                    "jsonrpc": "2.0",
                                    "id": "perm-search-empty",
                                    "method": "session/request_permission",
                                    "params": {
                                        "sessionId": "sess-standin-1",
                                        "toolCall": {"kind": "search", "rawInput": {"query": "secret"}},
                                        "options": [
                                            {"optionId": "opt-allow-once", "kind": "allow_once"},
                                            {"optionId": "opt-reject-once", "kind": "reject_once"},
                                        ],
                                    },
                                }
                            ],
                            "text": "done",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with ClaudeAcpAdapter(
            _stand_in_cmd("--mode", "script", "--script", str(script_path)),
            cwd=self.tmp,
            allowed_root=self.tmp,
        ) as acp:
            sid = acp.new_session(cwd=self.tmp, tool_policy="read-only")
            res = acp.prompt(sid, "go")
            self.assertEqual(res.permission_events[0]["decision"], "reject_once")
            self.assertEqual(res.permission_events[0]["reason"], "search-missing-path")

    def test_bad_json_and_deep_nesting_recursion_error_terminate_agent_group(self) -> None:
        deep_json = ("[" * 2500) + "1" + ("]" * 2500)
        for label, raw_line in (("bad-json", "{not valid json"), ("deep-json", deep_json)):
            with self.subTest(label=label):
                script_path = self.tmp / f"{label}.json"
                script_path.write_text(
                    json.dumps({"turns": [{"raw_lines": [raw_line], "text": "{}"}]}),
                    encoding="utf-8",
                )
                with ClaudeAcpAdapter(
                    _stand_in_cmd("--mode", "script", "--script", str(script_path)),
                    cwd=self.tmp,
                ) as acp:
                    sid = acp.new_session(cwd=self.tmp)
                    assert acp._proc is not None
                    pid = acp._proc.pid
                    with self.assertRaises(Infra) as caught:
                        acp.prompt(sid, "go", timeout_s=2.0)
                    self.assertEqual(caught.exception.code, "acp-bad-json")
                    self.assertIsNotNone(acp._proc.poll())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)

    def test_project_config_includes_claude_dir_in_forbidden_files(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        cfg = load_config(repo_root)
        self.assertTrue(cfg.is_forbidden(".claude/settings.json"))
        self.assertTrue(cfg.is_forbidden(".claude/hooks/pre.sh"))


class MicromanagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-test-mm-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.card = _sample_card()

    def test_parse_json_answer_strips_fences_and_rejects_garbage(self) -> None:
        fenced = 'Here is the plan:\n```json\n{"verdict": "ready"}\n```\nDone.'
        self.assertEqual(parse_json_answer(fenced), {"verdict": "ready"})
        with self.assertRaises(Invalid) as caught:
            parse_json_answer("not json at all")
        self.assertEqual(caught.exception.code, "bad-micromanager-json")

    def test_null_micromanager_returns_safe_defaults(self) -> None:
        mm = Micromanager(NullMicromanager())
        self.assertFalse(mm.enabled)
        self.assertEqual(mm.shape_and_plan(self.card, {}, {}).verdict, "ready")
        self.assertEqual(mm.scaffold_contract(self.card, {}, {}), [])
        self.assertFalse(mm.coach(self.card, "impl", 1, {}, "", "").abort)
        self.assertEqual(mm.adjudicate_checker(self.card, "", None, []).verdict, "uphold")
        self.assertEqual(mm.triage_and_review("pre_review", {}).verdict, "approve")

    def test_shape_and_plan_filters_extra_context_and_validates_steps(self) -> None:
        script = {
            "shape_and_plan": {
                "verdict": "split",
                "clarified_notes": "handle empty first",
                "extra_context_files": [
                    "src/lib.rs",
                    "../secret.txt",
                    "/etc/passwd",
                    "secrets/key.pem",
                    ".openfarm/config.toml",
                ],
                "steps": [
                    {
                        "n": 1,
                        "does": "guard empty",
                        "tests": ["test_empty_returns_err"],
                        "touches": ["src/lib.rs"],
                    }
                ],
                "findings": [{"title": "Edge", "body": "empty slice"}],
            }
        }
        mm = Micromanager(
            ScriptedMicromanager(script),
            never_touch=["secrets/**"],
            forbidden_files=[".openfarm/**"],
        )
        decision = mm.shape_and_plan(self.card, {}, {})
        self.assertEqual(decision.verdict, "split")
        self.assertEqual(decision.extra_context_files, ["src/lib.rs"])
        self.assertEqual(len(decision.steps), 1)
        self.assertEqual(decision.findings[0]["title"], "Edge")

        # Reject invalid test name not in card.test_names
        bad_test_mm = Micromanager(
            ScriptedMicromanager(
                {
                    "shape_and_plan": {
                        "verdict": "split",
                        "steps": [{"n": 1, "does": "x", "tests": ["invented_test"], "touches": ["src/lib.rs"]}],
                    }
                }
            )
        )
        with self.assertRaises(Refused) as caught:
            bad_test_mm.shape_and_plan(self.card, {}, {})
        self.assertEqual(caught.exception.code, "step-invalid-test")

        # Reject step touch outside card.touches
        bad_touch_mm = Micromanager(
            ScriptedMicromanager(
                {
                    "shape_and_plan": {
                        "verdict": "split",
                        "steps": [{"n": 1, "does": "x", "tests": ["test_empty_returns_err"], "touches": ["src/other.rs"]}],
                    }
                }
            )
        )
        with self.assertRaises(Refused) as caught2:
            bad_touch_mm.shape_and_plan(self.card, {}, {})
        self.assertEqual(caught2.exception.code, "outside-touches")

    def test_scaffold_contract_validates_paths_and_todo_macros(self) -> None:
        # Valid edit_blocks with todo!( in production file
        mm = Micromanager(
            ScriptedMicromanager(
                {
                    "scaffold_contract": {
                        "edit_blocks": (
                            "=== whole_file: src/lib.rs\n"
                            "pub fn parse(s: &str) -> Result<(), ()> {\n"
                            "    todo!()\n"
                            "}\n"
                        )
                    }
                }
            )
        )
        edits = mm.scaffold_contract(self.card, {}, {})
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].format, FORMAT_WHOLE_FILE)
        self.assertEqual(edits[0].path, "src/lib.rs")

        # Valid structured edits with unimplemented!(
        mm_struct = Micromanager(
            ScriptedMicromanager(
                {
                    "scaffold_contract": {
                        "edits": [
                            {
                                "format": FORMAT_SEARCH_REPLACE,
                                "path": "src/lib.rs",
                                "search": "old",
                                "replace": "unimplemented!()",
                            }
                        ]
                    }
                }
            )
        )
        self.assertEqual(len(mm_struct.scaffold_contract(self.card, {}, {})), 1)

        # Missing todo!(/unimplemented!( in production code -> Refused
        mm_no_todo = Micromanager(
            ScriptedMicromanager(
                {
                    "scaffold_contract": {
                        "edits": [{"format": "whole_file", "path": "src/lib.rs", "content": "pub fn parse() {}"}]
                    }
                }
            )
        )
        with self.assertRaises(Refused) as caught:
            mm_no_todo.scaffold_contract(self.card, {}, {})
        self.assertEqual(caught.exception.code, "scaffold-missing-todo")

        # Escaping / outside touches / never_touch -> Refused
        for bad_path in ("/etc/passwd", "../escape.rs", "src/other.rs", "secrets/k.rs", ".openfarm/config.toml"):
            bad_mm = Micromanager(
                ScriptedMicromanager(
                    {
                        "scaffold_contract": {
                            "edits": [{"format": "whole_file", "path": bad_path, "content": "todo!()"}]
                        }
                    }
                ),
                never_touch=["secrets/**"],
            )
            with self.assertRaises(Refused):
                bad_mm.scaffold_contract(self.card, {}, {})

    def test_coach_strips_edit_headers_and_rejects_bad_touches(self) -> None:
        mm = Micromanager(
            ScriptedMicromanager(
                {
                    "coach": {
                        "root_cause": "bad match",
                        "guidance": (
                            "Check empty string first.\n"
                            "=== search_replace: src/lib.rs\n"
                            "<<< SEARCH\na\n>>> REPLACE\nb\n"
                            "=== whole_file: src/other.rs\n"
                            "=== append_stub: src/lib.rs\n"
                        ),
                        "abort": False,
                        "abort_reason": "",
                    }
                }
            )
        )
        advice = mm.coach(self.card, "implementer", 1, {}, "", "error")
        self.assertNotIn("=== search_replace", advice.guidance)
        self.assertNotIn("=== whole_file", advice.guidance)
        self.assertNotIn("=== append_stub", advice.guidance)
        with self.assertRaises(Invalid):
            parse_edits(advice.guidance)

        bad_coach = Micromanager(
            ScriptedMicromanager(
                {
                    "coach": {
                        "root_cause": "x",
                        "guidance": "y",
                        "touches": ["secrets/key.pem"],
                    }
                }
            ),
            never_touch=["secrets/**"],
        )
        with self.assertRaises(Refused):
            bad_coach.coach(self.card, "implementer", 1, {}, "", "error")

    def test_adjudicate_checker_tracks_false_rejects_dismissed(self) -> None:
        mm = Micromanager(
            ScriptedMicromanager(
                {
                    "adjudicate_checker": {
                        "verdict": "dismiss_false_reject",
                        "rationale": "counter-test contradicts card done_text",
                        "fix_hint": "",
                    }
                }
            )
        )
        verdict = mm.adjudicate_checker(
            self.card, "diff", Restatement("restated", False, 0.8), ["ct_bad"]
        )
        self.assertEqual(verdict.verdict, "dismiss_false_reject")
        self.assertEqual(mm.budget.false_rejects_dismissed, 1)

    def test_triage_and_review_validation(self) -> None:
        mm = Micromanager(
            ScriptedMicromanager(
                {
                    "pre_review": {
                        "verdict": "bounce",
                        "reviewer_brief": "missed empty slice",
                        "missed_clause": "empty input",
                        "findings": [{"title": "Gap", "body": "missing case"}],
                    },
                    "prepare_queue": {
                        "summary": "1 ready",
                        "recommendations": [{"card_id": "CARD-42", "action": "run"}],
                        "findings": [],
                    },
                }
            ),
            never_touch=["secrets/**"],
        )
        rep = mm.triage_and_review("pre_review", {"card": self.card.as_dict()})
        self.assertEqual(rep.verdict, "bounce")
        q = mm.triage_and_review("prepare_queue", {"cards": ["CARD-42"]})
        self.assertEqual(q.summary, "1 ready")

        bad_triage = Micromanager(
            ScriptedMicromanager(
                {
                    "triage_blocked": {
                        "summary": "bad",
                        "recommendations": [{"card_id": "C1", "files": ["secrets/key.pem"]}],
                    }
                }
            ),
            never_touch=["secrets/**"],
        )
        with self.assertRaises(Refused):
            bad_triage.triage_and_review("triage_blocked", {})

    def test_micromanager_budget_caps(self) -> None:
        budget = MicromanagerBudget(max_calls_per_card=2, tokens_per_card=500, spend_per_night=1.0)
        mm = Micromanager(
            ScriptedMicromanager(
                {
                    "coach": {
                        "root_cause": "r",
                        "guidance": "g",
                        "_usage": {"tokens_in": 100, "tokens_out": 50, "spend": 0.25},
                    }
                }
            ),
            budget=budget,
        )
        mm.coach(self.card, "impl", 1, {}, "", "")
        mm.coach(self.card, "impl", 2, {}, "", "")
        self.assertEqual(budget.calls, 2)
        self.assertEqual(budget.calls_per_card["CARD-42"], 2)
        self.assertAlmostEqual(budget.spend, 0.50)
        with self.assertRaises(Refused) as caught:
            mm.coach(self.card, "impl", 3, {}, "", "")
        self.assertEqual(caught.exception.code, "micromanager-call-cap")

        # Token cap check
        tok_budget = MicromanagerBudget(max_calls_per_card=10, tokens_per_card=100, spend_per_night=10.0)
        mm_tok = Micromanager(
            ScriptedMicromanager(
                {"coach": {"root_cause": "r", "guidance": "g", "_usage": {"tokens_in": 80, "tokens_out": 30}}}
            ),
            budget=tok_budget,
        )
        mm_tok.coach(self.card, "impl", 1, {}, "", "")
        with self.assertRaises(Refused) as caught_tok:
            mm_tok.coach(self.card, "impl", 2, {}, "", "")
        self.assertEqual(caught_tok.exception.code, "micromanager-token-cap")

        # Night spend cap check
        spend_budget = MicromanagerBudget(max_calls_per_card=10, tokens_per_card=100_000, spend_per_night=0.50)
        mm_spend = Micromanager(
            ScriptedMicromanager(
                {"coach": {"root_cause": "r", "guidance": "g", "_usage": {"spend": 0.60}}}
            ),
            budget=spend_budget,
        )
        mm_spend.coach(self.card, "impl", 1, {}, "", "")
        with self.assertRaises(Refused) as caught_spend:
            mm_spend.coach(self.card, "impl", 2, {}, "", "")
        self.assertEqual(caught_spend.exception.code, "micromanager-spend-cap")

    def test_scripted_callable_and_error_injection(self) -> None:
        mm = Micromanager(
            ScriptedMicromanager(
                {
                    "coach": lambda op, payload: {
                        "root_cause": f"{op}:{payload['phase']}",
                        "guidance": "ok",
                    },
                    "pre_review": {"fail": True, "kind": "refused", "code": "policy-no"},
                }
            )
        )
        advice = mm.coach(self.card, "impl", 1, {}, "", "")
        self.assertEqual(advice.root_cause, "coach:impl")
        with self.assertRaises(Refused) as caught:
            mm.triage_and_review("pre_review", {"card": self.card.as_dict()})
        self.assertEqual(caught.exception.code, "policy-no")

    def test_cmd_micromanager_subprocess(self) -> None:
        cmd_script = self.tmp / "mm_cmd.py"
        cmd_script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "req = json.load(sys.stdin)\n"
            "op = req['op']\n"
            "rid = req['request_id']\n"
            "if op == 'hello':\n"
            "    res = {'name': 'fake-mm', 'versions': [1], 'ops': ['hello', 'assist']}\n"
            "elif op == 'assist':\n"
            "    res = {'verdict': 'approve', 'reviewer_brief': 'cmd ok', 'missed_clause': '', 'findings': []}\n"
            "else:\n"
            "    res = {}\n"
            "print(json.dumps({'openfarm': 1, 'request_id': rid, 'ok': True, 'result': res}))\n",
            encoding="utf-8",
        )
        cmd_script.chmod(0o755)
        backend = CmdMicromanager(f"cmd:{sys.executable} {cmd_script}", self.tmp)
        self.assertEqual(backend.hello()["name"], "fake-mm")
        mm = Micromanager(backend)
        rep = mm.triage_and_review("pre_review", {"card": self.card.as_dict()})
        self.assertEqual(rep.verdict, "approve")
        self.assertEqual(rep.reviewer_brief, "cmd ok")

    def test_claude_acp_micromanager_spend_and_artifacts(self) -> None:
        card_dir = self.tmp / "card-state"
        # Mode unterminated-osc emits vendor cost = 0.0, so cost_per_1k_in/out is used.
        backend = ClaudeAcpMicromanager(
            command=_stand_in_cmd(
                "--mode", "unterminated-osc", "--tokens-in", "1000", "--tokens-out", "500"
            ),
            cwd=self.tmp,
            cost_per_1k_in=0.01,
            cost_per_1k_out=0.03,
            card_dir=card_dir,
        )
        with Micromanager(backend, card_dir=card_dir) as mm:
            decision = mm.shape_and_plan(self.card, {}, {})
            self.assertEqual(decision.verdict, "ready")
            self.assertEqual(decision.clarified_notes, "osc survived")
            self.assertEqual(mm.budget.tokens_in, 1000)
            self.assertEqual(mm.budget.tokens_out, 500)
            # 1.0 * 0.01 + 0.5 * 0.03 = 0.025
            self.assertAlmostEqual(mm.budget.spend, 0.025, places=6)

        artifact = card_dir / "micromanager" / "01-shape_and_plan.json"
        self.assertTrue(artifact.exists())
        saved = json.loads(artifact.read_text(encoding="utf-8"))
        self.assertEqual(saved["op"], "shape_and_plan")
        self.assertAlmostEqual(saved["spend"], 0.025, places=6)

    def test_open_micromanager_factory(self) -> None:
        cfg_off = SimpleNamespace(never_touch=[], forbidden_files=[".openfarm/**"])
        mm_off = open_micromanager(cfg_off, self.tmp, self.tmp)
        self.assertFalse(mm_off.enabled)

        script_path = self.tmp / "mm-script.json"
        script_path.write_text(json.dumps({"coach": {"root_cause": "x", "guidance": "y"}}))
        mm_script = open_micromanager(cfg_off, self.tmp, self.tmp, script=script_path)
        self.assertTrue(mm_script.enabled)
        self.assertEqual(mm_script.coach(self.card, "impl", 1, {}, "", "").guidance, "y")

        cfg_acp = SimpleNamespace(
            never_touch=["secrets/**"],
            forbidden_files=[".openfarm/**"],
            cost_per_1k_in=0.01,
            cost_per_1k_out=0.02,
            micromanager=SimpleNamespace(
                enabled=True,
                adapter="claude-acp",
                command=_stand_in_cmd("--mode", "normal"),
                tool_policy="none",
                max_calls_per_card=5,
            ),
        )
        with open_micromanager(cfg_acp, self.tmp, self.tmp, worktree=self.tmp) as mm_acp:
            self.assertTrue(mm_acp.enabled)
            self.assertEqual(mm_acp.budget.max_calls_per_card, 5)
            decision = mm_acp.shape_and_plan(self.card, {}, {})
            self.assertEqual(decision.verdict, "ready")

    def test_budget_records_bad_json_eof_and_shared_night_budget(self) -> None:
        # 1. Paid ACP calls returning bad JSON still record spend/tokens and trigger spend cap.
        bad_json_script = self.tmp / "bad_json_paid.json"
        bad_json_script.write_text(
            json.dumps(
                {
                    "turns": [
                        {
                            "text": "not valid json at all",
                            "usage": {"tokens_in": 150, "tokens_out": 40, "cost": 0.30},
                        },
                        {
                            "text": "still not json",
                            "usage": {"tokens_in": 120, "tokens_out": 30, "cost": 0.30},
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        budget = MicromanagerBudget(
            max_calls_per_card=5, tokens_per_card=100_000, spend_per_night=0.50
        )
        backend = ClaudeAcpMicromanager(
            command=_stand_in_cmd("--mode", "script", "--script", str(bad_json_script)),
            cwd=self.tmp,
        )
        with Micromanager(backend, budget=budget) as mm:
            with self.assertRaises(Invalid):
                mm.coach(self.card, "impl", 1, {}, "", "err")
            self.assertEqual(budget.calls, 1)
            self.assertEqual(budget.calls_per_card["CARD-42"], 1)
            self.assertEqual(budget.tokens_in, 150)
            self.assertEqual(budget.tokens_out, 40)
            self.assertAlmostEqual(budget.spend, 0.30, places=6)

            with self.assertRaises(Invalid):
                mm.coach(self.card, "impl", 2, {}, "", "err")
            self.assertEqual(budget.calls, 2)
            self.assertAlmostEqual(budget.spend, 0.60, places=6)

            with self.assertRaises(Refused) as caught_cap:
                mm.coach(self.card, "impl", 3, {}, "", "err")
            self.assertEqual(caught_cap.exception.code, "micromanager-spend-cap")

        # 2. Partial usage before EOF still records into MicromanagerBudget.
        eof_script = self.tmp / "eof_partial.json"
        eof_script.write_text(
            json.dumps(
                {
                    "turns": [
                        {
                            "chunks": ["{\"verdict\":"],
                            "usage_updates": [
                                {"inputTokens": 90, "outputTokens": 10, "cost": {"amount": 0.12}}
                            ],
                            "eof_before_response": True,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        eof_budget = MicromanagerBudget(max_calls_per_card=1, spend_per_night=5.0)
        eof_backend = ClaudeAcpMicromanager(
            command=_stand_in_cmd("--mode", "script", "--script", str(eof_script)),
            cwd=self.tmp,
        )
        with Micromanager(eof_backend, budget=eof_budget) as mm_eof:
            with self.assertRaises(Infra) as caught_eof:
                mm_eof.shape_and_plan(self.card, {}, {})
            self.assertEqual(caught_eof.exception.code, "acp-eof")
            self.assertEqual(eof_budget.calls, 1)
            self.assertEqual(eof_budget.tokens_in, 90)
            self.assertEqual(eof_budget.tokens_out, 10)
            self.assertAlmostEqual(eof_budget.spend, 0.12, places=6)
            with self.assertRaises(Refused) as caught_call_cap:
                mm_eof.shape_and_plan(self.card, {}, {})
            self.assertEqual(caught_call_cap.exception.code, "micromanager-call-cap")

        # 3. open_micromanager(..., budget=shared_budget) shares caps across cards.
        shared = MicromanagerBudget(max_calls_per_card=5, spend_per_night=0.40)
        s1 = self.tmp / "mm_s1.json"
        s1.write_text(
            json.dumps({"coach": {"root_cause": "a", "guidance": "b", "_usage": {"spend": 0.25}}}),
            encoding="utf-8",
        )
        s2 = self.tmp / "mm_s2.json"
        s2.write_text(
            json.dumps({"coach": {"root_cause": "c", "guidance": "d", "_usage": {"spend": 0.20}}}),
            encoding="utf-8",
        )
        cfg_off = SimpleNamespace(never_touch=[], forbidden_files=[".openfarm/**"])
        card_b = Card(id="CARD-99", title="Other", state="ready", touches=["src/lib.rs"])
        mm1 = open_micromanager(cfg_off, self.tmp, self.tmp, script=s1, budget=shared)
        mm2 = open_micromanager(cfg_off, self.tmp, self.tmp, script=s2, budget=shared)
        mm1.coach(self.card, "impl", 1, {}, "", "")
        mm2.coach(card_b, "impl", 1, {}, "", "")
        self.assertEqual(shared.calls, 2)
        self.assertAlmostEqual(shared.spend, 0.45, places=6)
        with self.assertRaises(Refused) as caught_shared:
            mm2.coach(card_b, "impl", 2, {}, "", "")
        self.assertEqual(caught_shared.exception.code, "micromanager-spend-cap")

    def test_scaffold_contract_rejects_smuggled_real_fns_and_test_paths(self) -> None:
        bad_bodies = [
            "pub fn real() -> i32 { 1 }\n// todo!()",
            "pub fn stub() { todo!() }\npub fn real() -> i32 { 1 }",
            'pub fn str_smuggle() { let _ = "todo!()"; }',
            'pub fn raw_smuggle() { let _ = r#"unimplemented!()"#; }',
            "pub fn block_comment() { /* todo!() */ 42 }",
            'pub fn arg_exec() { todo!("{}", evil()); }',
            "pub fn empty() {}",
        ]
        for idx, body in enumerate(bad_bodies):
            with self.subTest(idx=idx, body=body):
                mm = Micromanager(
                    ScriptedMicromanager(
                        {
                            "scaffold_contract": {
                                "edits": [
                                    {
                                        "format": FORMAT_WHOLE_FILE,
                                        "path": "src/lib.rs",
                                        "content": body,
                                    }
                                ]
                            }
                        }
                    )
                )
                with self.assertRaises(Refused) as caught:
                    mm.scaffold_contract(self.card, {}, {})
                self.assertEqual(caught.exception.code, "scaffold-missing-todo")

        # Reject test paths even when listed in card.touches.
        mm_test_path = Micromanager(
            ScriptedMicromanager(
                {
                    "scaffold_contract": {
                        "edits": [
                            {
                                "format": FORMAT_WHOLE_FILE,
                                "path": "tests/parser_test.rs",
                                "content": "fn my_test() { todo!() }",
                            }
                        ]
                    }
                }
            )
        )
        with self.assertRaises(Refused) as caught_tp:
            mm_test_path.scaffold_contract(self.card, {}, {})
        self.assertEqual(caught_tp.exception.code, "scaffold-test-path")

        # Allow multi-fn todo stubs, brace-free trait signatures, and pure declarations.
        good_bodies = [
            'pub fn a() { todo!() }\npub fn b() { unimplemented!("later") }',
            "pub trait Parser { fn parse(&self, s: &str) -> Result<(), ()>; }",
            "pub struct Parsed { pub len: usize }",
        ]
        for body in good_bodies:
            with self.subTest(good=body):
                mm_good = Micromanager(
                    ScriptedMicromanager(
                        {
                            "scaffold_contract": {
                                "edits": [
                                    {
                                        "format": FORMAT_WHOLE_FILE,
                                        "path": "src/lib.rs",
                                        "content": body,
                                    }
                                ]
                            }
                        }
                    )
                )
                edits = mm_good.scaffold_contract(self.card, {}, {})
                self.assertEqual(len(edits), 1)

    def test_strip_edit_blocks_across_all_ops_and_claude_forbidden(self) -> None:
        hdr = "=== whole_file: src/lib.rs\n<<< SEARCH\na\n>>> REPLACE\n"
        mm = Micromanager(
            ScriptedMicromanager(
                {
                    "shape_and_plan": {
                        "verdict": "split",
                        "clarified_notes": hdr + "note",
                        "extra_context_files": [".claude/settings.json", "src/lib.rs"],
                        "steps": [
                            {
                                "n": 1,
                                "does": hdr + "step one",
                                "tests": ["test_empty_returns_err"],
                                "touches": ["src/lib.rs"],
                            }
                        ],
                        "findings": [{"title": hdr + "ftitle", "body": hdr + "fbody"}],
                    },
                    "coach": {
                        "root_cause": hdr + "rc",
                        "guidance": hdr + "guide",
                        "abort": True,
                        "abort_reason": hdr + "abort why",
                    },
                    "adjudicate_checker": {
                        "verdict": "uphold",
                        "rationale": hdr + "valid",
                        "fix_hint": hdr + "hint",
                    },
                    "pre_review": {
                        "verdict": "bounce",
                        "reviewer_brief": hdr + "brief",
                        "missed_clause": hdr + "clause",
                        "findings": [{"title": hdr + "pr", "body": hdr + "prb"}],
                    },
                    "triage_blocked": {
                        "summary": hdr + "summary",
                        "recommendations": [{"card_id": "C1", "action": hdr + "act"}],
                        "findings": [{"title": hdr + "tr", "body": hdr + "trb"}],
                    },
                }
            )
        )
        self.assertIn(".claude/**", mm.forbidden_files)

        shaped = mm.shape_and_plan(self.card, {}, {})
        self.assertEqual(shaped.extra_context_files, ["src/lib.rs"])
        for txt in (
            shaped.clarified_notes,
            shaped.steps[0]["does"],
            shaped.findings[0]["title"],
            shaped.findings[0]["body"],
        ):
            self.assertNotIn("=== whole_file", txt)
            self.assertNotIn("<<< SEARCH", txt)

        coached = mm.coach(self.card, "impl", 1, {}, "", "")
        self.assertNotIn("=== whole_file", coached.abort_reason)
        self.assertNotIn("<<< SEARCH", coached.abort_reason)

        adj = mm.adjudicate_checker(self.card, "", None, [])
        self.assertNotIn("=== whole_file", adj.rationale)
        self.assertNotIn("<<< SEARCH", adj.rationale)

        rev = mm.triage_and_review("pre_review", {"card": self.card.as_dict()})
        self.assertNotIn("=== whole_file", rev.reviewer_brief)
        self.assertNotIn("=== whole_file", rev.missed_clause)
        self.assertNotIn("=== whole_file", rev.findings[0]["title"])

        tri = mm.triage_and_review("triage_blocked", {})
        self.assertNotIn("=== whole_file", tri.summary)
        self.assertNotIn("=== whole_file", tri.recommendations[0]["action"])
        self.assertNotIn("=== whole_file", tri.findings[0]["body"])


if __name__ == "__main__":
    unittest.main()
