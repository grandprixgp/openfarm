"""v0.10.0 Pillar 3 tests: Findings Inbox core, durable decisions, folding, and CLI."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout

from openfarm_core.adapters.files import FileCardSource
from openfarm_core.cli import main as cli_main
from openfarm_core.config import load_config
from tests.test_python_e2e import build_python_toy


class FindingsInboxV010Tests(unittest.TestCase):
    def _seed_toy_with_findings(self):
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        toy.write_card(
            "CARD-1",
            title="Implement loader",
            category="small-feature",
            touches=["tally/bounded.py"],
            tests=[{"name": "test_a", "file": "tests/test_a.py"}],
            done_text="## Done\n\n- Loader returns expected item.",
        )
        toy.write_card(
            "CARD-2",
            title="Implement formatter",
            category="small-feature",
            touches=["tally/bounded.py"],
            tests=[{"name": "test_b", "file": "tests/test_b.py"}],
            done_text="## Done\n\n- Formatter returns expected text.",
        )
        toy.commit("add cards")
        cfg = load_config(toy.root)
        src = FileCardSource(toy.root, "cards", worktree=cfg.state_dir / "cards-source")
        src.prepare()
        f1 = src.finding("CARD-1", "Null guard missing in parser", "Parser crashes on empty string input.")
        f2 = src.finding("CARD-1", "Unused helper function", "Old helper _legacy_parse is dead code.")
        f3 = src.finding("CARD-2", "Formatting edge case for negative zero", "Ensure -0.0 renders as 0.0.")
        return toy, cfg, src, (f1["id"], f2["id"], f3["id"])

    def test_list_findings_newest_first_counts_and_card_lookup(self) -> None:
        from openfarm_core.findings import findings_for_card, list_findings

        toy, cfg, _src, (f1, f2, f3) = self._seed_toy_with_findings()
        res = list_findings(toy.root, cfg, state_filter="waiting")
        self.assertEqual(res["counts"]["waiting"], 3)
        self.assertEqual(res["counts"]["picked"], 0)
        # Newest first -> FIND-3, FIND-2, FIND-1
        ids = [item["id"] for item in res["findings"]]
        self.assertEqual(ids, [f3, f2, f1])
        self.assertEqual(res["findings"][0]["from_card"], "CARD-2")
        self.assertEqual(res["findings"][0]["from_card_title"], "Implement formatter")

        card1_findings = findings_for_card(toy.root, cfg, "CARD-1")
        self.assertEqual({f["id"] for f in card1_findings}, {f1, f2})
        self.assertTrue(all(f["body_preview"] for f in card1_findings))

    def test_durable_decisions_pick_dismiss_and_fold_remove_from_backlog(self) -> None:
        from openfarm_core.findings import (
            decide_finding,
            list_findings,
            picked_findings_for_planning,
        )

        toy, cfg, _src, (f1, f2, f3) = self._seed_toy_with_findings()

        # w: pick f1 for next wave
        d1 = decide_finding(toy.root, cfg, f1, "next-wave")
        self.assertEqual(d1["decision"], "next-wave")
        # x: dismiss f2 with reason
        d2 = decide_finding(toy.root, cfg, f2, "dismissed", reason="already cleaned up")
        self.assertEqual(d2["decision"], "dismissed")
        self.assertEqual(d2["reason"], "already cleaned up")
        # c: fold f3 into CARD-2
        d3 = decide_finding(toy.root, cfg, f3, "folded", target_card="CARD-2")
        self.assertEqual(d3["decision"], "folded")
        self.assertEqual(d3["folded_into"], "CARD-2")

        # Re-read from fresh FileCardSource to verify durability across restarts
        fresh_src = FileCardSource(toy.root, "cards", worktree=cfg.state_dir / "cards-source")
        fresh_src.prepare()
        backlog_ids = [c.id for c in fresh_src.all_cards() if c.state == "backlog"]
        self.assertNotIn(f2, backlog_ids)
        self.assertNotIn(f3, backlog_ids)

        card2 = fresh_src.read("CARD-2")["card"]
        self.assertIn(f3, card2["done_text"])
        self.assertIn("Ensure -0.0 renders as 0.0.", card2["done_text"])

        summary = list_findings(toy.root, cfg, state_filter="all")
        self.assertEqual(summary["counts"]["waiting"], 0)
        self.assertEqual(summary["counts"]["picked"], 1)
        self.assertEqual(summary["counts"]["dismissed"], 1)
        self.assertEqual(summary["counts"]["folded"], 1)

        picked = picked_findings_for_planning(toy.root, cfg)
        self.assertEqual([p["id"] for p in picked], [f1])

    def test_cli_findings_list_pick_dismiss_and_fold(self) -> None:
        toy, _cfg, _src, (f1, f2, f3) = self._seed_toy_with_findings()

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(["--repo", str(toy.root), "--json", "findings", "list"])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["counts"]["waiting"], 3)

        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            self.assertEqual(cli_main(["--repo", str(toy.root), "--json", "findings", "pick", f1]), 0)
            self.assertEqual(cli_main(["--repo", str(toy.root), "--json", "findings", "dismiss", f2, "--reason", "not needed"]), 0)
            self.assertEqual(cli_main(["--repo", str(toy.root), "--json", "findings", "fold", f3, "--into", "CARD-2"]), 0)


if __name__ == "__main__":
    unittest.main()
