"""Detached Factory Daemon for Openfarm v0.14.0 (100% stdlib-only).

Runs `openfarm run` in a detached OS session (`start_new_session=True`) so waves
continue smoothly after closing the TUI or SSH terminal, logging to
`<state_dir>/daemon.log` and tracking state in `<state_dir>/daemon.json`.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from .config import CONFIG_PATH, load_config
from .errors import ConfigError, Refused
from .state import (
    StateDir,
    _pid_alive as _state_pid_alive,
    holder_provably_gone,
    process_identity,
    utc_now as _utc_now,
    write_json,
)


def _pid_alive(pid: int) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    # Reap if this process is the parent and the child already exited (avoids zombies)
    with contextlib.suppress(ChildProcessError, OSError):
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    return _state_pid_alive(pid)


def _daemon_is_running(meta: dict) -> bool:
    """True only when `meta` names a live local process whose identity still matches."""
    if not isinstance(meta, dict):
        return False
    pid = meta.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    with contextlib.suppress(ChildProcessError, OSError):
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    if holder_provably_gone(meta):
        return False
    if "host" in meta:
        here = process_identity()
        if meta.get("host") != here.get("host"):
            return False
        for key in ("machine_id", "boot_id", "pidns"):
            recorded = meta.get(key)
            if recorded is not None and recorded != here.get(key):
                return False
    return _pid_alive(pid)


@contextlib.contextmanager
def _daemon_guard(state_dir: Path):
    guard_path = Path(state_dir) / "daemon.lock"
    guard_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(guard_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _tail_lines(path: Path, n: int, *, chunk_size: int = 8192) -> list[str]:
    """Read only the last `n` lines of `path` by seeking backwards from EOF."""
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        if pos == 0:
            return []
        buf = b""
        newlines = 0
        while pos > 0 and newlines <= n:
            read_size = min(chunk_size, pos)
            pos -= read_size
            fh.seek(pos)
            chunk = fh.read(read_size)
            buf = chunk + buf
            newlines += chunk.count(b"\n")
    return buf.decode("utf-8", errors="replace").splitlines()[-n:]


def _meta_path(state_dir: Path) -> Path:
    return Path(state_dir) / "daemon.json"


def _log_path(state_dir: Path) -> Path:
    return Path(state_dir) / "daemon.log"


def _load_cfg(repo: Path, config_path: Path | None = None):
    root = Path(repo).expanduser().resolve()
    cfg_p = Path(config_path).expanduser().resolve() if config_path else (root / CONFIG_PATH)
    if not cfg_p.is_file():
        raise Refused("not-configured", f"no {CONFIG_PATH} found in {root}")
    try:
        return root, load_config(root, cfg_p)
    except ConfigError as exc:
        raise Refused("bad-config", str(exc)) from exc


def daemon_status(repo: Path, *, config_path: Path | None = None) -> dict:
    """Inspect whether a detached Openfarm daemon is currently running for `repo`."""
    root = Path(repo).expanduser().resolve()
    cfg_p = Path(config_path).expanduser().resolve() if config_path else (root / CONFIG_PATH)
    if not cfg_p.is_file():
        return {"running": False, "pid": None, "configured": False, "log_path": "", "started_at": ""}
    try:
        cfg = load_config(root, cfg_p)
    except (ConfigError, OSError):
        return {"running": False, "pid": None, "configured": False, "log_path": "", "started_at": ""}

    mp = _meta_path(cfg.state_dir)
    lp = _log_path(cfg.state_dir)
    if not mp.is_file():
        return {
            "running": False,
            "pid": None,
            "configured": True,
            "log_path": str(lp),
            "started_at": "",
            "cmd": [],
        }
    try:
        meta = json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    raw_pid = meta.get("pid")
    pid = int(raw_pid) if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) else 0
    alive = _daemon_is_running(meta)
    return {
        "running": alive,
        "pid": pid if pid > 0 else None,
        "stale": bool(pid > 0 and not alive),
        "configured": True,
        "repo": str(meta.get("repo") or root),
        "started_at": str(meta.get("started_at") or ""),
        "log_path": str(meta.get("log_path") or lp),
        "cmd": list(meta.get("cmd") or []),
        "run_id": str(meta.get("run_id") or ""),
    }


def start_daemon(
    repo: Path,
    *,
    cards: int = 3,
    spend: float | None = None,
    profile: str | None = None,
    card_ids: list[str] | None = None,
    config_path: Path | None = None,
    command_override: list[str] | None = None,
    run_id: str = "",
    follow: bool = False,
) -> dict:
    """Spawn a detached background `openfarm run` worker session.

    `run_id` names the run, so anything that opens later (the TUI, the web page)
    can find its events; `follow` keeps the run going through dependent tasks.
    """
    root, cfg = _load_cfg(repo, config_path)
    state = StateDir(cfg.state_dir)
    with _daemon_guard(cfg.state_dir):
        cur = daemon_status(root, config_path=config_path)
        if cur["running"]:
            raise Refused("daemon-already-running", f"daemon already running (pid {cur['pid']})")

        # Clear an earlier STOP kill-switch file only when no live run holds run.lock.
        has_live_run_lock = state.run_lock.exists() and state.stale_lock_holder() is None
        if not has_live_run_lock:
            with contextlib.suppress(OSError):
                state.kill_switch(cfg.kill_switch).unlink(missing_ok=True)

        lp = _log_path(cfg.state_dir)
        lp.parent.mkdir(parents=True, exist_ok=True)

        if command_override is not None:
            cmd = [str(a) for a in command_override]
        else:
            cmd = [sys.executable, "-m", "openfarm_core.cli", "--repo", str(root)]
            if config_path is not None:
                cmd += ["--config", str(Path(config_path).expanduser().resolve())]
            cmd += ["run", "--cards", str(max(1, int(cards)))]
            if spend is not None:
                cmd += ["--spend", str(float(spend))]
            if profile:
                cmd += ["--profile", str(profile)]
            for cid in (card_ids or ()):
                if str(cid).strip():
                    cmd += ["--card", str(cid).strip()]
            if run_id:
                cmd += ["--run-id", run_id]
            if follow:
                cmd += ["--follow"]

        log_handle = lp.open("a", encoding="utf-8")
        try:
            log_handle.write(f"\n=== openfarm daemon started at {_utc_now()} ===\n")
            log_handle.flush()
            # The child must import this same openfarm, wherever the caller was started from.
            env = dict(os.environ)
            src = str(Path(__file__).resolve().parent.parent)
            env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            proc = subprocess.Popen(
                cmd,
                cwd=str(root),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_handle.close()

        # Reap the detached child if this parent stays alive (TUI, web, tests)
        # so neither a zombie nor a Popen.__del__ ResourceWarning is left.
        def _reap(child: subprocess.Popen) -> None:
            with contextlib.suppress(ChildProcessError, OSError):
                child.wait()

        threading.Thread(
            target=_reap,
            args=(proc,),
            name=f"openfarm-daemon-reap-{proc.pid}",
            daemon=True,
        ).start()

        meta = {
            **process_identity(int(proc.pid)),
            "repo": str(root),
            "started_at": _utc_now(),
            "log_path": str(lp),
            "cmd": cmd,
            "run_id": run_id,
        }
        write_json(_meta_path(cfg.state_dir), meta)
        return {"ok": True, "running": True, **meta}


def stop_daemon(repo: Path, *, config_path: Path | None = None) -> dict:
    """Request clean stop via kill-switch and send SIGTERM to the detached daemon process group."""
    root, cfg = _load_cfg(repo, config_path)
    state = StateDir(cfg.state_dir)
    ks = state.kill_switch(cfg.kill_switch)
    ks.parent.mkdir(parents=True, exist_ok=True)
    ks.write_text(f"stopped by daemon stop at {_utc_now()}\n", encoding="utf-8")

    with _daemon_guard(cfg.state_dir):
        st = daemon_status(root, config_path=config_path)
        pid = st.get("pid")
        signaled = False
        if st["running"] and isinstance(pid, int) and pid > 0:
            try:
                os.killpg(pid, signal.SIGTERM)
                signaled = True
            except OSError:
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGTERM)
                    signaled = True
    return {
        "ok": True,
        "was_running": bool(st["running"]),
        "pid": pid,
        "signaled": signaled,
        "kill_switch": str(ks),
    }


def read_daemon_logs(repo: Path, *, tail: int = 80, config_path: Path | None = None) -> dict:
    """Read the last `tail` lines of `<state_dir>/daemon.log`."""
    root, cfg = _load_cfg(repo, config_path)
    lp = _log_path(cfg.state_dir)
    if not lp.is_file():
        return {"log_path": str(lp), "lines": [], "exists": False}
    n = max(1, int(tail))
    return {"log_path": str(lp), "lines": _tail_lines(lp, n), "exists": True}


__all__ = ["daemon_status", "read_daemon_logs", "start_daemon", "stop_daemon"]
