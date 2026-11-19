"""v0.9.1 token accounting, concurrency, harness scope, and multi-wave tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openfarm_core.adapters.files import FileCardSource
from openfarm_core.cards import Card
from openfarm_core.coordinator import CoordinatorRuntime, retry_covers
from openfarm_core.globs import _globs_overlap_norm
from openfarm_core.harness_scope import project_markers
from openfarm_core.tui import _read_cards_readonly
from tests.test_python_e2e import build_python_toy


class AccountingConcurrencyTests(unittest.TestCase):
    def test_wave2_cards_sync_on_prepare(self) -> None:
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        toy.write_card(
            "CARD-1", title="Wave 1 card", category="pinned-bugfix",
            unit=".", touches=["tally/bounded.py"],
            tests=[{"name": "test_a", "file": "tests/test_a.py"}],
            done_text="Done 1.",
        )
        toy.commit("wave 1")
        src = FileCardSource(toy.root, "cards", worktree=toy.root.parent / "cards-wt", base_branch="main")
        src.prepare()
        self.assertEqual([c.id for c in src.all_cards()], ["CARD-1"])

        # Add a Wave 2 card on main after openfarm-cards branch already exists
        toy.write_card(
            "CARD-2", title="Wave 2 card", category="pinned-bugfix",
            unit=".", touches=["tally/bounded.py"],
            tests=[{"name": "test_b", "file": "tests/test_b.py"}],
            done_text="Done 2.",
        )
        toy.commit("wave 2")
        src.prepare()
        self.assertIn("CARD-2", [c.id for c in src.all_cards()])

    def test_tui_read_cards_readonly_does_not_call_prepare(self) -> None:
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        toy.write_card(
            "CARD-1", title="Wave 1 card", category="pinned-bugfix",
            unit=".", touches=["tally/bounded.py"],
            tests=[{"name": "test_a", "file": "tests/test_a.py"}],
            done_text="Done 1.",
        )
        toy.commit("wave 1")
        from types import SimpleNamespace
        from openfarm_core.cli import Context
        ctx = Context(toy.root)
        fsrc = ctx.cards()
        fsrc.prepare()
        tui_ctx = SimpleNamespace(configured=True, repo=toy.root, cfg=ctx.cfg, state=ctx.state)

        with mock.patch.object(FileCardSource, "prepare", side_effect=AssertionError("prepare must not be called")):
            cards, mode = _read_cards_readonly(tui_ctx)
        self.assertEqual([c.id for c in cards], ["CARD-1"])
        self.assertEqual(mode, "cards-source")

    def test_harness_scope_collects_ts_and_go_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "web.ts").write_text("export function AcmeCustomWidget() { return 1; }\n")
            (root / "api.go").write_text("package api\nfunc AcmeGoHandler() {}\n")
            markers = project_markers(root, "acme", ["web.ts", "api.go"])
            self.assertIn("acmecustomwidget", markers)
            self.assertIn("acmegohandler", markers)

    def test_coordinator_can_launch_respects_deps_and_retry_covers_ladder(self) -> None:
        c1 = Card.from_dict({
            "id": "CARD-1", "title": "First", "state": "ready", "unit": ".",
            "touches": ["a.py"], "tests": [{"name": "t1", "file": "tests/t1.py"}],
            "fields": {"Category": "pinned-bugfix", "Done": "d1"},
        })
        c2 = Card.from_dict({
            "id": "CARD-2", "title": "Second", "state": "ready", "unit": ".",
            "touches": ["b.py"], "deps": ["CARD-1"],
            "tests": [{"name": "t2", "file": "tests/t2.py"}],
            "fields": {"Category": "pinned-bugfix", "Done": "d2"},
        })
        from types import SimpleNamespace
        fake_state = SimpleNamespace(kill_requested=lambda _k: False, is_paused=lambda: False)
        fake_cfg = SimpleNamespace(kill_switch=None, max_cards=0)
        fake_night = SimpleNamespace(exhausted=lambda: False, cards_run=0, cards_cap=None)
        rt = CoordinatorRuntime(
            board=None, cfg=fake_cfg, source=None,
            card_map={"CARD-1": c1, "CARD-2": c2},
            pending_ids=["CARD-1", "CARD-2"],
            night=fake_night, state=fake_state,
            max_workers=2, deadline=None,
            launch_fn=lambda cid: None,
            block_unstarted_fn=lambda cid, code, detail: None,
        )
        self.assertTrue(rt.can_launch("CARD-1"))
        self.assertFalse(rt.can_launch("CARD-2"))

        class _FakeCfg:
            effort_ladder = ("medium", "high", "max")
            def effort_for_role(self, role, attempt):
                idx = min(max(0, attempt - 1), len(self.effort_ladder) - 1)
                return self.effort_ladder[idx], "ladder"

        self.assertTrue(retry_covers(_FakeCfg(), "implementer", "medium", "max"))

    def test_globs_overlap_cache_size(self) -> None:
        self.assertGreaterEqual(_globs_overlap_norm.cache_info().maxsize, 16384)


if __name__ == "__main__":
    unittest.main()
