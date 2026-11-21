"""Golden Replay / Regression Vault for Openfarm v0.12.0 (100% stdlib-only).

Stores deterministic bug reproducers and golden I/O assertions under
`.openfarm/replays/<id>.json` and replays them on demand or as a deterministic gate.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from .errors import Refused

_REPLAY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
REPLAYS_REL = Path(".openfarm") / "replays"


def _kill_process_group(proc: subprocess.Popen) -> None:
    pid = proc.pid
    if pid and pid > 0:
        try:
            os.killpg(pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    with contextlib.suppress(OSError):
        proc.kill()


def add_replay(
    repo: Path,
    replay_id: str,
    *,
    title: str,
    cmd: list[str],
    stdin: str = "",
    expect_exit: int = 0,
    expect_stdout: str | None = None,
    expect_stdout_contains: str = "",
    timeout_s: float = 15.0,
) -> dict:
    """Persist a deterministic golden replay case under `.openfarm/replays/<id>.json`."""
    rid = str(replay_id or "").strip()
    if not _REPLAY_ID_RE.match(rid):
        raise Refused("bad-replay-id", f"invalid replay id {replay_id!r}")
    if not isinstance(cmd, list) or not cmd or not str(cmd[0]).strip():
        raise Refused("empty-replay-cmd", "replay requires a non-empty command list")
    root = Path(repo).expanduser().resolve()
    openfarm_dir = root / ".openfarm"
    rdir = root / REPLAYS_REL
    if openfarm_dir.is_symlink() or rdir.is_symlink():
        raise Refused("symlink-replay", f"refusing to write replay through symlink {rdir}")
    rdir.mkdir(parents=True, exist_ok=True)
    record = {
        "id": rid,
        "title": str(title or rid).strip(),
        "cmd": [str(a) for a in cmd],
        "stdin": str(stdin or ""),
        "expect_exit": int(expect_exit),
        "expect_stdout": expect_stdout,
        "expect_stdout_contains": str(expect_stdout_contains or ""),
        "timeout_s": max(1.0, float(timeout_s)),
    }
    target = rdir / f"{rid}.json"
    if target.is_symlink():
        raise Refused("symlink-replay", f"refusing to overwrite symlink {target}")
    text = json.dumps(record, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{rid}.", suffix=".tmp", dir=str(rdir))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
        os.replace(tmp_path, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise
    return record


def list_replays(repo: Path) -> list[dict]:
    root = Path(repo).expanduser().resolve()
    rdir = root / REPLAYS_REL
    if not rdir.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(rdir.glob("*.json")):
        if p.is_symlink():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("id"):
            out.append(data)
    return out


def run_replays(repo: Path, *, replay_id: str = "") -> dict:
    """Execute all (or one named) golden replays inside `repo`."""
    root = Path(repo).expanduser().resolve()
    items = list_replays(root)
    if replay_id:
        items = [i for i in items if i["id"] == replay_id]
        if not items:
            raise Refused("replay-not-found", f"no replay {replay_id!r} found under {REPLAYS_REL}")
    if not items:
        return {"ok": True, "passed": 0, "failed": 0, "results": [], "skipped": True}

    results: list[dict] = []
    for item in items:
        started = time.monotonic()
        raw_cmd = item.get("cmd")
        if not isinstance(raw_cmd, list) or not raw_cmd or not str(raw_cmd[0]).strip():
            results.append({
                "id": item["id"],
                "title": item.get("title", item["id"]),
                "ok": False,
                "exit_code": -1,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "error": "invalid replay cmd: must be a non-empty list",
            })
            continue
        timeout_s = max(1.0, float(item.get("timeout_s", 15.0)))
        proc: subprocess.Popen[str] | None = None
        try:
            proc = subprocess.Popen(
                [str(a) for a in raw_cmd],
                cwd=str(root),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                stdout, _stderr = proc.communicate(
                    input=str(item.get("stdin") or ""),
                    timeout=timeout_s,
                )
            except subprocess.TimeoutExpired:
                _kill_process_group(proc)
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    proc.communicate(timeout=2.0)
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    proc.wait(timeout=2.0)
                raise
            ok = proc.returncode == int(item.get("expect_exit", 0))
            err = ""
            if not ok:
                err = f"expected exit {item.get('expect_exit', 0)}, got {proc.returncode}"
            exp_exact = item.get("expect_stdout")
            if ok and exp_exact is not None and (stdout or "") != str(exp_exact):
                ok = False
                err = "stdout did not match exact golden output"
            exp_sub = str(item.get("expect_stdout_contains") or "")
            if ok and exp_sub and exp_sub not in (stdout or ""):
                ok = False
                err = f"stdout missing {exp_sub!r}"
            results.append({
                "id": item["id"],
                "title": item.get("title", item["id"]),
                "ok": ok,
                "exit_code": proc.returncode,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "error": err,
            })
        except (OSError, subprocess.TimeoutExpired) as exc:
            results.append({
                "id": item["id"],
                "title": item.get("title", item["id"]),
                "ok": False,
                "exit_code": -1,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "error": str(exc),
            })
        finally:
            if proc is not None:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        with contextlib.suppress(OSError):
                            stream.close()

    passed = sum(1 for r in results if r["ok"])
    failed = len(results) - passed
    return {
        "ok": failed == 0,
        "passed": passed,
        "failed": failed,
        "results": results,
        "skipped": False,
    }


__all__ = ["REPLAYS_REL", "add_replay", "list_replays", "run_replays"]
