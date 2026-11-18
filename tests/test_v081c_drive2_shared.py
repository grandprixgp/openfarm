"""J6: the run's shared ACP supervisor passes the confinement settings itself, and a
confined standalone ACP server gets one server per card, rooted at the card's worktree."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openfarm_core import confine as confine_lib
from openfarm_core.adapters.acp import CODEX_NATIVE_ACP, acp_command
from openfarm_core.cli import _CardAcpSupervisor, _SharedAcpSupervisor
from openfarm_core.micromanager import ClaudeAcpMicromanager

STAND_IN = ROOT / "tests" / "acp_stand_in.py"
BWRAP_OK, BWRAP_WHY = confine_lib.bwrap_status()


class SharedSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v081c-d2-shared-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.worktrees = []
        for name in ("CARD-1-run", "CARD-2-run"):
            wt = self.tmp / "state" / "worktrees" / name
            wt.mkdir(parents=True)
            self.worktrees.append(wt)
        self.card_dirs = []
        for name in ("CARD-1", "CARD-2"):
            cd = self.tmp / "state" / "cards" / name
            cd.mkdir(parents=True)
            self.card_dirs.append(cd)

    def _proto(self, provider: str, command, *, confine=None) -> ClaudeAcpMicromanager:
        extra = {"confine": confine} if confine is not None else {}
        return ClaudeAcpMicromanager(command=command, cwd=self.repo, provider=provider,
                                     auth_mode="api-key", **extra)

    def _server(self, mode: str = "auto"):
        return acp_command([sys.executable, str(STAND_IN), "--mode", "normal"],
                           confine_lib.ConfineSettings(mode=mode))

    def test_the_settings_are_passed_explicitly(self) -> None:
        # A plain command list carries nothing; the supervisor's own settings apply.
        plain = [sys.executable, str(STAND_IN), "--mode", "normal"]
        proto = self._proto("claude", plain)
        proto.confine = confine_lib.ConfineSettings(mode="off")
        seat = _SharedAcpSupervisor(proto)
        self.addCleanup(seat.close)
        with seat.lock:
            self.assertEqual(seat.live_adapter().confine.mode, "off")
        card = seat.for_card(self.worktrees[0], self.card_dirs[0])
        self.assertEqual(card.confine.mode, "off")

    def test_the_micromanager_passes_its_settings_to_its_adapter(self) -> None:
        mm = ClaudeAcpMicromanager(command=[sys.executable, str(STAND_IN), "--mode", "normal"],
                                   cwd=self.repo, provider="claude",
                                   confine=confine_lib.ConfineSettings(mode="off"))
        self.addCleanup(mm.close)
        adapter, _sid = mm._ensure_session()
        self.assertEqual(adapter.confine.mode, "off")

    def test_a_confined_standalone_server_is_one_per_card(self) -> None:
        with mock.patch.object(confine_lib, "bwrap_status", return_value=(True, "bwrap works")):
            seat = _SharedAcpSupervisor(self._proto("antigravity", self._server()))
        self.addCleanup(seat.close)
        self.assertTrue(seat.per_card)
        sups = [seat.for_card(wt, cd) for wt, cd in zip(self.worktrees, self.card_dirs)]
        for sup, wt in zip(sups, self.worktrees):
            self.assertNotIsInstance(sup, _CardAcpSupervisor)
            self.assertEqual(sup.cwd, wt)
            self.assertEqual(sup.confine.mode, "auto")

    def test_claude_and_the_bridges_keep_the_one_process(self) -> None:
        bridge = acp_command([sys.executable, str(CODEX_NATIVE_ACP)],
                             confine_lib.ConfineSettings(mode="auto"))
        for provider, command in (("claude", self._server()), ("codex", bridge)):
            with self.subTest(provider=provider):
                with mock.patch.object(confine_lib, "bwrap_status",
                                       return_value=(True, "bwrap works")):
                    seat = _SharedAcpSupervisor(self._proto(provider, command))
                self.addCleanup(seat.close)
                self.assertFalse(seat.per_card)
                for wt, cd in zip(self.worktrees, self.card_dirs):
                    self.assertIsInstance(seat.for_card(wt, cd), _CardAcpSupervisor)

    @unittest.skipUnless(BWRAP_OK, f"bwrap is not usable here: {BWRAP_WHY}")
    def test_each_card_session_opens_inside_its_own_boundary(self) -> None:
        seat = _SharedAcpSupervisor(self._proto("antigravity", self._server()))
        self.addCleanup(seat.close)
        adapters = []
        for wt, cd in zip(self.worktrees, self.card_dirs):
            sup = seat.for_card(wt, cd)
            self.addCleanup(sup.close)
            adapter, sid = sup._ensure_session()
            self.assertTrue(sid)
            self.assertEqual(adapter.process_hold.describe(), "boundary")
            self.assertEqual(adapter._boundary_root, wt.resolve())
            adapters.append(adapter)
        self.assertIsNot(adapters[0], adapters[1])


if __name__ == "__main__":
    unittest.main()
