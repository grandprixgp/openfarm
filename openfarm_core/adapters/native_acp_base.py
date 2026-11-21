"""Shared stdlib NDJSON-RPC 2.0 ACP v1 server base for native CLI bridges.

Used by ``claude_native_acp.py``, ``codex_native_acp.py``, and
``agy_native_acp.py`` so framing, session tracking, cancellation, safe signal
shutdown, concurrent-prompt protection, and temp-dir cleanup live in one place.
"""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

from openfarm_core import confine as confine_lib
from openfarm_core.spawn import popen_with_retry

PROTOCOL_VERSION = 1


def extract_prompt_text(prompt_items: object) -> str:
    """Flatten ACP ``params.prompt`` content blocks into plain text."""
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


def parse_json_output(stdout_text: str) -> dict | None:
    """Parse a JSON object from whole stdout or the last JSON line."""
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


class BaseNativeAcpServer:
    """Minimal single-client NDJSON-RPC 2.0 ACP v1 bridge over a native CLI."""

    CAPABILITY_KEY: str = ""
    CLI_NAME: str = "cli"
    AUTH_METHODS: tuple[dict, ...] = ()
    ACCEPTED_AUTH_METHODS: tuple[str, ...] = ("oauth", "api-key", "api_key")
    AUTH_HINT_RE: re.Pattern[str] = re.compile(r"$^")

    def __init__(self, cli_bin: str = "") -> None:
        self._cli_bin = str(cli_bin or self.CLI_NAME)
        self._write_lock = threading.Lock()
        self._proc_lock = threading.Lock()
        self._active_procs: dict[str, subprocess.Popen[bytes]] = {}
        self._busy_sessions: set[str] = set()
        self._cancelled_sessions: set[str] = set()
        self._sessions: dict[str, dict] = {}
        self._initialized = False
        self._shutdown_signal: int | None = None

    def send(self, payload: dict) -> None:
        if self._shutdown_signal is not None:
            return
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

    def _send_auth_error(self, req_id: object, message: str) -> None:
        self._send_error(
            req_id,
            -32001,
            message,
            data={
                "kind": "refused",
                "code": "acp-auth-required",
                "errorKind": "authentication_failed",
            },
        )

    def _is_auth_failure(self, text: str) -> bool:
        return bool(self.AUTH_HINT_RE.search(str(text or "")))

    def _cleanup_session_temp_dirs(self, session: dict) -> None:
        with self._proc_lock:
            dirs = list(session.get("_temp_dirs") or ())
            if "_temp_dirs" in session:
                session["_temp_dirs"] = []
        for path in dirs:
            shutil.rmtree(path, ignore_errors=True)

    def _terminate_session_proc(self, session_id: str) -> None:
        with self._proc_lock:
            self._cancelled_sessions.add(session_id)
            proc = self._active_procs.get(session_id)
        # The child leads its own process group: end the whole group, so the
        # commands it started do not outlive a cancelled or timed-out turn.
        confine_lib.end_process_group(proc)

    def end_all(self) -> None:
        """End every running child's process group and clean up session temp dirs."""
        with self._proc_lock:
            sids = list(self._active_procs.keys())
            sessions = list(self._sessions.values())
        for sid in sids:
            self._terminate_session_proc(sid)
        for session in sessions:
            self._cleanup_session_temp_dirs(session)

    def _handle_initialize(self, req_id: object) -> None:
        self._initialized = True
        caps: dict[str, object] = {"loadSession": False}
        if self.CAPABILITY_KEY:
            caps[self.CAPABILITY_KEY] = True
        self._send_result(
            req_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "agentCapabilities": caps,
                "authMethods": [dict(m) for m in self.AUTH_METHODS],
            },
        )

    def _handle_authenticate(self, req_id: object, params: dict) -> None:
        method_id = str(
            params.get("methodId") or params.get("method") or "oauth"
        ).strip().lower()
        if method_id not in self.ACCEPTED_AUTH_METHODS:
            self._send_error(req_id, -32602, f"Unsupported auth method: {method_id!r}")
            return
        self._send_result(req_id, {"authenticated": True, "methodId": method_id})

    def _handle_new_session(self, req_id: object, params: dict) -> None:
        raise NotImplementedError

    def _dispatch_extra_method(
        self, method: object, req_id: object, params: dict
    ) -> bool:
        return False

    def _prepare_prompt(
        self, req_id: object, session: dict, prompt_text: str
    ) -> tuple[list[str], bytes] | None:
        raise NotImplementedError

    def _process_prompt_output(
        self,
        req_id: object,
        sid: str,
        session: dict,
        returncode: int | None,
        stdout_text: str,
        stderr_text: str,
    ) -> None:
        raise NotImplementedError

    def _emit_prompt_success(
        self,
        req_id: object,
        sid: str,
        session: dict,
        result_text: str,
        tokens_in: int,
        tokens_out: int,
        turn_cost: float,
        *,
        tokens_in_cached: int | None = None,
    ) -> None:
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
        usage_update: dict[str, object] = {
            "sessionUpdate": "usage_update",
            "inputTokens": tokens_in,
            "outputTokens": tokens_out,
            "used": session["used_tokens"],
            "size": 200_000,
            "cost": {"amount": session["cum_cost"], "currency": "USD"},
        }
        result_usage: dict[str, object] = {
            "inputTokens": tokens_in,
            "outputTokens": tokens_out,
        }
        if tokens_in_cached is not None:
            usage_update["cachedInputTokens"] = tokens_in_cached
            result_usage["cachedInputTokens"] = tokens_in_cached
        self.send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": usage_update,
                },
            }
        )
        self._send_result(
            req_id,
            {
                "stopReason": "end_turn",
                "usage": result_usage,
            },
        )

    def _run_prompt(self, req_id: object, params: dict) -> None:
        try:
            self._execute_prompt(req_id, params)
        except Exception as exc:
            if self._shutdown_signal is None:
                try:
                    self._send_error(req_id, -32603, f"Internal bridge error: {exc}")
                except Exception:
                    pass

    def _execute_prompt(self, req_id: object, params: dict) -> None:
        sid = str(params.get("sessionId") or "")
        session = self._sessions.get(sid)
        if session is None:
            self._send_error(req_id, -32602, f"Unknown sessionId: {sid!r}")
            return

        prompt_text = extract_prompt_text(params.get("prompt") or [])

        with self._proc_lock:
            if sid in self._busy_sessions:
                busy = True
            else:
                self._busy_sessions.add(sid)
                self._cancelled_sessions.discard(sid)
                busy = False
        if busy:
            self._send_error(
                req_id,
                -32000,
                f"Session {sid!r} already has an active prompt",
                data={"kind": "refused", "code": "acp-session-busy"},
            )
            return

        proc: subprocess.Popen[bytes] | None = None
        stdout_b = b""
        stderr_b = b""
        was_cancelled = False
        spawned = False
        try:
            try:
                prepared = self._prepare_prompt(req_id, session, prompt_text)
            except confine_lib.ConfineError as exc:
                self._send_error(
                    req_id,
                    -32001,
                    exc.message,
                    data={"kind": "refused", "code": "acp-confine-unavailable"},
                )
                return
            if prepared is None:
                return
            argv, stdin_bytes = prepared
            try:
                # A CLI mid-update is missing for a moment: try again (v0.8.5).
                proc = popen_with_retry(
                    argv,
                    cwd=session["cwd"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
            except OSError as exc:
                self._send_error(req_id, -32603, f"Failed to spawn {self._cli_bin}: {exc}")
                return
            spawned = True

            with self._proc_lock:
                self._active_procs[sid] = proc
                already_cancelled = (
                    sid in self._cancelled_sessions
                    or self._shutdown_signal is not None
                )
            if already_cancelled:
                self._terminate_session_proc(sid)

            stdout_b, stderr_b = proc.communicate(input=stdin_bytes)
        finally:
            if proc is not None and proc.poll() is None:
                confine_lib.end_process_group(proc)
            with self._proc_lock:
                if proc is not None and self._active_procs.get(sid) is proc:
                    self._active_procs.pop(sid, None)
                was_cancelled = sid in self._cancelled_sessions
                self._cancelled_sessions.discard(sid)
                self._busy_sessions.discard(sid)
            self._cleanup_session_temp_dirs(session)

        if not spawned or self._shutdown_signal is not None:
            return
        if was_cancelled:
            self._send_result(req_id, {"stopReason": "cancelled"})
            return

        stdout_text = stdout_b.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_b.decode("utf-8", errors="replace").strip()
        self._process_prompt_output(
            req_id,
            sid,
            session,
            proc.returncode if proc is not None else None,
            stdout_text,
            stderr_text,
        )

    def _handle_line(
        self, line: str, prompt_threads: list[threading.Thread]
    ) -> bool:
        """Process one NDJSON line. Returns False when the serve loop should stop."""
        try:
            msg = json.loads(line)
        except ValueError:
            return True
        if not isinstance(msg, dict):
            return True
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
            prompt_threads[:] = [t for t in prompt_threads if t.is_alive()]
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
                session = self._sessions.pop(sid, None)
                if session is not None:
                    self._cleanup_session_temp_dirs(session)
            else:
                for active_sid in list(self._sessions):
                    self._terminate_session_proc(active_sid)
                    session = self._sessions.pop(active_sid, None)
                    if session is not None:
                        self._cleanup_session_temp_dirs(session)
            self._send_result(req_id, {})
            return False
        elif not self._dispatch_extra_method(method, req_id, params):
            self._send_error(req_id, -32601, f"Method not found: {method}")
        return True

    def _install_signal_handlers(
        self, wake_w: int | None
    ) -> dict[int, object]:
        """Register lock-free SIGTERM/SIGHUP handlers that wake the serve loop."""
        prev: dict[int, object] = {}
        if threading.current_thread() is not threading.main_thread():
            return prev

        def _handler(signum, _frame):
            # Never acquire _proc_lock inside a signal handler: record the
            # signal and nudge the wake pipe so serve()'s finally cleans up.
            self._shutdown_signal = signum
            if wake_w is not None:
                try:
                    os.write(wake_w, b"\x01")
                except OSError:
                    pass

        for sig in (signal.SIGTERM, signal.SIGHUP):
            try:
                prev[sig] = signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass
        return prev

    def serve(self) -> int:
        prompt_threads: list[threading.Thread] = []
        self._shutdown_signal = None
        stdin_fd: int | None = None
        try:
            cand_fd = sys.stdin.fileno()
            if isinstance(cand_fd, int) and cand_fd >= 0:
                stdin_fd = cand_fd
        except (AttributeError, OSError, ValueError):
            stdin_fd = None

        wake_r: int | None = None
        wake_w: int | None = None
        if stdin_fd is not None:
            wake_r, wake_w = os.pipe()
            os.set_blocking(wake_r, False)
            os.set_blocking(wake_w, False)

        prev_handlers = self._install_signal_handlers(wake_w)
        try:
            if stdin_fd is not None and wake_r is not None:
                buf = bytearray()
                eof = False
                while self._shutdown_signal is None:
                    nl = buf.find(b"\n")
                    if nl == -1:
                        if eof:
                            if buf:
                                line_bytes = bytes(buf)
                                buf.clear()
                            else:
                                break
                        else:
                            try:
                                readable, _, _ = select.select(
                                    [stdin_fd, wake_r], [], []
                                )
                            except InterruptedError:
                                continue
                            if self._shutdown_signal is not None:
                                break
                            if wake_r in readable:
                                try:
                                    os.read(wake_r, 64)
                                except OSError:
                                    pass
                                if self._shutdown_signal is not None:
                                    break
                            if stdin_fd in readable:
                                try:
                                    chunk = os.read(stdin_fd, 65536)
                                except InterruptedError:
                                    continue
                                if not chunk:
                                    eof = True
                                else:
                                    buf.extend(chunk)
                            continue
                    else:
                        line_bytes = bytes(buf[:nl])
                        del buf[: nl + 1]
                    line = line_bytes.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    if not self._handle_line(line, prompt_threads):
                        break
            else:
                while self._shutdown_signal is None:
                    raw = sys.stdin.readline()
                    if not raw or self._shutdown_signal is not None:
                        break
                    line = raw.strip()
                    if not line:
                        continue
                    if not self._handle_line(line, prompt_threads):
                        break
        finally:
            for sig, handler in prev_handlers.items():
                try:
                    signal.signal(sig, handler)
                except (ValueError, OSError):
                    pass
            self.end_all()
            for th in prompt_threads:
                if th.is_alive():
                    th.join(timeout=1.0)
            for fd in (wake_r, wake_w):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
        return 128 + self._shutdown_signal if self._shutdown_signal is not None else 0
