"""Stdlib ACP v1 NDJSON-RPC 2.0 over stdio client and Claude ACP dialect.

Implements the Agent Client Protocol (ACP) v1 stdio transport for Claude ACP
(`claude-agent-acp` by default) with strict security boundaries:

- Strips terminal control/escape sequences (including unterminated OSC) without
  truncating subsequent printable text.
- Sanitises inherited Claude/auth environment variables and optionally overlays an
  isolated ``0700`` ``HOME`` cleaned up on ``close()`` / ``__exit__()``.
- Enforces tool permissions strictly by option ``kind`` family, never selecting
  ``allow_always`` or ``*_always`` options, and enforcing containment inside
  ``allowed_root`` outside ``never_touch`` / ``forbidden_files`` for
  ``read-only`` sessions.
- Rejects unknown agent->client methods with JSON-RPC ``-32601``, duplicate
  request IDs with ``-32600``, declines ``elicitation/create``, and ignores
  history replay notifications/requests during ``session/load``.
"""

from __future__ import annotations

import collections
import dataclasses
import json
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from ..errors import Infra, Invalid, Refused
from ..globs import escapes, matches_any, normalise

PROTOCOL_VERSION = 1
DEFAULT_MAX_LINE_BYTES = 8 * 1024 * 1024  # 8 MiB
_POLL_INTERVAL_S = 0.05
_CANCEL_DRAIN_S = 1.5

_DISALLOWED_NONE = (
    "Bash",
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
)
_DISALLOWED_READ_ONLY = (
    "Bash",
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
)

# Control sequence strippers. Order matters: complete sequences first, then
# lone unclosed prefixes so subsequent printable text is never truncated.
_OSC_COMPLETE_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI_COMPLETE_RE = re.compile(r"(?:\x1b\[|\x9b)[0-9;?]*[A-Za-z]")
_LONE_PREFIX_RE = re.compile(r"\x1b\]|\x1b\[|\x1b\\|\x1b|\x9b")
_C0_C1_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

_STRIPPED_ENV_EXACT = frozenset({"CLAUDECODE", "ANTHROPIC_AUTH_TOKEN", "AI_AGENT"})
_PATH_INPUT_KEYS = (
    "path",
    "file_path",
    "filePath",
    "file",
    "target",
    "dir",
    "directory",
    "cwd",
    "root",
    "paths",
    "files",
)


def strip_control_sequences(text: str) -> str:
    """Strip ANSI CSI/SGR, C1, and OSC sequences without truncating printable text."""
    if not text:
        return ""
    cleaned = _OSC_COMPLETE_RE.sub("", str(text))
    cleaned = _CSI_COMPLETE_RE.sub("", cleaned)
    cleaned = _LONE_PREFIX_RE.sub("", cleaned)
    cleaned = _C0_C1_RE.sub("", cleaned)
    return cleaned


def _sanitised_env(
    *,
    base_env: dict[str, str] | None = None,
    extra_env: dict[str, str] | None = None,
    isolate_home: bool = True,
    auth_file: Path | str | None = None,
) -> tuple[dict[str, str], Path | None]:
    """Build a sanitised subprocess environment and optional isolated HOME dir."""
    source = os.environ if base_env is None else base_env
    orig_home = source.get("HOME", "")

    env: dict[str, str] = {}
    for key, val in source.items():
        if key in _STRIPPED_ENV_EXACT or key.startswith("CLAUDE_CODE_"):
            continue
        env[key] = val

    if extra_env:
        for key, val in extra_env.items():
            if key in _STRIPPED_ENV_EXACT or key.startswith("CLAUDE_CODE_"):
                continue
            env[str(key)] = str(val)

    isolated_home: Path | None = None
    if isolate_home:
        isolated_home = Path(tempfile.mkdtemp(prefix="openfarm-acp-home-"))
        os.chmod(isolated_home, 0o700)
        try:
            claude_dir = isolated_home / ".claude"
            claude_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(claude_dir, 0o700)

            settings: dict[str, object] = {"hooks": {}}
            if auth_file is not None:
                auth_path = Path(auth_file).expanduser().resolve()
                helper_path = claude_dir / "api-key-helper.sh"
                helper_path.write_text(
                    f"#!/bin/sh\ntr -d '\\r\\n' < {shlex.quote(str(auth_path))}\n",
                    encoding="utf-8",
                )
                os.chmod(helper_path, 0o700)
                settings["apiKeyHelper"] = str(helper_path)
            else:
                if orig_home:
                    creds_src = Path(orig_home) / ".claude" / ".credentials.json"
                    try:
                        if creds_src.is_file():
                            creds_dst = claude_dir / ".credentials.json"
                            shutil.copyfile(creds_src, creds_dst)
                            os.chmod(creds_dst, 0o600)
                    except OSError:
                        pass

            settings_path = claude_dir / "settings.json"
            settings_path.write_text(
                json.dumps(settings, indent=2) + "\n", encoding="utf-8"
            )
            os.chmod(settings_path, 0o600)
            env["HOME"] = str(isolated_home)
        except Exception:
            shutil.rmtree(isolated_home, ignore_errors=True)
            raise

    return env, isolated_home


@dataclasses.dataclass
class AcpUsage:
    """Token and vendor-reported cost accounting for an ACP turn."""

    tokens_in: int = 0
    tokens_out: int = 0
    vendor_cost_usd: float = 0.0

    def as_dict(self) -> dict:
        return {
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "vendor_cost_usd": self.vendor_cost_usd,
        }


@dataclasses.dataclass
class AcpResult:
    """Result of a completed ACP ``session/prompt`` turn."""

    text: str = ""
    stop_reason: str = ""
    session_id: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    vendor_cost_usd: float = 0.0
    latency_s: float = 0.0
    permission_events: list[dict] = dataclasses.field(default_factory=list)
    rejected_methods: list[dict] = dataclasses.field(default_factory=list)

    @property
    def usage(self) -> AcpUsage:
        return AcpUsage(
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            vendor_cost_usd=self.vendor_cost_usd,
        )

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "stop_reason": self.stop_reason,
            "session_id": self.session_id,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "vendor_cost_usd": self.vendor_cost_usd,
            "latency_s": self.latency_s,
            "permission_events": list(self.permission_events),
            "rejected_methods": list(self.rejected_methods),
        }


def _is_always_kind(kind: str) -> bool:
    low = str(kind or "").strip().lower()
    return low.startswith("allow_always") or low.endswith("_always")


def _find_option_by_kind(options: list, target_kind: str) -> dict | None:
    for opt in options:
        if not isinstance(opt, dict):
            continue
        kind = str(opt.get("kind", "")).strip()
        if _is_always_kind(kind):
            continue
        if kind == target_kind and "optionId" in opt:
            return opt
    return None


def _extract_chunk_text(content: object) -> str:
    if isinstance(content, dict):
        if "text" in content:
            return str(content.get("text") or "")
        return ""
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    if isinstance(content, str):
        return content
    return ""


def _extract_paths_from_tool_call(tool_call: dict) -> list[str]:
    paths: list[str] = []

    def _add(val: object) -> None:
        if isinstance(val, str) and val.strip():
            paths.append(val.strip())
        elif isinstance(val, list):
            for item in val:
                _add(item)

    for loc in tool_call.get("locations") or []:
        if isinstance(loc, dict):
            for key in ("path", "file", "file_path", "filePath"):
                if key in loc:
                    _add(loc[key])
        elif isinstance(loc, str):
            _add(loc)

    raw_input = tool_call.get("rawInput")
    if isinstance(raw_input, dict):
        for key in _PATH_INPUT_KEYS:
            if key in raw_input:
                _add(raw_input[key])
        # Catch any sneaky path-looking strings under non-standard keys.
        for key, val in raw_input.items():
            if key in _PATH_INPUT_KEYS:
                continue
            if isinstance(val, str) and (
                val.startswith("/")
                or val.startswith("~")
                or ".." in val.replace("\\", "/").split("/")
            ):
                _add(val)
    elif isinstance(raw_input, str) and raw_input.strip():
        if raw_input.startswith("/") or "/" in raw_input or "\\" in raw_input or ".." in raw_input:
            _add(raw_input)

    return paths


class ClaudeAcpAdapter:
    """ACP v1 NDJSON-RPC 2.0 client over stdio targeting Claude ACP."""

    name = "claude-acp"

    def __init__(
        self,
        command: str | list[str] = "claude-agent-acp",
        cwd: Path | str | None = None,
        *,
        isolate_home: bool = True,
        auth_file: Path | str | None = None,
        env: dict[str, str] | None = None,
        never_touch: list[str] | tuple[str, ...] = (),
        forbidden_files: list[str] | tuple[str, ...] = (),
        allowed_root: Path | str | None = None,
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
        stderr_tail_lines: int = 100,
    ) -> None:
        if isinstance(command, str):
            self.argv = shlex.split(command)
        else:
            self.argv = [str(part) for part in command]
        if not self.argv:
            raise Invalid("acp-bad-command", "ACP adapter command must not be empty")

        self.cwd = Path(cwd or Path.cwd())
        self.allowed_root = Path(allowed_root or self.cwd).resolve()
        self.isolate_home = bool(isolate_home)
        self.auth_file = Path(auth_file) if auth_file is not None else None
        self.extra_env = dict(env) if env is not None else None
        self.never_touch = list(never_touch or ())
        self.forbidden_files = list(forbidden_files or ())
        if ".openfarm/**" not in self.forbidden_files:
            self.forbidden_files.append(".openfarm/**")
        self.max_line_bytes = int(max_line_bytes)

        self._proc: subprocess.Popen[bytes] | None = None
        self._isolated_home: Path | None = None
        self._msg_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._stderr_tail: collections.deque[str] = collections.deque(
            maxlen=max(1, int(stderr_tail_lines))
        )
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._turn_lock = threading.Lock()

        self._req_counter = 0
        self._initialized = False
        self._replaying_history = False
        self.agent_capabilities: dict = {}
        self._session_policies: dict[str, str] = {}
        self._seen_request_ids: dict[str, set[tuple[type, object]]] = {}
        self._session_ids: list[str] = []

    def __enter__(self) -> "ClaudeAcpAdapter":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def hello(self) -> dict:
        return {
            "name": self.name,
            "versions": [PROTOCOL_VERSION],
            "ops": [
                "hello",
                "initialize",
                "session/new",
                "session/prompt",
                "session/cancel",
                "session/close",
            ],
        }

    # -- subprocess & transport -----------------------------------------------

    def _next_id(self) -> int:
        self._req_counter += 1
        return self._req_counter

    def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        if self._proc is not None and self._proc.poll() is not None:
            # Clean up previous dead process before respawning.
            self._cleanup_process_and_threads()

        self._msg_queue = queue.Queue()
        self._stderr_tail.clear()
        env, isolated_home = _sanitised_env(
            extra_env=self.extra_env,
            isolate_home=self.isolate_home,
            auth_file=self.auth_file,
        )
        self._isolated_home = isolated_home
        try:
            proc = subprocess.Popen(
                self.argv,
                cwd=str(self.cwd),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                start_new_session=True,
            )
        except OSError as exc:
            self._cleanup_isolated_home()
            raise Infra(
                "acp-spawn-failed",
                f"{self.argv[0]} could not start: {exc}",
            ) from exc

        self._proc = proc
        self._stdout_thread = threading.Thread(
            target=self._read_stdout, args=(proc,), daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, args=(proc,), daemon=True
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _read_stdout(self, proc: subprocess.Popen[bytes]) -> None:
        stdout = proc.stdout
        if stdout is None:
            self._msg_queue.put(("_eof", None))
            return
        limit = self.max_line_bytes
        while True:
            try:
                raw = stdout.readline(limit + 1)
            except (OSError, ValueError):
                self._msg_queue.put(("_eof", None))
                break
            if not raw:
                self._msg_queue.put(("_eof", None))
                break
            if len(raw) > limit or (len(raw) == limit and not raw.endswith(b"\n")):
                self._msg_queue.put(("_oversized", len(raw)))
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self._msg_queue.put(("_bad_json", line))
                continue
            if not isinstance(msg, dict):
                self._msg_queue.put(("_bad_json", line))
                continue
            self._msg_queue.put(("_json", msg))

    def _read_stderr(self, proc: subprocess.Popen[bytes]) -> None:
        stderr = proc.stderr
        if stderr is None:
            return
        while True:
            try:
                raw = stderr.readline(65536)
            except (OSError, ValueError):
                break
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line:
                self._stderr_tail.append(line)

    def _stderr_summary(self) -> str:
        if not self._stderr_tail:
            return ""
        return " | ".join(list(self._stderr_tail)[-10:])[:600]

    def _send_json(self, payload: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise Infra("acp-not-running", "ACP subprocess is not running")
        data = (
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
            + b"\n"
        )
        with self._write_lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except OSError as exc:
                raise Infra(
                    "acp-pipe-closed", f"failed writing to ACP stdin: {exc}"
                ) from exc

    def _terminate_process_group(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.terminate()
                except OSError:
                    pass
            try:
                proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        proc.kill()
                    except OSError:
                        pass
                try:
                    proc.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    pass

    def _cleanup_process_and_threads(self) -> None:
        proc = self._proc
        if proc is not None:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            if proc.poll() is None:
                self._terminate_process_group()
            else:
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
        for th in (self._stdout_thread, self._stderr_thread):
            if th is not None and th.is_alive():
                th.join(timeout=1.0)
        self._stdout_thread = None
        self._stderr_thread = None
        self._proc = None

    def _cleanup_isolated_home(self) -> None:
        if self._isolated_home is not None:
            shutil.rmtree(self._isolated_home, ignore_errors=True)
            self._isolated_home = None

    def close(self) -> None:
        """Close active ACP session, terminate child process, and clean temp HOME."""
        try:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                for sid in list(self._session_ids):
                    try:
                        self._send_json(
                            {
                                "jsonrpc": "2.0",
                                "method": "session/close",
                                "params": {"sessionId": sid},
                            }
                        )
                    except (Infra, OSError):
                        break
                if proc.stdin is not None:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self._terminate_process_group()
            self._cleanup_process_and_threads()
        finally:
            self._initialized = False
            self._session_ids.clear()
            self._cleanup_isolated_home()

    # -- ACP RPC methods ------------------------------------------------------

    def _wait_simple_response(
        self,
        req_id: int,
        timeout_s: float,
        *,
        reject_requests: bool = False,
        ignore_updates: bool = False,
    ) -> dict:
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._terminate_process_group()
                raise Infra(
                    "acp-timeout",
                    f"ACP request {req_id} timed out after {timeout_s:.1f}s",
                )
            try:
                kind, item = self._msg_queue.get(
                    timeout=min(_POLL_INTERVAL_S, remaining)
                )
            except queue.Empty:
                continue

            if kind == "_eof":
                raise Infra(
                    "acp-eof",
                    f"ACP subprocess closed stdout unexpectedly: {self._stderr_summary()}",
                )
            if kind == "_oversized":
                self._terminate_process_group()
                raise Infra(
                    "acp-oversized",
                    f"ACP message exceeded {self.max_line_bytes} bytes ({item})",
                )
            if kind == "_bad_json":
                raise Infra("acp-bad-json", f"ACP emitted invalid JSON: {str(item)[:200]}")

            msg = item
            assert isinstance(msg, dict)

            if "method" in msg and "id" in msg and msg["id"] is not None:
                # Agent -> Client request during initialize / session/new / session/load.
                in_id = msg["id"]
                if reject_requests or self._replaying_history:
                    self._send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": in_id,
                            "error": {
                                "code": -32600,
                                "message": "Requests rejected during session replay",
                            },
                        }
                    )
                else:
                    self._send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": in_id,
                            "error": {
                                "code": -32601,
                                "message": f"Method not found: {msg.get('method')}",
                            },
                        }
                    )
                continue

            if "method" in msg:
                # Notification
                if ignore_updates or self._replaying_history:
                    continue
                continue

            if msg.get("id") == req_id:
                if msg.get("error") is not None:
                    err = msg["error"]
                    if isinstance(err, dict):
                        code = err.get("code", -1)
                        message = str(err.get("message", "ACP error"))
                    else:
                        code = -1
                        message = str(err)
                    raise Infra(f"acp-rpc-{code}", message)
                result = msg.get("result")
                return result if isinstance(result, dict) else {}

    def initialize(self, timeout_s: float = 30.0) -> dict:
        self._ensure_started()
        req_id = self._next_id()
        self._send_json(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientCapabilities": {},
                },
            }
        )
        result = self._wait_simple_response(req_id, timeout_s=timeout_s)
        if result.get("protocolVersion") != PROTOCOL_VERSION:
            raise Invalid(
                "acp-protocol-version",
                f"unsupported ACP protocolVersion: {result.get('protocolVersion')!r}",
            )
        caps = result.get("agentCapabilities")
        self.agent_capabilities = caps if isinstance(caps, dict) else {}
        self._initialized = True
        return result

    def new_session(
        self,
        cwd: Path | str | None = None,
        system_prompt: str = "",
        model: str = "",
        effort: str = "",
        tool_policy: str = "none",
        timeout_s: float = 30.0,
    ) -> str:
        if tool_policy not in ("none", "read-only"):
            raise Invalid(
                "acp-bad-tool-policy",
                f"tool_policy must be 'none' or 'read-only', got {tool_policy!r}",
            )
        if not self._initialized:
            self.initialize(timeout_s=timeout_s)

        options: dict[str, object] = {}
        if model:
            options["model"] = str(model)
        if effort:
            options["effort"] = str(effort)
        if tool_policy == "none":
            options["disallowedTools"] = list(_DISALLOWED_NONE)
        else:
            options["disallowedTools"] = list(_DISALLOWED_READ_ONLY)

        meta: dict[str, object] = {"claudeCode": {"options": options}}
        if system_prompt:
            meta["systemPrompt"] = {"append": str(system_prompt)}

        session_cwd = str(Path(cwd) if cwd is not None else self.cwd)
        req_id = self._next_id()
        self._send_json(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "session/new",
                "params": {
                    "cwd": session_cwd,
                    "mcpServers": [],
                    "_meta": meta,
                },
            }
        )
        result = self._wait_simple_response(req_id, timeout_s=timeout_s)
        session_id = str(result.get("sessionId") or "")
        if not session_id:
            raise Invalid("acp-no-session-id", "session/new returned no sessionId")
        self._session_policies[session_id] = tool_policy
        self._seen_request_ids.setdefault(session_id, set())
        if session_id not in self._session_ids:
            self._session_ids.append(session_id)
        return session_id

    def load_session(
        self,
        session_id: str,
        cwd: Path | str | None = None,
        tool_policy: str = "none",
        timeout_s: float = 30.0,
    ) -> dict:
        if tool_policy not in ("none", "read-only"):
            raise Invalid(
                "acp-bad-tool-policy",
                f"tool_policy must be 'none' or 'read-only', got {tool_policy!r}",
            )
        if not self._initialized:
            self.initialize(timeout_s=timeout_s)

        session_cwd = str(Path(cwd) if cwd is not None else self.cwd)
        req_id = self._next_id()
        self._replaying_history = True
        try:
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "session/load",
                    "params": {
                        "sessionId": session_id,
                        "cwd": session_cwd,
                        "mcpServers": [],
                    },
                }
            )
            result = self._wait_simple_response(
                req_id,
                timeout_s=timeout_s,
                reject_requests=True,
                ignore_updates=True,
            )
        finally:
            self._replaying_history = False

        self._session_policies[session_id] = tool_policy
        self._seen_request_ids.setdefault(session_id, set())
        if session_id not in self._session_ids:
            self._session_ids.append(session_id)
        return result

    def resume_session(
        self,
        session_id: str,
        cwd: Path | str | None = None,
        tool_policy: str = "none",
        timeout_s: float = 30.0,
    ) -> dict:
        return self.load_session(
            session_id, cwd=cwd, tool_policy=tool_policy, timeout_s=timeout_s
        )

    # -- permission & request enforcement -------------------------------------

    def _is_path_allowed_readonly(self, raw_path: str) -> tuple[bool, str]:
        text = str(raw_path).strip()
        if not text:
            return False, "empty-path"

        candidate = Path(text)
        if not candidate.is_absolute():
            lexical_norm = normalise(text)
            if escapes(lexical_norm):
                return False, "path-escape"
            if matches_any(lexical_norm, self.forbidden_files):
                return False, "forbidden-file"
            if matches_any(lexical_norm, self.never_touch):
                return False, "never-touch"
            resolved = (self.allowed_root / lexical_norm).resolve()
        else:
            resolved = candidate.resolve()

        root = self.allowed_root.resolve()
        if resolved != root and root not in resolved.parents:
            return False, "outside-allowed-root"

        rel = (
            "."
            if resolved == root
            else resolved.relative_to(root).as_posix()
        )
        if matches_any(rel, self.forbidden_files):
            return False, "forbidden-file"
        if matches_any(rel, self.never_touch):
            return False, "never-touch"
        return True, "ok"

    def _handle_permission(
        self,
        in_id: object,
        params: dict,
        tool_policy: str,
        permission_events: list[dict],
    ) -> None:
        options = params.get("options")
        if not isinstance(options, list):
            options = []
        tool_call = params.get("toolCall")
        if not isinstance(tool_call, dict):
            tool_call = {}

        tool_kind = str(tool_call.get("kind", "")).strip().lower()
        extracted_paths = _extract_paths_from_tool_call(tool_call)

        allow = False
        reason = "policy-none"
        if tool_policy == "read-only":
            if tool_kind not in ("read", "search"):
                allow = False
                reason = f"tool-kind-{tool_kind or 'unknown'}-disallowed"
            elif tool_kind == "read" and not extracted_paths:
                allow = False
                reason = "read-missing-path"
            else:
                all_ok = True
                for p in extracted_paths:
                    ok, why = self._is_path_allowed_readonly(p)
                    if not ok:
                        all_ok = False
                        reason = why
                        break
                if all_ok:
                    allow = True
                    reason = "read-only-contained"

        selected_opt = None
        if allow:
            selected_opt = _find_option_by_kind(options, "allow_once")
            if selected_opt is None:
                allow = False
                reason = "no-allow-once-option"

        if allow and selected_opt is not None:
            outcome = {
                "outcome": "selected",
                "optionId": selected_opt["optionId"],
            }
            decision = "allow_once"
            option_id = selected_opt["optionId"]
        else:
            reject_opt = _find_option_by_kind(options, "reject_once")
            if reject_opt is not None:
                outcome = {
                    "outcome": "selected",
                    "optionId": reject_opt["optionId"],
                }
                decision = "reject_once"
                option_id = reject_opt["optionId"]
            else:
                outcome = {"outcome": "cancelled"}
                decision = "cancelled"
                option_id = None

        permission_events.append(
            {
                "id": in_id,
                "tool_kind": tool_kind,
                "tool_policy": tool_policy,
                "decision": decision,
                "reason": reason,
                "paths": extracted_paths,
                "option_id": option_id,
            }
        )
        self._send_json(
            {"jsonrpc": "2.0", "id": in_id, "result": {"outcome": outcome}}
        )

    def _cancel_and_terminate(self, session_id: str, req_id: int) -> None:
        try:
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "method": "session/cancel",
                    "params": {"sessionId": session_id},
                }
            )
        except (Infra, OSError):
            pass

        drain_deadline = time.monotonic() + _CANCEL_DRAIN_S
        while True:
            rem = drain_deadline - time.monotonic()
            if rem <= 0:
                break
            try:
                kind, item = self._msg_queue.get(timeout=min(_POLL_INTERVAL_S, rem))
            except queue.Empty:
                continue
            if kind in ("_eof", "_oversized"):
                break
            if kind == "_json" and isinstance(item, dict) and item.get("id") == req_id:
                break

        self._terminate_process_group()

    # -- prompt turn ----------------------------------------------------------

    def prompt(
        self,
        session_id: str,
        text: str,
        timeout_s: float = 180.0,
        cancel_check: Callable[[], bool] | None = None,
    ) -> AcpResult:
        if not self._turn_lock.acquire(blocking=False):
            raise Refused(
                "acp-concurrent-prompt",
                "only one active prompt turn is permitted at a time",
            )
        try:
            return self._prompt_locked(
                session_id=session_id,
                text=text,
                timeout_s=timeout_s,
                cancel_check=cancel_check,
            )
        finally:
            self._turn_lock.release()

    def _prompt_locked(
        self,
        *,
        session_id: str,
        text: str,
        timeout_s: float,
        cancel_check: Callable[[], bool] | None,
    ) -> AcpResult:
        if self._proc is None or self._proc.poll() is not None:
            raise Infra("acp-not-running", "ACP subprocess is not running")

        req_id = self._next_id()
        started = time.monotonic()
        deadline = started + timeout_s
        tool_policy = self._session_policies.get(session_id, "none")
        seen_ids = self._seen_request_ids.setdefault(session_id, set())

        self._send_json(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
            }
        )

        chunks: list[str] = []
        tokens_in = 0
        tokens_out = 0
        vendor_cost_usd = 0.0
        permission_events: list[dict] = []
        rejected_methods: list[dict] = []

        while True:
            if cancel_check is not None and cancel_check():
                self._cancel_and_terminate(session_id, req_id)
                raise Refused("acp-cancelled", "ACP prompt turn was cancelled")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._cancel_and_terminate(session_id, req_id)
                raise Infra(
                    "acp-timeout",
                    f"ACP prompt turn timed out after {timeout_s:.1f}s",
                )

            try:
                kind, item = self._msg_queue.get(
                    timeout=min(_POLL_INTERVAL_S, remaining)
                )
            except queue.Empty:
                continue

            if kind == "_eof":
                raise Infra(
                    "acp-eof",
                    f"ACP subprocess exited before PromptResponse: {self._stderr_summary()}",
                )
            if kind == "_oversized":
                self._terminate_process_group()
                raise Infra(
                    "acp-oversized",
                    f"ACP line exceeded {self.max_line_bytes} bytes ({item})",
                )
            if kind == "_bad_json":
                raise Infra("acp-bad-json", f"ACP emitted invalid JSON: {str(item)[:200]}")

            msg = item
            assert isinstance(msg, dict)

            # Agent -> Client request (has both method and non-null id).
            if "method" in msg and "id" in msg and msg["id"] is not None:
                in_id = msg["id"]
                method = str(msg.get("method") or "")
                params = msg.get("params")
                if not isinstance(params, dict):
                    params = {}

                if self._replaying_history:
                    self._send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": in_id,
                            "error": {
                                "code": -32600,
                                "message": "Requests rejected during history replay",
                            },
                        }
                    )
                    rejected_methods.append(
                        {"id": in_id, "method": method, "code": -32600, "reason": "replay"}
                    )
                    continue

                id_key = (type(in_id), in_id)
                if id_key in seen_ids:
                    self._send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": in_id,
                            "error": {
                                "code": -32600,
                                "message": "Duplicate request id",
                            },
                        }
                    )
                    rejected_methods.append(
                        {
                            "id": in_id,
                            "method": method,
                            "code": -32600,
                            "reason": "duplicate-id",
                        }
                    )
                    continue
                seen_ids.add(id_key)

                if method == "session/request_permission":
                    self._handle_permission(
                        in_id, params, tool_policy, permission_events
                    )
                elif method == "elicitation/create":
                    permission_events.append(
                        {"id": in_id, "method": method, "decision": "decline"}
                    )
                    self._send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": in_id,
                            "result": {"action": "decline"},
                        }
                    )
                else:
                    rejected_methods.append(
                        {
                            "id": in_id,
                            "method": method,
                            "code": -32601,
                            "reason": "method-not-found",
                        }
                    )
                    self._send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": in_id,
                            "error": {
                                "code": -32601,
                                "message": f"Method not found: {method}",
                            },
                        }
                    )
                continue

            # Notification (method without id).
            if "method" in msg:
                if self._replaying_history:
                    continue
                if msg.get("method") == "session/update":
                    params = msg.get("params")
                    if not isinstance(params, dict):
                        continue
                    msg_sid = params.get("sessionId")
                    if msg_sid is not None and str(msg_sid) != session_id:
                        continue
                    update = params.get("update")
                    if not isinstance(update, dict):
                        update = params
                    upd_kind = update.get("sessionUpdate")
                    if upd_kind == "agent_message_chunk":
                        chunk = strip_control_sequences(
                            _extract_chunk_text(update.get("content"))
                        )
                        if chunk:
                            chunks.append(chunk)
                    elif upd_kind == "usage_update":
                        in_tok = _first_present(
                            update, ("inputTokens", "tokensIn", "promptTokens")
                        )
                        out_tok = _first_present(
                            update,
                            ("outputTokens", "tokensOut", "completionTokens"),
                        )
                        if in_tok is not None:
                            tokens_in += int(in_tok)
                        if out_tok is not None:
                            tokens_out += int(out_tok)
                        cost_val = update.get("cost")
                        if isinstance(cost_val, dict):
                            amt = cost_val.get("amount")
                            if amt is not None:
                                vendor_cost_usd += float(amt)
                        elif isinstance(cost_val, (int, float)) and not isinstance(
                            cost_val, bool
                        ):
                            vendor_cost_usd += float(cost_val)
                continue

            # JSON-RPC Response to our session/prompt request.
            if msg.get("id") == req_id:
                if msg.get("error") is not None:
                    err = msg["error"]
                    if isinstance(err, dict):
                        code = err.get("code", -1)
                        message = str(err.get("message", "ACP prompt error"))
                        data = err.get("data")
                        if isinstance(data, dict) and data.get("kind") == "refused":
                            raise Refused(str(data.get("code", "acp-refused")), message)
                    else:
                        code = -1
                        message = str(err)
                    raise Infra(f"acp-rpc-{code}", message)

                result = msg.get("result")
                if not isinstance(result, dict):
                    result = {}
                stop_reason = str(result.get("stopReason", ""))
                return AcpResult(
                    text="".join(chunks),
                    stop_reason=stop_reason,
                    session_id=session_id,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    vendor_cost_usd=vendor_cost_usd,
                    latency_s=time.monotonic() - started,
                    permission_events=permission_events,
                    rejected_methods=rejected_methods,
                )


def _first_present(mapping: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None
