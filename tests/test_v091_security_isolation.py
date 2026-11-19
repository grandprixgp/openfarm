"""v0.9.1 security & isolation hardening tests (Carbon audit #3, #4, #9, #13, #19)."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from openfarm_core import confine, logins
from openfarm_core.adapters.acp import _sanitised_env
from openfarm_core.adapters.files import FileCardSource
from openfarm_core.adapters.openai_compat import _within
from openfarm_core.config import apply_supervision_profile, load_config
from openfarm_core.config_template import render_config
from openfarm_core.errors import Invalid, Refused
from openfarm_core.packs.typescript import link_node_modules
from openfarm_core.state import StateDir


class SecurityIsolationTests(unittest.TestCase):
    def test_claude_settings_deny_grep_and_glob(self) -> None:
        env, iso = _sanitised_env(
            provider="claude",
            auth_mode="oauth",
            auth_file=None,
            isolate_home=True,
            never_touch=["src/invariants.rs"],
            forbidden_files=[".openfarm/**"],
        )
        self.assertIsNotNone(iso)
        try:
            settings = json.loads((iso / ".claude" / "settings.json").read_text())
            deny = settings["permissions"]["deny"]
            self.assertIn("Grep(src/invariants.rs)", deny)
            self.assertIn("Glob(src/invariants.rs)", deny)
            self.assertIn("Grep(.openfarm/**)", deny)
            self.assertIn("Glob(.openfarm/**)", deny)
        finally:
            import shutil
            shutil.rmtree(iso, ignore_errors=True)

    def test_scan_masks_multi_symlink_same_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "shared"
            shared.mkdir()
            secret = shared / "secret.txt"
            secret.write_text("top-secret\n")
            os.symlink(shared, root / "link_a")
            os.symlink(shared, root / "link_b")

            files, dirs = confine.scan_masks(root, ["link_b/secret.txt"])
            resolved = {p.resolve() for p in files}
            self.assertIn(secret.resolve(), resolved)

    def test_refresh_isolated_refuses_symlink_in_isolated_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = root / "iso"
            iso.mkdir()
            victim_dir = root / "victim"
            victim_dir.mkdir()
            os.symlink(victim_dir, iso / ".claude")

            src = root / "host-creds.json"
            payload = json.dumps({
                "claudeAiOauth": {"accessToken": "tok-new", "refreshToken": "ref", "expiresAt": 9999999999999}
            }).encode()
            src.write_bytes(payload)

            res, _ = logins.refresh_isolated("claude", isolated_home=iso, source=src, initial=None)
            self.assertEqual(res.status, "refused")
            self.assertFalse((victim_dir / ".credentials.json").exists())

    def test_link_node_modules_refuses_marker_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base"
            wt = root / "wt"
            (base / "node_modules" / "pkg").mkdir(parents=True)
            (wt / "node_modules").mkdir(parents=True)
            victim = root / "victim.txt"
            victim.write_text("do-not-overwrite\n")
            os.symlink(victim, wt / "node_modules" / ".openfarm-linked")

            link_node_modules(wt, base)
            self.assertEqual(victim.read_text(), "do-not-overwrite\n")

    def test_openai_within_rejects_file_ftp_and_encoded_traversal(self) -> None:
        allowed = ["https://api.example.com/team-a/v1"]
        self.assertFalse(_within("file:///etc/passwd", ["file:///etc"]))
        self.assertFalse(_within("ftp://api.example.com/team-a/v1", ["ftp://api.example.com/team-a/v1"]))
        self.assertFalse(_within("https://api.example.com/team-a/v1/..%2f..%2fteam-b", allowed))
        self.assertFalse(_within("https://api.example.com/team-a/v1/%2e%2e%5cteam-b", allowed))
        self.assertTrue(_within("https://api.example.com/team-a/v1/chat/completions", allowed))

    def test_file_card_source_safe_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = FileCardSource(root, "cards", worktree=root / "wt")
            with self.assertRaises((Invalid, Refused)):
                src._path("../escape")

    def test_profile_preserves_explicit_bwrap_and_confine_network_polarity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            text = render_config(
                name="p", language="python", base_branch="main",
                state_dir=Path(tmp) / "state", key_file=Path(tmp) / "key",
            ).replace('confine = "off"', 'confine = "bwrap"')
            p = root / ".openfarm" / "config.toml"
            p.parent.mkdir(parents=True)
            p.write_text(text)

            cfg = load_config(root)
            apply_supervision_profile(cfg, "lean")
            self.assertEqual(cfg.runner_confine, "bwrap")

            apply_supervision_profile(cfg, "strict")
            self.assertEqual(cfg.runner_confine, "bwrap")
            self.assertEqual(cfg.runner_confine_network, "on")

    def test_state_dir_mode_0700(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sd = StateDir(Path(tmp) / "state")
            mode = stat.S_IMODE(sd.root.stat().st_mode)
            self.assertEqual(mode, 0o700)


if __name__ == "__main__":
    unittest.main()
