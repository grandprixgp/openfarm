"""`openfarm setup`: asked step by step, or run unattended from flags, against a stand-in builders' address."""

from __future__ import annotations

import contextlib
import http.server
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openfarm_core import model_choices as mc
from openfarm_core.providers import load_global_providers
from openfarm_core.setup_helper import catalogue, run_setup

MODELS = ["acme/coder-1", "acme/coder-2", "other/chat"]


class _Models(http.server.BaseHTTPRequestHandler):
    seen: list[dict] = []

    def do_GET(self) -> None:  # noqa: N802
        type(self).seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                "zdr": self.headers.get("x-cmd-zdr")})
        # as long as a big provider's real list (several MB): it used to be cut at 512 KB and read as no models
        body = json.dumps({"data": [{"id": m, "description": "x" * 400_000} for m in MODELS]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass


def _args(**given) -> SimpleNamespace:
    base = dict(yes=False, json=False, frontier=None, preset=None, endpoint=None, api_key=None, key_file=None,
                no_zdr=False, timeout=5.0, project=False, repo=".",
                architect_model=None, architect_effort=None, reviewer_model=None, reviewer_effort=None,
                builder_model=None, builder_effort=None)
    return SimpleNamespace(**{**base, **given})


class SetupTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"OPENFARM_PROVIDERS_FILE": str(self.base / "providers.toml"),
                                               "OPENFARM_MODELS_FILE": str(self.base / "models.toml"),
                                               "OPENFARM_PROJECTS_FILE": str(self.base / "projects.json")})
        patcher.start()
        self.addCleanup(patcher.stop)
        _Models.seen = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Models)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.address = f"http://127.0.0.1:{self.server.server_port}/v1"

    def test_unattended_from_flags(self) -> None:
        result = run_setup(_args(yes=True, frontier="codex", endpoint=self.address, api_key="sk-test-not-real",
                                 builder_model="acme/coder-2", architect_effort="high", builder_effort="low"),
                           signed_in=lambda provider: True)
        saved = load_global_providers()
        self.assertEqual(saved["frontier"]["provider"], "codex")
        self.assertEqual((saved["worker"]["preset"], saved["worker"]["endpoint"], saved["worker"]["model"]),
                         ("custom", self.address, "acme/coder-2"))
        self.assertTrue(saved["worker"]["key_present"])
        self.assertNotIn("sk-test-not-real", (self.base / "providers.toml").read_text())  # the key is in its own file
        self.assertNotIn("sk-test-not-real", json.dumps(result))
        self.assertEqual(_Models.seen[-1], {"path": "/v1/models", "auth": "Bearer sk-test-not-real", "zdr": "1"})
        self.assertEqual((result["builders_answer"], result["models_listed"], result["signed_in"]), (True, 3, True))
        self.assertEqual(mc.load_choices(mc.global_path()),
                         {"architect": {"model": "", "effort": "high"}, "builder": {"model": "acme/coder-2", "effort": "low"}})

        # run again with one flag: everything else is kept, the key too
        run_setup(_args(yes=True, reviewer_effort="max"), signed_in=lambda provider: True)
        self.assertEqual(load_global_providers()["worker"]["model"], "acme/coder-2")
        self.assertTrue(load_global_providers()["worker"]["key_present"])
        self.assertEqual(mc.load_choices(mc.global_path())["reviewer"], {"model": "", "effort": "max"})
        self.assertEqual(mc.load_choices(mc.global_path())["architect"]["effort"], "high")

    def test_asked_step_by_step(self) -> None:
        answers = iter([
            "1",            # who plans: claude
            "y",            # sign in now
            "5",            # who builds: custom
            self.address,   # address
            "",             # keep nothing out: yes to zero retention
            "2",            # builders' model: the second listed
            "opus", "4",    # architect: model, effort xhigh
            "", "",         # reviewer: provider default, medium
            "3",            # builders: effort high
        ])
        asked: list[str] = []
        said: list[str] = []
        state = {"in": False}

        def ask(prompt: str) -> str:
            asked.append(prompt)
            return next(answers)

        result = run_setup(_args(), ask=ask, secret=lambda prompt: "sk-test-not-real", out=lambda *a: said.append(" ".join(a)),
                           signed_in=lambda provider: state["in"], sign_in=lambda provider: state.update({"in": True}))
        text = "\n".join(said)
        self.assertIn("Claude is not signed in.", text)
        self.assertIn("Claude is signed in.", text)  # after the sign-in it offered
        self.assertIn("lists 3 model(s)", text)
        self.assertIn("2. acme/coder-2", text)
        self.assertIn("Everything is ready", text)
        self.assertEqual(result["choices"], {"architect": {"model": "opus", "effort": "xhigh"},
                                             "builder": {"model": "acme/coder-2", "effort": "high"}})
        saved = load_global_providers()["worker"]
        self.assertEqual((saved["preset"], saved["endpoint"], saved["model"], saved["zdr"]),
                         ("custom", self.address, "acme/coder-2", True))
        with self.assertRaises(StopIteration):  # every prepared answer was asked for, and no more
            next(answers)

    def test_a_long_model_list_is_narrowed_by_typing_part_of_a_name(self) -> None:
        from openfarm_core.setup_helper import _pick_model

        models = [f"vendor{n}/chat-{n}" for n in range(60)] + ["acme/coder-1", "acme/coder-2"]
        answers = iter(["coder", "2"])
        said: list[str] = []
        self.assertEqual(_pick_model(lambda prompt: next(answers), said.append, models, "old/model"), "acme/coder-2")
        self.assertIn("62 models. Type part of a name", said[0])
        self.assertIn("  1. acme/coder-1", said)
        self.assertEqual(_pick_model(lambda prompt: "", said.append, models, "old/model"), "old/model")  # Enter keeps
        self.assertEqual(_pick_model(lambda prompt: "brand/new", said.append, models, "old/model"), "brand/new")

    def test_an_address_that_does_not_answer_is_said_and_setup_still_saves(self) -> None:
        said: list[str] = []
        with mock.patch("builtins.print"):
            result = run_setup(_args(yes=False, json=False, frontier="claude", preset="custom",
                                     endpoint="http://127.0.0.1:9/v1", api_key="k", no_zdr=True, builder_model="typed/name",
                                     architect_model="", architect_effort="medium", reviewer_model="",
                                     reviewer_effort="medium", builder_effort="medium", timeout=0.5),
                               ask=lambda prompt: self.fail(f"asked {prompt!r} though every answer was a flag"),
                               out=lambda *a: said.append(" ".join(a)), signed_in=lambda provider: False)
        self.assertFalse(result["builders_answer"])
        self.assertIn("did not answer", "\n".join(said))
        self.assertIn("still needs you", "\n".join(said))
        self.assertEqual(load_global_providers()["worker"]["model"], "typed/name")
        self.assertFalse(load_global_providers()["worker"]["zdr"])

    def test_the_catalogue_lists_what_each_job_can_be_given(self) -> None:
        run_setup(_args(yes=True, endpoint=self.address, api_key="k"))
        listed = catalogue()
        self.assertEqual(listed["builder"]["models"], MODELS)
        self.assertEqual(listed["builder"]["efforts"], ["low", "medium", "high", "max"])
        self.assertIn("xhigh", listed["architect"]["efforts"])
        self.assertEqual(listed["architect"]["models"], ["opus", "sonnet", "haiku"])

    def _cli(self, *argv: str) -> tuple[int, str, str]:
        from openfarm_core.cli import main

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--repo", str(self.base), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_the_command(self) -> None:
        with mock.patch("openfarm_core.acp_setup.inspect_acp_status", return_value={"oauth_ready": True}):
            code, out, _err = self._cli("--json", "setup", "--endpoint", self.address, "--api-key", "sk-test-not-real",
                                        "--builder-effort", "high")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["choices"]["builder"]["effort"], "high")
        self.assertNotIn("sk-test-not-real", out)
        code, out, _err = self._cli("models", "catalogue")
        self.assertEqual(code, 0)
        self.assertIn("acme/coder-1", out)
        code, _out, err = self._cli("setup", "--yes", "--builder-effort", "xhigh")
        self.assertEqual(code, 2)
        self.assertIn("not one of", err)
        code, _out, err = self._cli("setup", "--yes", "--project")
        self.assertEqual(code, 2)
        self.assertIn("not an openfarm project", err)
        code, _out, err = self._cli("setup")  # no terminal here
        self.assertEqual(code, 2)
        self.assertIn("needs a terminal", err)

    def test_unknown_preset_raises_refused_cleanly(self) -> None:
        from openfarm_core.errors import Refused

        with self.assertRaises(Refused) as caught:
            run_setup(_args(yes=True, preset="nonexistent-preset"), signed_in=lambda p: True)
        self.assertEqual(caught.exception.code, "unknown-preset")
        code, _out, err = self._cli("setup", "--yes", "--preset", "nonexistent-preset")
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)


if __name__ == "__main__":
    unittest.main()
