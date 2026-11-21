"""Regression tests for v0.20 Wave 2 gate fix and live dogfooding fixes."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openfarm_core import cli as cli_mod
from openfarm_core.adapters.direct import DirectRunner
from openfarm_core.adapters.files import FileCardSource
from openfarm_core.adopt import baseline_known_failing
from openfarm_core.bootstrap import initialize_project
from openfarm_core.cards import Card
from openfarm_core.cli import build_parser, main as cli_main
from openfarm_core.config import load_config
from openfarm_core.git import Git
from openfarm_core.human_cli import Style, format_human
from openfarm_core.packs.python import PythonPack
from openfarm_core.store import Store
from openfarm_core.tryit import how_to_start, try_it


class Wave2GateNotifyTests(unittest.TestCase):
    def test_notify_run_event_swallows_errors_without_banned_pattern(self) -> None:
        with mock.patch(
            "openfarm_core.notify.send_notification",
            side_effect=RuntimeError("boom"),
        ):
            # Must never raise into the run loop.
            cli_mod._notify_run_event(SimpleNamespace(), "wave_finished", "t", "b")


class BinaryRevertCheckAndBytecodeTests(unittest.TestCase):
    def test_git_show_blob_bytes_handles_non_utf8_binary_files(self) -> None:
        raw = b"\xcb\x0d\x0d\x0a\x00\x00\x00\x00\xe9\xff\xfe\x80"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git = Git(repo)
            git.init("main")
            (repo / "fixture.bin").write_bytes(raw)
            commit = git.commit_all("add binary fixture")
            self.assertEqual(git.show_blob_bytes(f"{commit}:fixture.bin"), raw)
            self.assertIsNone(git.show_blob_bytes(f"{commit}:missing.bin"))

    def test_revert_check_restores_binary_blob_from_frozen_commit(self) -> None:
        raw = b"\xcb\x0d\x0d\x0a\x00\x00\x00\x00\xe9\xff\xfe"
        from openfarm_core.flow import CardRun
        from tests.test_python_e2e import GOOD_IMPLEMENTATION, build_python_toy, script

        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        toy.write_card(
            "CARD-1",
            title="A full bundle refuses a push",
            category="pinned-bugfix",
            unit=".",
            touches=["tally/bounded.py", "tests/fixtures/sample.bin"],
            tests=[{
                "name": "test_a_full_bundle_refuses_a_push",
                "file": "tests/test_full.py",
                "asserts": "push raises FullError",
            }],
            done_text="`Bounded(1).push(8)` after one push raises `FullError`.",
        )
        toy.commit("tally: CARD-1")
        test_src = (
            "import unittest\n"
            "from pathlib import Path\n"
            "from tally.bounded import Bounded, FullError\n\n"
            "class FullTests(unittest.TestCase):\n"
            "    def test_a_full_bundle_refuses_a_push(self):\n"
            "        data = (Path(__file__).parent / 'fixtures' / 'sample.bin').read_bytes()\n"
            "        self.assertEqual(data[:4], b'\\xcb\\x0d\\x0d\\x0a')\n"
            "        bundle = Bounded(1)\n"
            "        bundle.push(7)\n"
            "        with self.assertRaises(FullError):\n"
            "            bundle.push(8)\n"
        )
        orig_commit = CardRun._commit

        def _commit_with_binary(run_self: CardRun, message: str) -> str:
            if message.endswith(": freeze the tests"):
                fix = run_self.worktree / "tests" / "fixtures" / "sample.bin"
                fix.parent.mkdir(parents=True, exist_ok=True)
                fix.write_bytes(raw)
            return orig_commit(run_self, message)

        script_path = script(**{
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + test_src,
            "CARD-1/implementer": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: a full bundle refuses a push",
            "CARD-1/checker_pass2": "{}",
        })
        with mock.patch.object(CardRun, "_commit", autospec=True, side_effect=_commit_with_binary):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli_main([
                    "--repo", str(toy.root), "--json", "run",
                    "--card", "CARD-1", "--script", str(script_path), "--cards", "1",
                ])
        self.assertEqual(rc, 0, buf.getvalue())
        outcome = json.loads(buf.getvalue())["outcomes"][0]
        self.assertEqual(
            outcome["final_state"],
            "in-review",
            f"{outcome.get('stop_code')}: {outcome.get('detail')}",
        )

    def test_python_pack_and_direct_runner_disable_bytecode_writes(self) -> None:
        pack = PythonPack(ruff=False)
        for purpose in ("test", "list", "lint", "ws-lint", "typecheck", "build"):
            cmd = pack.commands(purpose)
            self.assertEqual(cmd.get("env", {}).get("PYTHONDONTWRITEBYTECODE"), "1", purpose)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mylib.py").write_text("X = 7\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_mylib.py").write_text(
                "import unittest\nimport mylib\n"
                "class T(unittest.TestCase):\n"
                "    def test_x(self): self.assertEqual(mylib.X, 7)\n",
                encoding="utf-8",
            )
            pack.configure(root)
            runner = DirectRunner(root)
            res = runner.exec("test", pack.commands("test")["argv"], cwd=root)
            self.assertEqual(res.cls, "pass", res.stderr)
            self.assertEqual(list(root.rglob("*.pyc")), [])
            self.assertEqual(list(root.rglob("__pycache__")), [])


class Python314PlainTestsDirAndBootstrapTests(unittest.TestCase):
    def test_python_pack_and_adopt_work_without_tests_init_py(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "plain-py"
            root.mkdir()
            (root / "pyproject.toml").write_text(
                '[project]\nname = "plain-py"\nversion = "0.1.0"\n',
                encoding="utf-8",
            )
            (root / "calc.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_calc.py").write_text(
                "import unittest\n"
                "from calc import add\n\n"
                "class CalcTests(unittest.TestCase):\n"
                "    def test_add(self):\n"
                "        self.assertEqual(add(2, 3), 5)\n",
                encoding="utf-8",
            )

            pack = PythonPack(ruff=False)
            pack.configure(root)
            argv = pack.commands("test")["argv"]
            self.assertNotIn("-t", argv)

            run_env = {**os.environ, **(pack.commands("test").get("env") or {})}
            done = subprocess.run(argv, cwd=root, env=run_env, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            parsed = pack.parse_tests(done.stdout, done.stderr)
            self.assertEqual([r["name"] for r in parsed["results"]], ["test_add"])
            self.assertEqual(pack.row_file(parsed["results"][0]), "tests/test_calc.py")

            listed = subprocess.run(
                pack.commands("list")["argv"],
                cwd=root,
                env=run_env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertEqual(
                [t["name"] for t in pack.parse_list(listed.stdout)["tests"]],
                ["test_add"],
            )

            adopted = baseline_known_failing(root, "python", target_dir=Path(tmp) / "target")
            self.assertTrue(adopted["ran"])
            self.assertEqual(adopted["known_failing"], [])
            self.assertEqual(list(root.rglob("*.pyc")), [])

    def test_check_baseline_works_without_tests_init_py(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "plain-check"
            state_dir = Path(tmp) / "state"
            initialize_project(
                "plain-check",
                directory=root,
                language="python",
                idea="Plain tests dir",
                state_dir=state_dir,
                secrets_dir=Path(tmp) / "secrets",
                commit=False,
            )
            # Simulate an adopted repo that has tests/test_baseline.py without tests/__init__.py.
            (root / "tests" / "__init__.py").unlink()
            git = Git(root)
            git.commit_all("repo without tests/__init__.py")
            cfg = load_config(root)
            src = FileCardSource(root, "cards", worktree=state_dir / "cards-source", base_branch="main")
            src.prepare()
            src.add_card(
                Card(
                    "W1-01",
                    "Baseline check",
                    state="ready",
                    touches=["src/plain_check/helper.py"],
                    done_text="## Done\n- helper\n",
                    fields={
                        "category": "pinned-bugfix",
                        "tests": [{"name": "test_helper", "file": "tests/test_helper.py", "asserts": "helper returns 1"}],
                    },
                )
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli_main(["--repo", str(root), "--json", "check", "W1-01", "--baseline"])
            self.assertEqual(rc, 0, buf.getvalue())
            payload = json.loads(buf.getvalue())
            self.assertTrue(payload["baseline"]["ok"], payload)

    def test_init_python_scaffolds_tests_init_and_gitignore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "py-init"
            initialize_project(
                "py-init",
                directory=root,
                language="python",
                idea="Demo",
                state_dir=Path(tmp) / "state",
                secrets_dir=Path(tmp) / "secrets",
                commit=True,
            )
            self.assertTrue((root / "tests" / "__init__.py").is_file())
            gitignore = (root / ".gitignore").read_text(encoding="utf-8")
            self.assertIn("__pycache__/", gitignore)
            self.assertIn("*.pyc", gitignore)

            pack = PythonPack(ruff=False)
            pack.configure(root)
            argv = pack.commands("test")["argv"]
            self.assertIn("-t", argv)
            done = subprocess.run(
                argv,
                cwd=root,
                env={**os.environ, **(pack.commands("test").get("env") or {})},
                capture_output=True,
                text=True,
            )
            self.assertEqual(done.returncode, 0, done.stderr)


class TryItSrcLayoutTests(unittest.TestCase):
    def test_python_pack_try_it_sets_pythonpath_for_src_layout_and_preserves_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "src-cli"
            initialize_project(
                "src-cli",
                directory=root,
                language="python",
                idea="CLI tool",
                state_dir=Path(tmp) / "state",
                secrets_dir=Path(tmp) / "secrets",
                commit=True,
            )
            (root / "src" / "src_cli" / "__main__.py").write_text(
                "import os\n"
                "from src_cli import project_name\n"
                'print(project_name(), os.environ.get("PYTHONPATH", ""))\n',
                encoding="utf-8",
            )
            cfg = load_config(root)

            plan = how_to_start(root, cfg)
            self.assertEqual(plan["kind"], "cli")
            self.assertEqual(plan["env"], {"PYTHONPATH": str(root / "src")})

            with mock.patch.dict(os.environ, {"PYTHONPATH": "/existing/extra"}):
                result = try_it(root, cfg)
            self.assertTrue(result["ok"], result)
            self.assertIn("src-cli", result["output"])
            self.assertIn(f"{root / 'src'}{os.pathsep}/existing/extra", result["output"])

    def test_try_it_merges_plan_env_for_web_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "src-web"
            initialize_project(
                "src-web",
                directory=root,
                language="python",
                idea="Web app",
                state_dir=Path(tmp) / "state",
                secrets_dir=Path(tmp) / "secrets",
                commit=True,
            )
            (root / "src" / "src_web" / "__main__.py").write_text(
                "import http.server, os\n"
                "from src_web import project_name\n"
                "class H(http.server.BaseHTTPRequestHandler):\n"
                "    def do_GET(self):\n"
                "        self.send_response(200)\n"
                '        self.send_header("Content-Type", "text/html")\n'
                "        self.end_headers()\n"
                '        self.wfile.write(f"<h1>{project_name()}</h1>".encode())\n'
                "    def log_message(self, *a): return None\n"
                'http.server.HTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()\n',
                encoding="utf-8",
            )
            cfg_path = root / ".openfarm" / "config.toml"
            cfg_path.write_text(
                cfg_path.read_text(encoding="utf-8")
                + f'\n[try]\nstart = "{sys.executable} -m src_web"\n',
                encoding="utf-8",
            )
            cfg = load_config(root)
            with mock.patch("openfarm_core.tryit.find_browser", return_value=None):
                result = try_it(root, cfg)
            checks = {c["name"]: c for c in result["checks"]}
            self.assertTrue(checks["loads"]["ok"], result)


class DogfoodUxSnagsTests(unittest.TestCase):
    def test_daemon_start_accepts_follow_flag(self) -> None:
        parser = build_parser()
        ns = parser.parse_args(["daemon", "start", "--cards", "2", "--follow"])
        self.assertTrue(ns.follow)

        captured: dict = {}

        def _fake_start(repo, **kwargs):
            captured.update(kwargs)
            return {"pid": 12345, "log_path": "/tmp/daemon.log"}

        with mock.patch("openfarm_core.daemon.start_daemon", side_effect=_fake_start), \
             redirect_stdout(io.StringIO()):
            rc = cli_main(["daemon", "start", "--cards", "2", "--follow"])
        self.assertEqual(rc, 0)
        self.assertTrue(captured["follow"])

    def test_store_skip_and_drop_return_ok_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "store-skip"
            initialize_project(
                "store-skip",
                directory=root,
                language="python",
                idea="Skip test",
                state_dir=Path(tmp) / "state",
                secrets_dir=Path(tmp) / "secrets",
                commit=True,
            )
            cfg = load_config(root)
            store = Store(root, cfg)
            # Create two cards: one ready (passing), one blocked/failing preflight.
            store.src.add_card(
                Card("W1-01", "Ready card", state="ready", touches=["src/store_skip/a.py"],
                     done_text="## Done\n- a\n")
            )
            store.src.add_card(
                Card("W1-02", "Blocked card", state="blocked", touches=[], done_text="## Done\n- b\n")
            )

            res_skip = store.change_card("W1-01", "skip", why="duplicate")
            self.assertTrue(res_skip["ok"], res_skip)
            self.assertEqual(res_skip["state"], "dropped")
            self.assertEqual(res_skip["checks"], [])

            res_drop = store.change_card("W1-02", "drop", why="obsolete")
            self.assertTrue(res_drop["ok"], res_drop)
            self.assertEqual(res_drop["state"], "dropped")
            self.assertEqual(res_drop["checks"], [])

    def test_auto_land_updates_outcome_state_to_done_and_human_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "autoland-ux"
            state_dir = Path(tmp) / "state"
            initialize_project(
                "autoland-ux",
                directory=root,
                language="python",
                idea="Auto land UX",
                state_dir=state_dir,
                secrets_dir=Path(tmp) / "secrets",
                commit=True,
            )
            cfg_path = root / ".openfarm" / "config.toml"
            cfg_path.write_text(
                cfg_path.read_text(encoding="utf-8")
                + "\n[factory]\nauto_land = true\nintegration_branch = \"openfarm/integration\"\n",
                encoding="utf-8",
            )
            cfg = load_config(root)
            ctx = SimpleNamespace(repo=root, cfg=cfg, state=SimpleNamespace(card_dir=lambda *_a: state_dir))
            src = FileCardSource(root, "cards", worktree=state_dir / "cards-source", base_branch="main")
            src.prepare()
            src.add_card(Card("W1-01", "Auto card", state="in_review", touches=["src/autoland_ux/a.py"],
                              done_text="## Done\n- a\n"))
            outcome = {
                "card": "W1-01",
                "card_id": "W1-01",
                "final_state": "in-review",
                "branch": "openfarm/W1-01-auto-card",
                "gates": [{"name": "tests", "result": "pass"}],
                "detail": "all green",
            }
            bus = SimpleNamespace(emit=lambda **_k: None)
            with mock.patch(
                "openfarm_core.factory.auto_land_card",
                return_value={"landed": True, "integration_branch": "openfarm/integration", "commit": "abc1234"},
            ):
                cli_mod._maybe_auto_land(ctx, src, "W1-01", "run-1", outcome, bus)

            self.assertEqual(outcome["final_state"], "done")
            self.assertTrue(outcome["auto_landed"])

            notified: list[tuple[str, str, str]] = []
            with mock.patch.object(
                cli_mod,
                "_notify_run_event",
                side_effect=lambda _cfg, ev, title, body: notified.append((ev, title, body)),
            ):
                cli_mod._notify_card_outcome(cfg, "W1-01", outcome)
            self.assertEqual(len(notified), 1)
            self.assertEqual(notified[0][0], "card_in_review")
            self.assertIn("auto-landed", notified[0][2])

            rendered = format_human(
                "run",
                {
                    "run_id": "run-1",
                    "outcomes": [outcome],
                    "tokens": {},
                    "caps": {"cards": 1, "spend": 0.0},
                },
                style=Style(enabled=False),
            )
            self.assertIn("done", rendered)
            self.assertNotIn("openfarm verdict", rendered)
            self.assertIn("openfarm land --promote-integration", rendered)


if __name__ == "__main__":
    unittest.main()
