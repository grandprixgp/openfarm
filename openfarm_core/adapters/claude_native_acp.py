"""Stdlib ACP v1 server bridge over native `claude -p` (OAuth subscription CLI).

Allows Openfarm's ``ClaudeAcpAdapter`` to run natively using a user's logged-in
Claude Code OAuth session (`claude auth login`) without requiring a separate
third-party `claude-agent-acp` package or Anthropic Console API key.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path

if __package__ in (None, ""):
    # Run as a script by the ACP adapter: make the package importable.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from openfarm_core import confine as confine_lib

PROTOCOL_VERSION = 1
_AUTH_HINT_RE = re.compile(
    r"not logged in|please run /login|authentication_failed|oauth|"
    r"claude auth login|invalid bearer token|unauthorized|401",
    re.IGNORECASE,
)


def _is_auth_failure(text: str) -> bool:
    return bool(_AUTH_HINT_RE.search(str(text or "")))


class NativeClaudeAcpServer:
    """Minimal single-client NDJSON-RPC 2.0 ACP v1 bridge backed by ``claude -p``."""

    def __init__(self, claude_bin: str = "claude") -> None:
        self.claude_bin = str(claude_bin or "claude")
        self._write_lock = threading.Lock()
        self._proc_lock = threading.Lock()
        self._active_procs: dict[str, subprocess.Popen[bytes]] = {}
        self._cancelled_sessions: set[str] = set()
        self._sessions: dict[str, dict] = {}
        self._initialized = False

    def send(self, payload: dict) -> None:
        line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
        with self._write_lock:
            sys.stdout.write(line)
            sys.stdout.flush()

    def _send_result(self, req_id: object, result: dict) -> None:
        if req_id is not None:
            self.send({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _send_error(
        self, req_id: object, code: int, message: str, *, data: dict | None = None
    ) -> None:
        if req_id is None:
            return
        err: dict = {"code": code, "message": str(message)}
        if data is not None:
            err["data"] = data
        self.send({"jsonrpc": "2.0", "id": req_id, "error": err})

    def _terminate_session_proc(self, session_id: str) -> None:
        with self._proc_lock:
            self._cancelled_sessions.add(session_id)
            proc = self._active_procs.get(session_id)
        # The child leads its own process group: end the whole group, so the
        # commands it started do not outlive a cancelled or timed-out turn.
        confine_lib.end_process_group(proc)

    def end_all(self) -> None:
        """End every running child's process group (on close or SIGTERM)."""
        with self._proc_lock:
            procs = list(self._active_procs.items())
        for sid, _proc in procs:
            self._terminate_session_proc(sid)

    def _handle_initialize(self, req_id: object) -> None:
        self._initialized = True
        self._send_result(
            req_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "agentCapabilities": {
                    "loadSession": False,
                    "nativeClaudeCli": True,
                },
                "authMethods": [
                    {
                        "id": "oauth",
                        "name": "Claude OAuth",
                        "type": "oauth",
                        "description": "Use local `claude auth login` OAuth credentials",
                    },
                    {
                        "id": "api-key",
                        "name": "Anthropic API Key",
                        "type": "api_key",
                        "description": "Use configured API key helper or env",
                    },
                ],
            },
        )

    def _handle_authenticate(self, req_id: object, params: dict) -> None:
        method_id = str(
            params.get("methodId") or params.get("method") or "oauth"
        ).strip().lower()
        if method_id not in ("oauth", "claude-ai", "claude.ai", "api-key", "api_key"):
            self._send_error(req_id, -32602, f"Unsupported auth method: {method_id!r}")
            return
        self._send_result(req_id, {"authenticated": True, "methodId": method_id})

    def _handle_new_session(self, req_id: object, params: dict) -> None:
        cwd = str(params.get("cwd") or Path.cwd())
        meta = params.get("_meta")
        if not isinstance(meta, dict):
            meta = {}
        cc_meta = meta.get("claudeCode")
        cc_opts = (
            cc_meta.get("options")
            if isinstance(cc_meta, dict) and isinstance(cc_meta.get("options"), dict)
            else {}
        )
        disallowed = [
            str(x).strip()
            for x in (cc_opts.get("disallowedTools") or [])
            if str(x).strip()
        ]
        tool_policy = "none" if "Read" in disallowed else "read-only"
        sys_meta = meta.get("systemPrompt")
        system_prompt = (
            str(sys_meta.get("append") or "")
            if isinstance(sys_meta, dict)
            else ""
        )
        of_meta = meta.get("openfarm") if isinstance(meta.get("openfarm"), dict) else {}
        confine_meta = of_meta.get("confine")
        # Claude's own rules hold it; a boundary is added only when the adapter
        # says so (`[micromanager] confine = "bwrap"`).
        hold = confine_lib.bridge_plan("claude", tool_policy, confine_meta)
        sid = f"native-{uuid.uuid4().hex[:12]}"
        self._sessions[sid] = {
            "hold": hold,
            "confine_meta": confine_meta if isinstance(confine_meta, dict) else {},
            "cwd": cwd,
            "model": str(cc_opts.get("model") or ""),
            "effort": str(cc_opts.get("effort") or ""),
            "tool_policy": tool_policy,
            "disallowed_tools": disallowed,
            "system_prompt": system_prompt,
            "used_tokens": 0,
            "cum_cost": 0.0,
        }
        self._send_result(req_id, {"sessionId": sid})

    def _build_claude_argv(self, session: dict) -> list[str]:
        # Never pass `--bare`: `--bare` disables Claude OAuth credentials.
        argv = [
            self.claude_bin,
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
        ]
        if session["tool_policy"] == "none":
            argv.extend(["--tools", ""])
        else:
            argv.extend(["--tools", "Read,Glob,Grep"])
            if session["disallowed_tools"]:
                argv.extend(["--disallowedTools", ",".join(session["disallowed_tools"])])
        if session["model"]:
            argv.extend(["--model", session["model"]])
        if session["effort"]:
            argv.extend(["--effort", session["effort"]])
        if session["system_prompt"]:
            argv.extend(["--append-system-prompt", session["system_prompt"]])
        return argv

    def _run_prompt(self, req_id: object, params: dict) -> None:
        sid = str(params.get("sessionId") or "")
        session = self._sessions.get(sid)
        if session is None:
            self._send_error(req_id, -32602, f"Unknown sessionId: {sid!r}")
            return

        prompt_items = params.get("prompt") or []
        parts: list[str] = []
        if isinstance(prompt_items, list):
            for item in prompt_items:
                if isinstance(item, dict) and "text" in item:
                    parts.append(str(item.get("text") or ""))
                elif isinstance(item, str):
                    parts.append(item)
        elif isinstance(prompt_items, str):
            parts.append(prompt_items)
        prompt_text = "\n".join(parts)

        with self._proc_lock:
            self._cancelled_sessions.discard(sid)

        argv = self._build_claude_argv(session)
        hold = session.get("hold")
        if hold is not None:
            try:
                argv = confine_lib.bridge_child_argv(
                    argv, hold, session.get("confine_meta"), session_dir=session["cwd"])
            except confine_lib.ConfineError as exc:
                self._send_error(
                    req_id, -32001, exc.message,
                    data={"kind": "refused", "code": "acp-confine-unavailable"},
                )
                return
        try:
            proc = subprocess.Popen(
                argv,
                cwd=session["cwd"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            self._send_error(req_id, -32603, f"Failed to spawn {self.claude_bin}: {exc}")
            return

        with self._proc_lock:
            self._active_procs[sid] = proc
            already_cancelled = sid in self._cancelled_sessions
        if already_cancelled:
            self._terminate_session_proc(sid)

        try:
            stdout_b, stderr_b = proc.communicate(input=prompt_text.encode("utf-8"))
        finally:
            with self._proc_lock:
                self._active_procs.pop(sid, None)

        with self._proc_lock:
            was_cancelled = sid in self._cancelled_sessions
            self._cancelled_sessions.discard(sid)
        if was_cancelled:
            self._send_result(req_id, {"stopReason": "cancelled"})
            return

        stdout_text = stdout_b.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_b.decode("utf-8", errors="replace").strip()

        parsed: dict | None = None
        if stdout_text:
            try:
                cand = json.loads(stdout_text)
                if isinstance(cand, dict):
                    parsed = cand
            except ValueError:
                for line in reversed(stdout_text.splitlines()):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        cand = json.loads(line)
                        if isinstance(cand, dict):
                            parsed = cand
                            break
                    except ValueError:
                        continue

        if parsed is None:
            msg = stderr_text or stdout_text or f"claude exited with code {proc.returncode}"
            if _is_auth_failure(msg):
                self._send_error(
                    req_id,
                    -32001,
                    msg,
                    data={
                        "kind": "refused",
                        "code": "acp-auth-required",
                        "errorKind": "authentication_failed",
                    },
                )
            else:
                self._send_error(req_id, -32603, msg)
            return

        is_error = bool(parsed.get("is_error"))
        result_text = str(parsed.get("result") or "")
        if is_error or proc.returncode != 0:
            err_msg = result_text or stderr_text or f"claude failed (exit {proc.returncode})"
            term_reason = str(parsed.get("terminal_reason") or "")
            if _is_auth_failure(err_msg) or (
                term_reason == "api_error" and _is_auth_failure(stderr_text + "\n" + err_msg)
            ):
                self._send_error(
                    req_id,
                    -32001,
                    err_msg,
                    data={
                        "kind": "refused",
                        "code": "acp-auth-required",
                        "errorKind": "authentication_failed",
                    },
                )
            else:
                self._send_error(req_id, -32603, err_msg)
            return

        usage = parsed.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        tokens_in = (
            int(usage.get("input_tokens", 0) or 0)
            + int(usage.get("cache_creation_input_tokens", 0) or 0)
            + int(usage.get("cache_read_input_tokens", 0) or 0)
        )
        tokens_out = int(usage.get("output_tokens", 0) or 0)
        turn_cost = float(parsed.get("total_cost_usd", 0.0) or 0.0)

        session["used_tokens"] += tokens_in + tokens_out
        session["cum_cost"] += turn_cost

        self.send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": result_text},
                    },
                },
            }
        )
        self.send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "usage_update",
                        "inputTokens": tokens_in,
                        "outputTokens": tokens_out,
                        "used": session["used_tokens"],
                        "size": 200_000,
                        "cost": {"amount": session["cum_cost"], "currency": "USD"},
                    },
                },
            }
        )
        self._send_result(
            req_id,
            {
                "stopReason": "end_turn",
                "usage": {
                    "inputTokens": tokens_in,
                    "outputTokens": tokens_out,
                },
            },
        )

    def serve(self) -> int:
        prompt_threads: list[threading.Thread] = []
        _end_children_on_sigterm(self)
        try:
            while True:
                raw = sys.stdin.readline()
                if not raw:
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                method = msg.get("method")
                req_id = msg.get("id")
                params = msg.get("params")
                if not isinstance(params, dict):
                    params = {}

                if method == "initialize":
                    self._handle_initialize(req_id)
                elif method == "authenticate":
                    self._handle_authenticate(req_id, params)
                elif method == "session/new":
                    self._handle_new_session(req_id, params)
                elif method == "session/prompt":
                    th = threading.Thread(
                        target=self._run_prompt,
                        args=(req_id, params),
                        daemon=True,
                    )
                    th.start()
                    prompt_threads.append(th)
                elif method == "session/cancel":
                    sid = str(params.get("sessionId") or "")
                    if sid:
                        self._terminate_session_proc(sid)
                elif method == "session/close":
                    sid = str(params.get("sessionId") or "")
                    if sid:
                        self._terminate_session_proc(sid)
                        self._sessions.pop(sid, None)
                    else:
                        for active_sid in list(self._sessions):
                            self._terminate_session_proc(active_sid)
                        self._sessions.clear()
                    self._send_result(req_id, {})
                    break
                else:
                    self._send_error(req_id, -32601, f"Method not found: {method}")
        finally:
            self.end_all()
            for th in prompt_threads:
                if th.is_alive():
                    th.join(timeout=1.0)
        return 0


def _end_children_on_sigterm(server) -> None:
    """On SIGTERM (the adapter ending this bridge's group), end the CLI children too.

    Each child leads its own process group, so killing the bridge's group alone
    would leave them, and what they started, running.
    """
    if threading.current_thread() is not threading.main_thread():
        return

    def _handler(signum, _frame):
        server.end_all()
        os._exit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGHUP, _handler)
    except (ValueError, OSError):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Native Claude Code CLI (-p) ACP v1 stdio adapter."
    )
    parser.add_argument(
        "--claude-bin",
        default=shutil.which("claude") or "claude",
        help="Path to the `claude` CLI executable.",
    )
    args = parser.parse_args(argv)
    server = NativeClaudeAcpServer(claude_bin=args.claude_bin)
    return server.serve()


if __name__ == "__main__":
    raise SystemExit(main())
