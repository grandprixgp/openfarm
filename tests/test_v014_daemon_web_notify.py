"""v0.14.0 tests: Detached Factory Daemon, Stdlib Web Companion (SSE + CSRF), and Notifications."""

from __future__ import annotations

import asyncio
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

from openfarm_core.bootstrap import initialize_project
from openfarm_core.cli import main as cli_main
from openfarm_core.config import load_config
from openfarm_core.daemon import daemon_status, read_daemon_logs, start_daemon, stop_daemon
from openfarm_core.errors import Refused
from openfarm_core.notify import send_notification
from openfarm_core.web import build_web_state, create_web_server
from openfarm_tui import textual_available


class DaemonWebNotifyV014Tests(unittest.TestCase):
    def test_daemon_start_status_logs_and_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "dmn-app"
            initialize_project(
                "dmn-app",
                directory=repo,
                language="typescript",
                state_dir=Path(tmp) / "st",
                secrets_dir=Path(tmp) / "sec",
                commit=True,
            )
            st0 = daemon_status(repo)
            self.assertFalse(st0["running"])

            # Spawn a harmless detached python sleeper using command_override so test is fast & deterministic
            started = start_daemon(
                repo,
                command_override=[sys.executable, "-c", "import time; print('daemon-alive', flush=True); time.sleep(30)"],
            )
            try:
                self.assertTrue(started["ok"])
                self.assertTrue(started["pid"] > 0)
                st1 = daemon_status(repo)
                self.assertTrue(st1["running"])
                self.assertEqual(st1["pid"], started["pid"])

                # Starting a second daemon while one is alive must be refused
                with self.assertRaises(Refused):
                    start_daemon(repo, command_override=[sys.executable, "-c", "import time; time.sleep(5)"])

                # CLI status JSON
                buf = io.StringIO()
                with redirect_stdout(buf):
                    self.assertEqual(cli_main(["--repo", str(repo), "--json", "daemon", "status"]), 0)
                self.assertTrue(json.loads(buf.getvalue())["running"])

                # Wait briefly for log output then check logs
                time.sleep(0.2)
                logs = read_daemon_logs(repo, tail=20)
                self.assertTrue(logs["exists"])
                self.assertTrue(any("daemon-alive" in line or "daemon started" in line for line in logs["lines"]))
            finally:
                stopped = stop_daemon(repo)
                self.assertTrue(stopped["ok"])
                self.assertTrue(stopped["was_running"])
                # Give OS a moment to reap SIGTERM'd sleeper
                for _ in range(20):
                    if not daemon_status(repo)["running"]:
                        break
                    time.sleep(0.05)
                self.assertFalse(daemon_status(repo)["running"])

    def test_web_companion_loopback_guard_csrf_state_and_sse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "web-app"
            initialize_project(
                "web-app",
                directory=repo,
                language="typescript",
                state_dir=Path(tmp) / "st",
                secrets_dir=Path(tmp) / "sec",
                commit=True,
            )
            # Refuses wildcard bind without opt-in
            with self.assertRaises(Refused):
                create_web_server(repo, bind="0.0.0.0", port=0, allow_all_interfaces=False)

            srv = create_web_server(repo, bind="127.0.0.1", port=0)
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                base = f"http://127.0.0.1:{port}"
                # 1. HTML companion page loads and has zero dollar spend display
                with urllib.request.urlopen(f"{base}/", timeout=3.0) as resp:
                    self.assertEqual(resp.status, 200)
                    html = resp.read().decode("utf-8")
                    self.assertIn("Openfarm Factory Companion", html)
                    self.assertIn("X-Openfarm-Token", html)
                    self.assertNotIn("spend_per_night", html)

                # 2. JSON state API
                with urllib.request.urlopen(f"{base}/api/state", timeout=3.0) as resp:
                    self.assertEqual(resp.status, 200)
                    st = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(st["project"], "web-app")
                    self.assertEqual(st["language"], "typescript")
                    self.assertIn("tokens", st)
                    self.assertNotIn("spend", st)

                # 3. SSE endpoint emits event lines cleanly
                with urllib.request.urlopen(f"{base}/api/events?once=1", timeout=3.0) as resp:
                    self.assertEqual(resp.status, 200)
                    sse = resp.read().decode("utf-8")
                    self.assertIn("data:", sse)

                # 4. Mutating POST without token MUST be rejected with 403
                bad_req = urllib.request.Request(
                    f"{base}/api/daemon/stop",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(bad_req, timeout=3.0)
                self.assertEqual(ctx.exception.code, 403)
                ctx.exception.close()

                # 5. Mutating POST with valid per-session token succeeds
                ok_req = urllib.request.Request(
                    f"{base}/api/daemon/stop",
                    data=b"{}",
                    headers={"Content-Type": "application/json", "X-Openfarm-Token": srv.auth_token},
                    method="POST",
                )
                with urllib.request.urlopen(ok_req, timeout=3.0) as resp:
                    self.assertEqual(resp.status, 200)
                    res = json.loads(resp.read().decode("utf-8"))
                    self.assertTrue(res["ok"])
            finally:
                srv.shutdown()
                srv.server_close()

            snap = build_web_state(repo)
            self.assertTrue(snap["configured"])

    def test_web_companion_tailscale_helpers_and_bind(self) -> None:
        from types import SimpleNamespace
        from openfarm_core.web import expose_via_tailscale_serve, resolve_tailscale_ipv4

        calls: list[list[str]] = []

        def fake_run(argv, **_kwargs):
            calls.append(list(argv))
            if argv[1:3] == ["ip", "-4"]:
                return SimpleNamespace(returncode=0, stdout="127.0.0.1\n", stderr="")
            if argv[1:3] == ["serve", "--bg"]:
                return SimpleNamespace(returncode=0, stdout="Serve started\n", stderr="")
            if argv[1:3] == ["status", "--self"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"Self": {"DNSName": "devbox.tail123.ts.net."}}),
                    stderr="",
                )
            return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

        self.assertEqual(resolve_tailscale_ipv4(runner=fake_run), "127.0.0.1")
        ts = expose_via_tailscale_serve(8787, runner=fake_run)
        self.assertTrue(ts["ok"])
        self.assertEqual(ts["url"], "https://devbox.tail123.ts.net:8787/")
        self.assertEqual(ts["disable_cmd"], "tailscale serve --https=8787 off")
        self.assertTrue(any(c[1:4] == ["serve", "--bg", "--https=8787"] for c in calls))

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "ts-app"
            repo.mkdir()
            srv = create_web_server(repo, bind="tailscale", port=0, tailscale_runner=fake_run)
            try:
                self.assertEqual(srv.server_address[0], "127.0.0.1")
            finally:
                srv.server_close()

    def test_notifications_webhook_and_event_filtering(self) -> None:
        received: list[dict] = []

        class _Hook(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length") or "0")
                received.append(json.loads(self.rfile.read(n).decode("utf-8")))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, _fmt: str, *_args) -> None:
                return None

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Hook)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "ntf-app"
                initialize_project(
                    "ntf-app",
                    directory=repo,
                    language="typescript",
                    state_dir=Path(tmp) / "st",
                    secrets_dir=Path(tmp) / "sec",
                    commit=True,
                )
                cfg_p = repo / ".openfarm" / "config.toml"
                cfg_p.write_text(
                    cfg_p.read_text(encoding="utf-8")
                    + f'\n[notifications]\nwebhook_url = "http://127.0.0.1:{port}/hook"\ndesktop = false\nevents = ["wave_finished"]\n',
                    encoding="utf-8",
                )
                cfg = load_config(repo)
                skipped = send_notification(cfg, "card_blocked", "Blocked", "W1-01 blocked")
                self.assertTrue(skipped["skipped"])
                self.assertEqual(len(received), 0)

                sent = send_notification(cfg, "wave_finished", "Wave Complete", "3 cards in review")
                self.assertTrue(sent["sent"])
                self.assertTrue(sent["webhook"]["ok"])
                self.assertEqual(len(received), 1)
                self.assertEqual(received[0]["project"], "ntf-app")
                self.assertEqual(received[0]["event"], "wave_finished")
        finally:
            srv.shutdown()
            srv.server_close()

    @unittest.skipUnless(textual_available(), "textual optional extra not installed")
    def test_tui_has_detached_daemon_controls(self) -> None:
        from openfarm_tui.app import OpenfarmApp

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "tui-v014"
            prev_reg = os.environ.get("OPENFARM_PROJECTS_FILE")
            os.environ["OPENFARM_PROJECTS_FILE"] = str(Path(tmp) / "projects.toml")
            try:
                initialize_project(
                    "tui-v014",
                    directory=repo,
                    language="typescript",
                    state_dir=Path(tmp) / "st",
                    secrets_dir=Path(tmp) / "sec",
                    commit=True,
                )

                async def _run() -> None:
                    app = OpenfarmApp(repo=repo, initial_view="studio")
                    async with app.run_test(size=(120, 34)) as pilot:
                        await pilot.pause(0.15)
                        self.assertIsNotNone(app.query_one("#btn-daemon-start"))
                        self.assertIsNotNone(app.query_one("#btn-daemon-stop"))

                asyncio.run(_run())
            finally:
                if prev_reg is None:
                    os.environ.pop("OPENFARM_PROJECTS_FILE", None)
                else:
                    os.environ["OPENFARM_PROJECTS_FILE"] = prev_reg


if __name__ == "__main__":
    unittest.main()
