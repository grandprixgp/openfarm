"""v0.10.0 Pillar 1 tests: Global Provider Profile + Live Probe + Config Inheritance."""

from __future__ import annotations

import http.server
import io
import json
import os
import stat
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from openfarm_core.cli import main as cli_main


class _ProbeServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True


class ProvidersV010Tests(unittest.TestCase):
    def test_save_load_providers_and_symlink_safety(self) -> None:
        from openfarm_core.errors import Refused
        from openfarm_core.providers import (
            WORKER_PRESETS,
            load_global_providers,
            save_global_providers,
        )

        self.assertGreaterEqual(len(WORKER_PRESETS), 5)
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp) / "cfg"
            prov_path = cfg_dir / "providers.toml"
            key_path = cfg_dir / "worker.key"

            empty = load_global_providers(prov_path)
            self.assertFalse(empty["configured"])

            saved = save_global_providers(
                frontier_provider="claude",
                worker_preset="openrouter",
                endpoint="https://openrouter.ai/api/v1",
                model="qwen/qwen3-coder",
                key_file=key_path,
                api_key="sk-or-secret-test-12345",
                zdr=True,
                path=prov_path,
            )
            self.assertTrue(saved["configured"])
            self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(prov_path.stat().st_mode), 0o600)
            # Raw secret must NEVER be written into providers.toml
            self.assertNotIn("sk-or-secret-test-12345", prov_path.read_text(encoding="utf-8"))

            loaded = load_global_providers(prov_path)
            self.assertTrue(loaded["configured"])
            self.assertEqual(loaded["frontier"]["provider"], "claude")
            self.assertEqual(loaded["worker"]["endpoint"], "https://openrouter.ai/api/v1")
            self.assertEqual(loaded["worker"]["model"], "qwen/qwen3-coder")
            self.assertTrue(loaded["worker"]["key_present"])
            self.assertTrue(loaded["worker"]["zdr"])

            # Symlink key_file refused
            real_target = cfg_dir / "real.txt"
            real_target.write_text("x\n", encoding="utf-8")
            sym = cfg_dir / "sym.key"
            sym.symlink_to(real_target)
            with self.assertRaises(Refused):
                save_global_providers(
                    worker_preset="openrouter",
                    endpoint="https://openrouter.ai/api/v1",
                    model="qwen/qwen3-coder",
                    key_file=sym,
                    api_key="sk-nope",
                    path=prov_path,
                )

    def test_probe_worker_endpoint_sends_zdr_header_and_never_leaks_secret(self) -> None:
        from openfarm_core.providers import probe_worker_endpoint, save_global_providers

        seen_headers: dict[str, str] = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                for k, v in self.headers.items():
                    seen_headers[k.lower()] = v
                if self.path.endswith("/models"):
                    body = json.dumps({"data": [{"id": "qwen/qwen3-coder"}]}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, _fmt: str, *_args) -> None:
                return None

        srv = _ProbeServer(("127.0.0.1", 0), Handler)
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                key_path = Path(tmp) / "w.key"
                prov_path = Path(tmp) / "providers.toml"
                save_global_providers(
                    worker_preset="custom",
                    endpoint=f"http://127.0.0.1:{port}/v1",
                    model="qwen/qwen3-coder",
                    key_file=key_path,
                    api_key="sk-super-secret-xyz",
                    zdr=True,
                    path=prov_path,
                )
                res = probe_worker_endpoint(
                    f"http://127.0.0.1:{port}/v1",
                    key_file=key_path,
                    model="qwen/qwen3-coder",
                    zdr=True,
                    timeout_s=3.0,
                )
            self.assertTrue(res["ok"])
            self.assertEqual(res["status"], 200)
            self.assertTrue(res["zdr_sent"])
            self.assertTrue(res["model_found"])
            self.assertEqual(seen_headers.get("x-cmd-zdr"), "1")
            self.assertEqual(seen_headers.get("authorization"), "Bearer sk-super-secret-xyz")
            self.assertNotIn("sk-super-secret-xyz", json.dumps(res))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_load_config_inherits_global_provider_when_repo_has_placeholder(self) -> None:
        from openfarm_core.bootstrap import initialize_project
        from openfarm_core.config import load_config
        from openfarm_core.providers import save_global_providers

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "app"
            state_dir = Path(tmp) / "state"
            secrets_dir = Path(tmp) / "secrets"
            prov_path = Path(tmp) / "global" / "providers.toml"
            key_path = Path(tmp) / "global" / "worker.key"

            prev_env = os.environ.get("OPENFARM_PROVIDERS_FILE")
            os.environ["OPENFARM_PROVIDERS_FILE"] = str(prov_path)
            try:
                initialize_project(
                    "app",
                    directory=root,
                    state_dir=state_dir,
                    secrets_dir=secrets_dir,
                    no_git=True,
                )
                save_global_providers(
                    worker_preset="openrouter",
                    endpoint="https://openrouter.ai/api/v1",
                    model="qwen/qwen3-coder",
                    key_file=key_path,
                    api_key="sk-global-inherited",
                    zdr=True,
                    path=prov_path,
                )
                cfg = load_config(root)
                self.assertEqual(cfg.endpoint, "https://openrouter.ai/api/v1")
                self.assertIn("https://openrouter.ai/api/v1", cfg.allowed_endpoints)
                self.assertEqual(cfg.role_models["implementer"].model, "qwen/qwen3-coder")
                self.assertEqual(cfg.key_file, key_path.resolve())
            finally:
                if prev_env is None:
                    os.environ.pop("OPENFARM_PROVIDERS_FILE", None)
                else:
                    os.environ["OPENFARM_PROVIDERS_FILE"] = prev_env

    def test_cli_providers_status_and_set_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prov_path = Path(tmp) / "providers.toml"
            key_path = Path(tmp) / "worker.key"
            prev_env = os.environ.get("OPENFARM_PROVIDERS_FILE")
            os.environ["OPENFARM_PROVIDERS_FILE"] = str(prov_path)
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cli_main([
                        "--json", "providers", "set",
                        "--preset", "ollama",
                        "--model", "qwen2.5-coder:32b",
                        "--key-file", str(key_path),
                    ])
                self.assertEqual(rc, 0)
                data = json.loads(buf.getvalue())
                self.assertTrue(data["configured"])
                self.assertEqual(data["worker"]["endpoint"], "http://127.0.0.1:11434/v1")

                buf2 = io.StringIO()
                with redirect_stdout(buf2):
                    rc2 = cli_main(["--json", "providers", "status"])
                self.assertEqual(rc2, 0)
                st = json.loads(buf2.getvalue())
                self.assertTrue(st["configured"])
                self.assertEqual(st["worker"]["model"], "qwen2.5-coder:32b")
            finally:
                if prev_env is None:
                    os.environ.pop("OPENFARM_PROVIDERS_FILE", None)
                else:
                    os.environ["OPENFARM_PROVIDERS_FILE"] = prev_env


if __name__ == "__main__":
    unittest.main()
