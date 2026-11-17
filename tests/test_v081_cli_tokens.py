"""U2: a model coordinator's tokens and spend are part of the run's totals.

In v0.8 `tokens.total` was worker plus supervisor only: a `tier:` or ACP
coordinator's tokens showed up in `by_member` and `by_effort` but not in the
total or the shares, and its spend never reached the night's caps.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.bench import render_report, score  # noqa: E402
from openfarm_core.budget import NightBudget, describe_split, token_split  # noqa: E402
from openfarm_core.human_cli import Style, format_human_strict  # noqa: E402
from openfarm_core.tui import _format_overview_lines, collect_snapshot  # noqa: E402
from tests.test_v081_cli_toy import (  # noqa: E402
    COORD_TIER,
    SWARM_ON,
    make_toy,
    run_json,
    write_script,
)

PLAIN = Style(enabled=False)


def _coordinator_script(script: dict, n_cards: int, *, cost: float = 0.0) -> dict:
    # Call k starts CARD-k (call 1 is the plan, call k answers CARD-(k-1) finishing).
    for k in range(1, n_cards + 2):
        script[f"coordinator/{k}"] = {
            "content": json.dumps({"actions": [{"action": "start", "card": f"CARD-{k}"}]}),
            "tokens_in": 100, "tokens_out": 20, "cost": cost,
        }
    script["coordinator"] = {"content": json.dumps({"actions": [{"action": "wait"}]}),
                             "tokens_in": 100, "tokens_out": 20, "cost": cost}
    return script


class TokenSplitUnitTests(unittest.TestCase):
    def test_no_coordinator_keeps_the_old_shares(self) -> None:
        split = token_split(80_000, 20_000)
        self.assertEqual(split["coordinator"], 0)
        self.assertEqual(split["coordinator_share"], 0.0)
        self.assertEqual((split["total"], split["worker_share"], split["supervisor_share"]),
                         (100_000, 0.8, 0.2))
        self.assertEqual(describe_split(split), "100,000 tokens: 80% worker, 20% supervisor")

    def test_three_parts_add_up(self) -> None:
        split = token_split(11_100, 0, 1_080)
        self.assertEqual(split["total"], 12_180)
        self.assertAlmostEqual(split["worker_share"] + split["supervisor_share"]
                               + split["coordinator_share"], 1.0, places=3)
        self.assertEqual(describe_split(split),
                         "12,180 tokens: 91% worker, 0% supervisor, 9% coordinator")

    def test_describe_split_reads_an_older_split(self) -> None:
        old = {"worker": 6, "supervisor": 4, "total": 10,
               "worker_share": 0.6, "supervisor_share": 0.4}
        self.assertEqual(describe_split(old), "10 tokens: 60% worker, 40% supervisor")

    def test_night_budget_counts_coordinator_spend(self) -> None:
        night = NightBudget(1.0)
        night.note_spend(0.3)
        night.note_micromanager(0.3)
        self.assertIsNone(night.exhausted())
        night.note_coordinator(0.4)
        self.assertAlmostEqual(night.total_spend(), 1.0)
        self.assertIn("reached its cap", night.exhausted() or "")


class ModelCoordinatorRunTests(unittest.TestCase):
    def test_coordinator_tokens_are_in_the_total_and_the_maps_add_up(self) -> None:
        toy, script = make_toy(2, extra_config=COORD_TIER)
        self.addCleanup(toy.cleanup)
        _coordinator_script(script, 2, cost=0.25)
        code, payload, err = run_json(toy.root, "run", "--cards", "2",
                                      "--script", str(write_script(script)))
        self.assertEqual(code, 0, err[-800:])
        tokens = payload["tokens"]
        coord_member = tokens["by_member"]["coordinator"]
        self.assertGreater(coord_member, 0)
        self.assertEqual(tokens["coordinator"], coord_member)
        self.assertEqual(tokens["total"],
                         tokens["worker"] + tokens["supervisor"] + tokens["coordinator"])
        self.assertEqual(sum(tokens["by_member"].values()), tokens["total"])
        self.assertEqual(sum(tokens["by_effort"].values()), tokens["total"])
        self.assertAlmostEqual(tokens["coordinator_share"],
                               round(tokens["coordinator"] / tokens["total"], 4))
        # Its spend is the run's spend too.
        calls = coord_member // 120
        self.assertAlmostEqual(payload["coordinator_spend"], 0.25 * calls, places=6)
        self.assertAlmostEqual(payload["spend"],
                               payload["worker_spend"] + payload["micromanager_spend"]
                               + payload["coordinator_spend"], places=6)
        # Old readers' keys are still there.
        for key in ("worker", "supervisor", "total", "worker_share", "supervisor_share"):
            self.assertIn(key, tokens)

        text = format_human_strict("run", payload, style=PLAIN)
        self.assertIn(f"coordinator ({tokens['coordinator']:,})", text)
        self.assertIn("coordinator_spend", text)

        snap = collect_snapshot(toy.root, view="overview")
        overview = "\n".join(_format_overview_lines(snap, width=120))
        self.assertIn(f"coordinator={tokens['coordinator']}", overview)

    def test_coordinator_spend_counts_against_the_night_cap(self) -> None:
        toy, script = make_toy(2, extra_config=COORD_TIER)
        self.addCleanup(toy.cleanup)
        _coordinator_script(script, 2, cost=1.0)
        code, payload, err = run_json(toy.root, "run", "--cards", "2", "--spend", "0.5",
                                      "--script", str(write_script(script)))
        self.assertEqual(code, 0, err[-800:])
        self.assertGreaterEqual(payload["coordinator_spend"], 1.0)
        self.assertGreaterEqual(payload["spend"], 1.0)
        for outcome in payload["outcomes"]:
            self.assertIn("night spend", outcome.get("skipped", ""), outcome)

    def test_without_a_model_coordinator_the_part_is_zero(self) -> None:
        for extra in ("", SWARM_ON):
            with self.subTest(swarm=bool(extra)):
                toy, script = make_toy(1, extra_config=extra)
                self.addCleanup(toy.cleanup)
                code, payload, err = run_json(toy.root, "run", "--cards", "1",
                                              "--script", str(write_script(script)))
                self.assertEqual(code, 0, err[-800:])
                tokens = payload["tokens"]
                self.assertEqual(tokens["coordinator"], 0)
                self.assertEqual(tokens["coordinator_share"], 0.0)
                self.assertEqual(tokens["total"], tokens["worker"] + tokens["supervisor"])
                self.assertEqual(tokens["worker_share"], 1.0)
                self.assertEqual(payload["coordinator_spend"], 0.0)
                text = format_human_strict("run", payload, style=PLAIN)
                self.assertNotIn("coordinator (", text)
                self.assertNotIn("coordinator_spend", text)

    def test_supervisor_tokens_off_the_board_still_add_up(self) -> None:
        # The config's own supervisor keeps its usage in the card record, not on a
        # board member; by_member and by_effort list it as not on the board.
        toy, script = make_toy(1, extra_config="\n[swarm]\nenabled = true\n")
        self.addCleanup(toy.cleanup)
        mm = {
            "shape_and_plan": {"verdict": "ready", "clarified_notes": "", "findings": [],
                               "_usage": {"tokens_in": 100, "tokens_out": 40, "spend": 0.0}},
            "pre_review": {"verdict": "approve", "reviewer_brief": "ok", "missed_clause": "",
                           "findings": [],
                           "_usage": {"tokens_in": 80, "tokens_out": 20, "spend": 0.0}},
            "*": {"_usage": {"tokens_in": 1, "tokens_out": 1}},
        }
        mm_path = write_script(mm, Path(tempfile.mkdtemp(prefix="of-v081-mm-")))
        code, payload, err = run_json(toy.root, "run", "--cards", "1",
                                      "--script", str(write_script(script)),
                                      "--mm-script", str(mm_path))
        self.assertEqual(code, 0, err[-800:])
        tokens = payload["tokens"]
        self.assertGreater(tokens["supervisor"], 0)
        self.assertEqual(tokens["by_member"]["supervisor (not on the board)"],
                         tokens["supervisor"])
        self.assertEqual(sum(tokens["by_member"].values()), tokens["total"])
        self.assertEqual(sum(tokens["by_effort"].values()), tokens["total"])


class BenchCoordinatorTests(unittest.TestCase):
    def _record(self, arm: str, worker: int, coord: int) -> dict:
        return {"task": "t", "category": "pinned-bugfix", "arm": arm, "passed": True,
                "spend": 0.0, "tokens": worker + coord, "seconds": 1.0, "diff_lines": 1,
                "worker_tokens": worker, "supervisor_tokens": 0,
                "coordinator_tokens": coord}

    def test_report_shows_the_coordinator_only_when_it_spent(self) -> None:
        plain = score([self._record("farm", 100, 0)])
        self.assertNotIn("coordinator_share", plain["arms"]["farm"])
        self.assertNotIn("Coordinator share", render_report(plain))

        with_coord = score([self._record("farm", 90, 10), self._record("solo", 100, 0)])
        self.assertEqual(with_coord["arms"]["farm"]["coordinator_tokens"], 10)
        self.assertEqual(with_coord["arms"]["farm"]["coordinator_share"], 0.1)
        report = render_report(with_coord)
        self.assertIn("Coordinator share", report)
        self.assertIn("| farm | 1 | 1 | 100.0% | 100 | 100 | 90.0% | 10.0% |", report)

    def test_older_records_without_a_coordinator_still_score(self) -> None:
        rec = self._record("farm", 100, 0)
        del rec["coordinator_tokens"]
        self.assertEqual(score([rec])["arms"]["farm"].get("coordinator_tokens", 0), 0)


if __name__ == "__main__":
    unittest.main()
