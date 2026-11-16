"""v0.7 done test: lessons travel between projects through the real rules.

Nothing in openfarm is patched here. A stand-in supervisor proposes the lessons, and
each lesson's scores come from card outcomes recorded the way a run records them.
Two things are done by hand: promoting a lesson out of its probe (a probe needs a
run of cards) and the card outcomes themselves.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from openfarm_core.config import Config
from openfarm_core.harness_global import GlobalLibrary
from openfarm_core.harness_memory import HarnessLibrary
from openfarm_core.harness_scope import project_markers

GENERAL = {
    "title": "empty input",
    "surface": "implementer_guidance",
    "role": "implementer",
    "language": "python",
    "kind": "small-feature",
    "failure_class": "",
    "cue": "Return early on an empty list before indexing its first item.",
    "lesson": "Empty lists reached an index lookup.",
    "evidence": "attempt 1 failed with IndexError",
    "confidence": 0.8,
    "tags": [],
    "proposed_patch": {},
    "scope": "general",
    "scope_reason": "about Python lists, not this project",
}
# Claims to be general but names a function of project alpha.
TIED = {**GENERAL, "title": "ledger rows",
        "cue": "Call merge_ledger_rows before writing a row back.",
        "lesson": "Rows were written unmerged."}


class _Supervisor:
    """Proposes fixed lessons when asked to reflect on a card."""

    enabled = True

    def __init__(self, lessons: list[dict]) -> None:
        self.lessons = lessons

    def reflect_harness(self, *args, **kwargs) -> dict:
        return {"lessons": [dict(lesson) for lesson in self.lessons]}


def _config(repo: Path, name: str, **harness) -> Config:
    return Config({
        "project": {"name": name, "state_dir": str(repo.parent / "state")},
        "adapters": {"languages": ["python"], "model": "scripted"},
        "model": {"endpoint": "https://api.example.invalid/v1",
                  "allowed_endpoints": ["https://api.example.invalid/v1"],
                  "implementer": {"model": "m"}},
        "policy": {"categories": ["small-feature"]},
        "harness": harness,
    }, repo)


class GlobalLearningEndToEnd(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-e2e-global-",
                                         dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.glib = GlobalLibrary(self.tmp / "global")
        self.cards = 0

    def _project(self, name: str, **harness) -> HarnessLibrary:
        repo = self.tmp / name / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        lib = HarnessLibrary(self.tmp / name / "harness", config=_config(repo, name, **harness))
        lib.name = name
        return lib

    def _card(self, lib: HarnessLibrary, *, passed: bool, cues=(), supervisor=None,
              markers=None) -> dict:
        self.cards += 1
        meta = {"card_id": f"CARD-{self.cards}", "category": "small-feature",
                "language": "python", "attempts": 1,
                "final_state": "in-review" if passed else "blocked",
                "stop_code": "" if passed else "attempts"}
        records = [{"attempt": 1, "phase": "implementer",
                    "outcome": "pass" if passed else "fail",
                    "error_signature": "" if passed else "IndexError"}]
        return lib.record_card_outcome(meta, records, cues_applied=list(cues),
                                       micromanager=supervisor,
                                       reflect=supervisor is not None,
                                       scope_markers=markers)

    def _selected(self, lib: HarnessLibrary) -> list[str]:
        return list(lib.select_guidance("python", "small-feature", "implementer").cue_ids)

    def _alpha_with_proven_lessons(self, *, helped: int, **harness) -> HarnessLibrary:
        alpha = self._project("alpha", **harness)
        source = self.tmp / "alpha" / "repo" / "ledger.py"
        source.write_text("def merge_ledger_rows(rows):\n    return rows\n", encoding="utf-8")
        markers = project_markers(source.parent, "alpha", ["ledger.py"])
        added = self._card(alpha, passed=False, supervisor=_Supervisor([GENERAL, TIED]),
                           markers=markers)["lessons_added"]
        entries = {alpha.show_entry(i)["cue"]: alpha.show_entry(i) for i in added}
        self.general_id = entries[GENERAL["cue"]]["id"]
        self.tied_id = entries[TIED["cue"]]["id"]
        self.assertEqual(entries[GENERAL["cue"]]["scope"], "general")
        self.assertEqual(entries[TIED["cue"]]["scope"], "project")
        for entry_id in (self.general_id, self.tied_id):
            if alpha.show_entry(entry_id)["status"] != "active":
                alpha.promote_entry(entry_id)
        for _ in range(helped):
            self._card(alpha, passed=True, cues=[self.general_id, self.tied_id])
        return alpha

    def test_a_lesson_proven_in_one_project_travels_and_two_vetoes_retire_it(self) -> None:
        alpha = self._alpha_with_proven_lessons(helped=5)
        sent = alpha.report_to_global(self.glib, "alpha", share=True)
        self.assertEqual(sent["promoted"], [self.general_id])
        self.assertNotIn(self.tied_id, sent["promoted"])
        self.assertEqual(len(self.glib.list_entries()), 1)
        gid = alpha.show_entry(self.general_id)["global_id"]
        self.assertEqual(self.glib.get(gid)["projects"]["alpha"]["helped"], 5)
        self.assertNotIn("merge_ledger_rows", str(self.glib.list_entries()))

        # beta imports it, sees it in its briefs, and finds it hurts.
        beta = self._project("beta")
        local = f"exp-{gid}"
        self.assertEqual(beta.sync_from_global(self.glib)["imported"], [local])
        self.assertIn(local, self._selected(beta))
        self._card(beta, passed=False, cues=[local])
        self.assertEqual(beta.report_to_global(self.glib, "beta", share=True)["vetoed"], [local])
        self.assertNotIn(local, self._selected(beta))
        self.assertEqual(self.glib.get(gid)["status"], "active")

        # gamma vetoes it too: two vetoes retire it everywhere.
        gamma = self._project("gamma")
        gamma.sync_from_global(self.glib)
        self._card(gamma, passed=False, cues=[local])
        gamma.report_to_global(self.glib, "gamma", share=True)
        retired = self.glib.get(gid)
        self.assertEqual((retired["status"], retired["retire_reason"]),
                         ("retired", "vetoed by 2 projects"))

        # A later project never imports it, and gamma drops nothing it still uses.
        delta = self._project("delta")
        self.assertEqual(delta.sync_from_global(self.glib)["imported"], [])
        self.assertNotIn(local, self._selected(gamma))

    def test_a_lesson_hurting_more_than_it_helped_in_total_is_retired(self) -> None:
        alpha = self._alpha_with_proven_lessons(helped=3)
        alpha.report_to_global(self.glib, "alpha", share=True)
        gid = alpha.show_entry(self.general_id)["global_id"]
        beta = self._project("beta")
        beta.sync_from_global(self.glib)
        for _ in range(4):
            self._card(beta, passed=False, cues=[f"exp-{gid}"])
        beta.report_to_global(self.glib, "beta", share=True)
        self.assertEqual(self.glib.get(gid)["status"], "retired")

    def test_two_helps_are_not_enough_to_go_global(self) -> None:
        alpha = self._alpha_with_proven_lessons(helped=2)
        self.assertEqual(alpha.report_to_global(self.glib, "alpha", share=True)["promoted"], [])
        self._card(alpha, passed=True, cues=[self.general_id])
        self.assertEqual(alpha.report_to_global(self.glib, "alpha", share=True)["promoted"],
                         [self.general_id])

    def test_learn_from_and_share_are_obeyed(self) -> None:
        alpha = self._alpha_with_proven_lessons(helped=3, share=False)
        self.assertEqual(alpha.report_to_global(self.glib, "alpha", share=False)["promoted"], [])
        self.assertFalse(self.glib.root.exists())
        alpha.report_to_global(self.glib, "alpha", share=True)
        gid = alpha.show_entry(self.general_id)["global_id"]

        own = self._project("own-only", learn_from="project")
        self.assertEqual(own.sync_from_global(self.glib)["imported"], [])
        self.assertEqual(self._selected(own), [])

        shared = self._project("global-only", learn_from="global")
        shared.sync_from_global(self.glib)
        self.assertEqual(self._selected(shared), [f"exp-{gid}"])


if __name__ == "__main__":
    unittest.main()
