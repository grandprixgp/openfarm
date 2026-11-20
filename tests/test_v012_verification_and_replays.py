"""v0.12.0 tests: Architectural Invariants gate, Runtime Acceptance ('Proof-of-Life')
CLI + ephemeral HTTP gates, and Golden Replay / Regression Vault."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from openfarm_core.bootstrap import initialize_project
from openfarm_core.cli import main as cli_main
from openfarm_core.config import load_config


class VerificationAndReplaysV012Tests(unittest.TestCase):
    def test_architectural_invariants_forbidden_imports_max_bytes_and_exports(self) -> None:
        from openfarm_core.invariants import check_architectural_invariants

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "inv-py"
            initialize_project(
                "inv-py",
                directory=repo,
                language="python",
                state_dir=Path(tmp) / "st",
                secrets_dir=Path(tmp) / "sec",
                no_git=True,
            )
            cfg_file = repo / ".openfarm" / "config.toml"
            cfg_file.write_text(
                cfg_file.read_text(encoding="utf-8")
                + '\n[invariants]\n'
                + 'forbidden_imports = [{ path_glob = "src/**", disallow = ["requests", "textual"] }]\n'
                + 'max_file_bytes = [{ path_glob = "src/**", limit = 200 }]\n'
                + 'require_exports = [{ path = "src/inv_py/__init__.py", symbols = ["project_name"] }]\n',
                encoding="utf-8",
            )
            cfg = load_config(repo)

            # Baseline passes cleanly (except if __init__.py > 200 bytes; let's check)
            (repo / "src" / "inv_py" / "clean.py").write_text("import json\n", encoding="utf-8")
            (repo / "src" / "inv_py" / "invariants.py").write_text("SCHEMA_VERSION = 1\n", encoding="utf-8")
            (repo / "src" / "inv_py" / "__init__.py").write_text("def project_name(): return 'inv-py'\n", encoding="utf-8")
            ok_rep = check_architectural_invariants(repo, cfg)
            self.assertTrue(ok_rep["ok"], ok_rep)

            # Violate forbidden import & max_file_bytes
            (repo / "src" / "inv_py" / "bad.py").write_text(
                "import requests\n" + ("# padding\n" * 30),
                encoding="utf-8",
            )
            bad_rep = check_architectural_invariants(repo, cfg)
            self.assertFalse(bad_rep["ok"])
            rules_hit = {v["rule"] for v in bad_rep["violations"]}
            self.assertEqual(rules_hit, {"forbidden_imports", "max_file_bytes"})

    def test_runtime_acceptance_cli_and_ephemeral_http_smoke_gates(self) -> None:
        from openfarm_core.acceptance import run_acceptance_checks

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text("<html><body>Photo of the Day</body></html>\n", encoding="utf-8")
            ev_path = root / "acceptance.json"
            res = run_acceptance_checks(
                root,
                [
                    {
                        "name": "cli-hello",
                        "kind": "cli",
                        "cmd": [sys.executable, "-c", "print('hello factory')"],
                        "expect_exit": 0,
                        "expect_stdout_contains": "hello factory",
                        "timeout_s": 5.0,
                    },
                    {
                        "name": "http-home",
                        "kind": "http",
                        "start_cmd": [sys.executable, "-m", "http.server", "{PORT}", "--bind", "127.0.0.1"],
                        "url": "http://127.0.0.1:{PORT}/index.html",
                        "expect_status": 200,
                        "expect_body_contains": "Photo of the Day",
                        "startup_timeout_s": 5.0,
                    },
                ],
                evidence_path=ev_path,
            )
            self.assertTrue(res["ok"], res)
            self.assertEqual(len(res["results"]), 2)
            self.assertTrue(ev_path.is_file())

    def test_golden_replay_vault_add_list_and_run_via_cli(self) -> None:
        from openfarm_core.replay import add_replay, list_replays, run_replays

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "rep-app"
            initialize_project(
                "rep-app",
                directory=repo,
                language="python",
                state_dir=Path(tmp) / "st",
                secrets_dir=Path(tmp) / "sec",
                no_git=True,
            )
            add_replay(
                repo,
                "BUG-42",
                title="Empty caption returns non-zero exit",
                cmd=[sys.executable, "-c", "import sys; print('rejected:' + sys.stdin.read().strip()); sys.exit(0)"],
                stdin="blank",
                expect_exit=0,
                expect_stdout_contains="rejected:blank",
            )
            items = list_replays(repo)
            self.assertEqual([i["id"] for i in items], ["BUG-42"])

            run_res = run_replays(repo)
            self.assertTrue(run_res["ok"], run_res)
            self.assertEqual(run_res["passed"], 1)

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli_main(["--repo", str(repo), "--json", "replay", "run"])
            self.assertEqual(rc, 0)
            self.assertTrue(json.loads(buf.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
