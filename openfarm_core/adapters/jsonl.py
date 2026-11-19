"""Telemetry: one record per attempt and per card.

If telemetry fails, the core keeps writing local JSONL and notes it in the run
record. Telemetry trouble never stops a card.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from ..state import append_jsonl, write_json


STREAM_ATTEMPT = "attempt"
STREAM_CARD = "card"
STREAM_RUN = "run"


class JsonlTelemetry:
    """JSONL in the run folder. Optional SQLite is a later adapter, not this one."""

    name = "jsonl"

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        (self.run_dir / "telemetry").mkdir(parents=True, exist_ok=True)
        self.problems: list[str] = []
        self._lock = threading.Lock()

    def hello(self) -> dict:
        return {"name": self.name, "versions": [1], "ops": ["hello", "write", "flush"]}

    def write(self, stream: str, record: dict) -> dict:
        if stream not in (STREAM_ATTEMPT, STREAM_CARD, STREAM_RUN):
            raise ValueError(f"{stream!r} is not a telemetry stream")
        path = self.run_dir / "telemetry" / f"{stream}s.jsonl"
        with self._lock:
            try:
                append_jsonl(path, record)
            except OSError as exc:
                self.problems.append(f"{path}: {exc}")
        return {"written": True}

    def flush(self) -> dict:
        with self._lock:
            return {"flushed": True, "problems": list(self.problems)}


class MemoryTelemetry:
    """Telemetry held in memory, for tests."""

    name = "memory"

    def __init__(self) -> None:
        self.records: dict[str, list[dict]] = {"attempt": [], "card": [], "run": []}
        self._lock = threading.Lock()

    def hello(self) -> dict:
        return {"name": self.name, "versions": [1], "ops": ["hello", "write", "flush"]}

    def write(self, stream: str, record: dict) -> dict:
        with self._lock:
            self.records.setdefault(stream, []).append(record)
        return {"written": True}

    def flush(self) -> dict:
        return {"flushed": True}

    def attempts(self) -> list[dict]:
        with self._lock:
            return list(self.records.get("attempt", []))

    def cards(self) -> list[dict]:
        with self._lock:
            return list(self.records.get("card", []))


#: Fields every attempt record carries. A reader ignores what it doesn't know.
ATTEMPT_FIELDS = (
    "schema_version", "run_id", "card_id", "card_hash", "attempt", "phase",
    "started_at", "ended_at", "model", "temperature", "seed", "tokens_in",
    "tokens_out", "spend", "edit_format", "files_changed", "lines_added",
    "lines_removed", "diff_hash", "model_builds", "fixed_builds",
    "runner_seconds", "gates", "named_tests_passing", "error_signature",
    "outcome", "stop_code",
)

CARD_FIELDS = (
    "schema_version", "run_id", "card_id", "card_hash_at_claim",
    "card_hash_at_end", "category", "unit", "claimed_at", "ended_at",
    "final_state", "stop_code", "attempts", "depth", "planner_used",
    "builds", "runner_seconds", "wall_clock_s", "tokens_in", "tokens_in_fresh",
    "tokens_in_cached", "tokens_out",
    "spend", "files_changed", "lines_added", "lines_removed", "named_tests",
    "known_failing_excluded", "counter_tests", "mutation", "findings_filed",
    "frontier_estimate", "dossier_path",
    "micromanager_calls", "micromanager_tokens_in",
    "micromanager_tokens_in_fresh", "micromanager_tokens_in_cached",
    "micromanager_tokens_out", "micromanager_spend",
    "false_rejects_dismissed",
    "harness_revision", "harness_cues_applied", "harness_lessons_added",
    "team", "handbacks",
)


def complete_record(record: dict, fields: tuple[str, ...], schema_version: int = 1) -> dict:
    """Fill in every documented field, so a record is never a partial one."""
    out = {"schema_version": schema_version}
    for field in fields:
        value = record.get(field)
        if value is None:
            value = _default_for(field)
        out[field] = value
    for key, value in record.items():
        if key not in out:
            out[key] = value
    return out


def _default_for(field: str):
    if field in ("attempt", "tokens_in", "tokens_out", "lines_added",
                 "lines_removed", "model_builds", "fixed_builds",
                 "runner_seconds", "depth", "named_tests_passing",
                 "micromanager_calls", "micromanager_tokens_in",
                 "micromanager_tokens_in_fresh", "micromanager_tokens_in_cached",
                 "tokens_in_fresh", "tokens_in_cached",
    "micromanager_tokens_in_fresh", "micromanager_tokens_in_cached",
                 "micromanager_tokens_out", "false_rejects_dismissed",
                 "harness_revision"):
        return 0
    if field in ("spend", "wall_clock_s", "micromanager_spend"):
        return 0.0
    if field == "gates":
        return []
    if field == "builds":
        return {"model": 0, "fixed": 0, "mutation": 0}
    if field == "counter_tests":
        return {"written": 0, "dropped_uncompiled": 0, "passed": 0, "failed": 0}
    if field == "mutation":
        return {"killed": 0, "survived": 0, "total": 0, "capped": False}
    if field == "planner_used":
        return False
    if field in ("findings_filed", "known_failing_excluded", "named_tests",
                 "harness_cues_applied", "harness_lessons_added",
                 "team", "handbacks"):
        return []
    if field == "files_changed":
        return []
    return ""


class TelemetrySink:
    """Wraps a sink so a failure is noted and never stops a card."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.problems: list[str] = []
        self._lock = threading.Lock()

    def _sync_problems(self) -> None:
        for problem in getattr(self.inner, "problems", ()) or ():
            if problem not in self.problems:
                self.problems.append(problem)

    def write(self, stream: str, record: dict) -> None:
        with self._lock:
            try:
                self.inner.write(stream, record)
            except Exception as exc:  # telemetry must never stop a card
                msg = f"{stream}: {exc}"
                if msg not in self.problems:
                    self.problems.append(msg)
            self._sync_problems()

    def flush(self) -> None:
        with self._lock:
            try:
                self.inner.flush()
            except Exception as exc:  # telemetry flush errors must never raise
                msg = f"flush: {exc}"
                if msg not in self.problems:
                    self.problems.append(msg)
            self._sync_problems()


def open_telemetry(cfg, run_dir: Path):
    """Build the telemetry adapter from config (`jsonl` or `cmd:<argv>`)."""
    spec = getattr(cfg, "telemetry_adapter", "jsonl") or "jsonl"
    if spec.startswith("cmd:"):
        from ..protocol import CmdTelemetry
        return CmdTelemetry(spec, cfg.repo)
    return JsonlTelemetry(run_dir)
