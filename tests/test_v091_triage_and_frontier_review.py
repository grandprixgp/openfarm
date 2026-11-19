"""v0.9.1 frontier card-complexity triage and mixture-of-frontiers review tests."""

from __future__ import annotations

import json
import unittest

from openfarm_core.cards import Card
from openfarm_core.frontier_review import review_with_frontiers
from openfarm_core.triage import triage_card_heuristic, triage_cards_with_frontier
from tests.test_python_e2e import build_python_toy, run_cli


class _FakeFrontier:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0

    def ask(self, prompt: str) -> str:
        self.calls += 1
        return self.payload


class TriageAndFrontierReviewTests(unittest.TestCase):
    def test_heuristic_triage_scores_simple_vs_complex_vs_split(self) -> None:
        simple = Card.from_dict({
            "id": "C-1", "title": "Fix off-by-one", "state": "ready", "unit": ".",
            "touches": ["a.py"], "tests": [{"name": "t1", "file": "tests/t1.py"}],
            "fields": {"Category": "pinned-bugfix", "Done": "f(0) returns 1."},
        })
        res_s = triage_card_heuristic(simple)
        self.assertEqual(res_s["complexity"], "low")
        self.assertEqual(res_s["recommended_profile"], "lean")
        self.assertEqual(res_s["starting_effort"], "medium")
        self.assertFalse(res_s["split_suggested"])

        huge = Card.from_dict({
            "id": "C-2", "title": "Rewrite whole subsystem", "state": "ready", "unit": ".",
            "touches": ["a.py", "b.py", "c.py", "d.py", "e.py"],
            "tests": [{"name": "t1", "file": "tests/t1.py"}],
            "fields": {
                "Category": "refactor",
                "New symbols": "yes",
                "Type changes": "yes",
                "Done": "Refactor all 5 modules.",
            },
        })
        res_h = triage_card_heuristic(huge)
        self.assertIn(res_h["complexity"], ("high", "split"))
        self.assertEqual(res_h["recommended_profile"], "strict")
        self.assertTrue(res_h["split_suggested"])

    def test_batched_frontier_triage_uses_single_call(self) -> None:
        c1 = Card.from_dict({
            "id": "CARD-1", "title": "One", "state": "ready", "unit": ".",
            "touches": ["a.py"], "tests": [], "fields": {"Category": "pinned-bugfix", "Done": "d1"},
        })
        c2 = Card.from_dict({
            "id": "CARD-2", "title": "Two", "state": "ready", "unit": ".",
            "touches": ["b.py"], "tests": [], "fields": {"Category": "feature", "Done": "d2"},
        })
        backend = _FakeFrontier(json.dumps([
            {"card": "CARD-1", "complexity": "low", "recommended_profile": "lean",
             "starting_effort": "medium", "reasons": ["bounded single-file fix"]},
            {"card": "CARD-2", "complexity": "high", "recommended_profile": "strict",
             "starting_effort": "high", "reasons": ["subtle state machine"]},
        ]))
        results = triage_cards_with_frontier([c1, c2], backend=backend)
        self.assertEqual(backend.calls, 1)
        by_id = {r["card"]: r for r in results}
        self.assertEqual(by_id["CARD-1"]["recommended_profile"], "lean")
        self.assertEqual(by_id["CARD-1"]["source"], "frontier")
        self.assertEqual(by_id["CARD-2"]["recommended_profile"], "strict")
        self.assertEqual(by_id["CARD-2"]["starting_effort"], "high")

    def test_mixture_of_frontiers_review_aggregates_and_flags_disagreement(self) -> None:
        backends = {
            "claude": _FakeFrontier(json.dumps({
                "verdict": "approve",
                "summary": "Clean diff",
                "findings": ["minor naming nit"],
            })),
            "codex": _FakeFrontier(json.dumps({
                "verdict": "changes-requested",
                "summary": "Missing edge case",
                "findings": ["empty input panics"],
            })),
        }
        report = review_with_frontiers("diff", "diff --git a/x.py b/x.py\n+return 1\n",
                                       ["claude", "codex"], backends=backends)
        self.assertEqual(report["scope"], "diff")
        self.assertFalse(report["consensus"])
        self.assertEqual(len(report["reviews"]), 2)
        self.assertEqual(len(report["all_findings"]), 2)

    def test_cli_triage_heuristic_json(self) -> None:
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        toy.write_card(
            "CARD-1", title="Simple fix", category="pinned-bugfix",
            unit=".", touches=["tally/bounded.py"],
            tests=[{"name": "test_a", "file": "tests/test_a.py"}],
            done_text="Fix one line.",
        )
        toy.commit("add CARD-1")
        res = run_cli(toy.root, "triage", "--heuristic")
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["cards"][0]["card"], "CARD-1")
        self.assertEqual(data["cards"][0]["complexity"], "low")


if __name__ == "__main__":
    unittest.main()
