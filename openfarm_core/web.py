"""Stdlib Local / Tailnet Web Companion for Openfarm v0.14.0 (100% stdlib-only).

Serves a dark high-contrast single-page Factory Companion UI backed by JSON APIs
and Server-Sent Events (SSE). Binds strictly to `127.0.0.1` by default, requires a
per-session `X-Openfarm-Token` header on every mutating POST route, never pushes
to git remotes, and displays zero `$` spend values.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import secrets
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path

from .automode import load_project_chunks
from .config import CONFIG_PATH, load_config
from .dag import build_card_dag
from .daemon import daemon_status, stop_daemon
from .errors import ConfigError, Refused
from .findings import decide_finding, is_finding_id_or_header, list_findings
from .projects import list_projects
from .routing import compute_routing_recommendations
from .state import StateDir
from .tui import _load_tui_context, _read_cards_readonly

_WILDCARD_BINDS = {"0.0.0.0", "::", ""}


def build_web_state(repo: Path, *, config_path: Path | None = None) -> dict:
    """Build a sanitized, zero-spend JSON snapshot of the active Openfarm workspace."""
    root = Path(repo).expanduser().resolve()
    cfg_p = Path(config_path).expanduser().resolve() if config_path else (root / CONFIG_PATH)
    cfg = None
    if cfg_p.is_file():
        with contextlib.suppress(ConfigError, OSError):
            cfg = load_config(root, cfg_p)

    cards_out: list[dict] = []
    dag_summary: dict = {"has_cycle": False, "tiers": [], "ready_now": []}
    findings_out: list[dict] = []
    findings_counts: dict = {"waiting": 0, "new_since_last_run": 0, "picked": 0}
    routing_out: dict = {"samples": 0, "recommendations": []}
    token_split = {"worker": 0, "frontier": 0, "worker_pct": 0, "frontier_pct": 0}

    if cfg is not None:
        tctx = _load_tui_context(root, cfg_p)
        raw_cards, _ = _read_cards_readonly(tctx)
        non_findings = [c for c in raw_cards if not is_finding_id_or_header(c.id, getattr(c, "fields", None))]
        for c in non_findings:
            cards_out.append({
                "id": c.id,
                "title": c.title,
                "state": str(c.state),
                "category": str(getattr(c, "category", "") or ""),
                "wave": int((getattr(c, "fields", None) or {}).get("wave") or 1),
                "touches": list(c.touches),
                "depends": list(c.deps),
            })
        dag_summary = build_card_dag(non_findings)
        fres = list_findings(root, cfg, state_filter="waiting", deduplicate=True)
        findings_out = [
            {
                "id": f["id"],
                "title": f["title"],
                "from_card": f["from_card"],
                "is_new": bool(f.get("is_new")),
                "duplicate_count": int(f.get("duplicate_count") or 1),
            }
            for f in fres["findings"][:50]
        ]
        findings_counts = fres["counts"]
        routing_out = compute_routing_recommendations(root, config_path=cfg_p)

        # Aggregate token counts only (never expose dollar spend)
        cards_root = Path(cfg.state_dir) / "cards"
        if cards_root.is_dir():
            for rec_p in cards_root.glob("*/*/card-record.json"):
                try:
                    rec = json.loads(rec_p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(rec, dict):
                    token_split["worker"] += int(rec.get("tokens_in") or 0) + int(rec.get("tokens_out") or 0)
                    token_split["frontier"] += int(rec.get("micromanager_tokens_in") or 0) + int(
                        rec.get("micromanager_tokens_out") or 0
                    )
        tot = token_split["worker"] + token_split["frontier"]
        if tot > 0:
            token_split["worker_pct"] = int(round(100 * token_split["worker"] / tot))
            token_split["frontier_pct"] = 100 - token_split["worker_pct"]

    return {
        "project": cfg.name if cfg else root.name,
        "repo": str(root),
        "configured": cfg is not None,
        "language": (cfg.languages[0] if (cfg and cfg.languages) else ""),
        "base_branch": (cfg.base_branch if cfg else "main"),
        "chunks": load_project_chunks(root),
        "cards": cards_out,
        "dag": {
            "has_cycle": bool(dag_summary.get("has_cycle")),
            "tiers": dag_summary.get("tiers") or [],
            "ready_now": dag_summary.get("ready_now") or [],
        },
        "findings": findings_out,
        "findings_counts": findings_counts,
        "routing": routing_out,
        "daemon": daemon_status(root, config_path=config_path),
        "tokens": token_split,
        "projects": [{"id": p["id"], "name": p["name"], "language": p.get("language", ""), "repo": p["repo"]} for p in list_projects()[:20]],
    }


def _render_companion_html(token: str) -> str:
    # Single-page dark high-contrast companion UI (zero external CDNs, zero $ spend).
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Openfarm Factory Companion</title>
<style>
  :root {{ --bg:#0b0f17; --card:#111824; --line:#26344a; --fg:#eef4fc; --muted:#94a3b8; --cyan:#38bdf8; --green:#4ade80; --amber:#fbbf24; --rose:#fb7185; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; background:var(--bg); color:var(--fg); padding:20px; }}
  header {{ display:flex; align-items:center; justify-content:space-between; border-bottom:1px solid var(--line); padding-bottom:12px; margin-bottom:16px; }}
  .brand {{ background:var(--cyan); color:#08101a; font-weight:700; padding:4px 10px; border-radius:4px; margin-right:12px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:16px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px; }}
  h2 {{ margin:0 0 10px; font-size:14px; color:var(--cyan); text-transform:uppercase; letter-spacing:0.06em; }}
  .row {{ display:flex; justify-content:space-between; align-items:center; padding:6px 0; border-bottom:1px solid #1a2434; gap:8px; }}
  .pill {{ padding:2px 8px; border-radius:4px; font-size:12px; font-weight:700; }}
  .ok {{ background:#103623; color:var(--green); }}
  .warn {{ background:#423310; color:var(--amber); }}
  .err {{ background:#451320; color:var(--rose); }}
  button {{ background:#1b2638; color:var(--fg); border:1px solid var(--line); border-radius:4px; padding:4px 10px; cursor:pointer; font:inherit; font-size:12px; }}
  button.primary {{ background:var(--cyan); color:#08101a; border-color:var(--cyan); font-weight:700; }}
  button.approve {{ background:#22c55e; color:#06180d; border-color:#22c55e; font-weight:700; }}
  #log {{ background:#080c13; border:1px solid var(--line); border-radius:6px; padding:10px; height:180px; overflow:auto; font-size:12px; color:#cbd5e1; white-space:pre-wrap; }}
</style>
</head>
<body>
<header>
  <div><span class="brand">openfarm</span><strong id="proj-name">loading...</strong> <span id="proj-meta" style="color:var(--muted)"></span></div>
  <div><span id="tok-split" style="color:var(--muted);margin-right:12px"></span><span id="daemon-pill" class="pill ok">IDLE</span> <button onclick="stopDaemon()">Stop Daemon</button></div>
</header>
<div class="grid">
  <section class="card"><h2>Spec Chunks</h2><div id="chunks-list"></div></section>
  <section class="card"><h2>Wave Cards &amp; Local Land</h2><div id="cards-list"></div></section>
  <section class="card"><h2>Findings Inbox</h2><div id="findings-list"></div></section>
  <section class="card"><h2>Adaptive Model Routing</h2><div id="routing-list"></div></section>
</div>
<section class="card" style="margin-top:16px"><h2>Live Activity Stream (SSE)</h2><div id="log"></div></section>
<script>
const TOKEN = {json.dumps(token)};
function esc(s) {{ const d = document.createElement('div'); d.textContent = String(s ?? ''); return d.innerHTML; }}
async function postApi(url, body) {{
  const r = await fetch(url, {{ method:'POST', headers:{{'Content-Type':'application/json','X-Openfarm-Token':TOKEN}}, body:JSON.stringify(body||{{}}) }});
  const data = await r.json().catch(()=>({{}}));
  if (!r.ok) alert(data.error || ('HTTP ' + r.status));
  await refresh();
}}
async function stopDaemon() {{ await postApi('/api/daemon/stop', {{}}); }}
async function refresh() {{
  const s = await (await fetch('/api/state')).json();
  document.getElementById('proj-name').textContent = s.project;
  document.getElementById('proj-meta').textContent = '(' + (s.language || 'uninitialized') + ' · ' + s.base_branch + ')';
  document.getElementById('tok-split').textContent = 'Tokens: W ' + s.tokens.worker_pct + '% / F ' + s.tokens.frontier_pct + '%';
  const dp = document.getElementById('daemon-pill');
  dp.textContent = s.daemon.running ? ('DAEMON PID ' + s.daemon.pid) : 'IDLE';
  dp.className = 'pill ' + (s.daemon.running ? 'warn' : 'ok');
  document.getElementById('chunks-list').innerHTML = (s.chunks.length ? s.chunks.map(c =>
    '<div class="row"><span><strong>' + esc(c.id) + '</strong> ' + esc(c.title) + '</span><span class="pill ' + (c.status==='done'?'ok':'warn') + '">' + esc(c.status) + '</span></div>'
  ).join('') : '<div style="color:var(--muted)">No chunks yet.</div>');
  document.getElementById('cards-list').innerHTML = (s.cards.length ? s.cards.map(c => {{
    const st = String(c.state).toLowerCase().replace('-','_');
    const acts = st === 'in_review'
      ? '<button class="approve" onclick="postApi(\\'/api/cards/' + encodeURIComponent(c.id) + '/approve\\')">Approve</button> <button class="primary" onclick="postApi(\\'/api/cards/' + encodeURIComponent(c.id) + '/land\\')">Land</button>'
      : '<span class="pill ' + (st==='done'?'ok':(st==='blocked'?'err':'warn')) + '">' + esc(c.state) + '</span>';
    return '<div class="row"><span><strong>' + esc(c.id) + '</strong> ' + esc(c.title) + '</span><span>' + acts + '</span></div>';
  }}).join('') : '<div style="color:var(--muted)">No cards yet.</div>');
  document.getElementById('findings-list').innerHTML = (s.findings.length ? s.findings.map(f =>
    '<div class="row"><span><strong>' + esc(f.id) + '</strong> ' + esc(f.title) + '</span><span>' +
    '<button class="primary" onclick="postApi(\\'/api/findings/' + encodeURIComponent(f.id) + '/decide\\',{{decision:\\'next-wave\\'}})">Pick</button> ' +
    '<button onclick="postApi(\\'/api/findings/' + encodeURIComponent(f.id) + '/decide\\',{{decision:\\'dismiss\\'}})">Dismiss</button></span></div>'
  ).join('') : '<div style="color:var(--muted)">Inbox clear.</div>');
  document.getElementById('routing-list').innerHTML = (s.routing.recommendations && s.routing.recommendations.length ? s.routing.recommendations.map(r =>
    '<div class="row"><span>' + esc(r.language) + '/' + esc(r.category) + ' (' + Math.round(r.pass_rate*100) + '% pass)</span><span>' + esc(r.recommended_model) + ' · ' + esc(r.recommended_effort) + '</span></div>'
  ).join('') : '<div style="color:var(--muted)">No historical card runs yet.</div>');
}}
refresh();
setInterval(refresh, 4000);
try {{
  const es = new EventSource('/api/events');
  es.onmessage = (ev) => {{
    const el = document.getElementById('log');
    el.textContent += ev.data + '\\n';
    el.scrollTop = el.scrollHeight;
  }};
}} catch (_e) {{}}
</script>
</body>
</html>
"""


class OpenfarmWebServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        repo: Path,
        *,
        config_path: Path | None = None,
        token: str | None = None,
    ) -> None:
        self.repo = Path(repo).expanduser().resolve()
        self.config_path = Path(config_path).expanduser().resolve() if config_path else None
        self.auth_token = token or secrets.token_urlsafe(24)
        super().__init__(server_address, _OpenfarmWebHandler)


class _OpenfarmWebHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, _fmt: str, *_args) -> None:
        return None

    def _send_json(self, code: int, payload: dict) -> None:
        body = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return {}
        if length <= 0 or length > 256 * 1024:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def _check_token(self) -> bool:
        srv: OpenfarmWebServer = self.server  # type: ignore[assignment]
        got = str(self.headers.get("X-Openfarm-Token") or "").strip()
        if not secrets.compare_digest(got, srv.auth_token):
            self._send_json(403, {"ok": False, "error": "missing or invalid X-Openfarm-Token"})
            return False
        return True

    def do_GET(self) -> None:
        srv: OpenfarmWebServer = self.server  # type: ignore[assignment]
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "/index.html":
            html = _render_companion_html(srv.auth_token).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(html)
            return
        if path == "/api/state":
            self._send_json(200, build_web_state(srv.repo, config_path=srv.config_path))
            return
        if path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            once = "once=1" in (parsed.query or "")
            lines = _recent_event_lines(srv.repo, config_path=srv.config_path, limit=25)
            try:
                for line in lines:
                    self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
                self.wfile.flush()
                if once:
                    return
                for _ in range(3):
                    time.sleep(2.0)
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except OSError:
                return
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        srv: OpenfarmWebServer = self.server  # type: ignore[assignment]
        if not self._check_token():
            return
        path = urllib.parse.urlparse(self.path).path
        body = self._read_json_body()
        cfg_p = srv.config_path or (srv.repo / CONFIG_PATH)
        try:
            if path == "/api/daemon/stop":
                self._send_json(200, stop_daemon(srv.repo, config_path=srv.config_path))
                return
            parts = [urllib.parse.unquote(p) for p in path.strip("/").split("/")]
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "findings" and parts[3] == "decide":
                cfg = load_config(srv.repo, cfg_p)
                res = decide_finding(
                    srv.repo,
                    cfg,
                    parts[2],
                    str(body.get("decision") or "dismiss"),
                    reason=str(body.get("reason") or ""),
                    target_card=str(body.get("target_card") or ""),
                )
                self._send_json(200, {"ok": True, "finding": res})
                return
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "cards" and parts[3] == "approve":
                from .cli import main as cli_main

                rc = cli_main([
                    "--repo", str(srv.repo), "verdict", parts[2], "clean",
                    "--review-tokens", str(int(body.get("review_tokens") or 1000)),
                    "--note", str(body.get("note") or "approved via web companion"),
                ])
                self._send_json(200 if rc == 0 else 400, {"ok": rc == 0, "card": parts[2]})
                return
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "cards" and parts[3] == "land":
                from .cli import main as cli_main

                rc = cli_main(["--repo", str(srv.repo), "land", parts[2]])
                self._send_json(200 if rc == 0 else 400, {"ok": rc == 0, "card": parts[2]})
                return
            self._send_json(404, {"ok": False, "error": "unknown endpoint"})
        except (Refused, ConfigError, OSError) as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})


def _recent_event_lines(repo: Path, *, config_path: Path | None = None, limit: int = 25) -> list[str]:
    root = Path(repo).expanduser().resolve()
    cfg_p = Path(config_path).expanduser().resolve() if config_path else (root / CONFIG_PATH)
    if not cfg_p.is_file():
        return ["openfarm companion connected (repository not initialized yet)"]
    try:
        cfg = load_config(root, cfg_p)
    except (ConfigError, OSError):
        return ["openfarm companion connected"]
    state = StateDir(cfg.state_dir)
    if state.runs.is_dir():
        run_dirs = sorted([p for p in state.runs.iterdir() if p.is_dir()], key=lambda p: p.name)
        for rdir in reversed(run_dirs):
            ev_path = rdir / "telemetry" / "events.jsonl"
            if ev_path.is_file():
                out: list[str] = []
                for raw in ev_path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
                    try:
                        ev = json.loads(raw)
                        kind = ev.get("kind") or ev.get("event") or "event"
                        card = ev.get("card_id") or ev.get("card") or ""
                        detail = ev.get("detail") or ev.get("result") or ""
                        out.append(f"{kind} {card} {detail}".strip())
                    except ValueError:
                        out.append(raw[:160])
                if out:
                    return out
    return [f"openfarm companion watching {cfg.name}"]


def resolve_tailscale_ipv4(*, runner=None) -> str:
    """Resolve this host's Tailscale IPv4 address via `tailscale ip -4`."""
    binary = shutil.which("tailscale") if runner is None else "tailscale"
    if not binary:
        raise Refused("tailscale-missing", "tailscale CLI is not installed on PATH")
    run_fn = runner or subprocess.run
    proc = run_fn(
        [binary, "ip", "-4"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5.0,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise Refused("tailscale-unavailable", f"tailscale ip -4 failed: {detail or proc.returncode}")
    for line in (proc.stdout or "").splitlines():
        ip = line.strip()
        if ip:
            return ip
    raise Refused("tailscale-no-ip", "tailscale ip -4 returned no IPv4 address")


def resolve_tailscale_dns_name(*, runner=None) -> str:
    """Return this node's Tailscale MagicDNS name (without trailing dot), or empty string."""
    binary = shutil.which("tailscale") if runner is None else "tailscale"
    if not binary:
        return ""
    run_fn = runner or subprocess.run
    try:
        proc = run_fn(
            [binary, "status", "--self", "--json"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        if proc.returncode != 0:
            return ""
        data = json.loads(proc.stdout or "{}")
        dns = str(((data.get("Self") or {}).get("DNSName") or "")).strip().rstrip(".")
        return dns
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ""


def expose_via_tailscale_serve(port: int, *, runner=None) -> dict:
    """Expose `http://127.0.0.1:<port>` on the tailnet over HTTPS via `tailscale serve --bg`."""
    binary = shutil.which("tailscale") if runner is None else "tailscale"
    if not binary:
        raise Refused("tailscale-missing", "tailscale CLI is not installed on PATH")
    run_fn = runner or subprocess.run
    p = int(port)
    proc = run_fn(
        [binary, "serve", "--bg", f"--https={p}", f"http://127.0.0.1:{p}"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10.0,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise Refused("tailscale-serve-failed", f"tailscale serve failed: {detail or proc.returncode}")
    dns = resolve_tailscale_dns_name(runner=runner)
    ts_url = f"https://{dns}:{p}/" if dns else f"https://<tailnet-host>:{p}/"
    return {
        "ok": True,
        "port": p,
        "dns_name": dns,
        "url": ts_url,
        "disable_cmd": f"tailscale serve --https={p} off",
    }


def create_web_server(
    repo: Path,
    *,
    bind: str = "127.0.0.1",
    port: int = 8787,
    allow_all_interfaces: bool = False,
    config_path: Path | None = None,
    tailscale_runner=None,
) -> OpenfarmWebServer:
    """Create the stdlib HTTP companion server (refusing wildcard binds unless explicitly opted in).

    Passing `bind="tailscale"` resolves this host's Tailscale IPv4 via `tailscale ip -4`.
    """
    host = str(bind or "127.0.0.1").strip()
    if host.lower() == "tailscale":
        host = resolve_tailscale_ipv4(runner=tailscale_runner)
    if host in _WILDCARD_BINDS and not allow_all_interfaces:
        raise Refused(
            "wildcard-bind-refused",
            "refusing to bind to all interfaces without --allow-all-interfaces (default: 127.0.0.1)",
        )
    return OpenfarmWebServer((host, int(port)), repo, config_path=config_path)


__all__ = [
    "OpenfarmWebServer",
    "build_web_state",
    "create_web_server",
    "expose_via_tailscale_serve",
    "resolve_tailscale_dns_name",
    "resolve_tailscale_ipv4",
]
