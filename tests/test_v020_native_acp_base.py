"""Regression tests for Wave 2 native ACP consolidation, bridge fixes, and TUI view alignment."""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openfarm_core import confine as confine_lib  # noqa: E402
from openfarm_core.adapters.agy_native_acp import NativeAgyAcpServer  # noqa: E402
from openfarm_core.adapters.claude_native_acp import NativeClaudeAcpServer  # noqa: E402
from openfarm_core.adapters.codex_native_acp import NativeCodexAcpServer  # noqa: E402
from openfarm_core.adapters.native_acp_base import BaseNativeAcpServer  # noqa: E402
from openfarm_core.daemon import _pid_alive as daemon_pid_alive  # noqa: E402
from openfarm_core.errors import UsageError  # noqa: E402
from openfarm_core.state import atomic_write_text  # noqa: E402
from openfarm_core.tui import CLI_VIEWS, TEXTUAL_VIEWS, VIEWS, run_tui  # noqa: E402

CLAUDE_BRIDGE = ROOT / "openfarm_core" / "adapters" / "claude_native_acp.py"
CODEX_BRIDGE = ROOT / "openfarm_core" / "adapters" / "codex_native_acp.py"
AGY_BRIDGE = ROOT / "openfarm_core" / "adapters" / "agy_native_acp.py"


def _write_exec(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _rpc_send(proc: subprocess.Popen[str], payload: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()


def _rpc_read_until_id(
    proc: subprocess.Popen[str], target_id: object, timeout_s: float = 10.0
) -> tuple[dict, list[dict]]:
    """Read NDJSON lines from `proc.stdout` until a response with `id == target_id`."""
    assert proc.stdout is not None
    notifications: list[dict] = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            raise AssertionError(f"bridge closed stdout before id={target_id!r}")
        msg = json.loads(line)
        if msg.get("id") == target_id:
            return msg, notifications
        notifications.append(msg)
    raise AssertionError(f"timed out waiting for id={target_id!r}")


class NativeAcpConsolidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v020-bridges-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_all_three_bridges_subclass_base_and_run_as_standalone_scripts(self) -> None:
        for cls in (NativeClaudeAcpServer, NativeCodexAcpServer, NativeAgyAcpServer):
            self.assertTrue(issubclass(cls, BaseNativeAcpServer))

        clean_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        for script in (CLAUDE_BRIDGE, CODEX_BRIDGE, AGY_BRIDGE):
            with self.subTest(script=script.name):
                res = subprocess.run(
                    [sys.executable, str(script), "--help"],
                    cwd=str(self.tmp),
                    env=clean_env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn("ACP", res.stdout)

    def test_fix_a_sigterm_while_proc_lock_held_does_not_deadlock(self) -> None:
        server = NativeClaudeAcpServer(claude_bin="claude")
        wake_r, wake_w = os.pipe()
        os.set_blocking(wake_r, False)
        os.set_blocking(wake_w, False)
        prev = server._install_signal_handlers(wake_w)
        try:
            # Hold the non-reentrant _proc_lock on the main thread when SIGTERM arrives.
            with server._proc_lock:
                os.kill(os.getpid(), signal.SIGTERM)
                self.assertEqual(server._shutdown_signal, signal.SIGTERM)
                self.assertEqual(os.read(wake_r, 8), b"\x01")
        finally:
            for sig, handler in prev.items():
                signal.signal(sig, handler)
            os.close(wake_r)
            os.close(wake_w)

    def test_fix_a_sighup_and_sigterm_exit_bridge_and_terminate_child(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(sig=sig.name):
                pid_file = self.tmp / f"child-{sig.name}.pid"
                sleeper = _write_exec(
                    self.tmp / f"mock-claude-{sig.name}",
                    "#!/usr/bin/env python3\n"
                    "import os, pathlib, time\n"
                    f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
                    "time.sleep(60)\n",
                )
                proc = subprocess.Popen(
                    [sys.executable, str(CLAUDE_BRIDGE), "--claude-bin", str(sleeper)],
                    cwd=str(self.tmp),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    _rpc_send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
                    _rpc_read_until_id(proc, 1)
                    _rpc_send(
                        proc,
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "session/new",
                            "params": {"cwd": str(self.tmp)},
                        },
                    )
                    new_res, _ = _rpc_read_until_id(proc, 2)
                    sid = new_res["result"]["sessionId"]
                    _rpc_send(
                        proc,
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "method": "session/prompt",
                            "params": {"sessionId": sid, "prompt": [{"text": "hang"}]},
                        },
                    )
                    deadline = time.monotonic() + 10.0
                    while not pid_file.exists() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertTrue(pid_file.exists(), "mock CLI did not start")
                    child_pid = int(pid_file.read_text().strip())

                    proc.send_signal(sig)
                    rc = proc.wait(timeout=10)
                    self.assertEqual(rc, 128 + int(sig))

                    # Verify child was terminated as well
                    gone = False
                    end_deadline = time.monotonic() + 5.0
                    while time.monotonic() < end_deadline:
                        try:
                            os.kill(child_pid, 0)
                        except ProcessLookupError:
                            gone = True
                            break
                        time.sleep(0.05)
                    self.assertTrue(gone, f"child {child_pid} survived {sig.name}")
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=5)
                    for stream in (proc.stdin, proc.stdout, proc.stderr):
                        if stream is not None:
                            stream.close()

    def test_fix_b_malformed_usage_returns_jsonrpc_error_immediately(self) -> None:
        cases = [
            (
                "claude",
                CLAUDE_BRIDGE,
                "--claude-bin",
                json.dumps(
                    {
                        "result": "ok",
                        "usage": {"input_tokens": "N/A", "output_tokens": 5},
                        "total_cost_usd": 0.01,
                    }
                ),
            ),
            (
                "codex",
                CODEX_BRIDGE,
                "--codex-bin",
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {"type": "agent_message", "text": "ok"},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "usage": {"input_tokens": "N/A", "output_tokens": 5},
                            }
                        ),
                    ]
                ),
            ),
            (
                "agy",
                AGY_BRIDGE,
                "--agy-bin",
                json.dumps(
                    {
                        "result": "ok",
                        "usage": {"input_tokens": "N/A", "output_tokens": 5},
                    }
                ),
            ),
        ]
        for name, script, flag, payload in cases:
            with self.subTest(bridge=name):
                mock_cli = _write_exec(
                    self.tmp / f"bad-usage-{name}",
                    "#!/usr/bin/env python3\n"
                    f"print({payload!r})\n",
                )
                proc = subprocess.Popen(
                    [sys.executable, str(script), flag, str(mock_cli)],
                    cwd=str(self.tmp),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    _rpc_send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
                    _rpc_read_until_id(proc, 1)
                    _rpc_send(
                        proc,
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "session/new",
                            "params": {
                                "cwd": str(self.tmp),
                                "_meta": {
                                    "openfarm": {
                                        "toolPolicy": "none",
                                        "confine": {
                                            "mode": "off",
                                            "flags": False,
                                            "boundary": False,
                                        },
                                    }
                                },
                            },
                        },
                    )
                    new_res, _ = _rpc_read_until_id(proc, 2)
                    sid = new_res["result"]["sessionId"]
                    _rpc_send(
                        proc,
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "method": "session/prompt",
                            "params": {"sessionId": sid, "prompt": [{"text": "hi"}]},
                        },
                    )
                    err_res, _ = _rpc_read_until_id(proc, 3, timeout_s=5.0)
                    self.assertIn("error", err_res)
                    self.assertEqual(err_res["error"]["code"], -32603)
                    _rpc_send(proc, {"jsonrpc": "2.0", "id": 4, "method": "session/close", "params": {"sessionId": sid}})
                    _rpc_read_until_id(proc, 4)
                    proc.wait(timeout=5)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=5)
                    for stream in (proc.stdin, proc.stdout, proc.stderr):
                        if stream is not None:
                            stream.close()

    def test_fix_c_concurrent_prompt_on_same_sid_rejected_without_leaking_proc(self) -> None:
        started_file = self.tmp / "started.log"
        release_file = self.tmp / "release.flag"
        mock_cli = _write_exec(
            self.tmp / "slow-claude",
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, time\n"
            f"started = pathlib.Path({str(started_file)!r})\n"
            f"release = pathlib.Path({str(release_file)!r})\n"
            "with started.open('a', encoding='utf-8') as f:\n"
            "    f.write(f'{os.getpid()}\\n')\n"
            "deadline = time.monotonic() + 10.0\n"
            "while not release.exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.05)\n"
            "print(json.dumps({'result': 'done', 'usage': {'input_tokens': 1, 'output_tokens': 1}}))\n",
        )
        proc = subprocess.Popen(
            [sys.executable, str(CLAUDE_BRIDGE), "--claude-bin", str(mock_cli)],
            cwd=str(self.tmp),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _rpc_send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            _rpc_read_until_id(proc, 1)
            _rpc_send(
                proc,
                {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(self.tmp)}},
            )
            sid = _rpc_read_until_id(proc, 2)[0]["result"]["sessionId"]

            # Prompt 1 starts and blocks waiting for release_file
            _rpc_send(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "session/prompt",
                    "params": {"sessionId": sid, "prompt": [{"text": "first"}]},
                },
            )
            deadline = time.monotonic() + 5.0
            while not started_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(started_file.exists())

            # Concurrent prompt 2 on same sid is rejected immediately without spawning
            _rpc_send(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "session/prompt",
                    "params": {"sessionId": sid, "prompt": [{"text": "second"}]},
                },
            )
            res2, _ = _rpc_read_until_id(proc, 11, timeout_s=5.0)
            self.assertIn("error", res2)
            self.assertEqual(res2["error"]["data"]["code"], "acp-session-busy")
            self.assertEqual(len(started_file.read_text().splitlines()), 1)

            # Release prompt 1 and confirm it finishes cleanly
            release_file.write_text("go", encoding="utf-8")
            res1, _ = _rpc_read_until_id(proc, 10, timeout_s=5.0)
            self.assertEqual(res1["result"]["stopReason"], "end_turn")

            # Subsequent sequential prompt 3 on same sid succeeds
            _rpc_send(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 12,
                    "method": "session/prompt",
                    "params": {"sessionId": sid, "prompt": [{"text": "third"}]},
                },
            )
            res3, _ = _rpc_read_until_id(proc, 12, timeout_s=5.0)
            self.assertEqual(res3["result"]["stopReason"], "end_turn")
            self.assertEqual(len(started_file.read_text().splitlines()), 2)

            _rpc_send(proc, {"jsonrpc": "2.0", "id": 13, "method": "session/close", "params": {"sessionId": sid}})
            _rpc_read_until_id(proc, 13)
            proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()

    def test_fix_d_agy_cleans_up_empty_temp_dirs_on_prompt_and_close(self) -> None:
        mock_agy = _write_exec(
            self.tmp / "mock-agy",
            "#!/usr/bin/env python3\n"
            "import json\n"
            "print(json.dumps({'result': 'ok', 'usage': {'input_tokens': 2, 'output_tokens': 3}}))\n",
        )
        server = NativeAgyAcpServer(agy_bin=str(mock_agy))
        sent: list[dict] = []
        server.send = sent.append  # type: ignore[method-assign]

        created_dirs: list[Path] = []

        def fake_bridge_child_argv(argv, hold, meta, *, session_dir, env=None):
            p = Path(session_dir)
            if p.name.startswith("openfarm-agy-empty-"):
                created_dirs.append(p)
            return list(argv)

        with mock.patch.object(confine_lib, "bridge_child_argv", side_effect=fake_bridge_child_argv):
            server._handle_new_session(
                1,
                {
                    "cwd": str(self.tmp),
                    "_meta": {
                        "openfarm": {
                            "toolPolicy": "none",
                            "confine": {"mode": "bwrap", "flags": False, "boundary": True},
                        }
                    },
                },
            )
            sid = sent[-1]["result"]["sessionId"]
            server._run_prompt(2, {"sessionId": sid, "prompt": [{"text": "hello"}]})

            self.assertEqual(len(created_dirs), 1)
            self.assertFalse(
                created_dirs[0].exists(),
                f"temp dir {created_dirs[0]} was leaked after prompt",
            )
            self.assertEqual(server._sessions[sid].get("_temp_dirs"), [])

            # Also verify cleanup when bridge_child_argv raises ConfineError
            with mock.patch.object(
                confine_lib,
                "bridge_child_argv",
                side_effect=confine_lib.ConfineError("confine-unavailable", "no bwrap"),
            ):
                before = set(Path(tempfile.gettempdir()).glob("openfarm-agy-empty-*"))
                server._run_prompt(3, {"sessionId": sid, "prompt": [{"text": "fail"}]})
                after = set(Path(tempfile.gettempdir()).glob("openfarm-agy-empty-*"))
                self.assertEqual(after - before, set())

            # And verify end_all() cleans any manually registered temp dirs
            extra = Path(tempfile.mkdtemp(prefix="openfarm-agy-empty-"))
            server._sessions[sid].setdefault("_temp_dirs", []).append(extra)
            server.end_all()
            self.assertFalse(extra.exists())

    def test_fix_e_claude_defaults_fail_closed_when_disallowed_tools_omitted(self) -> None:
        server = NativeClaudeAcpServer(claude_bin="claude")
        sent: list[dict] = []
        server.send = sent.append  # type: ignore[method-assign]

        # 1) Omitted disallowedTools & omitted openfarm.toolPolicy -> fail closed ("none")
        server._handle_new_session(1, {"cwd": str(self.tmp), "_meta": {}})
        sid_default = sent[-1]["result"]["sessionId"]
        session_default = server._sessions[sid_default]
        self.assertEqual(session_default["tool_policy"], "none")
        argv_default = server._build_claude_argv(session_default)
        self.assertEqual(argv_default[argv_default.index("--tools") + 1], "")

        # 2) Explicit openfarm.toolPolicy = "read-only" -> "read-only"
        server._handle_new_session(
            2,
            {
                "cwd": str(self.tmp),
                "_meta": {"openfarm": {"toolPolicy": "read-only"}},
            },
        )
        sid_ro = sent[-1]["result"]["sessionId"]
        session_ro = server._sessions[sid_ro]
        self.assertEqual(session_ro["tool_policy"], "read-only")
        argv_ro = server._build_claude_argv(session_ro)
        self.assertEqual(argv_ro[argv_ro.index("--tools") + 1], "Read,Glob,Grep")

        # 3) Legacy disallowedTools without "Read" -> "read-only"
        server._handle_new_session(
            3,
            {
                "cwd": str(self.tmp),
                "_meta": {"claudeCode": {"options": {"disallowedTools": ["Bash", "Edit"]}}},
            },
        )
        sid_legacy_ro = sent[-1]["result"]["sessionId"]
        self.assertEqual(server._sessions[sid_legacy_ro]["tool_policy"], "read-only")

        # 4) Invalid toolPolicy -> -32602 error
        server._handle_new_session(
            4,
            {"cwd": str(self.tmp), "_meta": {"openfarm": {"toolPolicy": "write"}}},
        )
        self.assertIn("error", sent[-1])
        self.assertEqual(sent[-1]["error"]["code"], -32602)


class TuiViewAndSharedHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v020-tui-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_tui_cli_views_and_run_tui_routing(self) -> None:
        self.assertEqual(
            VIEWS,
            ("overview", "cards", "dossier", "board", "swarm", "harness", "ops"),
        )
        for v in ("studio", "review", "projects", "findings", "engine", "providers"):
            self.assertIn(v, TEXTUAL_VIEWS)
            self.assertIn(v, CLI_VIEWS)

        # --once with Textual views falls back cleanly to curses snapshot view
        for v in ("studio", "review"):
            buf = io.StringIO()
            rc = run_tui(repo=self.tmp, once=True, view=v, out=buf)
            self.assertEqual(rc, 0)
            self.assertIn("openfarm tui", buf.getvalue())

        # Invalid view still raises UsageError
        with self.assertRaises(UsageError):
            run_tui(repo=self.tmp, once=True, view="not-a-view")

        # Interactive mode forwards raw Textual view name to run_textual_tui
        seen: dict[str, object] = {}

        def fake_textual(**kwargs):
            seen.update(kwargs)
            return 0

        with mock.patch("openfarm_tui.textual_available", return_value=True), \
             mock.patch("openfarm_tui.run_textual_tui", side_effect=fake_textual):
            rc = run_tui(repo=self.tmp, once=False, view="studio", force_curses=False)
        self.assertEqual(rc, 0)
        self.assertEqual(seen.get("initial_view"), "studio")

    def test_atomic_write_text_and_daemon_pid_alive(self) -> None:
        target = self.tmp / "sub" / "hello.txt"
        atomic_write_text(target, "world\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "world\n")
        self.assertEqual(list(target.parent.glob(".*.tmp")), [])

        child = subprocess.Popen([sys.executable, "-c", "pass"])
        # Wait briefly for child to exit, then daemon_pid_alive should reap and return False
        deadline = time.monotonic() + 5.0
        alive = True
        while time.monotonic() < deadline:
            alive = daemon_pid_alive(child.pid)
            if not alive:
                break
            time.sleep(0.02)
        self.assertFalse(alive)
        child.wait(timeout=5)
        self.assertTrue(daemon_pid_alive(os.getpid()))
        self.assertFalse(daemon_pid_alive(0))


if __name__ == "__main__":
    unittest.main()
