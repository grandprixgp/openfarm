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

#: Ops every adapter must answer. `hash`, `heartbeat` and `stale` are deliberately
#: absent: the spec gives each a fallback, so an adapter without one still runs.
REQUIRED_OPS = {
    "cards": {"hello", "next", "read", "claim", "move", "release", "finding"},
    "runner": {"hello", "exec"},
    "pack": {"hello", "commands", "parse_list", "parse_tests", "classify_paths",
             "signatures", "edit_formats", "apply_edit",
             "format", "format_check", "patterns", "error_signature"},
    "model": {"hello", "complete"},
    "telemetry": {"hello", "write", "flush"},
    "micromanager": {"hello", "assist"},
}

#: Ops an adapter may omit, because the spec names a fallback for each.
OPTIONAL_OPS = {
    "cards": {"hash", "heartbeat", "stale"},
    "pack": {"append_stub", "mutation", "parse_mutation"},
    "micromanager": set(),
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


#: Ops a run can do without, and what it falls back to.
FALLBACKS = {
    "hash": "SHA-256 of the canonical JSON that `read` returned",
    "heartbeat": "no lease renewal; the card source's own stale list",
    "stale": "no stale list; the card source's own",
}


def card_hash(call, card_id: str) -> str:
    """The card's hash, from the `hash` op or from the canonical `read` payload.

    An adapter that does not offer `hash` is not a reason to refuse the run: the
    spec names this fallback, and it is the same value for a card source whose
    `read` is stable.
    """
    from .cards import hash_value
    try:
        result = call("cards", "hash", {"id": card_id})
        if isinstance(result, dict) and result.get("hash"):
            return str(result["hash"])
    except Unsupported:
        pass
    read = call("cards", "read", {"id": card_id})
    return hash_value((read or {}).get("card", read))


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


def _safe_int(value) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class CmdCardSource:
    """External `cmd:<argv>` card source speaking the adapter protocol."""

    name = "cmd"

    def __init__(self, spec: str, cwd: Path, timeout_s: float = 600.0) -> None:
        self.adapter = CmdAdapter(spec, cwd, timeout_s=timeout_s)
        self.cwd = Path(cwd)
        self.worktree: Path | None = self.cwd

    def hello(self) -> dict:
        return self.adapter.hello("cards", REQUIRED_OPS["cards"])

    def prepare(self) -> Path:
        return self.worktree or self.cwd

    def next(self, lane: str = "", limit: int = 10) -> dict:
        return self.adapter.call("cards", "next", {"lane": lane, "limit": limit})

    def read(self, card_id: str) -> dict:
        return self.adapter.call("cards", "read", {"id": card_id})

    def hash(self, card_id: str) -> dict:
        return {"hash": card_hash(self.adapter.call, card_id)}

    def claim(self, card_id: str, holder: str,
              lease_ttl_s: float = 1200) -> dict:
        return self.adapter.call("cards", "claim", {
            "id": card_id, "holder": holder, "lease_ttl_s": lease_ttl_s})

    def heartbeat(self, card_id: str, lease_id: str,
                  lease_ttl_s: float = 1200) -> dict:
        try:
            return self.adapter.call("cards", "heartbeat", {
                "id": card_id, "lease_id": lease_id, "lease_ttl_s": lease_ttl_s})
        except Unsupported:
            return {}

    def move(self, card_id: str, lease_id: str, to: str, note_md: str = "",
             stop_code: str = "") -> dict:
        return self.adapter.call("cards", "move", {
            "id": card_id, "lease_id": lease_id, "to": to,
            "note_md": note_md, "stop_code": stop_code})

    def release(self, card_id: str, lease_id: str = "", reason: str = "",
                stop_code: str = "") -> dict:
        return self.adapter.call("cards", "release", {
            "id": card_id, "lease_id": lease_id, "reason": reason,
            "stop_code": stop_code})

    def finding(self, from_card: str, title: str, body_md: str = "") -> dict:
        return self.adapter.call("cards", "finding", {
            "from_card": from_card, "title": title, "body_md": body_md})

    def stale(self, lane: str = "") -> dict:
        try:
            return self.adapter.call("cards", "stale", {"lane": lane})
        except Unsupported:
            return {"cards": []}

    def all_cards(self) -> list:
        from .cards import Card
        try:
            result = self.adapter.call("cards", "all", {})
        except Unsupported:
            return []
        cards = []
        for entry in result.get("cards") or []:
            if isinstance(entry, Card):
                cards.append(entry)
            elif isinstance(entry, dict):
                cards.append(Card.from_dict(entry))
        return cards


class CmdModelBackend:
    """External `cmd:<argv>` model backend speaking the adapter protocol."""

    name = "cmd"

    def __init__(self, spec: str, cwd: Path, timeout_s: float = 600.0,
                 cost_per_1k_in: float = 0.0,
                 cost_per_1k_out: float = 0.0) -> None:
        self.adapter = CmdAdapter(spec, cwd, timeout_s=timeout_s)
        self.cost_per_1k_in = cost_per_1k_in
        self.cost_per_1k_out = cost_per_1k_out

    @property
    def prices_spend(self) -> bool:
        return self.cost_per_1k_in > 0 or self.cost_per_1k_out > 0

    def hello(self) -> dict:
        return self.adapter.hello("model", REQUIRED_OPS["model"])

    def complete(self, role: str, model: str, temperature: float,
                 messages: list[dict], seed: int | None = None,
                 max_tokens: int = 4096,
                 response_format: str | None = None,
                 card_id: str = "", attempt: int = 1):
        from .adapters.openai_compat import Completion
        result = self.adapter.call("model", "complete", {
            "role": role,
            "model": model,
            "temperature": temperature,
            "messages": list(messages),
            "seed": seed,
            "max_tokens": max_tokens,
            "response_format": response_format,
            "card_id": card_id,
            "attempt": attempt,
        })
        tokens_in = _safe_int(result.get("tokens_in"))
        tokens_out = _safe_int(result.get("tokens_out"))
        raw_cost = result.get("cost")
        if raw_cost is None:
            cost = (tokens_in / 1000.0 * self.cost_per_1k_in
                    + tokens_out / 1000.0 * self.cost_per_1k_out)
        else:
            cost = float(raw_cost)
        return Completion(
            content=str(result.get("content", "") or ""),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=str(result.get("model", model) or model),
            finish_reason=str(result.get("finish_reason", "stop") or "stop"),
            latency_s=float(result.get("latency_s", 0.0) or 0.0),
            cost=cost,
        )


class CmdTelemetry:
    """External `cmd:<argv>` telemetry adapter speaking the adapter protocol."""

    name = "cmd"

    def __init__(self, spec: str, cwd: Path, timeout_s: float = 60.0) -> None:
        self.adapter = CmdAdapter(spec, cwd, timeout_s=timeout_s)
        self.problems: list[str] = []

    def hello(self) -> dict:
        return self.adapter.hello("telemetry", REQUIRED_OPS["telemetry"])

    def write(self, stream: str, record: dict) -> dict:
        return self.adapter.call("telemetry", "write", {
            "stream": stream, "record": dict(record)})

    def flush(self) -> dict:
        return self.adapter.call("telemetry", "flush", {})


class CmdPack:
    """External `cmd:<argv>` language pack speaking the adapter protocol."""

    name = "cmd"

    def __init__(self, spec: str, cwd: Path | None = None,
                 cargo_flags: list[str] | None = None,
                 timeout_s: float = 300.0) -> None:
        base_cwd = Path(cwd) if cwd is not None else Path.cwd()
        self.adapter = CmdAdapter(spec, base_cwd, timeout_s=timeout_s)
        self.cwd = base_cwd
        self.cargo_flags = list(cargo_flags or [])
        self.notes: list[str] = []

    def hello(self) -> dict:
        return self.adapter.hello("pack", REQUIRED_OPS["pack"])

    def configure(self, worktree: Path) -> None:
        self.cwd = Path(worktree)
        self.adapter.cwd = Path(worktree)

    def commands(self, purpose: str, unit: str = "",
                 tests: list[str] | None = None,
                 files: list[str] | None = None,
                 extra_flags: list[str] | None = None) -> dict:
        # Before v0.6 the core appended `-- <names>` and `--test <stem>` to a test
        # command itself. External packs were written against that, so the test
        # purpose keeps it: the pack answers the plain command, and this adds them.
        appended: list[str] = []
        if purpose == "test" and (tests or files):
            for path in files or []:
                appended += ["--test", Path(str(path)).stem]
            if tests:
                appended += ["--", *tests]
            tests, files = None, None
        answer = self.adapter.call("pack", "commands", {
            "purpose": purpose,
            "unit": unit,
            "tests": list(tests or []),
            "files": list(files or []),
            "extra_flags": list(extra_flags or []),
        })
        if appended and isinstance(answer, dict) and isinstance(answer.get("argv"), list):
            answer = dict(answer)
            answer["argv"] = list(answer["argv"]) + appended
        return answer

    def parse_list(self, stdout: str) -> dict:
        return self.adapter.call("pack", "parse_list", {"stdout": stdout})

    def parse_tests(self, stdout: str, stderr: str = "") -> dict:
        return self.adapter.call("pack", "parse_tests", {
            "stdout": stdout, "stderr": stderr})

    def parse_mutation(self, stdout: str) -> dict:
        return self.adapter.call("pack", "parse_mutation", {"stdout": stdout})

    def error_signature(self, stderr: str) -> dict:
        return self.adapter.call("pack", "error_signature", {"stderr": stderr})

    def classify_paths(self, paths: list[str]) -> dict:
        return self.adapter.call("pack", "classify_paths", {"paths": list(paths)})

    def signatures(self, path: Path) -> dict:
        return self.adapter.call("pack", "signatures", {"path": str(path)})

    def edit_formats(self) -> dict:
        return self.adapter.call("pack", "edit_formats", {})

    def apply_edit(self, fmt: str, path: Path, edit: dict) -> dict:
        return self.adapter.call("pack", "apply_edit", {
            "format": fmt, "path": str(path), "edit": dict(edit)})

    def append_stub(self, path: Path, test_name: str, body: str) -> dict:
        return self.adapter.call("pack", "append_stub", {
            "path": str(path), "test_name": test_name, "body": body})

    def format(self, paths: list[Path]) -> dict:
        return self.adapter.call("pack", "format", {
            "paths": [str(p) for p in paths]})

    def format_check(self, paths: list[Path]) -> dict:
        return self.adapter.call("pack", "format_check", {
            "paths": [str(p) for p in paths]})

    def patterns(self, added_lines: list[dict],
                 flagged_patterns: list[str] | None = None,
                 production_paths: set[str] | None = None) -> dict:
        return self.adapter.call("pack", "patterns", {
            "added_lines": list(added_lines),
            "flagged_patterns": list(flagged_patterns or []),
            "production_paths": sorted(production_paths or ()),
        })

    def mutation(self, files: list[str],
                 tests: list[str] | None = None) -> dict:
        return self.adapter.call("pack", "mutation", {
            "files": list(files), "tests": list(tests or [])})
