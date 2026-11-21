"""The global harness library (v0.7): lessons proven in one project, shared with all.

It lives at ``[harness] global_dir`` (default ``~/.openfarm/global/harness``), outside
every repository. Projects never apply a global lesson straight from here: each
project imports it into its own library as a candidate, so it goes through that
project's own probe cycle and scoring. Projects report their scores back with
``report``; a lesson vetoed by enough projects, or with a negative combined score,
is retired here and dropped by every project on its next sync.

Opening a library never creates its folder. Only a write does, so a project that
never promotes a lesson leaves nothing behind.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import threading
from pathlib import Path

from .errors import Invalid
from .state import append_jsonl, read_json, utc_now, write_json

_GLOBAL_LOCKS_GUARD = threading.Lock()
_GLOBAL_LOCKS: dict[Path, threading.RLock] = {}
_GLOBAL_THREAD_DEPTHS = threading.local()


def _global_thread_lock(root: Path) -> threading.RLock:
    key = Path(root).resolve()
    with _GLOBAL_LOCKS_GUARD:
        lock = _GLOBAL_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _GLOBAL_LOCKS[key] = lock
        return lock


def _global_depth_map() -> dict[Path, int]:
    depths = getattr(_GLOBAL_THREAD_DEPTHS, "depths", None)
    if depths is None:
        depths = {}
        _GLOBAL_THREAD_DEPTHS.depths = depths
    return depths

# The fields of a project entry that travel to the global library. Evidence and
# card IDs stay behind: they name the source project's cards.
_TRAVELLING_FIELDS = ("title", "surface", "role", "language", "kind", "failure_class",
                      "tags", "cue", "lesson", "proposed_patch", "confidence")
_GID_RE = re.compile(r"^g-[0-9a-f]{10}$")
_STAT_KEYS = ("applied", "helped", "hurt", "neutral")


def default_global_dir() -> Path:
    """`OPENFARM_GLOBAL_HARNESS`, else `~/.openfarm/global/harness`."""
    env = os.environ.get("OPENFARM_GLOBAL_HARNESS", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / ".openfarm" / "global" / "harness").resolve()


def global_dir_for(harness_cfg) -> Path:
    raw = str(getattr(harness_cfg, "global_dir", "") or "").strip()
    return Path(raw) if raw else default_global_dir()


def global_id_for(entry: dict) -> str:
    """A stable id from what the lesson says and where it applies."""
    parts = [str(entry.get(k, "") or "") for k in
             ("surface", "role", "language", "kind", "failure_class")]
    parts.append(re.sub(r"\s+", " ", str(entry.get("cue", "")).strip().lower()))
    return "g-" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:10]


# -- rules the farm fills in (v0.7 cards) ---------------------------------------

def should_promote(entry: dict) -> bool:
    """Whether a project's own lesson has proven itself enough to go global.

    Decision 3: `scope == "general"`, `status == "active"`, `helped >= 3` and
    `hurt < helped`. Eligibility (decisions 6 and 7) is checked separately by
    `harness_scope.global_eligibility`.
    """
    stats = entry.get("stats") or {}
    helped = stats.get("helped", 0)
    hurt = stats.get("hurt", 0)
    return (
        entry.get("scope", "") == "general"
        and entry.get("status", "") == "active"
        and helped >= 3
        and hurt < helped
    )


def should_veto(stats: dict) -> bool:
    """Whether a project switches a global lesson off for itself (decision 5)."""
    return stats.get("hurt", 0) > stats.get("helped", 0)


def retire_reason(global_entry: dict) -> str:
    """Why a global lesson should be retired, or "" to keep it (decision 5)."""
    projects = global_entry.get("projects") or {}
    vetoed = sum(
        1 for project in projects.values() if project.get("vetoed", False)
    )
    if vetoed >= 2:
        return f"vetoed by {vetoed} projects"
    score = sum(
        project.get("helped", 0) - project.get("hurt", 0)
        for project in projects.values()
    )
    if score < 0:
        return f"combined score {score}"
    return ""


# -- the store ---------------------------------------------------------------------

class GlobalLibrary:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.entries_dir = self.root / "entries"
        self.ledger_path = self.root / "ledger.jsonl"
        self._lock_path = self.root / ".lock"

    @contextlib.contextmanager
    def _locked(self):
        key = self.root.resolve()
        tlock = _global_thread_lock(key)
        with tlock:
            depths = _global_depth_map()
            depth = depths.get(key, 0)
            if depth > 0:
                depths[key] = depth + 1
                try:
                    yield
                finally:
                    depths[key] = depth
                return
            self.root.mkdir(parents=True, exist_ok=True)
            with self._lock_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                depths[key] = 1
                try:
                    yield
                finally:
                    depths.pop(key, None)
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _path(self, gid: str) -> Path:
        if not _GID_RE.match(str(gid)):
            raise Invalid("unknown-global-entry", f"{gid!r} is not a global lesson id")
        return self.entries_dir / f"{gid}.json"

    def _save(self, entry: dict) -> dict:
        entry["updated_at"] = utc_now()
        write_json(self._path(entry["id"]), entry)
        return entry

    def _event(self, event: str, data: dict) -> None:
        append_jsonl(self.ledger_path, {"at": utc_now(), "event": event, **data})

    def get(self, gid: str) -> dict | None:
        data = read_json(self._path(gid))
        return data if isinstance(data, dict) else None

    def list_entries(self, status: str | None = None) -> list[dict]:
        if not self.entries_dir.is_dir():
            return []
        out = []
        for path in sorted(self.entries_dir.glob("g-*.json")):
            data = read_json(path)
            if isinstance(data, dict) and (status is None or data.get("status") == status):
                out.append(data)
        return out

    def read_events(self) -> list[dict]:
        if not self.ledger_path.is_file():
            return []
        events = []
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def status(self) -> dict:
        entries = self.list_entries()
        return {
            "dir": str(self.root),
            "active": sum(1 for e in entries if e.get("status") == "active"),
            "retired": sum(1 for e in entries if e.get("status") == "retired"),
            "entries": [{
                "id": e["id"], "status": e.get("status"), "surface": e.get("surface"),
                "scope": f"{e.get('language', '*')}/{e.get('kind', '*')}",
                "sources": [s.get("project") for s in e.get("sources") or ()],
                "projects": sorted((e.get("projects") or {}).keys()),
                "cue": e.get("cue", ""),
            } for e in entries],
        }

    def add(self, entry: dict, project: str) -> dict:
        """Promote a project lesson. A lesson already here gains the project as a source."""
        gid = global_id_for(entry)
        with self._locked():
            existing = self.get(gid)
            source = {"project": project, "entry_id": str(entry.get("id", "")),
                      "at": utc_now()}
            if existing is not None:
                if all(s.get("project") != project for s in existing.get("sources") or ()):
                    existing.setdefault("sources", []).append(source)
                    self._save(existing)
                    self._event("source-added", {"id": gid, "project": project})
                return existing
            record = {k: entry.get(k) for k in _TRAVELLING_FIELDS}
            record.update({"id": gid, "status": "active", "scope": "general",
                           "sources": [source], "projects": {},
                           "created_at": utc_now()})
            self._save(record)
            self._event("promoted", {"id": gid, "project": project,
                                     "entry_id": source["entry_id"]})
            return record

    def report(self, gid: str, project: str, stats: dict, *, vetoed: bool) -> dict | None:
        """Record one project's current scores for a lesson; retire it if the rules say so."""
        with self._locked():
            entry = self.get(gid)
            if entry is None:
                return None
            projects = entry.setdefault("projects", {})
            before = projects.get(project) or {}
            now = {k: int((stats or {}).get(k, 0) or 0) for k in _STAT_KEYS}
            now["vetoed"] = bool(vetoed or before.get("vetoed"))
            if now == before:
                return entry
            projects[project] = now
            if now["vetoed"] and not before.get("vetoed"):
                self._event("vetoed", {"id": gid, "project": project, **now})
            if entry.get("status") == "active":
                reason = retire_reason(entry)
                if reason:
                    entry["status"] = "retired"
                    entry["retire_reason"] = reason
                    self._event("retired", {"id": gid, "reason": reason})
            return self._save(entry)

    def retire(self, gid: str, reason: str = "manual") -> dict:
        with self._locked():
            entry = self.get(gid)
            if entry is None:
                raise Invalid("unknown-global-entry", f"global lesson {gid!r} does not exist")
            entry["status"] = "retired"
            entry["retire_reason"] = reason
            self._event("retired", {"id": gid, "reason": reason})
            return self._save(entry)

    def restore(self, gid: str, reason: str = "manual") -> dict:
        """Undo a retirement by hand (`openfarm harness promote <gid> --global`)."""
        with self._locked():
            entry = self.get(gid)
            if entry is None:
                raise Invalid("unknown-global-entry", f"global lesson {gid!r} does not exist")
            entry["status"] = "active"
            entry.pop("retire_reason", None)
            self._event("restored", {"id": gid, "reason": reason})
            return self._save(entry)
