"""v0.20 Wave 2 wiring & Wave 1 seam-fix regression tests."""

from __future__ import annotations

import concurrent.futures
import http.server
import io
import json
import re
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openfarm_core import cli as cli_mod
from openfarm_core.adapters.files import FileCardSource
from openfarm_core.adapters.openai_compat import (
    OpenAICompatibleBackend,
    _MAX_RESPONSE_BYTES,
)
from openfarm_core.bootstrap import initialize_project
from openfarm_core.cards import Card
from openfarm_core.cli import build_parser, main as cli_main
from openfarm_core.config import load_config
from openfarm_core.coordinator import CoordinatorRuntime
from openfarm_core.errors import Infra, Refused
from openfarm_core.factory import (
    auto_land_card,
    evaluate_auto_land,
    trial_rebase_branch,
)
from openfarm_core.flow import CardRun
from openfarm_core.git import Git
from openfarm_core.notify import send_notification


class Wave1SeamStubCompatTests(unittest.TestCase):
    def test_openai_compat_zero_arg_read_stub_and_size_cap(self) -> None:
        class _ZeroArgResp:
            def __init__(self, payload: bytes) -> None:
                self._payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def read(self) -> bytes:
                return self._payload

        ok_body = json.dumps({
            "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2},
        }).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "k.key"
            key.write_text("sk-test\n", encoding="utf-8")
            key.chmod(0o600)
            backend = OpenAICompatibleBackend(
                endpoint="http://127.0.0.1:9/v1",
                allowed_endpoints=["http://127.0.0.1:9"],
                key_file=key,
                retries=0,
            )
            backend._opener = SimpleNamespace(open=lambda *_a, **_k: _ZeroArgResp(ok_body))
            comp = backend.complete("implementer", "m", 0.0, [{"role": "user", "content": "hi"}])
            self.assertEqual(comp.content, "hello")

            oversized = b"x" * (_MAX_RESPONSE_BYTES + 1)
            backend._opener = SimpleNamespace(open=lambda *_a, **_k: _ZeroArgResp(oversized))
            with self.assertRaises(Infra) as ctx:
                backend.complete("implementer", "m", 0.0, [{"role": "user", "content": "hi"}])
            self.assertEqual(ctx.exception.code, "response-too-large")

    def test_coordinator_free_slots_tolerates_minimal_state_and_respects_breaker(self) -> None:
        cfg = SimpleNamespace(kill_switch=Path("/nonexistent-kill-switch"))
        night = SimpleNamespace(exhausted=lambda: "", cards_cap=None, cards_claimed=0)
        minimal_state = SimpleNamespace(kill_requested=lambda _p: False, is_paused=lambda: False)
        rt = CoordinatorRuntime(
            board=SimpleNamespace(external_wait_count=lambda: 0),
            cfg=cfg,
            source=SimpleNamespace(),
            card_map={},
            pending_ids=["C1"],
            night=night,
            state=minimal_state,
            max_workers=2,
            deadline=None,
            launch_fn=lambda *_a, **_k: None,
            block_unstarted_fn=lambda *_a, **_k: None,
        )
        self.assertEqual(rt.free_slots(), 2)

        rt.state = SimpleNamespace(
            kill_requested=lambda _p: False,
            is_paused=lambda: False,
            read_breaker=lambda: {"tripped": True},
        )
        self.assertEqual(rt.free_slots(), 0)

    def test_flow_effective_unit_tolerates_minimal_card_stub(self) -> None:
        flow = object.__new__(CardRun)
        flow.card = SimpleNamespace(id="CARD-1")
        flow.worktree = Path(".")
        self.assertEqual(flow._effective_unit(), "")


class NotifyHardeningTests(unittest.TestCase):
    def _cfg_for(self, *, webhook_url: str = "", desktop: bool = False, events=()):
        return SimpleNamespace(
            name="demo",
            notifications=SimpleNamespace(
                webhook_url=webhook_url,
                desktop=desktop,
                events=tuple(events),
            ),
        )

    def test_notify_catches_http_exception_and_value_error(self) -> None:
        # Server that drops connection immediately -> http.client.RemoteDisconnected (HTTPException)
        class _DropHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.connection.close()

            def log_message(self, _fmt: str, *_args) -> None:
                return None

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _DropHandler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            res = send_notification(
                self._cfg_for(webhook_url=f"http://127.0.0.1:{port}/drop"),
                "wave_finished",
                "Done",
                "Body",
            )
            self.assertFalse(res["sent"])
            self.assertFalse(res["webhook"]["ok"])
            self.assertTrue(res["webhook"]["error"])
        finally:
            srv.shutdown()
            srv.server_close()

        # Malformed URL / bad port / bad timeout_s raise ValueError internally, never escape
        bad_port = send_notification(
            self._cfg_for(webhook_url="http://127.0.0.1:notaport/hook"),
            "wave_finished",
            "Done",
            "Body",
        )
        self.assertFalse(bad_port["sent"])
        self.assertIn("invalid", bad_port["webhook"]["error"].lower())

        bad_scheme = send_notification(
            self._cfg_for(webhook_url="file:///etc/passwd"),
            "wave_finished",
            "Done",
            "Body",
        )
        self.assertFalse(bad_scheme["sent"])
        self.assertIn("invalid", bad_scheme["webhook"]["error"].lower())

        bad_timeout = send_notification(
            self._cfg_for(webhook_url="http://127.0.0.1:9/hook"),
            "wave_finished",
            "Done",
            "Body",
            timeout_s="not-a-number",  # type: ignore[arg-type]
        )
        self.assertFalse(bad_timeout["sent"])
        self.assertTrue(bad_timeout["webhook"]["error"])

    def test_notify_blocks_cross_origin_and_non_http_redirects_but_allows_same_origin(self) -> None:
        secret_hits: list[str] = []
        same_origin_hits: list[dict] = []

        class _SecretHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                secret_hits.append(self.path)
                self.send_response(200)
                self.end_headers()

            def do_GET(self) -> None:
                secret_hits.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, _fmt: str, *_args) -> None:
                return None

        secret_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SecretHandler)
        secret_port = secret_srv.server_address[1]

        class _RedirectHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(n)
                if self.path == "/to-secret":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{secret_port}/steal")
                    self.end_headers()
                    return
                if self.path == "/to-metadata":
                    self.send_response(302)
                    self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
                    self.end_headers()
                    return
                if self.path == "/to-file":
                    self.send_response(302)
                    self.send_header("Location", "file:///etc/passwd")
                    self.end_headers()
                    return
                if self.path == "/to-same":
                    self.send_response(302)
                    self.send_header("Location", "/final")
                    self.end_headers()
                    return
                self.send_response(404)
                self.end_headers()

            def do_GET(self) -> None:
                if self.path == "/final":
                    same_origin_hits.append({"path": self.path})
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"ok")
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, _fmt: str, *_args) -> None:
                return None

        redir_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
        redir_port = redir_srv.server_address[1]
        t1 = threading.Thread(target=secret_srv.serve_forever, daemon=True)
        t2 = threading.Thread(target=redir_srv.serve_forever, daemon=True)
        t1.start()
        t2.start()
        try:
            for path in ("/to-secret", "/to-metadata", "/to-file"):
                res = send_notification(
                    self._cfg_for(webhook_url=f"http://127.0.0.1:{redir_port}{path}"),
                    "wave_finished",
                    "Done",
                    "Body",
                )
                self.assertFalse(res["sent"], f"expected redirect {path} to be refused: {res}")
                err_l = res["webhook"]["error"].lower()
                self.assertTrue(
                    "refused" in err_l or "not allowed" in err_l,
                    f"unexpected redirect error for {path}: {res}",
                )
            self.assertEqual(secret_hits, [])

            ok_res = send_notification(
                self._cfg_for(webhook_url=f"http://127.0.0.1:{redir_port}/to-same"),
                "wave_finished",
                "Done",
                "Same origin",
            )
            self.assertTrue(ok_res["sent"], ok_res)
            self.assertEqual(len(same_origin_hits), 1)
            self.assertEqual(same_origin_hits[0]["path"], "/final")
        finally:
            redir_srv.shutdown()
            redir_srv.server_close()
            secret_srv.shutdown()
            secret_srv.server_close()

    def test_notify_send_passes_double_dash_option_terminator(self) -> None:
        captured: list[list[str]] = []

        def _fake_run(argv, **_kwargs):
            captured.append(list(argv))
            return SimpleNamespace(returncode=0)

        with mock.patch("openfarm_core.notify.shutil.which", return_value="/usr/bin/notify-send"), \
             mock.patch("openfarm_core.notify.subprocess.run", side_effect=_fake_run):
            res = send_notification(
                self._cfg_for(desktop=True),
                "card_blocked",
                "-u critical",
                "--body-text",
            )
        self.assertTrue(res["sent"])
        self.assertTrue(res["desktop"]["ok"])
        self.assertEqual(captured, [["/usr/bin/notify-send", "--", "-u critical", "--body-text"]])


class FactoryHardeningAndConcurrencyTests(unittest.TestCase):
    def test_evaluate_auto_land_supports_real_gate_name_and_retries(self) -> None:
        cfg = SimpleNamespace(
            factory=SimpleNamespace(
                auto_land=True,
                auto_land_profiles=("lean",),
                require_zero_findings=True,
                require_frontier_consensus=False,
            )
        )
        card = Card("W1-01", "Helper", state="in_review", touches=["src/a.ts"], fields={"profile": "lean"})

        # Attempt 1 failed 'tests', attempt 2 passed 'tests' -> latest result is pass.
        retried_ok = {
            "gates": [
                {"name": "tests", "result": "fail"},
                {"name": "tests", "result": "pass"},
                {"name": "diff-within-touches", "result": "pass"},
            ]
        }
        self.assertTrue(evaluate_auto_land(card, cfg, record=retried_ok, findings=[])["eligible"])

        # Latest result is fail -> blocked with gate name preserved.
        still_failing = {
            "gates": [
                {"name": "tests", "result": "pass"},
                {"name": "lint", "result": "fail"},
            ]
        }
        verdict = evaluate_auto_land(card, cfg, record=still_failing, findings=[])
        self.assertFalse(verdict["eligible"])
        self.assertTrue(any("lint" in r for r in verdict["reasons"]))

    def test_auto_land_propagates_mark_done_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "prop-ts"
            state_dir = Path(tmp) / "st"
            initialize_project(
                "prop-ts",
                directory=repo,
                language="typescript",
                state_dir=state_dir,
                secrets_dir=Path(tmp) / "sec",
                commit=True,
            )
            cfg_file = repo / ".openfarm" / "config.toml"
            cfg_file.write_text(
                cfg_file.read_text(encoding="utf-8")
                + '\n[factory]\nauto_land = true\nintegration_branch = "openfarm/integration"\nauto_land_profiles = ["lean"]\n',
                encoding="utf-8",
            )
            git = Git(repo)
            git.commit_all("enable auto_land")
            cfg = load_config(repo)

            git.create_branch("openfarm/W1-01-x", "main")
            git.checkout("openfarm/W1-01-x")
            (repo / "src" / "x.ts").write_text("export const X = 1;\n", encoding="utf-8")
            git.commit_all("W1-01")
            git.checkout("main")

            # Card in card source is still 'ready' (not 'in_review'), while caller passes a stale in_review Card.
            src = FileCardSource(repo, "cards", worktree=state_dir / "cards-source", base_branch="main")
            src.prepare()
            src.add_card(Card("W1-01", "X", state="ready", touches=["src/x.ts"], done_text="## Done\n- x\n"))

            stale_card = Card("W1-01", "X", state="in_review", touches=["src/x.ts"], fields={"profile": "lean"})
            clean_record = {"gates": [{"name": "tests", "result": "pass"}]}
            with self.assertRaises(Refused) as ctx:
                auto_land_card(repo, cfg, src, stale_card, branch="openfarm/W1-01-x", record=clean_record, findings=[])
            self.assertEqual(ctx.exception.code, "not-in-review")

    def test_concurrent_auto_land_and_trial_rebase_do_not_collide_or_lose_merges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "conc-ts"
            state_dir = Path(tmp) / "st"
            initialize_project(
                "conc-ts",
                directory=repo,
                language="typescript",
                state_dir=state_dir,
                secrets_dir=Path(tmp) / "sec",
                commit=True,
            )
            cfg_file = repo / ".openfarm" / "config.toml"
            cfg_file.write_text(
                cfg_file.read_text(encoding="utf-8")
                + '\n[factory]\nauto_land = true\nintegration_branch = "openfarm/integration"\nauto_land_profiles = ["lean"]\n',
                encoding="utf-8",
            )
            git = Git(repo)
            git.commit_all("enable auto_land")
            cfg = load_config(repo)

            # Create 3 independent branches & cards
            card_specs = [("W1-01", "a"), ("W1-02", "b"), ("W1-03", "c")]
            main_before = git.branch_head("main")

            for cid, name in card_specs:
                br = f"openfarm/{cid}-{name}"
                git.create_branch(br, "main")
                git.checkout(br)
                (repo / "src" / f"{name}.ts").write_text(f"export const {name.upper()} = 1;\n", encoding="utf-8")
                git.commit_all(f"implement {cid}")
                git.checkout("main")

            src = FileCardSource(repo, "cards", worktree=state_dir / "cards-source", base_branch="main")
            src.prepare()
            for cid, name in card_specs:
                src.add_card(
                    Card(
                        cid,
                        f"Card {name}",
                        state="in_review",
                        touches=[f"src/{name}.ts"],
                        done_text=f"## Done\n- {name}\n",
                        fields={"profile": "lean"},
                    )
                )
            clean_record = {"gates": [{"name": "tests", "result": "pass"}]}

            # Concurrent trial_rebase_branch calls must never collide on worktree name
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                reb_futs = [
                    pool.submit(trial_rebase_branch, repo, f"openfarm/{cid}-{name}", "main", state_dir)
                    for cid, name in card_specs
                ]
                reb_results = [f.result() for f in reb_futs]
            self.assertTrue(all(r["ok"] for r in reb_results), reb_results)

            # Concurrent auto_land_card calls must serialize via flock and preserve ALL 3 merges
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                land_futs = [
                    pool.submit(
                        auto_land_card,
                        repo,
                        cfg,
                        src,
                        Card(cid, f"Card {name}", state="in_review", touches=[f"src/{name}.ts"], fields={"profile": "lean"}),
                        branch=f"openfarm/{cid}-{name}",
                        record=clean_record,
                        findings=[],
                    )
                    for cid, name in card_specs
                ]
                land_results = [f.result() for f in land_futs]
            self.assertTrue(all(r["landed"] and r.get("marked_done") for r in land_results), land_results)
            self.assertEqual(git.branch_head("main"), main_before)

            # Every card is marked done and every file exists on openfarm/integration
            for cid, name in card_specs:
                self.assertEqual(src.read(cid)["card"]["state"], "done")
                show = subprocess.run(
                    ["git", "show", f"openfarm/integration:src/{name}.ts"],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(show.returncode, 0, f"missing src/{name}.ts on openfarm/integration: {show.stderr}")


class CliWiringNotificationsFactoryAndViewTests(unittest.TestCase):
    def _init_wired_repo(self, tmp: str, *, webhook_url: str = "", auto_land: bool = False, breaker: int = 2, swarm: bool = False):
        repo = Path(tmp) / "wired-app"
        state_dir = Path(tmp) / "st"
        initialize_project(
            "wired-app",
            directory=repo,
            language="typescript",
            state_dir=state_dir,
            secrets_dir=Path(tmp) / "sec",
            commit=True,
        )
        cfg_file = repo / ".openfarm" / "config.toml"
        text = cfg_file.read_text(encoding="utf-8")
        if re.search(r"(?m)^circuit_breaker\s*=", text):
            text = re.sub(r"(?m)^circuit_breaker\s*=\s*\d+", f"circuit_breaker = {breaker}", text, count=1)
        elif "[gates]" in text:
            text = text.replace("[gates]\n", f"[gates]\ncircuit_breaker = {breaker}\n", 1)
        else:
            text += f"\n[gates]\ncircuit_breaker = {breaker}\n"
        extra = []
        if webhook_url:
            extra.append(
                f'\n[notifications]\nwebhook_url = "{webhook_url}"\ndesktop = false\nevents = []\n'
            )
        if auto_land:
            extra.append(
                '\n[factory]\nauto_land = true\nintegration_branch = "openfarm/integration"\nauto_land_profiles = ["lean"]\nrequire_zero_findings = true\n'
            )
        if swarm:
            if "[swarm]" in text:
                text = re.sub(r"(?ms)(\[swarm\].*?^enabled\s*=\s*)false", r"\1true", text, count=1)
            else:
                extra.append('\n[swarm]\nenabled = true\ncoordinator = "code"\nsupervisor = "off"\n')
        cfg_file.write_text(text + "".join(extra), encoding="utf-8")
        git = Git(repo)
        git.commit_all("configure notifications/factory")
        return repo, state_dir, git

    def _run_with_stubbed_card(self, repo: Path, state_dir: Path, fake_run_card, *, cards: int = 2) -> dict:
        buf = io.StringIO()
        with mock.patch.object(cli_mod, "_handshake", return_value={"refused": False, "reason": "", "adapters": {}}), \
             mock.patch.object(cli_mod, "_run_card", side_effect=fake_run_card), \
             redirect_stdout(buf):
            rc = cli_main(["--repo", str(repo), "--json", "run", "--cards", str(cards)])
        self.assertEqual(rc, 0, buf.getvalue())
        return json.loads(buf.getvalue())

    def test_cli_run_wires_notifications_and_auto_land_then_promote(self) -> None:
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
                repo, state_dir, git = self._init_wired_repo(
                    tmp,
                    webhook_url=f"http://127.0.0.1:{port}/hook",
                    auto_land=True,
                )
                src = FileCardSource(repo, "cards", worktree=state_dir / "cards-source", base_branch="main")
                src.prepare()
                src.add_card(
                    Card(
                        "W1-01",
                        "Small helper",
                        state="ready",
                        touches=["src/helper.ts"],
                        done_text="## Done\n- helper\n",
                        fields={"category": "mechanical", "profile": "lean"},
                    )
                )
                main_before = git.branch_head("main")

                def _fake_run_card(ctx, source, card_id, run_id, *_args, **_kwargs):
                    card = Card.from_dict(source.read(card_id)["card"])
                    claim = source.claim(card_id, f"openfarm:{run_id}", 60)
                    branch = f"openfarm/{card.id}-{card.slug()}"
                    git.create_branch(branch, "main")
                    git.checkout(branch)
                    (repo / "src" / "helper.ts").write_text("export const HELPER = 7;\n", encoding="utf-8")
                    git.commit_all("implement W1-01")
                    git.checkout("main")
                    card_dir = ctx.state.create_card(card.id, run_id)
                    (card_dir / "gates" / "summary.json").write_text(
                        json.dumps({"gates": [{"name": "tests", "result": "pass"}]}),
                        encoding="utf-8",
                    )
                    source.move(card_id, claim["lease_id"], "in-review", note_md="all green")
                    return {
                        "card": card_id,
                        "card_id": card_id,
                        "final_state": "in-review",
                        "branch": branch,
                        "findings_filed": [],
                        "detail": "all green",
                    }

                run_rec = self._run_with_stubbed_card(repo, state_dir, _fake_run_card, cards=1)
                self.assertTrue(run_rec["outcomes"][0]["auto_land"]["landed"])
                self.assertEqual(src.read("W1-01")["card"]["state"], "done")
                self.assertEqual(git.branch_head("main"), main_before)
                self.assertTrue(git.branch_exists("openfarm/integration"))

                events = [item["event"] for item in received]
                self.assertIn("card_in_review", events)
                self.assertIn("wave_finished", events)

                # Now `openfarm land --promote-integration` actually promotes openfarm/integration -> main!
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cli_main(["--repo", str(repo), "--json", "land", "--promote-integration"])
                self.assertEqual(rc, 0, buf.getvalue())
                promoted = json.loads(buf.getvalue())
                self.assertTrue(promoted["promoted"])
                self.assertTrue((repo / "src" / "helper.ts").is_file())
        finally:
            srv.shutdown()
            srv.server_close()

    def test_cli_run_notifies_card_blocked_and_breaker_tripped_and_swarm_auto_land(self) -> None:
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
                repo, state_dir, _git = self._init_wired_repo(
                    tmp,
                    webhook_url=f"http://127.0.0.1:{port}/hook",
                    breaker=2,
                )
                src = FileCardSource(repo, "cards", worktree=state_dir / "cards-source", base_branch="main")
                src.prepare()
                for cid in ("W1-01", "W1-02"):
                    src.add_card(
                        Card(
                            cid,
                            f"Card {cid}",
                            state="ready",
                            touches=[f"src/{cid.lower()}.ts"],
                            done_text="## Done\n- x\n",
                        )
                    )

                def _fake_blocked(ctx, source, card_id, run_id, *_args, **_kwargs):
                    claim = source.claim(card_id, f"openfarm:{run_id}", 60)
                    source.move(card_id, claim["lease_id"], "blocked", stop_code="tests-failed")
                    return {
                        "card": card_id,
                        "card_id": card_id,
                        "final_state": "blocked",
                        "stop_code": "tests-failed",
                        "detail": "unit test failed",
                    }

                self._run_with_stubbed_card(repo, state_dir, _fake_blocked, cards=2)
                events = [item["event"] for item in received]
                self.assertEqual(events.count("card_blocked"), 2)
                self.assertEqual(events.count("breaker_tripped"), 1)
                self.assertEqual(events.count("wave_finished"), 1)
        finally:
            srv.shutdown()
            srv.server_close()

        # Also verify swarm path wires auto_land cleanly
        with tempfile.TemporaryDirectory() as tmp:
            repo, state_dir, git = self._init_wired_repo(tmp, auto_land=True, swarm=True)
            src = FileCardSource(repo, "cards", worktree=state_dir / "cards-source", base_branch="main")
            src.prepare()
            src.add_card(
                Card(
                    "W1-01",
                    "Swarm helper",
                    state="ready",
                    touches=["src/sw.ts"],
                    done_text="## Done\n- sw\n",
                    fields={"category": "mechanical", "profile": "lean"},
                )
            )

            def _fake_swarm_card(ctx, source, card_id, run_id, *_args, **_kwargs):
                card = Card.from_dict(source.read(card_id)["card"])
                claim = source.claim(card_id, f"openfarm:{run_id}", 60)
                branch = f"openfarm/{card.id}-{card.slug()}"
                git.create_branch(branch, "main")
                git.checkout(branch)
                (repo / "src" / "sw.ts").write_text("export const SW = 1;\n", encoding="utf-8")
                git.commit_all("implement swarm W1-01")
                git.checkout("main")
                card_dir = ctx.state.create_card(card.id, run_id)
                (card_dir / "gates" / "summary.json").write_text(
                    json.dumps({"gates": [{"name": "tests", "result": "pass"}]}),
                    encoding="utf-8",
                )
                source.move(card_id, claim["lease_id"], "in-review", note_md="ok")
                return {
                    "card": card_id,
                    "card_id": card_id,
                    "final_state": "in-review",
                    "branch": branch,
                    "findings_filed": [],
                    "detail": "ok",
                }

            run_rec = self._run_with_stubbed_card(repo, state_dir, _fake_swarm_card, cards=1)
            self.assertTrue(run_rec["outcomes"][0]["auto_land"]["landed"])
            self.assertEqual(src.read("W1-01")["card"]["state"], "done")
            self.assertTrue(git.branch_exists("openfarm/integration"))

    def test_cli_view_choices_include_textual_and_curses_views(self) -> None:
        parser = build_parser()
        expected_views = (
            "overview", "cards", "dossier", "board", "swarm", "harness", "ops",
            "studio", "review", "projects", "findings", "engine", "providers",
        )
        for view in expected_views:
            ns_tui = parser.parse_args(["tui", "--view", view])
            self.assertEqual(ns_tui.view, view)
            ns_auto = parser.parse_args(["auto", "--view", view])
            self.assertEqual(ns_auto.view, view)

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["tui", "--view", "not-a-real-view"])


if __name__ == "__main__":
    unittest.main()
