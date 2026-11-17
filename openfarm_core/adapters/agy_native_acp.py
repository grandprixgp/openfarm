"""Stdlib ACP v1 server bridge over native `agy -p --output-format json`.

Fallback transport for Antigravity when `--binary agy-print` is requested or
an external ACP server binary is not used directly.
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
    r"not logged in|please run /login|authentication_failed|"
    r"authentication required|not authenticated|invalid.*token|"
    r"unauthorized|\b401\b|\boauth\b|agy acp --login|api.?key",
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


def _parse_json_output(stdout_text: str) -> dict | None:
    if not stdout_text:
        return None
    try:
        cand = json.loads(stdout_text)
        if isinstance(cand, dict):
            return cand
    except ValueError:
        pass
    for line in reversed(stdout_text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            cand = json.loads(line)
            if isinstance(cand, dict):
                return cand
        except ValueError:
            continue
    return None


def _extract_usage(parsed: dict) -> tuple[int, int, float]:
    usage = parsed.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    if (
        "input_tokens" in usage
        or "cache_creation_input_tokens" in usage
        or "cache_read_input_tokens" in usage
        or "cache_read_tokens" in usage
    ):
        tokens_in = (
            int(usage.get("input_tokens", 0) or 0)
            + int(usage.get("cache_creation_input_tokens", 0) or 0)
            + int(usage.get("cache_read_input_tokens", 0) or 0)
            + int(usage.get("cache_read_tokens", 0) or 0)
        )
    else:
        tokens_in = int(
            usage.get("prompt_tokens")
            or usage.get("promptTokenCount")
            or usage.get("inputTokens")
            or 0
        )
    base_out = int(
        usage.get("output_tokens")
        or usage.get("completion_tokens")
        or usage.get("candidatesTokenCount")
        or usage.get("outputTokens")
        or 0
    )
    tokens_out = base_out + int(usage.get("thinking_tokens", 0) or 0)
    turn_cost = float(parsed.get("total_cost_usd", 0.0) or 0.0)
    return tokens_in, tokens_out, turn_cost


class NativeAgyAcpServer:
    """Minimal single-client NDJSON-RPC 2.0 ACP v1 bridge backed by ``agy -p``."""

    def __init__(self, agy_bin: str = "agy") -> None:
        self.agy_bin = str(agy_bin or "agy")
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
                    "nativeAgyCli": True,
                },
                "authMethods": [
                    {
                        "id": "oauth",
                        "name": "Antigravity OAuth",
                        "type": "oauth",
                        "description": "Use local Antigravity OAuth token",
                    },
                    {
                        "id": "api-key",
                        "name": "Gemini API Key",
                        "type": "api_key",
                        "description": "Use GEMINI_API_KEY env",
                    },
                ],
            },
        )

    def _handle_authenticate(self, req_id: object, params: dict) -> None:
        method_id = str(
            params.get("methodId") or params.get("method") or "oauth"
        ).strip().lower()
        if method_id not in ("oauth", "oauth-personal", "api-key", "api_key", "gemini-api-key"):
            self._send_error(req_id, -32602, f"Unsupported auth method: {method_id!r}")
            return
        self._send_result(req_id, {"authenticated": True, "methodId": method_id})

    def _handle_new_session(self, req_id: object, params: dict) -> None:
        cwd = str(params.get("cwd") or Path.cwd())
        meta = params.get("_meta")
        if not isinstance(meta, dict):
            meta = {}
        ag_meta = meta.get("antigravity") if isinstance(meta.get("antigravity"), dict) else {}
        cc_meta = meta.get("claudeCode") if isinstance(meta.get("claudeCode"), dict) else {}
        opts: dict = {}
        if isinstance(ag_meta.get("options"), dict):
            opts = ag_meta["options"]
        elif isinstance(cc_meta.get("options"), dict):
            opts = cc_meta["options"]

        system_prompt = ""
        ag_sys = ag_meta.get("systemPrompt")
        sys_meta = meta.get("systemPrompt")
        if isinstance(ag_sys, dict) and ag_sys.get("append"):
            system_prompt = str(ag_sys.get("append") or "")
        elif isinstance(sys_meta, dict) and sys_meta.get("append"):
            system_prompt = str(sys_meta.get("append") or "")

        of_meta = meta.get("openfarm") if isinstance(meta.get("openfarm"), dict) else {}
        tool_policy = str(of_meta.get("toolPolicy") or "none")
        if tool_policy not in ("none", "read-only"):
            self._send_error(req_id, -32602, f"Unknown tool policy: {tool_policy!r}")
            return
        confine_meta = of_meta.get("confine")
        hold = confine_lib.bridge_plan("antigravity", tool_policy, confine_meta)
        if hold.refused:
            self._send_error(
                req_id, -32001, hold.refused,
                data={"kind": "refused", "code": "acp-confine-unavailable"},
            )
            return

        sid = f"agy-native-{uuid.uuid4().hex[:12]}"
        model = str(opts.get("model") or "")
        effort = str(opts.get("effort") or "")
        self._sessions[sid] = {
            "cwd": cwd,
            "model": model,
            "effort": effort,
            "tool_policy": tool_policy,
            "hold": hold,
            "confine_meta": confine_meta if isinstance(confine_meta, dict) else {},
            "system_prompt": system_prompt,
            "used_tokens": 0,
            "cum_cost": 0.0,
        }
        self._send_result(
            req_id,
            {
                "sessionId": sid,
                "configOptions": [
                    {"id": "model", "category": "model", "type": "select", "currentValue": model},
                    {
                        "id": "thought_level",
                        "category": "thought_level",
                        "type": "select",
                        "currentValue": effort,
                    },
                ],
            },
        )

    def _handle_set_config_option(self, req_id: object, params: dict) -> None:
        sid = str(params.get("sessionId") or "")
        session = self._sessions.get(sid)
        if session is None:
            self._send_error(req_id, -32602, f"Unknown sessionId: {sid!r}")
            return
        config_id = str(params.get("configId") or params.get("id") or "").strip()
        value = str(params.get("value") or "")
        if config_id == "model":
            session["model"] = value
        elif config_id in ("thought_level", "effort", "reasoning_effort"):
            session["effort"] = value
        else:
            self._send_error(req_id, -32602, f"Unknown configId: {config_id!r}")
            return
        self._send_result(req_id, {})

    def _build_agy_argv(self, session: dict, full_prompt: str = "") -> list[str]:
        argv = [
            self.agy_bin,
            "--output-format",
            "json",
            "--sandbox",
            "--disable-slash-commands",
        ]
        if session["model"]:
            argv.extend(["--model", session["model"]])
        if session["effort"]:
            argv.extend(["--effort", session["effort"]])
        argv.extend(["-p", full_prompt])
        return argv

    def _child_argv(self, session: dict, argv: list[str]) -> list[str]:
        hold = session.get("hold")
        if hold is None:
            return argv
        return confine_lib.bridge_child_argv(
            argv, hold, session.get("confine_meta"), session_dir=session["cwd"])

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

        argv = self._build_agy_argv(session, full_prompt)
        try:
            argv = self._child_argv(session, argv)
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
            self._send_error(req_id, -32603, f"Failed to spawn {self.agy_bin}: {exc}")
            return

        with self._proc_lock:
            self._active_procs[sid] = proc
            already_cancelled = sid in self._cancelled_sessions
        if already_cancelled:
            self._terminate_session_proc(sid)

        try:
            stdout_b, stderr_b = proc.communicate(input=b"")
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
        parsed = _parse_json_output(stdout_text)

        if parsed is None:
            msg = stderr_text or stdout_text or f"agy exited with code {proc.returncode}"
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

        raw_err = parsed.get("error")
        is_error = (
            bool(parsed.get("is_error"))
            or bool(raw_err)
            or str(parsed.get("status") or "").upper() == "ERROR"
        )
        raw_res = (
            parsed.get("result")
            if parsed.get("result") is not None
            else parsed.get("response")
        )
        result_text = "" if raw_res is None else str(raw_res)

        if is_error or proc.returncode != 0:
            err_detail = ""
            if isinstance(raw_err, dict):
                err_detail = str(raw_err.get("message") or raw_err)
            elif raw_err:
                err_detail = str(raw_err)
            err_msg = (
                result_text
                or err_detail
                or stderr_text
                or f"agy failed (exit {proc.returncode})"
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

        tokens_in, tokens_out, turn_cost = _extract_usage(parsed)
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
                elif method == "session/set_config_option":
                    self._handle_set_config_option(req_id, params)
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
        description="Native Antigravity CLI (`agy -p`) ACP v1 stdio adapter."
    )
    parser.add_argument(
        "--agy-bin",
        default=shutil.which("agy") or "agy",
        help="Path to the `agy` CLI executable.",
    )
    args = parser.parse_args(argv)
    server = NativeAgyAcpServer(agy_bin=args.agy_bin)
    return server.serve()


if __name__ == "__main__":
    raise SystemExit(main())
