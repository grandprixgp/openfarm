"""Claude over ACP doing a worker's job, the bench's solo arm, against the stand-in."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adapters.claude_worker import ClaudeAcpWorkerBackend

STAND_IN = Path(__file__).resolve().parent / "acp_stand_in.py"


class ClaudeWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _backend(self, turns: list[dict]) -> ClaudeAcpWorkerBackend:
        script = self.tmp / "turns.json"
        script.write_text(json.dumps({"turns": turns}), encoding="utf-8")
        backend = ClaudeAcpWorkerBackend(
            command=[sys.executable, str(STAND_IN), "--mode", "script",
                     "--script", str(script)],
            cwd=self.tmp, timeout_s=10.0)
        self.addCleanup(backend.close)
        return backend

    def test_each_call_returns_claudes_text_and_cost(self) -> None:
        backend = self._backend([
            {"text": "=== whole_file: tests/test_a.py\nx = 1\n",
             "usage": {"tokens_in": 900, "tokens_out": 40, "cost": 0.02}},
            {"text": "second answer",
             "usage": {"tokens_in": 500, "tokens_out": 10, "cost": 0.01}}])
        first = backend.complete("test_writer", "", 0.2,
                                 [{"role": "user", "content": "brief one"}])
        second = backend.complete("implementer", "", 0.3,
                                  [{"role": "user", "content": "brief two"}])
        self.assertEqual(first.content, "=== whole_file: tests/test_a.py\nx = 1\n")
        self.assertEqual(second.content, "second answer")
        self.assertAlmostEqual(first.cost, 0.02)
        # A fresh session per call: the second call's cost is its own, not a total.
        self.assertAlmostEqual(second.cost, 0.01)
        self.assertEqual(first.finish_reason, "stop")

    def test_a_turn_cut_at_max_tokens_reports_length(self) -> None:
        backend = self._backend([{"text": "=== whole_file: a.py\nhalf",
                                  "stop_reason": "max_tokens"}])
        answer = backend.complete("implementer", "", 0.3,
                                  [{"role": "user", "content": "brief"}])
        self.assertEqual(answer.finish_reason, "length")

    def test_the_backend_prices_spend(self) -> None:
        self.assertTrue(self._backend([{"text": "x"}]).prices_spend)


if __name__ == "__main__":
    unittest.main()
