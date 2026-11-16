from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.bench import render_report, score


def record(task, category, arm, passed, spend):
    return {
        "task": task,
        "category": category,
        "arm": arm,
        "passed": passed,
        "spend": spend,
        "tokens": 10,
        "seconds": 1.0,
        "diff_lines": 4,
    }


class BenchReportTests(unittest.TestCase):
    def test_the_arm_table(self):
        records = [
            record("t1", "small-feature", "farm", True, 0.10),
            record("t2", "small-feature", "farm", True, 0.10),
            record("t3", "small-feature", "farm", False, 0.10),
        ]
        report = render_report(score(records))
        self.assertIn(
            "| farm | 3 | 2 | 66.7% | 30 | 15 | 100.0% |",
            report.splitlines(),
        )

    def test_no_tokens_per_pass_is_a_dash(self):
        records = [
            record("t1", "small-feature", "solo", False, 0.10),
        ]
        report = render_report(score(records))
        self.assertIn(
            "| solo | 1 | 0 | 0.0% | 10 | — | 100.0% |",
            report.splitlines(),
        )

    def test_category_rows_are_sorted(self):
        records = [
            record("t1", "small-feature", "farm", True, 0.10),
            record("t2", "small-feature", "farm", False, 0.10),
            record("t3", "pattern", "solo", True, 0.10),
            record("t4", "pattern", "solo", False, 0.10),
        ]
        report = render_report(score(records))
        lines = report.splitlines()
        start = lines.index("## By category")
        category_rows = [
            line for line in lines[start + 1:]
            if line.startswith("| ")
            and not line.startswith("| Category")
            and not line.startswith("| ---")
        ]
        self.assertEqual(
            category_rows,
            [
                "| pattern | — | 50.0% |",
                "| small-feature | 50.0% | — |",
            ],
        )


if __name__ == "__main__":
    unittest.main()