"""Shared append-only message board for v0.8 multi-member runs.

Each run has one board under ``<state_dir>/runs/<run_id>/board/`` containing:
- ``board.jsonl``: append-only numbered posts (``seq`` starting at 1, no gaps)
- ``members.json``: roster keyed by member name
- ``state.json``: run-end state (``ended``, ``ended_at``, ``reason``)
- ``.lock``: ``fcntl.LOCK_EX`` lock file paired with an in-process ``RLock``/``Condition``
- ``evidence/``: spilled long post bodies, answer JSON payloads, briefs, and artifacts
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from .errors import Refused
from .harness_memory import sanitize_lesson_text
from .state import StateDir, utc_now

VALID_MEMBER_KINDS: tuple[str, ...] = ("api", "acp", "code", "external", "scripted")
VALID_ROLES: tuple[str, ...] = (
    "coordinator",
    "supervisor",
    "planner",
    "test-writer",
    "implementer",
    "checker",
    "gates",
    "monitor",
)
VALID_MEMBER_STATES: tuple[str, ...] = ("active", "waiting", "removed")
VALID_OWNER_KINDS: tuple[str, ...] = ("", "human", "agent")
VALID_POST_KINDS: tuple[str, ...] = (
    "brief",
    "result",
    "gate",
    "defect",
    "question",
    "answer",
    "handback",
    "action",
    "done",
    "blocked",
    "stall",
    "roster",
    "note",
)

MAX_POST_BODY_LEN = 8000
_MODEL_KINDS = frozenset({"api", "acp", "scripted"})
_RESERVED_POST_KINDS = frozenset({"done", "gate"})
# Names only code writes under. A post from one of these is always code's own
# (posted with `trusted=True`), so readers can rely on `from == "gates"`.
CODE_AUTHOR_NAMES: frozenset[str] = frozenset({"gates", "monitor", "system"})
# Kinds an outside member may write by hand with `openfarm board post`.
OUTSIDE_POST_KINDS: tuple[str, ...] = ("note", "question", "defect", "result")
# Where every other kind comes from, for the refusal message.
_OUTSIDE_REFUSALS: dict[str, tuple[str, str]] = {
    "action": ("use-swarm-command", "is written by the checked swarm commands (openfarm swarm start/set/hint)"),
    "blocked": ("use-swarm-command", "is written by `openfarm swarm block`"),
    "roster": ("use-swarm-command", "is written by `openfarm swarm join` and `openfarm swarm remove`"),
    "answer": ("use-board-answer", "is written by `openfarm board answer`"),
    "brief": ("reserved-kind", "is reserved for code"),
    "gate": ("reserved-kind", "is reserved for code"),
    "done": ("reserved-kind", "is reserved for code"),
    "handback": ("reserved-kind", "is reserved for code"),
    "stall": ("reserved-kind", "is reserved for code"),
}
# Body of a coordinator's stop request on a running card (see `swarm_block`).
STOP_REQUEST_RE = re.compile(
    r"^stop requested for \S+ \(it keeps running until it ends\):\s*"
)
# C0 and C1 control characters other than tab and newline.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_ROLE_ALIASES: dict[str, str] = {
    "test_writer": "test-writer",
    "micromanager": "supervisor",
}
_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_.-])@([A-Za-z0-9_-]+)")
_MEMBER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

_USAGE_KEYS: tuple[str, ...] = ("calls", "tokens", "tokens_by_effort")

_REGISTRY_LOCK = threading.Lock()
_PROCESS_LOCKS: dict[Path, tuple[threading.RLock, threading.Condition]] = {}
_LOCK_DEPTHS: dict[Path, int] = {}

_TYPE_NAMES: dict[str, type | tuple[type, ...]] = {
    "str": str,
    "string": str,
    "int": int,
    "integer": int,
    "float": (int, float),
    "number": (int, float),
    "bool": bool,
    "boolean": bool,
    "list": list,
    "array": list,
    "dict": dict,
    "object": dict,
}


def _get_lock_pair(board_key: Path) -> tuple[threading.RLock, threading.Condition]:
    with _REGISTRY_LOCK:
        pair = _PROCESS_LOCKS.get(board_key)
        if pair is None:
            rlock = threading.RLock()
            pair = (rlock, threading.Condition(rlock))
            _PROCESS_LOCKS[board_key] = pair
            _LOCK_DEPTHS[board_key] = 0
        return pair


def normalize_role(role: str) -> str:
    """Normalize role aliases such as ``test_writer`` and ``micromanager``."""
    raw = str(role or "").strip().lower()
    if raw in _ROLE_ALIASES:
        return _ROLE_ALIASES[raw]
    dashed = raw.replace("_", "-")
    if dashed in _ROLE_ALIASES:
        return _ROLE_ALIASES[dashed]
    return dashed


def _strip_controls(text: str) -> str:
    return _CONTROL_RE.sub("", text)


def clean_post_body(body: str, *, max_len: int = MAX_POST_BODY_LEN) -> str:
    """Clean untrusted post text using ``harness_memory.sanitize_lesson_text``.

    Also drops C0/C1 control characters other than tab and newline (BEL, CR,
    backspace, ...), which the lesson cleaner leaves in. When dropping them
    changes the text, it is cleaned again, so a control character cannot hide
    a directive from the first pass.
    """
    text = str(body or "")
    cleaned = sanitize_lesson_text(text, max_len=0)
    stripped = _strip_controls(cleaned)
    if stripped != cleaned:
        stripped = _strip_controls(sanitize_lesson_text(stripped, max_len=0))
    if max_len > 0 and len(stripped) > max_len:
        if max_len < 3:
            return stripped[:max_len]
        return stripped[: max_len - 3].rstrip() + "..."
    return stripped


def board_dir_for(state_dir: StateDir | Path | str, run_id: str) -> Path:
    """Return ``<state_dir>/runs/<run_id>/board`` with safe run-id validation."""
    sd = state_dir if isinstance(state_dir, StateDir) else StateDir(Path(state_dir))
    return sd.run_dir(run_id) / "board"


def _atomic_write_text(path: Path, text: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return path


def _atomic_write_json(path: Path, payload: object) -> Path:
    return _atomic_write_text(
        path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    )


def _matches_type(value: object, expected: object) -> bool:
    if isinstance(expected, str):
        key = expected.strip().lower()
        target = _TYPE_NAMES.get(key)
        if target is None:
            raise Refused("invalid-expects", f"unsupported type expectation {expected!r}")
    elif isinstance(expected, type):
        target = expected
    elif isinstance(expected, tuple) and all(isinstance(t, type) for t in expected):
        target = expected
    else:
        raise Refused("invalid-expects", f"unsupported type expectation {expected!r}")

    if target is bool:
        return isinstance(value, bool)
    if target is int or target == (int, float):
        if isinstance(value, bool):
            return False
    return isinstance(value, target)


def _validate_field_spec(field: str, value: object, spec: object) -> None:
    if isinstance(spec, (str, type)):
        if not _matches_type(value, spec):
            raise Refused(
                "invalid-answer",
                f"field {field!r} must match type {spec!r}, got {type(value).__name__}",
            )
        return
    if isinstance(spec, (list, tuple, set)):
        if value not in spec:
            raise Refused(
                "invalid-answer",
                f"field {field!r} value {value!r} is not in {list(spec)!r}",
            )
        return
    if not isinstance(spec, dict):
        return

    if "type" in spec and spec["type"] is not None:
        if not _matches_type(value, spec["type"]):
            raise Refused(
                "invalid-answer",
                f"field {field!r} must match type {spec['type']!r}, got {type(value).__name__}",
            )
    allowed = spec.get("enum") or spec.get("values") or spec.get("allowed")
    if allowed is not None and value not in allowed:
        raise Refused(
            "invalid-answer",
            f"field {field!r} value {value!r} is not in {list(allowed)!r}",
        )
    if spec.get("non_empty"):
        if isinstance(value, str):
            if not value.strip():
                raise Refused("invalid-answer", f"field {field!r} must not be empty")
        elif not value:
            raise Refused("invalid-answer", f"field {field!r} must not be empty")
    if isinstance(value, list) and "items" in spec:
        item_spec = spec["items"]
        for idx, item in enumerate(value):
            _validate_field_spec(f"{field}[{idx}]", item, item_spec)


def validate_expected_answer(expects: dict, data: object) -> dict:
    """Validate an answer payload against a question's ``expects`` contract."""
    if not isinstance(data, dict):
        raise Refused("invalid-answer", "answer payload must be a JSON object")
    if not expects:
        return dict(data)
    if not isinstance(expects, dict):
        raise Refused("invalid-expects", "question expects must be a JSON object")

    required = list(expects.get("required") or ())
    for key in required:
        if key not in data or data[key] is None:
            raise Refused("invalid-answer", f"missing required field {key!r}")

    for group in expects.get("any_required") or ():
        group_keys = [str(k) for k in group]
        if group_keys and not any(k in data and data[k] is not None for k in group_keys):
            raise Refused(
                "invalid-answer",
                f"answer must include at least one of {group_keys!r}",
            )

    verdicts = expects.get("verdicts")
    if verdicts is None:
        verdicts = expects.get("allowed_verdicts")
    if verdicts is None and "verdict" in expects:
        raw_verdict = expects["verdict"]
        if isinstance(raw_verdict, (list, tuple, set)):
            verdicts = list(raw_verdict)
        elif isinstance(raw_verdict, str) and "|" in raw_verdict:
            verdicts = [part.strip() for part in raw_verdict.split("|") if part.strip()]
    if verdicts is not None:
        allowed_verdicts = list(verdicts)
        if "verdict" not in data or data["verdict"] not in allowed_verdicts:
            raise Refused(
                "invalid-answer",
                f"verdict {data.get('verdict')!r} must be one of {allowed_verdicts!r}",
            )

    for enum_key in ("enum", "values", "choices"):
        mapping = expects.get(enum_key)
        if isinstance(mapping, dict):
            for field, allowed in mapping.items():
                if field in data and data[field] not in allowed:
                    raise Refused(
                        "invalid-answer",
                        f"field {field!r} value {data[field]!r} must be one of {list(allowed)!r}",
                    )

    types_map = expects.get("types")
    if isinstance(types_map, dict):
        for field, expected_type in types_map.items():
            if field in data and data[field] is not None:
                _validate_field_spec(field, data[field], expected_type)

    props = expects.get("properties")
    if props is None:
        props = expects.get("fields")
    if isinstance(props, dict):
        for field, spec in props.items():
            if isinstance(spec, dict) and spec.get("required") and (
                field not in data or data[field] is None
            ):
                raise Refused("invalid-answer", f"missing required field {field!r}")
            if field in data and data[field] is not None:
                _validate_field_spec(field, data[field], spec)

    for field in expects.get("non_empty") or ():
        if field not in data or data[field] is None:
            raise Refused("invalid-answer", f"missing non-empty field {field!r}")
        val = data[field]
        if isinstance(val, str):
            if not val.strip():
                raise Refused("invalid-answer", f"field {field!r} must not be empty")
        elif not val:
            raise Refused("invalid-answer", f"field {field!r} must not be empty")

    allowed_keys = expects.get("allowed_keys")
    if allowed_keys is not None:
        extra = sorted(set(data.keys()) - set(allowed_keys))
        if extra:
            raise Refused("invalid-answer", f"unexpected answer keys: {extra!r}")
    elif expects.get("additional_properties") is False or expects.get("allow_unknown") is False:
        known = set(required)
        if verdicts is not None:
            known.add("verdict")
        if isinstance(types_map, dict):
            known.update(types_map.keys())
        if isinstance(props, dict):
            known.update(props.keys())
        for enum_key in ("enum", "values", "choices"):
            mapping = expects.get(enum_key)
            if isinstance(mapping, dict):
                known.update(mapping.keys())
        extra = sorted(set(data.keys()) - known)
        if extra:
            raise Refused("invalid-answer", f"unexpected answer keys: {extra!r}")

    return dict(data)


def _validate_member_name(name: str) -> str:
    clean = str(name or "").strip()
    if (
        not clean
        or clean in ("all", "*", ".", "..")
        or ".." in clean
        or not _MEMBER_NAME_RE.match(clean)
    ):
        raise Refused("bad-member-name", f"invalid board member name {name!r}")
    return clean


def _validate_member_fields(
    *,
    kind: str | None = None,
    role: str | None = None,
    state: str | None = None,
    owner_kind: str | None = None,
) -> tuple[str | None, str | None, str | None, str | None]:
    if kind is not None and kind not in VALID_MEMBER_KINDS:
        raise Refused("bad-member-kind", f"invalid member kind {kind!r}")
    norm_role = None
    if role is not None:
        norm_role = normalize_role(role)
        if norm_role not in VALID_ROLES:
            raise Refused("bad-member-role", f"invalid member role {role!r}")
    if state is not None and state not in VALID_MEMBER_STATES:
        raise Refused("bad-member-state", f"invalid member state {state!r}")
    if owner_kind is not None and owner_kind not in VALID_OWNER_KINDS:
        raise Refused("bad-owner-kind", f"invalid external owner_kind {owner_kind!r}")
    return kind, norm_role, state, owner_kind


def _post_addresses_member(post: dict, member: dict) -> bool:
    name = str(member.get("name", ""))
    role = normalize_role(member.get("role", ""))
    for target in post.get("to") or ():
        target_str = str(target or "").strip()
        if not target_str:
            continue
        if target_str == "all" or target_str == name:
            return True
        if role and normalize_role(target_str) == role:
            return True
    return False


def post_is_code(post: dict, members: dict[str, dict] | None = None) -> bool:
    """True when `post` was written as code (`Board.post(..., trusted=True)`).

    Posts record it in `trusted`. Boards written before that field existed
    fall back to the old reading: a fixed code name, or an author whose roster
    entry is a code member.
    """
    if "trusted" in post:
        return post.get("trusted") is True
    author = str(post.get("from", "") or "")
    if author in CODE_AUTHOR_NAMES:
        return True
    member = (members or {}).get(author)
    return bool(member and member.get("kind") == "code")


def is_stop_request(post: dict, members: dict[str, dict]) -> bool:
    """True for a `blocked` post written by a holder of the coordinator seat.

    Code's own posts (`gates`, `monitor`, `system`) are never requests: the
    card's final `blocked` is written by `gates`.
    """
    if post.get("kind") != "blocked" or not str(post.get("card", "") or ""):
        return False
    author = str(post.get("from", "") or "")
    if not author or author in CODE_AUTHOR_NAMES:
        return False
    member = members.get(author)
    if member is None:
        return False
    return normalize_role(str(member.get("role", "") or "")) == "coordinator"


def stop_request_reason(posts: list[dict], members: dict[str, dict], card_id: str) -> str:
    """Reason of the latest coordinator stop request for `card_id`, or ''."""
    for post in reversed(posts):
        if str(post.get("card", "") or "") != card_id or not is_stop_request(post, members):
            continue
        body = STOP_REQUEST_RE.sub("", str(post.get("body", "") or "").strip(), count=1)
        return body.strip() or "blocked by coordinator"
    return ""


class Board:
    """Thread- and process-safe append-only board under ``<run_dir>/board/``."""

    def __init__(
        self,
        board_dir: Path | str,
        state_dir: Path | str | StateDir | None = None,
    ) -> None:
        self.board_dir = Path(board_dir)
        if state_dir is None:
            self.state_dir: Path | None = None
        elif isinstance(state_dir, StateDir):
            self.state_dir = state_dir.root
        else:
            self.state_dir = Path(state_dir)
        self.posts_path = self.board_dir / "board.jsonl"
        self.members_path = self.board_dir / "members.json"
        self.state_path = self.board_dir / "state.json"
        self._ext_wait_lock = threading.Lock()
        self._ext_wait_count = 0
        self.lock_path = self.board_dir / ".lock"
        self.evidence_dir = self.board_dir / "evidence"

    @contextlib.contextmanager
    def _locked(self):
        key = self.board_dir.resolve()
        rlock, cond = _get_lock_pair(key)
        rlock.acquire()
        depth = _LOCK_DEPTHS.get(key, 0)
        if depth > 0:
            _LOCK_DEPTHS[key] = depth + 1
            try:
                yield cond
            finally:
                _LOCK_DEPTHS[key] = depth
                rlock.release()
            return

        self.board_dir.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            _LOCK_DEPTHS[key] = 1
            try:
                yield cond
            finally:
                _LOCK_DEPTHS[key] = 0
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            rlock.release()

    @contextlib.contextmanager
    def transaction(self):
        """Hold the board's lock across several calls, so a check and the write it
        decides are one step for every thread and process. Re-entrant: the
        board's own methods may be called inside it."""
        with self._locked():
            yield

    def _ensure_unlocked(self) -> None:
        self.board_dir.mkdir(parents=True, exist_ok=True)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        if not self.posts_path.exists():
            self.posts_path.write_text("", encoding="utf-8")
        if not self.members_path.exists():
            _atomic_write_json(self.members_path, {})
        if not self.state_path.exists():
            _atomic_write_json(
                self.state_path, {"ended": False, "ended_at": "", "reason": ""}
            )

    def ensure(self) -> Path:
        """Create board directory, files, and evidence directory if missing."""
        with self._locked():
            self._ensure_unlocked()
        return self.board_dir

    def _load_state_unlocked(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        return {
            "ended": bool(data.get("ended", False)),
            "ended_at": str(data.get("ended_at", "") or ""),
            "reason": str(data.get("reason", "") or ""),
        }

    def _is_ended_unlocked(self) -> bool:
        return bool(self._load_state_unlocked().get("ended", False))

    def is_ended(self) -> bool:
        if not self.board_dir.exists():
            return False
        with self._locked():
            return self._is_ended_unlocked()

    def end_run(self, reason: str = "finished") -> dict:
        """Mark the run ended and wake all threads waiting on the board."""
        with self._locked() as cond:
            self._ensure_unlocked()
            current = self._load_state_unlocked()
            if not current["ended"]:
                current = {
                    "ended": True,
                    "ended_at": utc_now(),
                    "reason": str(reason or "finished"),
                }
                _atomic_write_json(self.state_path, current)
            cond.notify_all()
            return dict(current)

    def _load_members_unlocked(self) -> dict[str, dict]:
        try:
            data = json.loads(self.members_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}

    def _save_members_unlocked(self, members: dict[str, dict]) -> dict[str, dict]:
        self._ensure_unlocked()
        _atomic_write_json(self.members_path, members)
        return {k: dict(v) for k, v in members.items()}

    def save_members(self, members: dict[str, dict]) -> dict[str, dict]:
        if not isinstance(members, dict):
            raise Refused("bad-members", "members roster must be a mapping")
        cleaned: dict[str, dict] = {}
        for key, raw in members.items():
            if not isinstance(raw, dict):
                raise Refused("bad-members", f"member entry {key!r} must be a mapping")
            name = _validate_member_name(key)
            entry = dict(raw)
            entry["name"] = name
            _validate_member_fields(
                kind=entry.get("kind"),
                role=entry.get("role"),
                state=entry.get("state", "active"),
                owner_kind=entry.get("owner_kind", ""),
            )
            if "role" in entry:
                entry["role"] = normalize_role(entry["role"])
            cleaned[name] = entry
        with self._locked():
            return self._save_members_unlocked(cleaned)

    def upsert_member(
        self,
        name: str,
        *,
        kind: str,
        role: str,
        card: str = "",
        tier: str = "",
        model: str = "",
        effort: str = "",
        state: str = "active",
        added_by: str = "system",
        owner_kind: str = "",
        last_read_seq: int | None = None,
    ) -> dict:
        clean_name = _validate_member_name(name)
        _, norm_role, _, _ = _validate_member_fields(
            kind=kind,
            role=role,
            state=state,
            owner_kind=owner_kind,
        )
        with self._locked():
            members = self._load_members_unlocked()
            existing = members.get(clean_name) or {}
            cursor = (
                int(existing.get("last_read_seq", 0) or 0)
                if last_read_seq is None
                else int(last_read_seq)
            )
            wait_since = (
                str(existing.get("wait_since", "") or "")
                if state == "waiting"
                else ""
            )
            if state == "waiting" and not wait_since:
                wait_since = utc_now()
            record = {
                "name": clean_name,
                "kind": kind,
                "role": norm_role,
                "card": str(card or ""),
                "tier": str(tier or ""),
                "model": str(model or ""),
                "effort": str(effort or ""),
                "state": state,
                "added_by": str(added_by or "system"),
                "owner_kind": str(owner_kind or ""),
                "last_read_seq": cursor,
                "wait_since": wait_since,
                "tokens_in": int(existing.get("tokens_in", 0) or 0),
                "tokens_out": int(existing.get("tokens_out", 0) or 0),
                "attempts": int(existing.get("attempts", 0) or 0),
            }
            # Usage counters survive a re-register (a re-join, a fresh start
            # of the same member), so the token report never loses calls.
            if existing:
                for key in _USAGE_KEYS:
                    if key in existing:
                        record[key] = (
                            dict(existing[key] or {})
                            if key == "tokens_by_effort"
                            else int(existing[key] or 0)
                        )
            members[clean_name] = record
            self._save_members_unlocked(members)
            return dict(record)

    def get_member(self, name: str) -> dict | None:
        if not self.board_dir.exists():
            return None
        with self._locked():
            member = self._load_members_unlocked().get(name)
            return dict(member) if member is not None else None

    def update_member(self, name: str, **fields) -> dict:
        with self._locked():
            members = self._load_members_unlocked()
            if name not in members:
                raise Refused("unknown-member", f"unknown board member {name!r}")
            member = dict(members[name])
            _validate_member_fields(
                kind=fields.get("kind"),
                role=fields.get("role"),
                state=fields.get("state"),
                owner_kind=fields.get("owner_kind"),
            )
            for key, value in fields.items():
                if key == "name":
                    continue
                if key == "role" and value is not None:
                    member[key] = normalize_role(value)
                elif key in ("last_read_seq", "tokens_in", "tokens_out", "attempts"):
                    member[key] = int(value or 0)
                else:
                    member[key] = value
            if "state" in fields:
                if member.get("state") == "waiting":
                    if "wait_since" not in fields and not member.get("wait_since"):
                        member["wait_since"] = utc_now()
                elif "wait_since" not in fields:
                    member["wait_since"] = ""
            members[name] = member
            self._save_members_unlocked(members)
            return dict(member)

    def add_member_usage(
        self,
        name: str,
        *,
        tokens_in: int = 0,
        tokens_out: int = 0,
        calls: int = 0,
        attempts: int = 0,
        effort: str = "",
    ) -> dict:
        """Atomically increment member usage counters under the board lock."""
        with self._locked():
            members = self._load_members_unlocked()
            if name not in members:
                raise Refused("unknown-member", f"unknown board member {name!r}")
            member = dict(members[name])
            new_in = int(member.get("tokens_in", 0) or 0) + int(tokens_in or 0)
            new_out = int(member.get("tokens_out", 0) or 0) + int(tokens_out or 0)
            new_calls = int(member.get("calls", 0) or 0) + int(calls or 0)
            new_attempts = max(
                0, int(member.get("attempts", 0) or 0) + int(attempts or 0)
            )
            by_eff = dict(member.get("tokens_by_effort") or {})
            eff_key = str(effort or "none")
            by_eff[eff_key] = (
                int(by_eff.get(eff_key, 0) or 0) + int(tokens_in or 0) + int(tokens_out or 0)
            )
            member["tokens_in"] = new_in
            member["tokens_out"] = new_out
            member["tokens"] = new_in + new_out
            member["calls"] = new_calls
            member["attempts"] = new_attempts
            member["tokens_by_effort"] = by_eff
            members[name] = member
            self._save_members_unlocked(members)
            return dict(member)

    def list_members(self, include_removed: bool = False) -> list[dict]:
        if not self.board_dir.exists():
            return []
        with self._locked():
            members = self._load_members_unlocked()
            out = [
                dict(m)
                for m in members.values()
                if include_removed or m.get("state") != "removed"
            ]
            return out

    def _load_posts_unlocked(self) -> list[dict]:
        """Every readable post, in file order.

        A torn or unparseable line (a crash mid-write, a stray byte) is skipped
        on its own; the posts after it are still read.
        """
        if not self.posts_path.is_file():
            return []
        posts: list[dict] = []
        for raw_line in self.posts_path.read_bytes().split(b"\n"):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(item, dict) and isinstance(item.get("seq"), int):
                posts.append(item)
        return posts

    def _last_seq_unlocked(self) -> int:
        if not self.posts_path.is_file():
            return 0
        raw = self.posts_path.read_bytes().rstrip(b"\r\n \t")
        if not raw:
            return 0
        idx = raw.rfind(b"\n")
        last_line = raw[idx + 1 :] if idx != -1 else raw
        try:
            record = json.loads(last_line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            record = None
        if isinstance(record, dict) and isinstance(record.get("seq"), int):
            return int(record["seq"])
        posts = self._load_posts_unlocked()
        return max((int(p["seq"]) for p in posts), default=0)

    def _append_record_unlocked(self, record: dict) -> None:
        """Append one post line, never gluing it onto a torn last line."""
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        if self.posts_path.is_file() and self.posts_path.stat().st_size > 0:
            with self.posts_path.open("rb") as check:
                check.seek(-1, os.SEEK_END)
                if check.read(1) != b"\n":
                    line = b"\n" + line
        with self.posts_path.open("ab") as handle:
            handle.write(line)
            handle.flush()

    def _validate_evidence_paths(self, evidence: list[str] | None) -> list[str]:
        if not evidence:
            return []
        board_root = self.board_dir.resolve()
        allowed_roots = [board_root]
        if self.state_dir is not None:
            allowed_roots.append(self.state_dir.resolve())

        validated: list[str] = []
        for raw_item in evidence:
            if not isinstance(raw_item, str) or not raw_item.strip() or "\x00" in raw_item:
                raise Refused("bad-evidence-path", f"invalid evidence path {raw_item!r}")
            item = raw_item.strip()
            candidate = Path(item)
            if ".." in candidate.parts:
                raise Refused(
                    "evidence-escape",
                    f"evidence path {item!r} contains disallowed '..'",
                )
            resolved = (
                candidate.resolve()
                if candidate.is_absolute()
                else (self.board_dir / candidate).resolve()
            )
            if not any(resolved.is_relative_to(root) for root in allowed_roots):
                raise Refused(
                    "evidence-escape",
                    f"evidence path {item!r} escapes allowed board/state directory",
                )
            validated.append(item)
        return validated

    def _resolve_recipients(
        self, to: list[str] | str, raw_body: str, members: dict[str, dict]
    ) -> list[str]:
        if isinstance(to, str):
            initial = [part.strip() for part in to.split(",") if part.strip()]
        else:
            initial = [str(part).strip() for part in (to or ()) if str(part).strip()]

        ordered: list[str] = []
        seen: set[str] = set()

        def _push(target: str) -> None:
            if target not in seen:
                seen.add(target)
                ordered.append(target)

        for target in initial:
            if target != "all" and target not in members:
                norm = normalize_role(target)
                if norm in VALID_ROLES:
                    target = norm
            _push(target)

        for match in _MENTION_RE.finditer(raw_body):
            mention = match.group(1)
            if mention == "all":
                _push("all")
            elif mention in members:
                _push(mention)
            else:
                norm = normalize_role(mention)
                if norm in VALID_ROLES:
                    _push(norm)

        return ordered

    def _post_unlocked(
        self,
        *,
        cond: threading.Condition,
        members: dict[str, dict],
        author: str,
        to: list[str] | str,
        kind: str,
        body: str,
        card: str,
        attempt: int,
        ref: int | None,
        evidence: list[str] | None,
        expects: dict | None,
        trusted: bool | None,
        advance_author_cursor: bool,
    ) -> dict:
        clean_author = str(author or "").strip()
        if not clean_author:
            raise Refused("bad-author", "post author must not be empty")
        if kind not in VALID_POST_KINDS:
            raise Refused("bad-post-kind", f"invalid post kind {kind!r}")
        if ref is not None and (isinstance(ref, bool) or not isinstance(ref, int) or ref < 1):
            raise Refused("bad-ref", f"post ref must be a positive integer, got {ref!r}")

        author_member = members.get(clean_author)
        # Only the caller says a post is code's own. Being named like a code
        # member (or registered as one) is never enough.
        author_is_code = trusted is True
        if kind in _RESERVED_POST_KINDS and not author_is_code:
            raise Refused(
                "reserved-kind",
                f"post kind {kind!r} is reserved for code members",
            )
        if clean_author in CODE_AUTHOR_NAMES and not author_is_code:
            raise Refused(
                "code-author",
                f"only code posts as {clean_author!r}",
            )

        evidence_list = self._validate_evidence_paths(evidence)
        raw_body = "" if body is None else str(body)
        recipients = self._resolve_recipients(to, raw_body, members)
        body_text = raw_body if author_is_code else clean_post_body(raw_body, max_len=0)

        self._ensure_unlocked()
        seq = self._last_seq_unlocked() + 1

        if len(body_text) > MAX_POST_BODY_LEN:
            spill_path = self.evidence_dir / f"post-{seq:04d}.txt"
            _atomic_write_text(spill_path, body_text)
            spill_str = str(spill_path)
            if spill_str not in evidence_list:
                evidence_list.append(spill_str)
            truncated = body_text[: MAX_POST_BODY_LEN - 3].rstrip()
            final_body = (
                f"{truncated}..." if truncated else body_text[:MAX_POST_BODY_LEN]
            )
        else:
            final_body = body_text

        record = {
            "seq": seq,
            "at": utc_now(),
            "from": clean_author,
            "to": recipients,
            "kind": kind,
            "card": str(card or ""),
            "attempt": int(attempt or 0),
            "ref": ref,
            "body": final_body,
            "evidence": evidence_list,
            "expects": dict(expects) if isinstance(expects, dict) else expects,
            "trusted": author_is_code,
        }

        self._append_record_unlocked(record)

        if advance_author_cursor and author_member is not None:
            current_cursor = int(author_member.get("last_read_seq", 0) or 0)
            if seq > current_cursor:
                members[clean_author]["last_read_seq"] = seq
                self._save_members_unlocked(members)

        cond.notify_all()
        return dict(record)

    def post(
        self,
        author: str,
        to: list[str] | str,
        kind: str,
        body: str,
        *,
        card: str = "",
        attempt: int = 0,
        ref: int | None = None,
        evidence: list[str] | None = None,
        expects: dict | None = None,
        trusted: bool | None = None,
        advance_author_cursor: bool = False,
    ) -> dict:
        with self._locked() as cond:
            members = self._load_members_unlocked()
            return self._post_unlocked(
                cond=cond,
                members=members,
                author=author,
                to=to,
                kind=kind,
                body=body,
                card=card,
                attempt=attempt,
                ref=ref,
                evidence=evidence,
                expects=expects,
                trusted=trusted,
                advance_author_cursor=advance_author_cursor,
            )

    def outside_post(
        self,
        author: str,
        to: list[str] | str,
        kind: str,
        body: str,
        *,
        card: str = "",
        attempt: int = 0,
        ref: int | None = None,
        evidence: list[str] | None = None,
    ) -> dict:
        """Post typed by an outside member (`openfarm board post`).

        The author must be a registered, not-removed, non-code member, and the
        kind one an outside member writes by hand (`OUTSIDE_POST_KINDS`). Every
        other kind has its own checked path: `action`, `blocked` and `roster`
        the `swarm` commands, `answer` `board answer`, the rest code only.
        The post is never trusted.
        """
        clean_author = str(author or "").strip()
        if kind not in VALID_POST_KINDS:
            raise Refused("bad-post-kind", f"invalid post kind {kind!r}")
        if kind not in OUTSIDE_POST_KINDS:
            code, where = _OUTSIDE_REFUSALS.get(
                kind, ("reserved-kind", "cannot be posted by hand")
            )
            raise Refused(
                code,
                f"post kind {kind!r} {where}; `board post` writes only "
                f"{', '.join(OUTSIDE_POST_KINDS)}",
            )
        if not clean_author:
            raise Refused("bad-author", "post author (--as) must not be empty")
        with self._locked() as cond:
            members = self._load_members_unlocked()
            member = members.get(clean_author)
            if clean_author in CODE_AUTHOR_NAMES or (
                member is not None and member.get("kind") == "code"
            ):
                raise Refused(
                    "code-author",
                    f"{clean_author!r} is a code member; only code posts as it",
                )
            if member is None:
                raise Refused(
                    "unknown-member",
                    f"{clean_author!r} is not a member of this run's board; "
                    "join a seat first (openfarm swarm join --name NAME --seat ...)",
                )
            if member.get("state") == "removed":
                raise Refused(
                    "removed-member", f"board member {clean_author!r} was removed"
                )
            return self._post_unlocked(
                cond=cond,
                members=members,
                author=clean_author,
                to=to,
                kind=kind,
                body=body,
                card=card,
                attempt=attempt,
                ref=ref,
                evidence=evidence,
                expects=None,
                trusted=False,
                advance_author_cursor=False,
            )

    def _clean_posts_for_viewer(
        self,
        posts: list[dict],
        members: dict[str, dict],
        *,
        viewer_member: dict | None,
        for_model: bool | None,
        viewer_kind: str,
    ) -> list[dict]:
        if for_model is True:
            should_clean = True
        elif for_model is False:
            should_clean = False
        else:
            member_kind = viewer_member.get("kind", "") if viewer_member else ""
            should_clean = viewer_kind in _MODEL_KINDS or member_kind in _MODEL_KINDS

        out: list[dict] = []
        for post in posts:
            item = dict(post)
            if should_clean and not post_is_code(item, members):
                item["body"] = clean_post_body(item.get("body", ""))
            out.append(item)
        return out

    def _matching_posts_unlocked(
        self,
        members: dict[str, dict],
        viewer_member: dict | None,
        *,
        effective_since: int,
        limit: int | None,
        for_model: bool | None,
        viewer_kind: str,
        newest: bool = False,
    ) -> list[dict]:
        posts = self._load_posts_unlocked()
        matched: list[dict] = []
        for post in posts:
            if int(post.get("seq", 0) or 0) <= effective_since:
                continue
            if viewer_member is not None and not _post_addresses_member(post, viewer_member):
                continue
            matched.append(post)
        if limit is not None:
            if newest:
                matched = matched[-limit:] if limit > 0 else []
            else:
                matched = matched[:limit]
        return self._clean_posts_for_viewer(
            matched,
            members,
            viewer_member=viewer_member,
            for_model=for_model,
            viewer_kind=viewer_kind,
        )

    def read(
        self,
        as_name: str | None = None,
        *,
        since: int | None = None,
        limit: int | None = 20,
        seqs: list[int] | None = None,
        for_model: bool | None = None,
        viewer_kind: str = "",
        advance_cursor: bool = True,
    ) -> list[dict]:
        if limit is not None and limit < 0:
            raise Refused("bad-limit", f"read limit must be >= 0, got {limit!r}")

        if not self.board_dir.exists():
            if as_name is not None and as_name != "*":
                raise Refused("unknown-member", f"unknown board member {as_name!r}")
            return []

        with self._locked():
            members = self._load_members_unlocked()
            viewer_member: dict | None = None
            if as_name is not None and as_name != "*":
                viewer_member = members.get(as_name)
                if viewer_member is None:
                    raise Refused("unknown-member", f"unknown board member {as_name!r}")

            if seqs is not None:
                seq_set = {int(s) for s in seqs}
                selected = [
                    post
                    for post in self._load_posts_unlocked()
                    if int(post.get("seq", 0) or 0) in seq_set
                ]
                return self._clean_posts_for_viewer(
                    selected,
                    members,
                    viewer_member=viewer_member,
                    for_model=for_model,
                    viewer_kind=viewer_kind,
                )

            if viewer_member is not None:
                effective_since = (
                    int(viewer_member.get("last_read_seq", 0) or 0)
                    if since is None
                    else int(since)
                )
            else:
                effective_since = 0 if since is None else int(since)
            # No cursor and no --since: someone looking at the board wants the
            # latest posts. Paging forward from a cursor or --since still gives
            # the oldest unread first.
            newest = viewer_member is None and since is None

            result = self._matching_posts_unlocked(
                members,
                viewer_member,
                effective_since=effective_since,
                limit=limit,
                for_model=for_model,
                viewer_kind=viewer_kind,
                newest=newest,
            )
            if viewer_member is not None and advance_cursor and result:
                new_cursor = max(
                    int(viewer_member.get("last_read_seq", 0) or 0),
                    int(result[-1]["seq"]),
                )
                if new_cursor != int(viewer_member.get("last_read_seq", 0) or 0):
                    members[as_name]["last_read_seq"] = new_cursor
                    self._save_members_unlocked(members)
            return result

    def wait(
        self,
        as_name: str,
        *,
        since: int | None = None,
        timeout_s: float = 1800.0,
        limit: int | None = None,
    ) -> dict:
        if limit is not None and limit < 0:
            raise Refused("bad-limit", f"wait limit must be >= 0, got {limit!r}")
        if not as_name or as_name == "*":
            raise Refused("unknown-member", f"unknown board member {as_name!r}")
        if not self.board_dir.exists():
            raise Refused("unknown-member", f"unknown board member {as_name!r}")

        key = self.board_dir.resolve()
        _, cond = _get_lock_pair(key)
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        entered_wait = False

        with cond:
            with self._locked():
                members = self._load_members_unlocked()
                member = members.get(as_name)
                if member is None:
                    raise Refused("unknown-member", f"unknown board member {as_name!r}")
                effective_since = (
                    int(member.get("last_read_seq", 0) or 0)
                    if since is None
                    else int(since)
                )
                posts = self._matching_posts_unlocked(
                    members,
                    member,
                    effective_since=effective_since,
                    limit=limit,
                    for_model=None,
                    viewer_kind="",
                )
                if posts:
                    return {
                        "woke": True,
                        "posts": posts,
                        "last_seq": int(posts[-1]["seq"]),
                    }
                if self._is_ended_unlocked():
                    return {"woke": False, "reason": "run-ended", "posts": []}
                if float(timeout_s) <= 0:
                    return {"woke": False, "reason": "timeout", "posts": []}

                members[as_name]["state"] = "waiting"
                members[as_name]["wait_since"] = utc_now()
                self._save_members_unlocked(members)
                entered_wait = True

            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return {"woke": False, "reason": "timeout", "posts": []}
                    cond.wait(min(0.05, remaining))
                    with self._locked():
                        members = self._load_members_unlocked()
                        member = members.get(as_name)
                        if member is None:
                            raise Refused(
                                "unknown-member", f"unknown board member {as_name!r}"
                            )
                        posts = self._matching_posts_unlocked(
                            members,
                            member,
                            effective_since=effective_since,
                            limit=limit,
                            for_model=None,
                            viewer_kind="",
                        )
                        if posts:
                            return {
                                "woke": True,
                                "posts": posts,
                                "last_seq": int(posts[-1]["seq"]),
                            }
                        if self._is_ended_unlocked():
                            return {"woke": False, "reason": "run-ended", "posts": []}
            finally:
                if entered_wait:
                    with self._locked():
                        members = self._load_members_unlocked()
                        if as_name in members:
                            members[as_name]["wait_since"] = ""
                            if members[as_name].get("state") != "removed":
                                members[as_name]["state"] = "active"
                            self._save_members_unlocked(members)

    def head_seq(self) -> int:
        if not self.board_dir.exists():
            return 0
        with self._locked():
            return self._last_seq_unlocked()

    def wait_after(self, since_seq: int, timeout_s: float) -> dict:
        """Wait until any post with seq > since_seq lands, run ends, or timeout."""
        key = self.board_dir.resolve()
        _, cond = _get_lock_pair(key)
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with cond:
            with self._locked():
                self._ensure_unlocked()
                cur = self._last_seq_unlocked()
                if cur > since_seq:
                    return {"woke": True, "head": cur}
                if self._is_ended_unlocked():
                    return {"woke": False, "reason": "run-ended", "head": cur}
                if float(timeout_s) <= 0:
                    return {"woke": False, "reason": "timeout", "head": cur}
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    with self._locked():
                        return {"woke": False, "reason": "timeout", "head": self._last_seq_unlocked()}
                cond.wait(min(0.05, remaining))
                with self._locked():
                    cur = self._last_seq_unlocked()
                    if cur > since_seq:
                        return {"woke": True, "head": cur}
                    if self._is_ended_unlocked():
                        return {"woke": False, "reason": "run-ended", "head": cur}

    def answer(
        self,
        question_seq: int,
        *,
        as_name: str,
        data: object,
        body: str = "",
    ) -> dict:
        if isinstance(question_seq, bool) or not isinstance(question_seq, int) or question_seq < 1:
            raise Refused(
                "bad-question-seq",
                f"question_seq must be a positive integer, got {question_seq!r}",
            )

        with self._locked() as cond:
            members = self._load_members_unlocked()
            member = members.get(as_name)
            if member is None:
                raise Refused("unknown-member", f"unknown board member {as_name!r}")
            if member.get("state") == "removed":
                raise Refused("removed-member", f"board member {as_name!r} was removed")

            posts = self._load_posts_unlocked()
            question = next(
                (p for p in posts if int(p.get("seq", 0) or 0) == question_seq),
                None,
            )
            if question is None:
                raise Refused(
                    "unknown-question", f"post #{question_seq} does not exist"
                )
            if question.get("kind") != "question":
                raise Refused(
                    "not-a-question",
                    f"post #{question_seq} has kind {question.get('kind')!r}, not 'question'",
                )
            already = any(
                p.get("kind") == "answer" and p.get("ref") == question_seq
                for p in posts
            )
            if already:
                raise Refused(
                    "already-answered",
                    f"question #{question_seq} has already been answered",
                )
            if not _post_addresses_member(question, member):
                raise Refused(
                    "not-addressee",
                    f"member {as_name!r} is not an addressee of question #{question_seq}",
                )

            validated = validate_expected_answer(question.get("expects") or {}, data)
            self._ensure_unlocked()
            ans_path = self.evidence_dir / f"answer-q{question_seq}.json"
            _atomic_write_json(ans_path, validated)

            if body:
                post_body = body
            else:
                compact = json.dumps(validated, ensure_ascii=False)
                post_body = (
                    compact if len(compact) <= 1000 else f"answer to #{question_seq}"
                )

            return self._post_unlocked(
                cond=cond,
                members=members,
                author=as_name,
                to=[str(question.get("from", ""))],
                kind="answer",
                body=post_body,
                card=str(question.get("card", "") or ""),
                attempt=int(question.get("attempt", 0) or 0),
                ref=question_seq,
                evidence=[str(ans_path)],
                expects=None,
                trusted=None,
                advance_author_cursor=False,
            )

    @contextlib.contextmanager
    def external_wait(self, *, card_id: str = "", member_name: str = ""):
        """Track an external-seat wait and park the card's roster members."""
        targets: list[str] = []
        if card_id:
            for member in self.list_members(include_removed=False):
                if member.get("card") == card_id and member.get("state") == "active":
                    targets.append(str(member["name"]))
        if member_name and member_name not in targets:
            existing = self.get_member(member_name)
            if existing is not None and existing.get("state") == "active":
                targets.append(member_name)
        with self._ext_wait_lock:
            self._ext_wait_count += 1
        for name in targets:
            try:
                self.update_member(name, state="waiting")
            except Refused:
                continue
        try:
            yield
        finally:
            for name in targets:
                current = self.get_member(name)
                if current is not None and current.get("state") == "waiting":
                    try:
                        self.update_member(name, state="active")
                    except Refused:
                        continue
            with self._ext_wait_lock:
                self._ext_wait_count = max(0, self._ext_wait_count - 1)

    def external_wait_count(self) -> int:
        with self._ext_wait_lock:
            return self._ext_wait_count

    def is_code_post(self, post: dict) -> bool:
        """True when `post` was written as code (see `post_is_code`)."""
        if "trusted" in post:
            return post.get("trusted") is True
        members = {}
        if self.board_dir.exists():
            with self._locked():
                members = self._load_members_unlocked()
        return post_is_code(post, members)

    def seat_of(self, name: str) -> str:
        """Seat a board name holds: 'coordinator', 'supervisor', or ''.

        A removed or unknown name holds no seat. Callers use this to decide
        whose posts may steer a run (start or stop a card, hint a worker).
        """
        member = self.get_member(str(name or "").strip())
        if member is None or member.get("state") == "removed":
            return ""
        role = normalize_role(str(member.get("role", "") or ""))
        return role if role in ("coordinator", "supervisor") else ""

    def stop_requested(self, card_id: str) -> str:
        """Reason the coordinator asked for `card_id` to stop, or ''.

        A stop request is a `blocked` post for the card written by the
        coordinator's seat (through `swarm block`). The card's own final
        `done` / `blocked` post is written by code as `gates` and is not a
        request. A running card checks this before each model call.
        """
        clean = str(card_id or "").strip()
        if not clean or not self.board_dir.exists():
            return ""
        with self._locked():
            posts = self._load_posts_unlocked()
            members = self._load_members_unlocked()
        return stop_request_reason(posts, members, clean)

    def status(self) -> dict:
        if not self.board_dir.exists():
            return {
                "posts": 0,
                "last_seq": 0,
                "ended": False,
                "members": 0,
                "unanswered_questions": 0,
            }
        with self._locked():
            posts = self._load_posts_unlocked()
            members = self._load_members_unlocked()
            state = self._load_state_unlocked()
            answered_refs = {
                int(p["ref"])
                for p in posts
                if p.get("kind") == "answer" and isinstance(p.get("ref"), int)
            }
            unanswered = sum(
                1
                for p in posts
                if p.get("kind") == "question"
                and int(p.get("seq", 0) or 0) not in answered_refs
            )
            active_members = sum(
                1 for m in members.values() if m.get("state") != "removed"
            )
            return {
                "posts": len(posts),
                "last_seq": int(posts[-1]["seq"]) if posts else 0,
                "ended": bool(state.get("ended", False)),
                "members": active_members,
                "unanswered_questions": unanswered,
            }
