"""Reports compare runs by tokens: the worker's share and the supervisor's."""

from __future__ import annotations

import unittest

from openfarm_core.budget import describe_split, token_split


class TokenSplitTests(unittest.TestCase):
    def test_shares(self) -> None:
        split = token_split(80_000, 20_000)
        self.assertEqual((split["total"], split["worker_share"], split["supervisor_share"]),
                         (100_000, 0.8, 0.2))
        self.assertEqual(describe_split(split), "100,000 tokens: 80% worker, 20% supervisor")

    def test_no_tokens_is_no_share(self) -> None:
        split = token_split(0, 0)
        self.assertEqual((split["worker_share"], split["supervisor_share"]), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
