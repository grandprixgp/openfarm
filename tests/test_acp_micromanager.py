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
    AcpResult,
    ClaudeAcpAdapter,
    _sanitised_env,
    strip_control_sequences,
)
from openfarm_core.cards import Card
from openfarm_core.edits import FORMAT_SEARCH_REPLACE, FORMAT_WHOLE_FILE, parse_edits
from openfarm_core.errors import Infra, Invalid, Refused
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
            self.assertEqual(settings, {"hooks": {}})
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
            self.assertEqual(
                opts["disallowedTools"],
                ["Bash", "Edit", "Write", "MultiEdit", "NotebookEdit"],
            )

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
            with self.assertRaises(Refused) as caught:
                acp.prompt(sid, "hang", timeout_s=10.0, cancel_check=kill_switch)
            self.assertEqual(caught.exception.code, "acp-cancelled")

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


if __name__ == "__main__":
    unittest.main()
