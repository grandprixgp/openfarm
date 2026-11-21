"""v0.20 Wave 1 security & boundary regression tests."""

from __future__ import annotations

import http.client
import http.server
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openfarm_core import confine, logins, providers, spawn, web
from openfarm_core.adapters import acp as acp_mod
from openfarm_core.adapters.direct import open_runner
from openfarm_core.adapters.openai_compat import (
    OpenAICompatibleBackend,
    _AllowlistRedirectHandler,
)
from openfarm_core.bootstrap import initialize_project
from openfarm_core.errors import Infra, Invalid, Refused
from openfarm_core.state import StateDir

BWRAP_OK, BWRAP_WHY = confine.bwrap_status()


def _flag_values(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, tok in enumerate(argv[:-1]) if tok == flag]


class ConfineAndRunnerBoundaryTests(unittest.TestCase):
    def test_runner_argv_hides_parent_state_dir_without_wiping_worktree(self) -> None:
        with tempfile.TemporaryDirectory(prefix="of-v020-run-") as td:
            root = Path(td).resolve()
            state = StateDir(root / "state")
            wt = state.worktrees / "card-1"
            wt.mkdir(parents=True)
            (wt / "hello.txt").write_text("WORKTREE_OK\n", encoding="utf-8")
            state.runs.mkdir(parents=True, exist_ok=True)
            secret_run = state.runs / "secret.txt"
            secret_run.write_text("STATE_SECRET\n", encoding="utf-8")

            argv = confine.runner_argv(
                ["/bin/true"],
                worktree=wt,
                hide_paths=[state.root],
                bwrap="/usr/bin/bwrap",
                environ={},
            )
            state_s = str(state.root.resolve())
            wt_s = str(wt.resolve())
            idx_tmpfs = next(
                i for i in range(len(argv) - 1) if argv[i] == "--tmpfs" and argv[i + 1] == state_s
            )
            idx_bind = next(
                i for i in range(len(argv) - 2)
                if argv[i] == "--bind" and argv[i + 1] == wt_s and argv[i + 2] == wt_s
            )
            idx_ro = next(
                i for i in range(len(argv) - 1) if argv[i] == "--remount-ro" and argv[i + 1] == state_s
            )
            self.assertLess(idx_tmpfs, idx_bind)
            self.assertLess(idx_bind, idx_ro)

    @unittest.skipUnless(BWRAP_OK, f"bwrap unavailable: {BWRAP_WHY}")
    def test_open_runner_confined_executes_in_worktree_under_state_dir(self) -> None:
        # Use /var/tmp when available so state_dir is not already under bwrap's /tmp tmpfs.
        base_parent = "/var/tmp" if os.path.isdir("/var/tmp") else None
        with tempfile.TemporaryDirectory(prefix="of-v020-openrun-", dir=base_parent) as td:
            root = Path(td).resolve()
            state = StateDir(root / "state")
            wt = state.worktrees / "card-1"
            wt.mkdir(parents=True)
            (wt / "code.txt").write_text("WT_DATA\n", encoding="utf-8")
            state.runs.mkdir(parents=True, exist_ok=True)
            (state.runs / "secret.json").write_text("TOP_SECRET_STATE\n", encoding="utf-8")
            key_file = root / "worker.key"
            key_file.write_text("sk-secret-worker\n", encoding="utf-8")

            cfg = SimpleNamespace(
                runner_adapter="direct",
                runner_confine="bwrap",
                runner_confine_network="on",
                runner_write_roots=[],
                key_file=key_file,
                state_dir=state.root,
                micromanager=SimpleNamespace(auth_file=None),
            )
            runner = open_runner(cfg, wt)
            script = (
                "cat code.txt; "
                "echo WRITTEN > out.txt && cat out.txt; "
                f"cat {state.runs / 'secret.json'} 2>/dev/null || echo SECRET_HIDDEN; "
                f"cat {key_file} 2>/dev/null || echo KEY_HIDDEN; "
                f"touch {state.root / 'escape.txt'} 2>/dev/null || echo STATE_RO"
            )
            res = runner.exec("test", ["/bin/sh", "-c", script], cwd=wt)
            self.assertEqual(res.exit_code, 0, res.stderr)
            self.assertIn("WT_DATA", res.stdout)
            self.assertIn("WRITTEN", res.stdout)
            self.assertIn("SECRET_HIDDEN", res.stdout)
            self.assertNotIn("TOP_SECRET_STATE", res.stdout)
            self.assertNotIn("sk-secret-worker", res.stdout)
            self.assertIn("STATE_RO", res.stdout)
            self.assertEqual((wt / "out.txt").read_text(encoding="utf-8").strip(), "WRITTEN")

    def test_runner_argv_nested_hidden_dirs_deduped_parent_first(self) -> None:
        with tempfile.TemporaryDirectory(prefix="of-v020-nested-") as td:
            root = Path(td).resolve()
            wt = root / "wt"
            wt.mkdir()
            parent = root / "claude_root"
            child = parent / "profiles" / "work"
            child.mkdir(parents=True)
            argv = confine.runner_argv(
                ["/bin/true"],
                worktree=wt,
                hide_paths=[child, parent],
                environ={"CLAUDE_CONFIG_DIR": str(child)},
                bwrap="/usr/bin/bwrap",
            )
            tmpfs_targets = _flag_values(argv, "--tmpfs")
            self.assertIn(str(parent), tmpfs_targets)
            self.assertNotIn(str(child), tmpfs_targets)

    def test_scan_masks_and_boundary_argv_handle_external_and_self_symlinks_and_parent_order(self) -> None:
        base_parent = "/var/tmp" if os.path.isdir("/var/tmp") else None
        with tempfile.TemporaryDirectory(prefix="of-v020-mask-", dir=base_parent) as td:
            root = Path(td).resolve()
            sess = root / "session"
            sess.mkdir()
            (sess / "ok.txt").write_text("OK\n", encoding="utf-8")
            (sess / "a" / "b").mkdir(parents=True)
            (sess / "a" / "b" / "x.txt").write_text("NESTED\n", encoding="utf-8")

            ext = root / "ext_root"
            ext.mkdir()
            ext_secret = ext / "secret.txt"
            ext_secret.write_text("EXT_SECRET\n", encoding="utf-8")
            os.symlink(ext_secret, sess / "ext_link")

            # 1. Symlink pointing to mounted external root is masked.
            files, dirs = confine.scan_masks(sess, ["ext_link"], mounted_roots=[ext])
            self.assertIn(ext_secret, files)

            # 2. Symlink pointing to session_dir itself is masked.
            sess2 = root / "session2"
            sess2.mkdir()
            os.symlink(sess2, sess2 / "self_link")
            _f2, d2 = confine.scan_masks(sess2, ["self_link"])
            self.assertIn(sess2, d2)

            # 3. Parent-before-child deduplication in boundary_argv avoids ro-tmpfs failure.
            argv = confine.boundary_argv(
                ["/bin/sh", "-c", f"cat {sess / 'ok.txt'}; cat {sess / 'ext_link'} 2>/dev/null || true"],
                session_dir=sess,
                home_dir=None,
                hide_globs=["ext_link"],
                hide_paths=[sess / "a" / "b", sess / "a"],
                read_roots=[ext],
                bwrap=None if BWRAP_OK else "/usr/bin/bwrap",
            )
            tmpfs_targets = _flag_values(argv, "--tmpfs")
            self.assertIn(str(sess / "a"), tmpfs_targets)
            self.assertNotIn(str(sess / "a" / "b"), tmpfs_targets)
            if BWRAP_OK:
                proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("OK", proc.stdout)
                self.assertNotIn("EXT_SECRET", proc.stdout)


class WebCompanionSecurityTests(unittest.TestCase):
    def _send_raw_http(self, port: int, raw: bytes) -> tuple[int, str]:
        with socket.create_connection(("127.0.0.1", port), timeout=3.0) as sock:
            sock.sendall(raw)
            chunks = []
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
        text = b"".join(chunks).decode("utf-8", errors="replace")
        status_line = text.splitlines()[0]
        code = int(status_line.split()[1])
        body = text.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in text else ""
        return code, body

    def test_html_uses_delegated_events_no_onclick_and_dismissed_decision(self) -> None:
        html = web._render_companion_html("tok-123")
        self.assertNotIn("onclick=", html)
        self.assertIn("addEventListener('click'", html)
        self.assertIn('data-decision="dismissed"', html)
        self.assertNotIn("'dismiss'", html)
        self.assertIn("&quot;", html)
        self.assertIn("&#39;", html)

    def test_wildcard_bind_variants_refused_without_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            for bad_bind in ("", "0", "0.0", "0.0.0", "0.0.0.0", "::", "::0", "0:0:0:0:0:0:0:0", "::ffff:0.0.0.0"):
                with self.assertRaises(Refused, msg=f"expected Refused for bind={bad_bind!r}"):
                    web.create_web_server(repo, bind=bad_bind, port=0, allow_all_interfaces=False)

    def test_host_origin_dns_rebinding_and_non_loopback_token_exposure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "web-sec"
            initialize_project(
                "web-sec",
                directory=repo,
                language="python",
                state_dir=Path(td) / "st",
                secrets_dir=Path(td) / "sec",
                no_git=True,
            )
            srv = web.create_web_server(repo, bind="127.0.0.1", port=0)
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                # 1. DNS rebinding Host rejected
                code, _ = self._send_raw_http(
                    port,
                    f"GET / HTTP/1.1\r\nHost: evil.attacker:{port}\r\nConnection: close\r\n\r\n".encode(),
                )
                self.assertEqual(code, 403)

                # 2. Cross-origin Origin rejected
                code, _ = self._send_raw_http(
                    port,
                    (
                        f"GET /api/state HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{port}\r\n"
                        f"Origin: http://evil.attacker\r\n"
                        f"Connection: close\r\n\r\n"
                    ).encode(),
                )
                self.assertEqual(code, 403)

                # 3. Proxied loopback request must not expose auth_token unauthenticated
                code, body = self._send_raw_http(
                    port,
                    (
                        f"GET / HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{port}\r\n"
                        f"X-Forwarded-For: 100.64.0.9\r\n"
                        f"Connection: close\r\n\r\n"
                    ).encode(),
                )
                self.assertEqual(code, 200)
                self.assertNotIn(srv.auth_token, body)

                # 4. Proxied /api/state requires token
                code, _ = self._send_raw_http(
                    port,
                    (
                        f"GET /api/state HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{port}\r\n"
                        f"X-Forwarded-For: 100.64.0.9\r\n"
                        f"Connection: close\r\n\r\n"
                    ).encode(),
                )
                self.assertEqual(code, 403)
            finally:
                srv.shutdown()
                srv.server_close()

            # 5. Non-loopback / tailscale bind never exposes auth_token on unauthenticated GET /
            ts_runner = lambda argv, **_kw: SimpleNamespace(returncode=0, stdout="127.0.0.1\n", stderr="")
            ts_srv = web.create_web_server(repo, bind="tailscale", port=0, tailscale_runner=ts_runner)
            ts_port = ts_srv.server_address[1]
            t2 = threading.Thread(target=ts_srv.serve_forever, daemon=True)
            t2.start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{ts_port}/", timeout=3.0) as resp:
                    html = resp.read().decode("utf-8")
                    self.assertNotIn(ts_srv.auth_token, html)
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(f"http://127.0.0.1:{ts_port}/api/state", timeout=3.0)
                self.assertEqual(ctx.exception.code, 403)
                ctx.exception.close()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{ts_port}/api/state",
                    headers={"X-Openfarm-Token": ts_srv.auth_token},
                )
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    self.assertEqual(resp.status, 200)
            finally:
                ts_srv.shutdown()
                ts_srv.server_close()

    def test_web_cli_flag_injection_config_forwarding_and_invalid_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "web-cli"
            repo.mkdir()
            custom_cfg = Path(td) / "custom.toml"
            custom_cfg.write_text("", encoding="utf-8")
            srv = web.create_web_server(repo, bind="127.0.0.1", port=0, config_path=custom_cfg)
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            base = f"http://127.0.0.1:{port}"
            try:
                seen_cli: list[list[str]] = []

                def fake_cli(argv):
                    seen_cli.append(list(argv))
                    return 0

                with mock.patch("openfarm_core.cli.main", side_effect=fake_cli):
                    # Flag injection rejected
                    for endpoint in ("/api/cards/--promote-integration/land", "/api/cards/-x/approve"):
                        req = urllib.request.Request(
                            f"{base}{endpoint}",
                            data=b"{}",
                            headers={"Content-Type": "application/json", "X-Openfarm-Token": srv.auth_token},
                            method="POST",
                        )
                        with self.assertRaises(urllib.error.HTTPError) as ctx:
                            urllib.request.urlopen(req, timeout=3.0)
                        self.assertEqual(ctx.exception.code, 400)
                        ctx.exception.close()
                    self.assertEqual(seen_cli, [])

                    # Non-int / bool review_tokens returns 400 (not 500)
                    for bad_body in (b'{"review_tokens": "abc"}', b'{"review_tokens": true}'):
                        req = urllib.request.Request(
                            f"{base}/api/cards/W1-01/approve",
                            data=bad_body,
                            headers={"Content-Type": "application/json", "X-Openfarm-Token": srv.auth_token},
                            method="POST",
                        )
                        with self.assertRaises(urllib.error.HTTPError) as ctx:
                            urllib.request.urlopen(req, timeout=3.0)
                        self.assertEqual(ctx.exception.code, 400)
                        ctx.exception.close()

                    # Valid approve & land forward --config and use -- before positional card id
                    req = urllib.request.Request(
                        f"{base}/api/cards/W1-01/approve",
                        data=b'{"review_tokens": 500, "note": "--looks-like-flag"}',
                        headers={"Content-Type": "application/json", "X-Openfarm-Token": srv.auth_token},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=3.0) as resp:
                        self.assertEqual(resp.status, 200)
                    req2 = urllib.request.Request(
                        f"{base}/api/cards/W1-01/land",
                        data=b"{}",
                        headers={"Content-Type": "application/json", "X-Openfarm-Token": srv.auth_token},
                        method="POST",
                    )
                    with urllib.request.urlopen(req2, timeout=3.0) as resp:
                        self.assertEqual(resp.status, 200)

                self.assertEqual(len(seen_cli), 2)
                self.assertIn("--config", seen_cli[0])
                self.assertIn(str(custom_cfg.resolve()), seen_cli[0])
                self.assertEqual(seen_cli[0][-3:], ["--", "W1-01", "clean"])
                self.assertIn("--note=--looks-like-flag", seen_cli[0])
                self.assertIn("--config", seen_cli[1])
                self.assertEqual(seen_cli[1][-2:], ["--", "W1-01"])
            finally:
                srv.shutdown()
                srv.server_close()

    def test_web_dismiss_default_decision_and_sse_crlf_strip_and_tailscale_errors(self) -> None:
        self.assertGreater(web._OpenfarmWebHandler.timeout, 0)
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "web-sse"
            repo.mkdir()
            srv = web.create_web_server(repo, bind="127.0.0.1", port=0)
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            base = f"http://127.0.0.1:{port}"
            try:
                # SSE strips CR/LF to prevent event frame injection
                with mock.patch("openfarm_core.web._recent_event_lines", return_value=["line1\r\nevent: spoof\ndata: evil"]):
                    with urllib.request.urlopen(f"{base}/api/events?once=1", timeout=3.0) as resp:
                        sse = resp.read().decode("utf-8")
                self.assertNotIn("\nevent: spoof", sse)
                self.assertNotIn("\r", sse)

                # Default finding decision is "dismissed"
                seen_decisions: list[str] = []
                with mock.patch("openfarm_core.web.load_config", return_value=SimpleNamespace()), \
                     mock.patch("openfarm_core.web.decide_finding", side_effect=lambda _r, _c, fid, dec, **_kw: seen_decisions.append(dec) or {"id": fid, "decision": dec}):
                    req = urllib.request.Request(
                        f"{base}/api/findings/F-1/decide",
                        data=b"{}",
                        headers={"Content-Type": "application/json", "X-Openfarm-Token": srv.auth_token},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=3.0) as resp:
                        self.assertEqual(resp.status, 200)
                self.assertEqual(seen_decisions, ["dismissed"])
            finally:
                srv.shutdown()
                srv.server_close()

        # Tailscale helpers wrap OSError and TimeoutExpired as Refused
        def raise_timeout(*_a, **_kw):
            raise subprocess.TimeoutExpired(cmd="tailscale", timeout=5)

        def raise_oserror(*_a, **_kw):
            raise OSError("boom")

        for fn in (raise_timeout, raise_oserror):
            with self.assertRaises(Refused):
                web.resolve_tailscale_ipv4(runner=fn)
            with self.assertRaises(Refused):
                web.expose_via_tailscale_serve(8787, runner=fn)


class AcpAndCredentialBoundaryTests(unittest.TestCase):
    def test_acp_git_forbidden_ls_disallowed_and_nested_path_extraction(self) -> None:
        self.assertIn(".git/**", acp_mod._DEFAULT_FORBIDDEN_FILES)
        self.assertIn("LS", acp_mod._DISALLOWED_READ_ONLY)

        _env, iso = acp_mod._sanitised_env(provider="claude", auth_mode="oauth", isolate_home=True)
        self.assertIsNotNone(iso)
        try:
            settings = json.loads((iso / ".claude" / "settings.json").read_text(encoding="utf-8"))
            deny = settings["permissions"]["deny"]
            self.assertIn("Read(.git/**)", deny)
            self.assertIn("LS(.git/**)", deny)
            self.assertIn("LS(.openfarm/**)", deny)
        finally:
            shutil.rmtree(iso, ignore_errors=True)

        extracted = acp_mod._extract_paths_from_tool_call({
            "rawInput": {
                "options": {"include": ".git/config", "nested": [{"file_path": "../secret.txt"}]},
                "glob": "seams/**",
                "args": ["/etc/passwd"],
            }
        })
        self.assertIn(".git/config", extracted)
        self.assertIn("../secret.txt", extracted)
        self.assertIn("seams/**", extracted)
        self.assertIn("/etc/passwd", extracted)

    def test_acp_search_overlaps_protected_when_session_root_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            (root / "seams").mkdir()
            (root / "seams" / "core.py").write_text("x = 1\n", encoding="utf-8")
            adapter = acp_mod.ClaudeAcpAdapter(
                ["/bin/true"],
                cwd=root,
                allowed_root=root,
                never_touch=["seams/**"],
                isolate_home=False,
                confine="off",
            )
            sent: list[dict] = []
            adapter._send_json = lambda obj, **_kw: sent.append(obj)  # type: ignore[method-assign]
            events: list[dict] = []
            adapter._handle_permission(
                1,
                {
                    "toolCall": {"kind": "search", "rawInput": {"path": "."}},
                    "options": [{"kind": "allow_once", "optionId": "a"}, {"kind": "reject_once", "optionId": "r"}],
                },
                "read-only",
                events,
                session_id="no-cwd-session",
            )
            self.assertEqual(events[0]["decision"], "reject_once")
            self.assertEqual(events[0]["reason"], "search-overlaps-protected")

    def test_acp_unhashable_jsonrpc_id_rejected_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            script = root / "fake_acp.py"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "for line in sys.stdin:\n"
                "    msg = json.loads(line)\n"
                "    m = msg.get('method')\n"
                "    mid = msg.get('id')\n"
                "    if m == 'initialize':\n"
                "        print(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'protocolVersion':1}}), flush=True)\n"
                "    elif m == 'session/new':\n"
                "        print(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'sessionId':'s1'}}), flush=True)\n"
                "    elif m == 'session/prompt':\n"
                "        print(json.dumps({'jsonrpc':'2.0','id':{'bad':1},'method':'session/request_permission','params':{}}), flush=True)\n"
                "        print(json.dumps({'jsonrpc':'2.0','id':[1,2],'method':'session/request_permission','params':{}}), flush=True)\n"
                "        print(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'stopReason':'end_turn'}}), flush=True)\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            with acp_mod.ClaudeAcpAdapter(
                [sys.executable, str(script)],
                cwd=root,
                provider="claude",
                isolate_home=False,
                confine="off",
            ) as adapter:
                sid = adapter.new_session(cwd=root, tool_policy="none")
                res = adapter.prompt(sid, "hi", timeout_s=10)
            self.assertEqual(res.stop_reason, "end_turn")
            reasons = [r["reason"] for r in res.rejected_methods]
            self.assertEqual(reasons, ["invalid-id", "invalid-id"])

    def test_acp_terminate_process_group_kills_orphan_after_leader_exits(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pid_file = Path(td) / "child.pid"
            proc = subprocess.Popen(
                [
                    sys.executable, "-c",
                    f"import os, subprocess, sys; "
                    f"c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                    f"open({str(pid_file)!r}, 'w').write(str(c.pid))",
                ],
                start_new_session=True,
            )
            proc.wait(timeout=5)
            child_pid = int(pid_file.read_text(encoding="utf-8").strip())
            adapter = acp_mod.ClaudeAcpAdapter(["/bin/true"], isolate_home=False, confine="off")
            adapter._proc = proc
            adapter._terminate_process_group()
            # Grandchild must be gone (poll briefly for kernel reap)
            dead = False
            for _ in range(30):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    dead = True
                    break
                time.sleep(0.05)
            self.assertTrue(dead, f"orphan grandchild {child_pid} survived _terminate_process_group")

    def test_acp_read_api_key_file_refuses_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            real = root / "real.key"
            real.write_text("sk-real\n", encoding="utf-8")
            link = root / "sym.key"
            link.symlink_to(real)
            with self.assertRaises(Refused):
                acp_mod._read_api_key_file(link)

    def test_logins_check_shape_rejects_bool_nan_and_refresh_isolated_refuses_symlinks(self) -> None:
        orig = json.dumps({"claudeAiOauth": {"accessToken": "a1", "refreshToken": "r1", "expiresAt": 1000}}).encode()
        for bad_exp in ("true", "false", "NaN", "Infinity", "-Infinity"):
            raw = (
                '{"claudeAiOauth": {"accessToken": "a2", "refreshToken": "r2", "expiresAt": '
                + bad_exp + "}}"
            ).encode()
            self.assertTrue(logins.check_shape("claude", raw, orig), f"expected refusal for expiresAt={bad_exp}")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            iso = root / "iso"
            (iso / ".claude").mkdir(parents=True)
            victim = root / "victim.json"
            victim.write_text("ORIGINAL", encoding="utf-8")
            os.symlink(victim, iso / ".claude" / ".credentials.json")

            src = root / "host.json"
            src.write_bytes(json.dumps({
                "claudeAiOauth": {"accessToken": "a2", "refreshToken": "r2", "expiresAt": 2000}
            }).encode())
            res, _ = logins.refresh_isolated("claude", isolated_home=iso, source=src, initial=orig)
            self.assertEqual(res.status, "refused")
            self.assertEqual(victim.read_text(encoding="utf-8"), "ORIGINAL")

            # Normal refresh writes mode 0600 atomically
            (iso / ".claude" / ".credentials.json").unlink()
            res2, new_bytes = logins.refresh_isolated("claude", isolated_home=iso, source=src, initial=orig)
            self.assertEqual(res2.status, "refreshed", res2.reason)
            self.assertEqual(new_bytes, src.read_bytes())
            self.assertEqual(stat.S_IMODE((iso / ".claude" / ".credentials.json").stat().st_mode), 0o600)

    def test_providers_does_not_chmod_home_or_dot_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td).resolve() / "home"
            dot_config = fake_home / ".config"
            dot_config.mkdir(parents=True)
            os.chmod(fake_home, 0o755)
            os.chmod(dot_config, 0o755)
            with mock.patch("pathlib.Path.home", return_value=fake_home):
                providers.save_global_providers(
                    worker_preset="openrouter",
                    endpoint="https://openrouter.ai/api/v1",
                    model="qwen/qwen3-coder",
                    key_file=fake_home / "worker.key",
                    api_key="sk-test-home",
                    path=dot_config / "providers.toml",
                )
            self.assertEqual(stat.S_IMODE(fake_home.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(dot_config.stat().st_mode), 0o755)

    def test_providers_probe_strips_auth_cross_origin_and_redacts_multiline_key(self) -> None:
        seen_on_b: dict[str, str] = {}

        class HandlerB(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                for k, v in self.headers.items():
                    seen_on_b[k.lower()] = v
                body = b'{"data":[{"id":"m1"}]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _fmt: str, *_args) -> None:
                return None

        srv_b = http.server.ThreadingHTTPServer(("127.0.0.1", 0), HandlerB)
        port_b = srv_b.server_address[1]

        class HandlerA(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{port_b}/v1/models")
                self.end_headers()

            def log_message(self, _fmt: str, *_args) -> None:
                return None

        srv_a = http.server.ThreadingHTTPServer(("127.0.0.1", 0), HandlerA)
        port_a = srv_a.server_address[1]
        ta = threading.Thread(target=srv_a.serve_forever, daemon=True)
        tb = threading.Thread(target=srv_b.serve_forever, daemon=True)
        ta.start()
        tb.start()
        try:
            res = providers.probe_worker_endpoint(
                f"http://127.0.0.1:{port_a}/v1",
                api_key="sk-should-not-cross-origin",
                timeout_s=3.0,
            )
            self.assertTrue(res["ok"])
            self.assertNotIn("authorization", seen_on_b)

            # Multi-line key file fails cleanly without leaking any line of the key
            with tempfile.TemporaryDirectory() as td:
                kf = Path(td) / "multi.key"
                kf.write_text("sk-secret-line-1\nsk-secret-line-2\n", encoding="utf-8")
                res2 = providers.probe_worker_endpoint(
                    f"http://127.0.0.1:{port_b}/v1",
                    key_file=kf,
                    timeout_s=3.0,
                )
            self.assertFalse(res2["ok"])
            self.assertNotIn("sk-secret-line-1", json.dumps(res2))
            self.assertNotIn("sk-secret-line-2", json.dumps(res2))
        finally:
            srv_a.shutdown()
            srv_a.server_close()
            srv_b.shutdown()
            srv_b.server_close()

        # HTTPS -> HTTP downgrade refused
        handler = providers._SafeProbeRedirectHandler()
        req = urllib.request.Request("https://api.example.com/v1/models", headers={"Authorization": "Bearer sk-x"})
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(req, None, 302, "Found", {}, "http://api.example.com/v1/models")

    def test_openai_compat_redirect_symlink_multiline_and_bounded_read(self) -> None:
        # 1. Redirect handler refuses HTTPS->HTTP downgrade & strips auth cross-origin
        rh = _AllowlistRedirectHandler([
            "https://a.example.com/v1",
            "http://a.example.com/v1",
            "https://b.example.com/v1",
        ])
        req = urllib.request.Request(
            "https://a.example.com/v1/chat/completions",
            headers={"Authorization": "Bearer sk-secret"},
        )
        with self.assertRaises(Refused) as ctx:
            rh.redirect_request(req, None, 302, "Found", {}, "http://a.example.com/v1/chat/completions")
        self.assertEqual(ctx.exception.code, "redirect-downgrade")

        redirected = rh.redirect_request(req, None, 302, "Found", {}, "https://b.example.com/v1/chat/completions")
        self.assertIsNotNone(redirected)
        all_hdrs = {k.lower() for k in (*redirected.headers.keys(), *redirected.unredirected_hdrs.keys())}
        self.assertNotIn("authorization", all_hdrs)

        # 2. Symlink and multi-line key files refused without leaking raw secret
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            real = root / "real.key"
            real.write_text("sk-ok\n", encoding="utf-8")
            sym = root / "sym.key"
            sym.symlink_to(real)
            backend_sym = OpenAICompatibleBackend(
                "http://127.0.0.1:9/v1", ["http://127.0.0.1:9/v1"], key_file=sym, retries=0
            )
            with self.assertRaises(Refused):
                backend_sym._key()

            multi = root / "multi.key"
            multi.write_text("sk-leak-1\nsk-leak-2\n", encoding="utf-8")
            backend_multi = OpenAICompatibleBackend(
                "http://127.0.0.1:9/v1", ["http://127.0.0.1:9/v1"], key_file=multi, retries=0
            )
            with self.assertRaises(Invalid) as ctx_m:
                backend_multi._key()
            self.assertNotIn("sk-leak-1", str(ctx_m.exception))
            self.assertNotIn("sk-leak-2", str(ctx_m.exception))

            # 3. HTTPException and oversized response handled cleanly
            backend_ok = OpenAICompatibleBackend(
                "http://127.0.0.1:9/v1", ["http://127.0.0.1:9/v1"], key_file=real, retries=0
            )
            with mock.patch.object(backend_ok._opener, "open", side_effect=http.client.RemoteDisconnected("gone")):
                with self.assertRaises(Infra) as ctx_h:
                    backend_ok.complete("impl", "m", 0.0, [{"role": "user", "content": "hi"}])
                self.assertEqual(ctx_h.exception.code, "endpoint-unreachable")

            class _HugeResp:
                def __enter__(self):
                    return self

                def __exit__(self, *_a):
                    return False

                def read(self, n=-1):
                    return b"x" * (n if n > 0 else 10)

            with mock.patch.object(backend_ok._opener, "open", return_value=_HugeResp()):
                with self.assertRaises(Infra) as ctx_big:
                    backend_ok.complete("impl", "m", 0.0, [{"role": "user", "content": "hi"}])
                self.assertEqual(ctx_big.exception.code, "response-too-large")

    def test_spawn_retry_delays_rejects_non_finite(self) -> None:
        with mock.patch.dict(os.environ, {"OPENFARM_SPAWN_RETRY_DELAYS": "0.1,inf,-inf,nan,0.2,-1"}):
            self.assertEqual(spawn.retry_delays(), (0.1, 0.2))


if __name__ == "__main__":
    unittest.main()
