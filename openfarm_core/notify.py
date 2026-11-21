"""Optional Webhook & Desktop Notifications for Openfarm v0.14.0 (100% stdlib-only).

Sends non-blocking best-effort notifications when configured under `[notifications]`
in `.openfarm/config.toml`. Never raises into the run loop and never leaks secrets.
"""

from __future__ import annotations

import http.client
import json
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request


def _effective_port(parts: urllib.parse.SplitResult) -> int | None:
    try:
        if parts.port is not None:
            return parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme == "https":
        return 443
    if scheme == "http":
        return 80
    return None


class _SafeWebhookRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow redirects only on the configured host/port without HTTPS downgrade."""

    def __init__(self, orig_scheme: str, orig_host: str, orig_port: int) -> None:
        self.orig_scheme = orig_scheme
        self.orig_host = orig_host
        self.orig_port = orig_port

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target_url = urllib.parse.urljoin(req.full_url, newurl)
        dest = urllib.parse.urlsplit(target_url)
        scheme = dest.scheme.lower()
        host = (dest.hostname or "").lower()
        port = _effective_port(dest)
        if scheme not in ("http", "https") or not host or port is None:
            if fp is not None:
                fp.close()
            raise urllib.error.URLError("refused unsafe webhook redirect URL")
        if self.orig_scheme == "https" and scheme != "https":
            if fp is not None:
                fp.close()
            raise urllib.error.URLError("refused HTTPS-to-HTTP webhook redirect")
        if host != self.orig_host or port != self.orig_port:
            if fp is not None:
                fp.close()
            raise urllib.error.URLError(
                f"refused cross-origin webhook redirect to {host}:{port}"
            )
        return super().redirect_request(req, fp, code, msg, headers, target_url)


def _webhook_opener(
    orig_scheme: str, orig_host: str, orig_port: int
) -> urllib.request.OpenerDirector:
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler(),
        urllib.request.HTTPHandler(),
        urllib.request.HTTPSHandler(),
        _SafeWebhookRedirectHandler(orig_scheme, orig_host, orig_port),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    return opener


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

    if webhook_url:
        try:
            parsed = urllib.parse.urlsplit(webhook_url)
            orig_scheme = parsed.scheme.lower()
            orig_host = (parsed.hostname or "").lower()
            orig_port = _effective_port(parsed)
            if orig_scheme not in ("http", "https") or not orig_host or orig_port is None:
                result["webhook"]["error"] = "invalid webhook URL"
            else:
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
                opener = _webhook_opener(orig_scheme, orig_host, orig_port)
                with opener.open(req, timeout=max(0.5, float(timeout_s))) as resp:
                    status = int(getattr(resp, "status", 200) or 200)
                    result["webhook"]["ok"] = 200 <= status < 300
                    if not result["webhook"]["ok"]:
                        result["webhook"]["error"] = f"HTTP {status}"
        except urllib.error.HTTPError as exc:
            try:
                result["webhook"]["error"] = str(exc)[:200]
            finally:
                exc.close()
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            TimeoutError,
            OSError,
            ValueError,
        ) as exc:
            result["webhook"]["error"] = str(exc)[:200]

    if desktop:
        binary = shutil.which("notify-send")
        if binary:
            try:
                proc = subprocess.run(
                    [binary, "--", str(title), str(body)],
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
