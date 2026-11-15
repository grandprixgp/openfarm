"""The adapter protocol: one JSON request on stdin, one JSON response on stdout.

Built-in adapters speak the same messages in process. An external adapter is named
`cmd:<argv>` and gets exactly this envelope, so a repository can supply its own
without touching the core.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

from .errors import AdapterError, Infra, Invalid, Refused, Unsupported

PROTOCOL_VERSION = 1

#: Ops every adapter must answer.
REQUIRED_OPS = {
    "cards": {"hello", "next", "read", "hash", "claim", "move", "release",
              "finding", "stale"},
    "runner": {"hello", "exec"},
    "pack": {"hello", "commands", "parse_list", "parse_tests", "classify_paths",
             "signatures", "edit_formats", "apply_edit", "append_stub", "format",
             "format_check", "patterns", "error_signature", "mutation",
             "parse_mutation"},
    "model": {"hello", "complete"},
    "telemetry": {"hello", "write", "flush"},
}

ERROR_KINDS = ("refused", "infra", "invalid", "unsupported")


def make_request(op: str, args: dict, adapter: str,
                 request_id: str = "r-0") -> dict:
    return {"openfarm": PROTOCOL_VERSION, "adapter": adapter, "op": op,
            "request_id": request_id, "args": args}


def make_success(request_id: str, result: dict) -> dict:
    return {"openfarm": PROTOCOL_VERSION, "request_id": request_id, "ok": True,
            "result": result}


def make_error(request_id: str, kind: str, code: str, message: str,
               retry_after_s: float | None = None) -> dict:
    if kind not in ERROR_KINDS:
        raise Invalid("bad-error-kind", f"{kind!r} is not an adapter error kind")
    return {"openfarm": PROTOCOL_VERSION, "request_id": request_id, "ok": False,
            "error": {"kind": kind, "code": code, "message": message,
                      "retry_after_s": retry_after_s}}


def parse_response(payload) -> dict:
    """The result of a response, or the exception its error calls for."""
    if not isinstance(payload, dict):
        raise Invalid("not-an-object", "an adapter answer must be one JSON object")
    if "openfarm" not in payload:
        raise Invalid("no-version", "an adapter answer must carry `openfarm`")
    if payload.get("ok") is True:
        result = payload.get("result")
        return result if isinstance(result, dict) else {}
    if payload.get("ok") is False:
        error = payload.get("error") or {}
        kind = error.get("kind", "infra")
        code = str(error.get("code", ""))
        message = str(error.get("message", ""))
        retry = error.get("retry_after_s")
        cls = {"refused": Refused, "infra": Infra, "invalid": Invalid,
               "unsupported": Unsupported}.get(kind, Infra)
        raise cls(code, message, kind=kind, retry_after_s=retry)
    raise Invalid("no-ok", "an adapter answer must carry `ok`")


def check_hello(result: dict, adapter: str, needed: set[str]) -> None:
    """A run refuses to start if no version is shared or an op is missing."""
    versions = result.get("versions")
    if isinstance(versions, int):
        versions = [versions]
    if not isinstance(versions, list) or PROTOCOL_VERSION not in versions:
        raise Invalid("version-mismatch",
                      f"{adapter} adapter shares no protocol version with openfarm "
                      f"{PROTOCOL_VERSION} (it offers {versions!r})")
    ops = set(result.get("ops") or [])
    missing = needed - ops
    if missing:
        raise Invalid("missing-ops",
                      f"{adapter} adapter does not offer {', '.join(sorted(missing))}")


class CmdAdapter:
    """An external adapter: `cmd:<argv>`, one process per request."""

    def __init__(self, spec: str, cwd: Path, timeout_s: float = 600.0) -> None:
        text = spec[4:].strip() if spec.startswith("cmd:") else spec
        self.argv = shlex.split(text)
        if not self.argv:
            raise Invalid("bad-adapter", "cmd: adapter has no argv")
        self.cwd = Path(cwd)
        self.timeout_s = timeout_s
        self._counter = 0

    def call(self, adapter: str, op: str, args: dict) -> dict:
        self._counter += 1
        request = make_request(op, args, adapter, f"r-{self._counter:04d}")
        try:
            result = subprocess.run(
                self.argv, cwd=self.cwd, input=json.dumps(request),
                capture_output=True, text=True, timeout=self.timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise Infra("timeout", f"{self.argv[0]} timed out after "
                                   f"{self.timeout_s:.0f}s") from exc
        except OSError as exc:
            raise Infra("spawn-failed", f"{self.argv[0]} could not start: {exc}") from exc

        if result.returncode != 0:
            raise Infra("exit-code",
                        f"{self.argv[0]} exited {result.returncode}: "
                        f"{(result.stderr or '').strip()[:400]}")
        text = (result.stdout or "").strip()
        if not text:
            raise Infra("empty-output", f"{self.argv[0]} printed nothing")
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise Infra("bad-json",
                        f"{self.argv[0]} did not print one JSON object") from exc
        return parse_response(payload)

    def hello(self, adapter: str, needed: set[str]) -> dict:
        result = self.call(adapter, "hello", {})
        check_hello(result, adapter, needed)
        return result


def is_cmd_adapter(spec: str) -> bool:
    return str(spec).startswith("cmd:")


def error_from(exc: AdapterError) -> dict:
    return make_error("r-0", exc.kind, exc.code, exc.message, exc.retry_after_s)
