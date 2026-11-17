"""v0.8.1c glue, G6: `acp status` and `acp probe` say, in their own tables, how
each provider's child is held and where its login was found.

The payloads come from `inspect_acp_status` with fake binaries (nothing real is
started) and a mocked bwrap check. `cmd_acp` no longer prints a block of its own
beside the formatter, and the JSON payload is the same as before.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core import cli
from openfarm_core import confine as confine_lib
from openfarm_core.acp_setup import inspect_acp_status
from openfarm_core.human_cli import Style, format_human_strict


class AcpHumanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v081c-acp-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.codex = bin_dir / "codex"
        self.codex.write_text("#!/bin/sh\necho 'Not logged in'\nexit 1\n")
        self.codex.chmod(0o755)
        self.home = self.tmp / "home"
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".codex" / "auth.json").write_text("{}")
        self.claude_dir = self.tmp / "claude-config"
        self.claude_dir.mkdir()

    def status(self, provider: str = "all", usable: bool = True) -> dict:
        with mock.patch.object(confine_lib, "bwrap_status",
                               return_value=(usable, "bwrap works" if usable else "missing")):
            return inspect_acp_status(
                None, provider=provider, home=self.home, claude_bin="", acp_bin="",
                agy_bin="", antigravity_acp_bin="", codex_bin=str(self.codex),
                codex_acp_bin="", env={"CLAUDE_CONFIG_DIR": str(self.claude_dir)})

    @staticmethod
    def human(kind: str, payload: dict) -> str:
        return format_human_strict(kind, payload, style=Style(enabled=False))

    @staticmethod
    def row(text: str, provider: str) -> str:
        table = text.split("Provider Children", 1)[1]
        return next(line for line in table.splitlines()
                    if line.strip().startswith(provider + " "))

    def test_status_shows_each_providers_hold_and_login_in_its_own_table(self) -> None:
        payload = self.status()
        text = self.human("acp_status", payload)
        self.assertIn("Provider Children", text)
        self.assertIn("LOGIN FOUND IN", text)
        claude, codex, agy = (self.row(text, p) for p in ("claude", "codex", "antigravity"))
        self.assertIn("CLAUDE_CONFIG_DIR (not found)", claude)
        self.assertIn("nothing", claude)
        self.assertIn("$HOME (found)", codex)
        self.assertIn("flags + boundary", codex)
        self.assertIn("$HOME (not found)", agy)
        self.assertIn("boundary", agy)
        self.assertIn("Confinement", text)
        self.assertIn('[micromanager] confine = "auto"', text)
        self.assertIn("usable (bwrap works)", text)

    def test_a_refused_hold_says_why(self) -> None:
        payload = self.status("codex", usable=False)
        text = self.human("acp_status", payload)
        self.assertIn("refused", self.row(text, "codex"))
        why = payload["providers"]["codex"]["confinement"]["read-only_refused"]
        self.assertIn(f"codex, tool policy read-only: {why}", text)
        self.assertIn("not usable (missing)", text)

    def test_probe_shows_the_same_table_from_the_probes_own_hold(self) -> None:
        status = self.status("codex")
        held = {"mode": "auto", "transport": "codex-exec",
                "none": {"held_by": "flags + boundary", "refused": ""},
                "read-only": {"held_by": "flags + boundary", "refused": ""}}
        payload = {**status, "ok": True, "provider": "codex", "protocol_version": 1,
                   "auth_methods": [], "confinement": held, "action": "probe"}
        text = self.human("acp_probe", payload)
        row = self.row(text, "codex")
        self.assertIn("flags + boundary", row)
        self.assertIn("$HOME (found)", row)
        self.assertIn('[micromanager] confine = "auto"', text)
        # The probe for all providers carries each one's hold in its results.
        everything = self.status()
        everything.update({"action": "probe", "results": {
            "codex": {"ok": True, "provider": "codex", "protocol_version": 1,
                      "confinement": {"mode": "auto", "transport": "codex-exec",
                                      "none": {"held_by": "boundary"},
                                      "read-only": {"held_by": "boundary"}}},
            "antigravity": {"ok": None, "skipped": True, "error": "not probed"}}})
        text = self.human("acp_probe", everything)
        self.assertIn("codex-exec", self.row(text, "codex"))

    def test_cmd_acp_prints_through_the_formatter_only_and_json_is_unchanged(self) -> None:
        payload = self.status()
        for action, target in (("status", "inspect_acp_status"), ("probe", "probe_acp")):
            with self.subTest(action):
                given = {**payload, "action": action}
                with mock.patch.object(cli, target, return_value=dict(given)):
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        code = cli.main(["acp", action, "--repo", str(self.tmp)])
                    self.assertEqual(code, 0)
                    text = out.getvalue()
                    self.assertEqual(text.count("Provider Children"), 1)
                    self.assertNotIn("tool policy none ->", text)
                    self.assertEqual(text.count("Confinement"), 1)
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        cli.main(["--json", "acp", action, "--repo", str(self.tmp)])
                    self.assertEqual(json.loads(out.getvalue()), given)

    def test_other_acp_payloads_still_render(self) -> None:
        for kind, payload in (("acp_status", {"action": "status"}),
                              ("acp_login", {"action": "login",
                                             "login_command": ["claude", "auth", "login"]}),
                              ("acp_probe", {"action": "probe", "providers": "odd"})):
            with self.subTest(kind):
                text = self.human(kind, payload)
                self.assertNotIn("Provider Children", text)


if __name__ == "__main__":
    unittest.main()
