"""A zero spend cap means no cap, for the night, the card, and Claude's share."""

from __future__ import annotations

import unittest

from openfarm_core.budget import Budget, NightBudget
from openfarm_core.micromanager import MicromanagerBudget


class ZeroSpendCapTests(unittest.TestCase):
    def test_the_night_is_never_exhausted_by_spend(self) -> None:
        night = NightBudget(0.0)
        night.note_spend(1_000.0)
        night.note_micromanager(1_000.0)
        self.assertIsNone(night.exhausted())

    def test_a_card_never_stops_on_spend(self) -> None:
        budget = Budget(spend=0.0, frontier_estimate=1.0, frontier_ratio=None)
        budget.note_tokens(10, 10, spend=1_000.0)
        self.assertEqual(budget.spend, 1_000.0)

    def test_claude_never_stops_on_spend(self) -> None:
        mm = MicromanagerBudget(max_calls_per_card=100, tokens_per_card=10**9,
                                spend_per_night=0.0)
        mm.spend = 1_000.0
        mm.check("CARD-1")


if __name__ == "__main__":
    unittest.main()
