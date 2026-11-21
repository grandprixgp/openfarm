"""Optional Webhook & Desktop Notifications for Openfarm v0.14.0 (100% stdlib-only).

Sends non-blocking best-effort notifications when configured under `[notifications]`
in `.openfarm/config.toml`. Never raises into the run loop and never leaks secrets.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.request


def send_notification(
    cfg,
    event: str,
    title: str,
    body: str,
    *,
    timeout_s: float = 3.0,
) -> dict:
    """Deliver an optional webhook and/or desktop notification according to `cfg.notifications`."""
    ncfg = getattr(cfg, "notifications", None)
    if ncfg is None:
        return {"sent": False, "skipped": True, "reason": "not configured"}

    allowed_events = tuple(getattr(ncfg, "events", ()) or ())
    if allowed_events and event not in allowed_events:
        return {"sent": False, "skipped": True, "reason": f"event {event!r} filtered"}

    webhook_url = str(getattr(ncfg, "webhook_url", "") or "").strip()
    desktop = bool(getattr(ncfg, "desktop", False))
    result = {
        "sent": False,
        "skipped": False,
        "event": event,
        "webhook": {"ok": False, "skipped": not bool(webhook_url), "error": ""},
        "desktop": {"ok": False, "skipped": not desktop, "error": ""},
    }

    if webhook_url and (webhook_url.startswith("http://") or webhook_url.startswith("https://")):
        payload = json.dumps({
            "source": "openfarm",
            "project": str(getattr(cfg, "name", "") or ""),
            "event": str(event),
            "title": str(title),
            "body": str(body),
        }).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "openfarm-notify/0.20.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=max(0.5, float(timeout_s))) as resp:
                status = int(getattr(resp, "status", 200) or 200)
                result["webhook"]["ok"] = 200 <= status < 300
                if not result["webhook"]["ok"]:
                    result["webhook"]["error"] = f"HTTP {status}"
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            result["webhook"]["error"] = str(exc)[:200]

    if desktop:
        binary = shutil.which("notify-send")
        if binary:
            try:
                proc = subprocess.run(
                    [binary, str(title), str(body)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3.0,
                    check=False,
                )
                result["desktop"]["ok"] = proc.returncode == 0
                if proc.returncode != 0:
                    result["desktop"]["error"] = f"notify-send exited {proc.returncode}"
            except (OSError, subprocess.TimeoutExpired) as exc:
                result["desktop"]["error"] = str(exc)[:200]
        else:
            result["desktop"]["error"] = "notify-send not installed"

    result["sent"] = bool(result["webhook"]["ok"] or result["desktop"]["ok"])
    return result


__all__ = ["send_notification"]
