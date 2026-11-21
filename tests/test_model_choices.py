"""Which model does which job: chosen for every project, or for one, and laid over its config."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openfarm_core import model_choices as mc
from openfarm_core.bootstrap import initialize_project
from openfarm_core.config import load_config


class ModelChoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"OPENFARM_PROJECTS_FILE": str(self.base / "projects.json"),
                                               "OPENFARM_MODELS_FILE": str(self.base / "global" / "models.toml")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = self.base / "app"
        initialize_project("app", directory=self.root, idea="a page", language="typescript",
                           state_dir=self.base / "state", secrets_dir=self.base / "secrets", commit=True)

    def test_with_no_choice_the_config_is_used_as_it_is(self) -> None:
        cfg = load_config(self.root)
        self.assertEqual({v["model_from"] for v in cfg.model_choices.values()}, {"config"})
        self.assertEqual(cfg.micromanager.effort, "medium")
        self.assertFalse(mc.project_path(self.root).exists())

    def test_a_choice_for_every_project_is_used_and_one_for_this_project_wins(self) -> None:
        before = load_config(self.root)
        mc.save_choice(mc.global_path(), "reviewer", model="opus", effort="high")
        mc.save_choice(mc.global_path(), "architect", effort="max")
        mc.save_choice(mc.global_path(), "builder", model="acme/coder-2", effort="high")
        cfg = load_config(self.root)
        self.assertEqual((cfg.micromanager.model, cfg.micromanager.effort), ("opus", "high"))
        self.assertEqual(cfg.model_choices["architect"]["effort"], "max")
        self.assertEqual(cfg.model_choices["architect"]["model_from"], "config")
        for role in ("test_writer", "implementer", "checker", "planner"):
            self.assertEqual(cfg.role_models[role].model, "acme/coder-2", role)
            self.assertEqual(cfg.effort_for_role(role, 1)[0], "high", role)
            self.assertEqual(cfg.effort_for_role(role, 2)[0], "max", role)  # a retry still climbs
        self.assertEqual(cfg.role_models["implementer"].temperature, before.role_models["implementer"].temperature)

        mc.save_choice(mc.project_path(self.root), "reviewer", effort="low")
        cfg = load_config(self.root)
        self.assertEqual((cfg.micromanager.model, cfg.micromanager.effort), ("opus", "low"))
        self.assertEqual(cfg.model_choices["reviewer"]["effort_from"], "project")
        self.assertEqual(cfg.model_choices["reviewer"]["model_from"], "global")
        # config.toml itself is never rewritten
        self.assertNotIn("opus", (self.root / ".openfarm" / "config.toml").read_text())

        mc.save_choice(mc.project_path(self.root), "reviewer")  # both empty: the choice is removed
        self.assertEqual(load_config(self.root).micromanager.effort, "high")

    def test_the_architect_gets_its_own_model_and_effort(self) -> None:
        from openfarm_core import automode

        mc.save_choice(mc.project_path(self.root), "architect", model="fable", effort="xhigh")
        mc.save_choice(mc.project_path(self.root), "reviewer", model="sonnet", effort="low")
        worker = mock.Mock(effort="low", timeout_s=600.0)
        with mock.patch.object(automode, "inspect_acp_status", return_value={"oauth_ready": True}), \
                mock.patch("openfarm_core.adapters.claude_worker.open_acp_worker", return_value=worker) as opened:
            backend, _name = automode.open_architect_backend(self.root)
        self.assertEqual(opened.call_args.args[0].micromanager.model, "sonnet")  # the reviewer's stays the reviewer's
        self.assertEqual(worker.effort, "xhigh")
        self.assertEqual(backend._model, "fable")

    def test_a_bad_choice_is_refused_and_a_broken_file_is_no_choice(self) -> None:
        with self.assertRaises(ValueError):
            mc.save_choice(mc.global_path(), "builder", effort="xhigh")  # builders have no xhigh
        with self.assertRaises(ValueError):
            mc.save_choice(mc.global_path(), "reviewer", model='a"b')
        with self.assertRaises(ValueError):
            mc.save_choice(mc.global_path(), "manager", model="x")
        mc.global_path().parent.mkdir(parents=True, exist_ok=True)
        mc.global_path().write_text("[reviewer\neffort = ")
        self.assertEqual(mc.load_choices(mc.global_path()), {})
        mc.global_path().write_text('[reviewer]\neffort = "silly"\nmodel = "opus"\n')
        self.assertEqual(mc.load_choices(mc.global_path()), {"reviewer": {"model": "opus", "effort": ""}})
        self.assertEqual(load_config(self.root).micromanager.effort, "medium")

    def _cli(self, *argv: str, cwd: Path | None = None) -> tuple[int, str, str]:
        from openfarm_core.cli import main

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--repo", str(cwd or self.root), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_the_command_shows_sets_and_clears(self) -> None:
        code, out, _err = self._cli("models")
        self.assertEqual(code, 0)
        self.assertIn("Builders", out)
        self.assertIn("from config.toml", out)
        self.assertEqual(self._cli("models", "set", "architect", "--effort", "high")[0], 0)
        self.assertEqual(self._cli("models", "set", "architect", "--model", "opus")[0], 0)  # the effort is kept
        self.assertEqual(mc.load_choices(mc.project_path(self.root))["architect"], {"model": "opus", "effort": "high"})
        self.assertEqual(self._cli("models", "set", "builder", "--effort", "low", "--global")[0], 0)
        code, out, _err = self._cli("--json", "models")
        jobs = json.loads(out)["jobs"]
        self.assertEqual((jobs["architect"]["model"], jobs["architect"]["effort_from"]), ("opus", "project"))
        self.assertEqual((jobs["builder"]["effort"], jobs["builder"]["effort_from"]), ("low", "global"))
        self.assertEqual(self._cli("models", "clear", "architect")[0], 0)
        self.assertNotIn("architect", mc.load_choices(mc.project_path(self.root)))

        code, _out, err = self._cli("models", "set", "builder", "--effort", "xhigh")
        self.assertEqual(code, 2)
        self.assertIn("not one of", err)
        code, _out, err = self._cli("models", "set", "builder", "--effort", "low", cwd=self.base)
        self.assertEqual(code, 2)
        self.assertIn("--global", err)


if __name__ == "__main__":
    unittest.main()
