"""The state folder: everything a run knows, outside the repository.

The layout is the spec's, exactly. Nothing here is inside the repo, and the config
points at it, never the other way round.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
import platform
import shutil
import tempfile
import threading
from pathlib import Path

from .errors import Infra, Invalid

SCHEMA_VERSION = 1

_APPEND_LOCKS_GUARD = threading.Lock()
_APPEND_LOCKS: dict[Path, threading.Lock] = {}
_BREAKER_LOCKS_GUARD = threading.Lock()
_BREAKER_LOCKS: dict[Path, threading.Lock] = {}
_BASELINE_LOCKS_GUARD = threading.Lock()
_BASELINE_LOCKS: dict[Path, threading.RLock] = {}


def _append_lock_for(path: Path) -> threading.Lock:
    key = Path(path).resolve()
    with _APPEND_LOCKS_GUARD:
        lock = _APPEND_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _APPEND_LOCKS[key] = lock
        return lock


def _breaker_lock_for(root: Path) -> threading.Lock:
    key = Path(root).resolve()
    with _BREAKER_LOCKS_GUARD:
        lock = _BREAKER_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _BREAKER_LOCKS[key] = lock
        return lock


def baseline_lock_for(path: Path) -> threading.RLock:
    """Return the per-cache-path reentrant lock guarding baseline cache RMW."""
    key = Path(path).resolve()
    with _BASELINE_LOCKS_GUARD:
        lock = _BASELINE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _BASELINE_LOCKS[key] = lock
        return lock


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
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        #: The dead holder the last `acquire_run_lock` took the lock over from.
        self.last_lock_takeover: dict | None = None

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
        return self.runs / _safe_id(run_id, "run_id")

    def card_dir(self, card_id: str, run_id: str) -> Path:
        card = _safe_id(card_id, "card_id")
        if run_id == "":
            return self.cards / card
        return self.cards / card / _safe_id(run_id, "run_id")

    def check_dir(self, card_id: str) -> Path:
        return self.checks / _safe_id(card_id, "card_id")

    def harness(self, tag: str = "") -> Path:
        if tag == "":
            return self.root / "harness"
        return self.root / "harness" / _safe_id(tag, "harness tag")

    def create_run(self, run_id: str) -> Path:
        path = self.run_dir(run_id)
        (path / "telemetry").mkdir(parents=True, exist_ok=True)
        return path

    def create_card(self, card_id: str, run_id: str) -> Path:
        _safe_id(run_id, "run_id")
        path = self.card_dir(card_id, run_id)
        for sub in ("frozen", "briefs", "responses", "edits", "gates", "partial",
                    "micromanager", "attempts"):
            (path / sub).mkdir(parents=True, exist_ok=True)
        return path

    # -- lock, pause, kill switch --------------------------------------------

    def acquire_run_lock(self, run_id: str) -> bool:
        """Exclusive: one live run at a time.

        The lock records who holds it (`pid`, `host`, `started_at`). A lock whose
        holder was on this host and is no longer alive is taken over, and what was
        taken over is kept in `last_lock_takeover` for the caller to report. A lock
        whose holder is alive, is on another host, or does not say who it is (a
        lock file from before those fields) is refused as before.
        """
        self.last_lock_takeover = None
        # The guard serialises takeovers: two commands that both find the same
        # dead holder must not both delete the lock and both think they hold it.
        guard_path = self.root / "run.lock.guard"
        guard_fd = os.open(guard_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_EX)
            if self._create_run_lock(run_id):
                return True
            stale = self.stale_lock_holder()
            if stale is None:
                return False
            try:
                self.run_lock.unlink()
            except FileNotFoundError:
                pass
            if not self._create_run_lock(run_id):
                return False
            self.last_lock_takeover = stale
            return True
        finally:
            fcntl.flock(guard_fd, fcntl.LOCK_UN)
            os.close(guard_fd)

    def _create_run_lock(self, run_id: str) -> bool:
        try:
            fd = os.open(self.run_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps({"run_id": run_id, "started_at": utc_now(),
                                     **process_identity()}))
        return True

    def stale_lock_holder(self) -> dict | None:
        """The lock's holder when it is provably gone, else None.

        Provably gone means the lock names a pid and this host, and no process
        with that pid exists here. Anything less certain counts as alive.
        """
        holder = self.lock_holder()
        if not isinstance(holder, dict):
            return None
        if not holder_provably_gone(holder):
            return None
        return dict(holder)

    def release_run_lock(self, run_id: str) -> None:
        guard_path = self.root / "run.lock.guard"
        guard_fd = os.open(guard_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_EX)
            try:
                data = json.loads(self.run_lock.read_text())
            except (OSError, ValueError):
                return
            if isinstance(data, dict) and data.get("run_id") == run_id:
                try:
                    self.run_lock.unlink()
                except OSError:
                    pass
        finally:
            fcntl.flock(guard_fd, fcntl.LOCK_UN)
            os.close(guard_fd)

    def remove_stale_run_lock(self) -> dict | None:
        """Unlink `run.lock` under the guard only if its holder is still provably gone."""
        guard_path = self.root / "run.lock.guard"
        guard_fd = os.open(guard_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_EX)
            stale = self.stale_lock_holder()
            if stale is None:
                return None
            try:
                self.run_lock.unlink()
            except FileNotFoundError:
                pass
            return stale
        finally:
            fcntl.flock(guard_fd, fcntl.LOCK_UN)
            os.close(guard_fd)

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
        with _breaker_lock_for(self.root):
            try:
                return json.loads(self.breaker.read_text())
            except (OSError, ValueError):
                return {"tripped": False, "cards": [], "reason": ""}

    def write_breaker(self, tripped: bool, cards: list[str],
                      reason: str = "") -> None:
        with _breaker_lock_for(self.root):
            write_json(self.breaker, {
                "tripped": tripped,
                "cards": cards,
                "reason": reason,
                "at": utc_now(),
            })

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

    def check_disk_space(self, min_free_mb: int) -> int:
        free = self.free_bytes()
        if min_free_mb > 0 and free < min_free_mb * 1024 * 1024:
            free_mb = free / (1024 * 1024)
            raise Infra(
                "disk-full",
                f"state dir has {free_mb:.1f} MiB free, below {min_free_mb} MiB",
            )
        return free


def _this_host() -> str:
    return platform.node() or ""


def _read_first(path: str) -> str:
    try:
        with open(path, encoding="ascii", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _start_time(pid: int) -> str:
    """The process's start time in clock ticks since boot (`/proc/<pid>/stat`
    field 22), or "" where there is no /proc: a pid reused by a newer process
    shows a different one."""
    raw = _read_first(f"/proc/{pid}/stat")
    if not raw or ")" not in raw:
        return ""
    fields = raw.rsplit(")", 1)[1].split()
    return fields[19] if len(fields) > 19 else ""


def process_identity(pid: int | None = None) -> dict:
    """Who holds a lock or a seat, beyond pid and hostname (v0.8.5).

    Two containers share a hostname but not a pid namespace; two machines can
    share a hostname but not a machine id or a boot. Fields this system cannot
    read are left out, and a record that lacks one is judged as before.
    """
    target = os.getpid() if pid is None else int(pid)
    out = {"pid": target, "host": _this_host()}
    for key, value in (("machine_id", _read_first("/etc/machine-id")),
                       ("boot_id", _read_first("/proc/sys/kernel/random/boot_id")),
                       ("start", _start_time(target))):
        if value:
            out[key] = value
    try:
        out["pidns"] = os.readlink("/proc/self/ns/pid")
    except OSError:
        pass
    return out


def holder_provably_gone(rec: dict) -> bool:
    """True only when the recorded process was this system's and is gone.

    Same host, and the same machine, boot and pid namespace wherever the record
    names them; then gone when no process has the pid, or when the one that has
    it started at another time (the pid was reused). Anything less certain,
    another host, container or machine, counts as still there.
    """
    pid = rec.get("pid")
    host = rec.get("host")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if not isinstance(host, str) or not host or host != _this_host():
        return False
    here = process_identity()
    for key in ("machine_id", "boot_id", "pidns"):
        recorded = rec.get(key)
        if recorded is None:
            continue
        if not isinstance(recorded, str) or recorded != here.get(key):
            return False
    if not _pid_alive(pid):
        return True
    recorded_start = rec.get("start")
    if isinstance(recorded_start, str) and recorded_start and "boot_id" in rec:
        now_start = _start_time(pid)
        if now_start and now_start != recorded_start:
            return True
    return False


def _pid_alive(pid: int) -> bool:
    """True unless no process with `pid` exists on this host."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # EPERM and the like: something is there, just not ours to signal.
        return True
    raw = _read_first(f"/proc/{pid}/stat")
    if raw and ")" in raw:
        fields = raw.rsplit(")", 1)[1].split()
        if fields and fields[0] == "Z":
            return False
    return True


def _safe_id(name: str, what: str) -> str:
    if not isinstance(name, str) or not name:
        raise Invalid("bad-id", f"{what} must not be empty")
    if "/" in name or "\\" in name or "\x00" in name or ".." in name or name.startswith("."):
        raise Invalid("bad-id", f"{what} {name!r} is not a safe identifier")
    return name


def _safe(name: str) -> str:
    """A unit or card name that is safe as one path component."""
    cleaned = "".join(ch if ch.isalnum() or ch in "-._" else "-" for ch in str(name))
    return cleaned.strip("-") or "unit"


def write_json(path: Path, payload) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
        os.replace(tmp_path, path)
    except BaseException:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise
    return path


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def append_jsonl(path: Path, record: dict) -> None:
    path = Path(path)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with _append_lock_for(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(line)
                handle.flush()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
