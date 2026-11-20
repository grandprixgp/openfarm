"""v0.10.0 Pillar 4 tests: draft_wave_plan with Frontier Architect, silent eligibility auto-repair, and picked findings."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from openfarm_core.adapters.files import FileCardSource, parse_card_file
from openfarm_core.bootstrap import initialize_project
from openfarm_core.cards import Card
from openfarm_core.config import load_config
from openfarm_core.findings import decide_finding
from openfarm_core.gates.eligibility import check_eligibility
from openfarm_core.git import Git


class WavePlanV010Tests(unittest.TestCase):
    def test_draft_wave_plan_silently_repairs_ineligible_frontier_cards(self) -> None:
        from openfarm_core.automode import draft_wave_plan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "web-photo"
            initialize_project(
                "web-photo",
                directory=root,
                language="typescript",
                idea="Image of the day website",
                state_dir=Path(tmp) / "state",
                secrets_dir=Path(tmp) / "secrets",
                no_git=True,
            )

            # Fake Frontier backend deliberately emits an ineligible card (bad category,
            # empty touches, no tests, vague/design words in Done).
            broken_card = (
                "+++\n"
                'id = "W1-01"\n'
                'title = "Build hero view"\n'
                'state = "ready"\n'
                'category = "bogus"\n'
                "touches = []\n"
                "depends = []\n"
                "+++\n\n"
                "## Done\n\n"
                "- Design a robust hero layout that handles images properly.\n"
            )

            def fake_backend(_prompt: str) -> str:
                return json.dumps({
                    "files": {
                        ".openfarm/PROJECT_SPEC.md": "# web-photo Spec\n\n## Goal\n\nShow image of the day cleanly.\n",
                        "cards/W1-01.md": broken_card,
                    }
                })

            plan = draft_wave_plan(
                root,
                answers=[
                    {"id": "goal", "question": "Core workflow", "answer": "Show image of the day with design and facts"},
                    {"id": "invariants", "question": "Invariants", "answer": "No data loss"},
                    {"id": "slice", "question": "Golden slice", "answer": "Hero loader + tests"},
                ],
                backend=fake_backend,
            )
            self.assertTrue(plan["eligibility_ok"], plan)
            self.assertGreaterEqual(plan["repaired_count"], 1)
            self.assertEqual(len(plan["cards"]), 1)
            self.assertIn(plan["cards"][0]["profile"], ("lean", "balanced", "strict"))

            # Confirm the file on disk passes real check_eligibility with zero failures
            header, body = parse_card_file((root / "cards" / "W1-01.md").read_text(encoding="utf-8"))
            card = Card(
                id=str(header["id"]),
                title=str(header["title"]),
                state=str(header["state"]),
                done_text=body,
                touches=list(header.get("touches") or []),
                deps=list(header.get("depends") or []),
                fields=dict(header),
            )
            elig = check_eligibility(card, load_config(root), worktree=root)
            self.assertTrue(elig.ok, f"repaired card still failed eligibility: {elig.failures}")

    def test_draft_wave_plan_incorporates_picked_findings_as_eligible_cards(self) -> None:
        from openfarm_core.automode import draft_wave_plan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "py-app"
            initialize_project(
                "py-app",
                directory=root,
                language="python",
                idea="Daily notes tracker",
                state_dir=Path(tmp) / "state",
                secrets_dir=Path(tmp) / "secrets",
                commit=True,
            )
            cfg = load_config(root)
            src = FileCardSource(root, "cards", worktree=cfg.state_dir / "cards-source")
            src.prepare()
            fid = src.finding("W0-01", "Reject negative timestamps", "Parser accepts negative epoch timestamps.")["id"]
            decide_finding(root, cfg, fid, "next-wave")

            plan = draft_wave_plan(
                root,
                answers=[
                    {"id": "goal", "question": "Core workflow", "answer": "Create and list daily notes"},
                    {"id": "invariants", "question": "Invariants", "answer": "Never lose notes"},
                    {"id": "slice", "question": "Golden slice", "answer": "Note store + CLI"},
                ],
                include_picked_findings=True,
            )
            self.assertTrue(plan["eligibility_ok"], plan)
            self.assertIn(fid, plan["promoted_findings"])
            card_ids = {c["id"] for c in plan["cards"]}
            self.assertIn(f"W-{fid}", card_ids)
            for c in plan["cards"]:
                self.assertTrue(c["eligible"], c)


if __name__ == "__main__":
    unittest.main()
