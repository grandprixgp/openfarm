"""The state folder: everything a run knows, outside the repository.

The layout is the spec's, exactly. Nothing here is inside the repo, and the config
points at it, never the other way round.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
from pathlib import Path

from .errors import Infra

SCHEMA_VERSION = 1


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_after(seconds: float) -> str:
    moment = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: str) -> _dt.datetime:
    return _dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=_dt.timezone.utc)


class StateDir:
    """The state folder, and the names of everything in it."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    # -- fixed paths ----------------------------------------------------------

    @property
    def config_lock(self) -> Path:
        return self.root / "config.lock.toml"

    @property
    def run_lock(self) -> Path:
        return self.root / "run.lock"

    @property
    def paused(self) -> Path:
        return self.root / "paused"

    @property
    def breaker(self) -> Path:
        return self.root / "breaker.json"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    @property
    def cards(self) -> Path:
        return self.root / "cards"

    @property
    def checks(self) -> Path:
        return self.root / "checks"

    @property
    def worktrees(self) -> Path:
        return self.root / "worktrees"

    @property
    def baseline_cache(self) -> Path:
        return self.root / "baseline-cache"

    def kill_switch(self, configured: Path | None = None) -> Path:
        return Path(configured).expanduser() if configured else self.root / "STOP"

    # -- run and card folders -------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        return self.runs / run_id

    def card_dir(self, card_id: str, run_id: str) -> Path:
        return self.cards / card_id / run_id

    def check_dir(self, card_id: str) -> Path:
        return self.checks / card_id

    def create_run(self, run_id: str) -> Path:
        path = self.run_dir(run_id)
        (path / "telemetry").mkdir(parents=True, exist_ok=True)
        return path

    def create_card(self, card_id: str, run_id: str) -> Path:
        path = self.card_dir(card_id, run_id)
        for sub in ("frozen", "briefs", "responses", "edits", "gates", "partial"):
            (path / sub).mkdir(parents=True, exist_ok=True)
        return path

    # -- lock, pause, kill switch --------------------------------------------

    def acquire_run_lock(self, run_id: str) -> bool:
        """Exclusive: one live run at a time."""
        try:
            fd = os.open(self.run_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps({"run_id": run_id, "started_at": utc_now()}))
        return True

    def release_run_lock(self, run_id: str) -> None:
        try:
            data = json.loads(self.run_lock.read_text())
        except (OSError, ValueError):
            data = {}
        if not data or data.get("run_id") == run_id:
            try:
                self.run_lock.unlink()
            except OSError:
                pass

    def lock_holder(self) -> dict | None:
        try:
            return json.loads(self.run_lock.read_text())
        except (OSError, ValueError):
            return None

    def is_paused(self) -> bool:
        return self.paused.exists()

    def pause(self, reason: str = "") -> None:
        self.paused.write_text(json.dumps({"reason": reason, "at": utc_now()}))

    def resume(self) -> None:
        try:
            self.paused.unlink()
        except FileNotFoundError:
            pass

    def kill_requested(self, configured: Path | None = None) -> bool:
        return self.kill_switch(configured).exists()

    def request_kill(self, configured: Path | None = None, reason: str = "") -> Path:
        path = self.kill_switch(configured)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"reason": reason, "at": utc_now()}))
        return path

    def clear_kill(self, configured: Path | None = None) -> None:
        try:
            self.kill_switch(configured).unlink()
        except FileNotFoundError:
            pass

    # -- breaker --------------------------------------------------------------

    def read_breaker(self) -> dict:
        try:
            return json.loads(self.breaker.read_text())
        except (OSError, ValueError):
            return {"tripped": False, "cards": [], "reason": ""}

    def write_breaker(self, tripped: bool, cards: list[str],
                      reason: str = "") -> None:
        self.breaker.write_text(json.dumps(
            {"tripped": tripped, "cards": cards, "reason": reason,
             "at": utc_now()}, indent=2))

    # -- baseline cache -------------------------------------------------------

    def cache_path(self, commit: str, unit: str, known_failing_hash: str,
                   env_hash: str) -> Path:
        return (self.baseline_cache / commit[:12] / _safe(unit)
                / f"{known_failing_hash[:12]}-{env_hash}.json")

    # -- disk ----------------------------------------------------------------

    def free_bytes(self) -> int:
        try:
            usage = shutil.disk_usage(self.root)
        except OSError as exc:
            raise Infra("state-dir", f"cannot read free space: {exc}") from exc
        return usage.free


def _safe(name: str) -> str:
    """A unit or card name that is safe as one path component."""
    cleaned = "".join(ch if ch.isalnum() or ch in "-._" else "-" for ch in str(name))
    return cleaned.strip("-") or "unit"


def write_json(path: Path, payload) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)
    return path


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def append_jsonl(path: Path, record: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
