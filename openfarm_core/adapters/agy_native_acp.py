"""Stdlib ACP v1 server bridge over native `agy -p --output-format json`.

Fallback transport for Antigravity when `--binary agy-print` is requested or
an external ACP server binary is not used directly.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

if __package__ in (None, ""):
    # Run as a script by the ACP adapter: make the package importable.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from openfarm_core import confine as confine_lib
from openfarm_core.adapters.native_acp_base import (
    PROTOCOL_VERSION,
    BaseNativeAcpServer,
    extract_prompt_text as _extract_prompt_text,
    parse_json_output as _parse_json_output,
)

# Linux's MAX_ARG_STRLEN: one argv word, NUL included, is at most 32 pages.
MAX_ARG_BYTES = 32 * 4096
_AUTH_HINT_RE = re.compile(
    r"not logged in|please run /login|authentication_failed|"
    r"authentication required|not authenticated|invalid.*token|"
    r"unauthorized|\b401\b|\boauth\b|agy acp --login|api.?key",
    re.IGNORECASE,
)


def _is_auth_failure(text: str) -> bool:
    return bool(_AUTH_HINT_RE.search(str(text or "")))


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


class NativeAgyAcpServer(BaseNativeAcpServer):
    """Minimal single-client NDJSON-RPC 2.0 ACP v1 bridge backed by ``agy -p``."""

    CAPABILITY_KEY = "nativeAgyCli"
    CLI_NAME = "agy"
    AUTH_HINT_RE = _AUTH_HINT_RE
    ACCEPTED_AUTH_METHODS = (
        "oauth",
        "oauth-personal",
        "api-key",
        "api_key",
        "gemini-api-key",
    )
    AUTH_METHODS = (
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
    )

    def __init__(self, agy_bin: str = "agy") -> None:
        super().__init__(cli_bin=agy_bin or "agy")
        self.agy_bin = self._cli_bin

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
                req_id,
                -32001,
                hold.refused,
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

    def _dispatch_extra_method(
        self, method: object, req_id: object, params: dict
    ) -> bool:
        if method == "session/set_config_option":
            self._handle_set_config_option(req_id, params)
            return True
        return False

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
        session_dir = session["cwd"]
        if str(session.get("tool_policy") or "none") == "none":
            empty = Path(tempfile.mkdtemp(prefix="openfarm-agy-empty-")).resolve()
            with self._proc_lock:
                session.setdefault("_temp_dirs", []).append(empty)
            session_dir = str(empty)
        return confine_lib.bridge_child_argv(
            argv, hold, session.get("confine_meta"), session_dir=session_dir
        )

    def _prepare_prompt(
        self, req_id: object, session: dict, prompt_text: str
    ) -> tuple[list[str], bytes] | None:
        sys_prompt = session["system_prompt"]
        if sys_prompt and prompt_text:
            full_prompt = f"{sys_prompt}\n\n{prompt_text}"
        else:
            full_prompt = sys_prompt or prompt_text

        size = len(full_prompt.encode("utf-8"))
        if size + 1 > MAX_ARG_BYTES:
            # agy takes the prompt as one argv word (-p); Linux refuses a word over
            # MAX_ARG_STRLEN with "[Errno 7] Argument list too long".
            self._send_error(
                req_id,
                -32602,
                f"the prompt is {size} bytes, but agy takes it as one command-line "
                f"argument (-p) and Linux caps one argument at {MAX_ARG_BYTES} bytes "
                f"(MAX_ARG_STRLEN, including its NUL); shorten the prompt below "
                f"{MAX_ARG_BYTES - 1} bytes",
                data={
                    "kind": "refused",
                    "code": "agy-prompt-too-large",
                    "bytes": size,
                    "limit": MAX_ARG_BYTES - 1,
                },
            )
            return None
        argv = self._build_agy_argv(session, full_prompt)
        return self._child_argv(session, argv), b""

    def _process_prompt_output(
        self,
        req_id: object,
        sid: str,
        session: dict,
        returncode: int | None,
        stdout_text: str,
        stderr_text: str,
    ) -> None:
        parsed = _parse_json_output(stdout_text)
        if parsed is None:
            msg = stderr_text or stdout_text or f"agy exited with code {returncode}"
            if self._is_auth_failure(msg):
                self._send_auth_error(req_id, msg)
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

        if is_error or returncode != 0:
            err_detail = ""
            if isinstance(raw_err, dict):
                err_detail = str(raw_err.get("message") or raw_err)
            elif raw_err:
                err_detail = str(raw_err)
            err_msg = (
                result_text
                or err_detail
                or stderr_text
                or f"agy failed (exit {returncode})"
            )
            if self._is_auth_failure(err_msg + "\n" + stderr_text):
                self._send_auth_error(req_id, err_msg)
            else:
                self._send_error(req_id, -32603, err_msg)
            return

        tokens_in, tokens_out, turn_cost = _extract_usage(parsed)
        self._emit_prompt_success(
            req_id,
            sid,
            session,
            result_text,
            tokens_in,
            tokens_out,
            turn_cost,
        )


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
