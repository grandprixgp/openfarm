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

    def test_overlay_handles_string_and_omitted_builder_roles(self) -> None:
        cfg_path = self.root / ".openfarm" / "config.toml"
        cfg_path.write_text(
            f'[project]\nname = "app"\nstate_dir = "{self.base / "state"}"\n\n'
            '[adapters]\nlanguages = ["python"]\n\n'
            '[model]\nendpoint = "https://x.invalid/v1"\nallowed_endpoints = ["https://x.invalid/v1"]\n'
            'implementer = "old-model"\n\n'
            '[policy]\ncategories = ["pattern"]\n',
            encoding="utf-8",
        )
        mc.save_choice(mc.global_path(), "builder", model="new-builder", effort="high")
        cfg = load_config(self.root)
        for role in ("test_writer", "implementer", "checker", "planner"):
            self.assertEqual(cfg.role_models[role].model, "new-builder", role)
            self.assertEqual(cfg.effort_for_role(role, 1)[0], "high", role)

    def test_overlay_updates_default_tier_and_intersects_tier_efforts(self) -> None:
        cfg_path = self.root / ".openfarm" / "config.toml"
        base_cfg = (
            f'[project]\nname = "app"\nstate_dir = "{self.base / "state"}"\n\n'
            '[adapters]\nlanguages = ["python"]\n\n'
            '[model]\nendpoint = "https://x.invalid/v1"\nallowed_endpoints = ["https://x.invalid/v1"]\n'
            'implementer = "old-model"\n\n'
            '[policy]\ncategories = ["pattern"]\n\n'
        )
        # Explicit [tiers] with subset efforts (no "max") + [swarm] default_tier
        cfg_path.write_text(
            base_cfg
            + '[tiers.fast]\nmodel = "old-model"\nefforts = ["low", "medium", "high"]\n\n'
            + '[swarm]\ndefault_tier = "fast"\n',
            encoding="utf-8",
        )
        mc.save_choice(mc.global_path(), "builder", model="chosen-model", effort="medium")
        cfg = load_config(self.root)
        self.assertEqual(cfg.tiers["fast"].model, "chosen-model")
        self.assertEqual(cfg.member_defaults("implementer"), ("fast", "chosen-model"))
        self.assertEqual(cfg.effort_ladder, ("medium", "high"))

        # Tier with effort_param = "" must not crash load_config when global builder effort is set
        cfg_path.write_text(
            base_cfg + '[tiers.plain]\nmodel = "old-model"\neffort_param = ""\n',
            encoding="utf-8",
        )
        mc.save_choice(mc.global_path(), "builder", model="chosen-model", effort="high")
        cfg2 = load_config(self.root)
        self.assertEqual(cfg2.tiers["plain"].model, "chosen-model")
        self.assertEqual(cfg2.effort_for_role("implementer", 1), ("", ""))

        # Tier with disjoint efforts (["low"] vs global "high") must not crash load_config
        cfg_path.write_text(
            base_cfg + '[tiers.tiny]\nmodel = "old-model"\nefforts = ["low"]\n',
            encoding="utf-8",
        )
        cfg3 = load_config(self.root)
        self.assertEqual(cfg3.tiers["tiny"].model, "chosen-model")

    def test_save_choice_refuses_symlink_and_is_concurrency_safe(self) -> None:
        import threading

        target = self.base / "real-models.toml"
        target.write_text("", encoding="utf-8")
        link = self.base / "link-models.toml"
        link.symlink_to(target)
        with self.assertRaises(OSError):
            mc.save_choice(link, "architect", model="opus")

        shared = self.base / "concurrent" / "models.toml"
        errors: list[BaseException] = []

        def writer(role: str, model: str) -> None:
            try:
                for _ in range(10):
                    mc.save_choice(shared, role, model=model, effort="high")
            except BaseException as exc:
                errors.append(exc)

        t1 = threading.Thread(target=writer, args=("architect", "opus"))
        t2 = threading.Thread(target=writer, args=("builder", "coder"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(errors, [])
        loaded = mc.load_choices(shared)
        self.assertEqual(loaded["architect"]["model"], "opus")
        self.assertEqual(loaded["builder"]["model"], "coder")

    def test_open_architect_backend_applies_global_choice_in_new_folder(self) -> None:
        from openfarm_core import automode

        empty_dir = self.base / "brand-new"
        empty_dir.mkdir()
        mc.save_choice(mc.global_path(), "architect", model="opus-global", effort="xhigh")
        worker = mock.Mock(effort="medium", timeout_s=600.0)
        with mock.patch.object(automode, "inspect_acp_status", return_value={"oauth_ready": True}), \
                mock.patch("openfarm_core.adapters.claude_worker.open_acp_worker", return_value=worker):
            backend, _name = automode.open_architect_backend(empty_dir)
        self.assertIsNotNone(backend)
        self.assertEqual(backend._model, "opus-global")
        self.assertEqual(worker.effort, "xhigh")


if __name__ == "__main__":
    unittest.main()
