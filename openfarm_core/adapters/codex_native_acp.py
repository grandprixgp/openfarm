"""Stdlib ACP v1 server bridge over native `codex exec` (ChatGPT OAuth / API key CLI).

Allows Openfarm's ``ClaudeAcpAdapter`` (with ``provider="codex"``) to drive
Codex CLI non-interactively via JSON-RPC 2.0 over stdio without requiring an
external ``codex-acp`` wrapper binary.
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

PROTOCOL_VERSION = 1
_AUTH_HINT_RE = re.compile(
    r"not logged in|please run.*login|codex login|authentication_failed|"
    r"authentication required|not authenticated|invalid api key|incorrect api key|"
    r"invalid bearer token|unauthorized|\b401\b|missing.*auth",
    re.IGNORECASE,
)


def _is_auth_failure(text: str) -> bool:
    return bool(_AUTH_HINT_RE.search(str(text or "")))


def _extract_prompt_text(prompt_items: object) -> str:
    parts: list[str] = []
    if isinstance(prompt_items, list):
        for item in prompt_items:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
    elif isinstance(prompt_items, str):
        parts.append(prompt_items)
    return "\n".join(parts)


def _extract_agent_message_text(item: dict) -> str:
    if isinstance(item.get("text"), str):
        return item["text"]
    content = item.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    if isinstance(content, str):
        return content
    return ""


def _parse_codex_ndjson(stdout_text: str) -> tuple[str, int, int, float, str, bool]:
    """Parse `codex exec --json` NDJSON events.

    Returns ``(text, tokens_in, tokens_out, cost_usd, error_msg, saw_event)``.
    """
    messages: list[str] = []
    tokens_in = 0
    tokens_out = 0
    cost_usd = 0.0
    error_msgs: list[str] = []
    saw_event = False

    for raw_line in stdout_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except ValueError:
            continue
        if not isinstance(evt, dict):
            continue
        saw_event = True
        evt_type = str(evt.get("type") or "")

        if evt_type == "item.completed":
            item = evt.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = _extract_agent_message_text(item)
                if text:
                    messages.append(text)
        elif evt_type == "turn.completed":
            usage = evt.get("usage")
            if isinstance(usage, dict):
                tokens_in = int(
                    usage.get("input_tokens")
                    or usage.get("prompt_tokens")
                    or usage.get("inputTokens")
                    or 0
                )
                tokens_out = int(
                    usage.get("output_tokens")
                    or usage.get("completion_tokens")
                    or usage.get("outputTokens")
                    or 0
                )
                if usage.get("cost_usd") is not None:
                    cost_usd = float(usage.get("cost_usd") or 0.0)
            if evt.get("total_cost_usd") is not None:
                cost_usd = float(evt.get("total_cost_usd") or 0.0)
        elif evt_type in ("turn.failed", "error"):
            err_obj = evt.get("error")
            if isinstance(err_obj, dict) and err_obj.get("message"):
                error_msgs.append(str(err_obj["message"]))
            elif evt.get("message"):
                error_msgs.append(str(evt["message"]))
            elif isinstance(err_obj, str) and err_obj:
                error_msgs.append(err_obj)

    return ("\n".join(messages), tokens_in, tokens_out, cost_usd, "; ".join(error_msgs), saw_event)


class NativeCodexAcpServer:
    """Minimal single-client NDJSON-RPC 2.0 ACP v1 bridge backed by ``codex exec``."""

    def __init__(self, codex_bin: str = "codex") -> None:
        self.codex_bin = str(codex_bin or "codex")
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
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass

    def _handle_initialize(self, req_id: object) -> None:
        self._initialized = True
        self._send_result(
            req_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "agentCapabilities": {
                    "loadSession": False,
                    "nativeCodexCli": True,
                },
                "authMethods": [
                    {
                        "id": "oauth",
                        "name": "Codex OAuth",
                        "type": "oauth",
                        "description": "Use local `codex login` credentials",
                    },
                    {
                        "id": "api-key",
                        "name": "OpenAI API Key",
                        "type": "api_key",
                        "description": "Use OPENAI_API_KEY env",
                    },
                ],
            },
        )

    def _handle_authenticate(self, req_id: object, params: dict) -> None:
        method_id = str(
            params.get("methodId") or params.get("method") or "oauth"
        ).strip().lower()
        if method_id not in ("oauth", "chatgpt", "api-key", "api_key"):
            self._send_error(req_id, -32602, f"Unsupported auth method: {method_id!r}")
            return
        self._send_result(req_id, {"authenticated": True, "methodId": method_id})

    def _handle_new_session(self, req_id: object, params: dict) -> None:
        cwd = str(params.get("cwd") or Path.cwd())
        meta = params.get("_meta")
        if not isinstance(meta, dict):
            meta = {}
        codex_meta = meta.get("codex")
        cc_meta = meta.get("claudeCode")
        opts: dict = {}
        if isinstance(codex_meta, dict) and isinstance(codex_meta.get("options"), dict):
            opts = codex_meta["options"]
        elif isinstance(cc_meta, dict) and isinstance(cc_meta.get("options"), dict):
            opts = cc_meta["options"]

        sys_meta = meta.get("systemPrompt")
        system_prompt = (
            str(sys_meta.get("append") or "")
            if isinstance(sys_meta, dict)
            else ""
        )
        sid = f"codex-native-{uuid.uuid4().hex[:12]}"
        self._sessions[sid] = {
            "cwd": cwd,
            "model": str(opts.get("model") or ""),
            "effort": str(opts.get("effort") or ""),
            "tool_policy": str(opts.get("toolPolicy") or "read-only"),
            "system_prompt": system_prompt,
            "used_tokens": 0,
            "cum_cost": 0.0,
        }
        self._send_result(req_id, {"sessionId": sid})

    def _build_codex_argv(self, session: dict) -> list[str]:
        argv = [
            self.codex_bin,
            "exec",
            "--json",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "-s",
            "read-only",
            "-C",
            session["cwd"],
        ]
        if session["model"]:
            argv.extend(["-m", session["model"]])
        if session["effort"]:
            argv.extend(["-c", f'model_reasoning_effort={json.dumps(session["effort"])}'])
        argv.append("-")
        return argv

    def _run_prompt(self, req_id: object, params: dict) -> None:
        sid = str(params.get("sessionId") or "")
        session = self._sessions.get(sid)
        if session is None:
            self._send_error(req_id, -32602, f"Unknown sessionId: {sid!r}")
            return

        prompt_text = _extract_prompt_text(params.get("prompt") or [])
        sys_prompt = session["system_prompt"]
        if sys_prompt and prompt_text:
            full_prompt = f"{sys_prompt}\n\n{prompt_text}"
        else:
            full_prompt = sys_prompt or prompt_text

        with self._proc_lock:
            self._cancelled_sessions.discard(sid)

        argv = self._build_codex_argv(session)
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
            self._send_error(req_id, -32603, f"Failed to spawn {self.codex_bin}: {exc}")
            return

        with self._proc_lock:
            self._active_procs[sid] = proc
            already_cancelled = sid in self._cancelled_sessions
        if already_cancelled:
            self._terminate_session_proc(sid)

        try:
            stdout_b, stderr_b = proc.communicate(input=full_prompt.encode("utf-8"))
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
        result_text, tokens_in, tokens_out, turn_cost, evt_err, saw_event = _parse_codex_ndjson(
            stdout_text
        )

        if evt_err or proc.returncode != 0 or (not saw_event and not result_text):
            err_msg = (
                evt_err
                or stderr_text
                or stdout_text
                or f"codex exited with code {proc.returncode}"
            )
            if _is_auth_failure(err_msg + "\n" + stderr_text):
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
            for sid in list(self._active_procs):
                self._terminate_session_proc(sid)
            for th in prompt_threads:
                if th.is_alive():
                    th.join(timeout=1.0)
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Native Codex CLI (`codex exec --json`) ACP v1 stdio adapter."
    )
    parser.add_argument(
        "--codex-bin",
        default=shutil.which("codex") or "codex",
        help="Path to the `codex` CLI executable.",
    )
    args = parser.parse_args(argv)
    server = NativeCodexAcpServer(codex_bin=args.codex_bin)
    return server.serve()


if __name__ == "__main__":
    raise SystemExit(main())
