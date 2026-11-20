"""Runtime Acceptance ('Proof-of-Life') Gate for Openfarm v0.12.0 (100% stdlib-only).

Executes declarative CLI and ephemeral loopback HTTP smoke checks inside the card's
worktree with strict timeouts, process-group cleanup, and structured JSON evidence.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _kill_proc_group(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=2.0)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=2.0)


def _run_cli_check(worktree: Path, check: dict) -> dict:
    name = str(check.get("name") or "cli-check")
    cmd = [str(a) for a in (check.get("cmd") or [])]
    timeout_s = max(1.0, float(check.get("timeout_s", 15.0)))
    expect_exit = int(check.get("expect_exit", 0))
    expect_out = str(check.get("expect_stdout_contains") or "")
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            start_new_session=True,
        )
        ok = proc.returncode == expect_exit and (not expect_out or expect_out in (proc.stdout or ""))
        err = ""
        if proc.returncode != expect_exit:
            err = f"expected exit {expect_exit}, got {proc.returncode}"
        elif expect_out and expect_out not in (proc.stdout or ""):
            err = f"stdout missing {expect_out!r}"
        return {
            "name": name,
            "kind": "cli",
            "ok": ok,
            "exit_code": proc.returncode,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "stdout_tail": (proc.stdout or "")[-400:],
            "stderr_tail": (proc.stderr or "")[-400:],
            "error": err,
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "name": name,
            "kind": "cli",
            "ok": False,
            "exit_code": -1,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": str(exc),
        }


def _run_http_check(worktree: Path, check: dict) -> dict:
    name = str(check.get("name") or "http-check")
    port = _pick_free_port()
    start_cmd = [str(a).replace("{PORT}", str(port)) for a in (check.get("start_cmd") or [])]
    url = str(check.get("url") or f"http://127.0.0.1:{port}/").replace("{PORT}", str(port))
    expect_status = int(check.get("expect_status", 200))
    expect_body = str(check.get("expect_body_contains") or "")
    startup_timeout = max(1.0, float(check.get("startup_timeout_s", 8.0)))

    started = time.monotonic()
    proc: subprocess.Popen | None = None
    try:
        if start_cmd:
            proc = subprocess.Popen(
                start_cmd,
                cwd=str(worktree),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        deadline = time.monotonic() + startup_timeout
        last_err = ""
        status = 0
        body = ""
        while time.monotonic() < deadline:
            if proc is not None and proc.poll() is not None:
                last_err = f"service exited early with code {proc.returncode}"
                break
            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    status = int(getattr(resp, "status", 200) or 200)
                    body = resp.read(256 * 1024).decode("utf-8", errors="replace")
                last_err = ""
                break
            except urllib.error.HTTPError as exc:
                status = int(getattr(exc, "code", 0) or 0)
                body = exc.read(64 * 1024).decode("utf-8", errors="replace") if exc.fp else ""
                last_err = ""
                break
            except (urllib.error.URLError, OSError) as exc:
                last_err = str(exc)
                time.sleep(0.15)

        ok = (not last_err) and status == expect_status and (not expect_body or expect_body in body)
        err = last_err
        if not err and status != expect_status:
            err = f"expected HTTP {expect_status}, got {status}"
        elif not err and expect_body and expect_body not in body:
            err = f"response body missing {expect_body!r}"
        return {
            "name": name,
            "kind": "http",
            "ok": ok,
            "url": url,
            "status": status,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "body_preview": body[:300],
            "error": err,
        }
    finally:
        if proc is not None:
            _kill_proc_group(proc)


def run_acceptance_checks(
    worktree: Path,
    checks: list[dict],
    *,
    evidence_path: Path | None = None,
) -> dict:
    """Run all configured runtime acceptance checks and optionally write JSON evidence."""
    root = Path(worktree).expanduser().resolve()
    if not checks:
        return {"ok": True, "results": [], "skipped": True}
    results: list[dict] = []
    for chk in checks:
        kind = str(chk.get("kind") or "cli").strip().lower()
        if kind == "http":
            results.append(_run_http_check(root, chk))
        else:
            results.append(_run_cli_check(root, chk))
    payload = {
        "ok": all(r["ok"] for r in results),
        "results": results,
        "skipped": False,
    }
    if evidence_path is not None:
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


__all__ = ["run_acceptance_checks"]
