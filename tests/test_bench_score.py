from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.bench import Invalid, score


def record(
    *,
    task: str = "task",
    category: str = "pinned-bugfix",
    arm: str = "farm",
    passed: bool = True,
    spend: float = 0.0,
    tokens: int = 0,
    seconds: float = 0.0,
    diff_lines: int = 0,
) -> dict:
    return {
        "task": task,
        "category": category,
        "arm": arm,
        "passed": passed,
        "spend": spend,
        "tokens": tokens,
        "seconds": seconds,
        "diff_lines": diff_lines,
    }


class BenchScoreTests(unittest.TestCase):
    def test_pass_rate_and_cost_per_pass(self):
        records = [
            record(passed=True, spend=0.10, tokens=100),
            record(passed=False, spend=0.05, tokens=50),
            record(passed=True, spend=0.15, tokens=150),
        ]

        scored = score(records)

        self.assertEqual(
            scored["arms"]["farm"],
            {
                "runs": 3,
                "passed": 2,
                "pass_rate": 0.6667,
                "tokens": 300,
                "tokens_per_pass": 150,
                "worker_share": 1.0,
                "spend": 0.3,
                "cost_per_pass": 0.15,
            },
        )

    def test_no_pass_has_no_cost_per_pass(self):
        records = [record(arm="solo", passed=False, spend=0.2)]

        scored = score(records)

        self.assertEqual(
            scored["arms"]["solo"],
            {
                "runs": 1,
                "passed": 0,
                "pass_rate": 0.0,
                "tokens": 0,
                "tokens_per_pass": None,
                "worker_share": 0.0,
                "spend": 0.2,
                "cost_per_pass": None,
            },
        )

    def test_worker_share_counts_worker_tokens(self):
        records = [record(passed=True, tokens=100), record(passed=True, tokens=300)]
        records[0].update(worker_tokens=80, supervisor_tokens=20)
        records[1].update(worker_tokens=100, supervisor_tokens=200)
        self.assertEqual(score(records)["arms"]["farm"]["worker_share"], 0.45)

    def test_categories_split_by_arm(self):
        records = [
            record(arm="farm", passed=True, category="pinned-bugfix"),
            record(arm="solo", passed=True, category="pinned-bugfix"),
            record(arm="solo", passed=False, category="pinned-bugfix"),
        ]

        scored = score(records)

        self.assertEqual(
            scored["categories"]["pinned-bugfix"],
            {"farm": 1.0, "solo": 0.5},
        )

    def test_an_unknown_arm_is_refused(self):
        records = [record(arm="other")]

        with self.assertRaises(Invalid) as raised:
            score(records)

        self.assertEqual(raised.exception.code, "bad-bench-record")
        self.assertIn("other", str(raised.exception))

    def test_no_records_score_empty(self):
        self.assertEqual(score([]), {"arms": {}, "categories": {}})


if __name__ == "__main__":
    unittest.main()