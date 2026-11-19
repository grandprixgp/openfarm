"""v0.8.8: operational hardening and network-off runner bwrap."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openfarm_core import confine, logins
from openfarm_core.adapters.direct import DirectRunner
from openfarm_core.config import ConfigError, load_config
from openfarm_core.gates.checks import mutation_gate


class RunnerNetworkConfinementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v088-hard-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()

    def _write_cfg(self, runner_extra: str = "") -> Path:
        of = self.repo / ".openfarm"
        of.mkdir(parents=True, exist_ok=True)
        state = self.tmp / "state"
        key = self.tmp / "key.txt"
        key.write_text("k\n")
        (of / "config.toml").write_text(
            f"""
[project]
name = "demo"
state_dir = "{state}"

[adapters]
model = "scripted"
micromanager = "off"

[model]
key_file = "{key}"

[policy]
categories = ["pinned-bugfix"]

[runner]
{runner_extra}
""".strip() + "\n"
        )
        return self.repo

    def test_confine_network_config_defaults_off_and_validates(self) -> None:
        cfg = load_config(self._write_cfg())
        self.assertEqual(cfg.runner_confine_network, "off")
        cfg_on = load_config(self._write_cfg('confine_network = "on"\n'))
        self.assertEqual(cfg_on.runner_confine_network, "on")
        with self.assertRaises(ConfigError):
            load_config(self._write_cfg('confine_network = "maybe"\n'))

    def test_runner_argv_unshares_network_by_default_and_shares_when_on(self) -> None:
        argv_off = confine.runner_argv(
            ["/bin/true"], worktree=self.repo, bwrap="/usr/bin/bwrap", network="off",
        )
        self.assertIn("--unshare-net", argv_off)
        self.assertNotIn("--share-net", argv_off)

        argv_on = confine.runner_argv(
            ["/bin/true"], worktree=self.repo, bwrap="/usr/bin/bwrap", network="on",
        )
        self.assertIn("--share-net", argv_on)
        self.assertNotIn("--unshare-net", argv_on)

    @unittest.skipUnless(confine.bwrap_status()[0], "needs usable bwrap")
    def test_real_bwrap_unshare_net_blocks_network_and_records_confinement(self) -> None:
        runner = DirectRunner(
            self.repo,
            confine={"write_roots": [], "hide_paths": [], "network": "off"},
        )
        # With --unshare-net, connecting to a non-loopback address fails with ENETUNREACH.
        probe = (
            "import socket, sys\n"
            "s = socket.socket()\n"
            "s.settimeout(1.0)\n"
            "try:\n"
            "    s.connect(('1.1.1.1', 80))\n"
            "    print('CONNECTED')\n"
            "except OSError as e:\n"
            "    print('BLOCKED', e.errno)\n"
        )
        res = runner.exec("test", [sys.executable, "-c", probe], cwd=self.repo)
        self.assertIn("BLOCKED", res.stdout)
        self.assertTrue(res.confined)
        self.assertEqual(res.confine_network, "off")
        self.assertTrue(res.runner_argv)


class StaleLoginRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v088-login-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_refresh_isolated_updates_copy_when_host_login_refreshes(self) -> None:
        host_home = self.tmp / "host"
        iso_home = self.tmp / "iso"
        (host_home / ".claude").mkdir(parents=True)
        (iso_home / ".claude").mkdir(parents=True)
        old_payload = json.dumps({
            "claudeAiOauth": {
                "accessToken": "old-at", "refreshToken": "old-rt",
                "expiresAt": 1700000000000, "subscriptionType": "max",
            }
        }).encode()
        new_payload = json.dumps({
            "claudeAiOauth": {
                "accessToken": "new-at", "refreshToken": "new-rt",
                "expiresAt": 1900000000000, "subscriptionType": "max",
            }
        }).encode()
        src = host_home / ".claude" / ".credentials.json"
        dst = iso_home / ".claude" / ".credentials.json"
        src.write_bytes(old_payload)
        dst.write_bytes(old_payload)

        # Unchanged -> no copy
        res0, cur0 = logins.refresh_isolated(
            "claude", isolated_home=iso_home, source=src, initial=old_payload,
        )
        self.assertEqual(res0.status, "unchanged")
        self.assertEqual(cur0, old_payload)

        # Host refreshed -> isolated copy updated
        src.write_bytes(new_payload)
        res1, cur1 = logins.refresh_isolated(
            "claude", isolated_home=iso_home, source=src, initial=old_payload,
        )
        self.assertEqual(res1.status, "refreshed")
        self.assertEqual(cur1, new_payload)
        self.assertEqual(dst.read_bytes(), new_payload)


class ScopedMutationZeroJudgedTests(unittest.TestCase):
    def test_scoped_non_backfill_zero_judged_passes_with_note(self) -> None:
        parsed = {"killed": 0, "survived": 0, "total": 1, "unviable": 1,
                  "timeout": 0, "judged": 0, "capped": False, "scoped": True}
        gate = mutation_gate(parsed, 0.8, "required", "pinned-bugfix",
                             required_for=("test-backfill",))
        self.assertTrue(gate.ok, gate.detail)
        self.assertIn("diff", gate.detail.lower())

        # test-backfill stays strict even if scoped flag were set
        gate_tb = mutation_gate(parsed, 0.8, "required", "test-backfill",
                                required_for=("test-backfill",))
        self.assertFalse(gate_tb.ok)


if __name__ == "__main__":
    unittest.main()
