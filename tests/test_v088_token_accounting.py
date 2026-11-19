"""v0.8.8: fresh vs. cached input token accounting across adapters, budget, records and bench."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openfarm_core.adapters.acp import AcpResult, AcpUsage
from openfarm_core.adapters.openai_compat import Completion
from openfarm_core.bench import score, render_report
from openfarm_core.budget import Budget
from openfarm_core.micromanager import MicromanagerBudget


class TokenAccountingUnitTests(unittest.TestCase):
    def test_completion_and_acp_usage_split_fresh_and_cached(self) -> None:
        c = Completion("ok", tokens_in=1000, tokens_out=200, tokens_in_cached=750)
        self.assertEqual(c.tokens_in_cached, 750)
        self.assertEqual(c.tokens_in_fresh, 250)
        d = c.as_dict()
        self.assertEqual(d["tokens_in_cached"], 750)
        self.assertEqual(d["tokens_in_fresh"], 250)

        u = AcpUsage(tokens_in=5000, tokens_out=300, tokens_in_cached=4200)
        self.assertEqual(u.tokens_in_cached, 4200)
        self.assertEqual(u.tokens_in_fresh, 800)

        r = AcpResult(text="hi", tokens_in=5000, tokens_out=300, tokens_in_cached=4200)
        self.assertEqual(r.tokens_in_fresh, 800)
        self.assertEqual(r.usage.tokens_in_cached, 4200)

    def test_budget_and_micromanager_budget_tally_fresh_and_cached(self) -> None:
        b = Budget(
            implementer_attempts=4, test_writer_attempts=2,
            model_builds=10, mutation_builds=10,
            tokens=100_000, wall_clock_s=600,
        )
        b.note_tokens(1000, 200, 0.0, tokens_in_cached=600)
        b.note_tokens(500, 100, 0.0, tokens_in_cached=100)
        self.assertEqual(b.tokens_in, 1500)
        self.assertEqual(b.tokens_in_cached, 700)
        self.assertEqual(b.tokens_in_fresh, 800)

        mb = MicromanagerBudget(max_calls_per_card=4, tokens_per_card=50_000, spend_per_night=5.0)
        mb.record("CARD-1", tokens_in=4000, tokens_out=200, spend=0.0, tokens_in_cached=3000)
        s = mb.usage_for("CARD-1")
        self.assertEqual(s["tokens_in"], 4000)
        self.assertEqual(s["tokens_in_cached"], 3000)
        self.assertEqual(s["tokens_in_fresh"], 1000)

    def test_bench_score_and_report_include_fresh_and_cached(self) -> None:
        records = [
            {
                "task": "py-bounded-full", "category": "pinned-bugfix",
                "arm": "farm", "repeat": 1, "passed": True,
                "tokens": 10000, "worker_tokens": 6000, "supervisor_tokens": 4000,
                "tokens_in_fresh": 3000, "tokens_in_cached": 5000, "tokens_out": 2000,
                "spend": 0.0, "seconds": 10.0, "diff_lines": 12,
            }
        ]
        summary = score(records)
        arm = summary["arms"]["farm"]
        self.assertEqual(arm["tokens_in_fresh"], 3000)
        self.assertEqual(arm["tokens_in_cached"], 5000)
        md = render_report(summary)
        self.assertIn("fresh", md.lower())


if __name__ == "__main__":
    unittest.main()
