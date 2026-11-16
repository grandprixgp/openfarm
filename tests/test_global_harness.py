"""v0.7 global learning: the config keys, the global store, and the exchange with it.

The rules that decide promotion, veto and retirement are v0.7 cards; here they are
patched, so these tests cover only the plumbing around them.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openfarm_core import harness_global, harness_memory
from openfarm_core.config import Config, ConfigError
from openfarm_core.harness_global import GlobalLibrary, global_dir_for, global_id_for
from openfarm_core.harness_memory import HarnessLibrary
from openfarm_core.micromanager import Micromanager, NullMicromanager


def _config(repo: Path, state: Path, **harness) -> Config:
    return Config({
        "project": {"name": "alpha", "state_dir": str(state)},
        "adapters": {"languages": ["python"], "model": "scripted"},
        "model": {"endpoint": "https://api.example.invalid/v1",
                  "allowed_endpoints": ["https://api.example.invalid/v1"],
                  "implementer": {"model": "m"}},
        "policy": {"categories": ["small-feature"]},
        "harness": harness,
    }, repo)


LESSON = {
    "title": "empty input",
    "surface": "implementer_guidance",
    "role": "implementer",
    "language": "python",
    "kind": "small-feature",
    "failure_class": "",
    "cue": "Return early on an empty list before indexing its first item.",
    "lesson": "Empty lists reached an index lookup.",
    "evidence": "card=CARD-9",
    "confidence": 0.8,
    "tags": [],
    "proposed_patch": {},
}


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-global-",
                                         dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.glib = GlobalLibrary(self.tmp / "global")

    def _project(self, name: str, **harness) -> HarnessLibrary:
        repo = self.tmp / name / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        cfg = _config(repo, self.tmp / name / "state", **harness)
        return HarnessLibrary(self.tmp / name / "harness", config=cfg)

    def _own_active(self, lib: HarnessLibrary, lesson: dict, *, entry_id: str,
                    stats: dict | None = None, **extra) -> dict:
        entry = {**lesson, "id": entry_id, "status": "candidate", "source": "micromanager",
                 "scope": "general", "origin": "project", "card_id": "CARD-9",
                 "revision": 0, "stats": stats or {"applied": 0, "helped": 0,
                                                    "hurt": 0, "neutral": 0}, **extra}
        lib._save_entry(entry)
        lib.promote_entry(entry_id)
        return lib.show_entry(entry_id)


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-global-cfg-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()

    def test_defaults(self) -> None:
        h = _config(self.repo, self.tmp / "state").harness
        self.assertEqual((h.learn_from, h.share, h.global_dir), ("both", True, ""))
        self.assertEqual(global_dir_for(h), Path(os.environ["OPENFARM_GLOBAL_HARNESS"]))

    def test_values_and_refusals(self) -> None:
        h = _config(self.repo, self.tmp / "state", learn_from="global", share=False,
                    global_dir=str(self.tmp / "g")).harness
        self.assertEqual((h.learn_from, h.share), ("global", False))
        self.assertEqual(global_dir_for(h), (self.tmp / "g").resolve())
        with self.assertRaises(ConfigError):
            _config(self.repo, self.tmp / "state", learn_from="everything")
        with self.assertRaises(ConfigError) as caught:
            _config(self.repo, self.tmp / "state", global_dir=str(self.repo / "g"))
        self.assertIn("outside the repository", str(caught.exception))


class GlobalStoreTests(_Base):
    def test_opening_creates_nothing(self) -> None:
        self.assertEqual(self.glib.list_entries(), [])
        self.assertEqual(self.glib.status()["active"], 0)
        self.assertFalse(self.glib.root.exists())

    def test_add_drops_evidence_and_card_and_records_sources(self) -> None:
        entry = {**LESSON, "id": "exp-a", "card_id": "CARD-9"}
        g = self.glib.add(entry, "alpha")
        self.assertEqual(g["id"], global_id_for(LESSON))
        self.assertNotIn("evidence", g)
        self.assertNotIn("card_id", g)
        self.assertEqual([s["project"] for s in g["sources"]], ["alpha"])
        again = self.glib.add({**LESSON, "id": "exp-b"}, "beta")
        self.assertEqual([s["project"] for s in again["sources"]], ["alpha", "beta"])
        self.assertEqual(len(self.glib.list_entries()), 1)
        self.assertEqual([e["event"] for e in self.glib.read_events()],
                         ["promoted", "source-added"])

    def test_report_keeps_a_veto_and_asks_the_retire_rule(self) -> None:
        gid = self.glib.add({**LESSON, "id": "exp-a"}, "alpha")["id"]
        self.glib.report(gid, "beta", {"helped": 0, "hurt": 2}, vetoed=True)
        self.glib.report(gid, "beta", {"helped": 1, "hurt": 2}, vetoed=False)
        self.assertTrue(self.glib.get(gid)["projects"]["beta"]["vetoed"])
        self.assertEqual(self.glib.get(gid)["status"], "active")
        with mock.patch.object(harness_global, "retire_reason", return_value="two vetoes"):
            self.glib.report(gid, "gamma", {"hurt": 1}, vetoed=True)
        self.assertEqual(self.glib.get(gid)["status"], "retired")
        self.assertEqual(self.glib.get(gid)["retire_reason"], "two vetoes")
        self.glib.restore(gid)
        self.assertEqual(self.glib.get(gid)["status"], "active")


class ExchangeTests(_Base):
    def test_import_is_a_global_candidate_that_starts_a_probe(self) -> None:
        gid = self.glib.add({**LESSON, "id": "exp-a"}, "alpha")["id"]
        beta = self._project("beta")
        res = beta.sync_from_global(self.glib)
        self.assertEqual(res["imported"], [f"exp-{gid}"])
        entry = beta.show_entry(f"exp-{gid}")
        self.assertEqual((entry["origin"], entry["global_id"], entry["source_project"]),
                         ("global", gid, "alpha"))
        self.assertEqual(entry["status"], "probing")
        self.assertEqual(beta.sync_from_global(self.glib)["imported"], [])

    def test_learn_from_project_imports_nothing(self) -> None:
        self.glib.add({**LESSON, "id": "exp-a"}, "alpha")
        beta = self._project("beta", learn_from="project")
        self.assertEqual(beta.sync_from_global(self.glib)["imported"], [])

    def test_a_retired_lesson_is_dropped(self) -> None:
        gid = self.glib.add({**LESSON, "id": "exp-a"}, "alpha")["id"]
        beta = self._project("beta")
        beta.sync_from_global(self.glib)
        self.glib.retire(gid, "manual")
        self.assertEqual(beta.sync_from_global(self.glib)["dropped"], [f"exp-{gid}"])
        self.assertEqual(beta.show_entry(f"exp-{gid}")["demote_reason"], "global_retired")
        self.assertNotIn(f"exp-{gid}", beta.status().get("active_entry_ids", []))

    def test_learn_from_picks_which_lessons_apply_and_project_wins_ties(self) -> None:
        own = {**LESSON, "cue": "Check the list length first."}
        self.glib.add({**LESSON, "id": "exp-a"}, "alpha")
        for learn_from, expected in (("both", 2), ("project", 1), ("global", 1)):
            with self.subTest(learn_from=learn_from):
                beta = self._project(f"beta-{learn_from}", learn_from=learn_from)
                self._own_active(beta, own, entry_id="exp-own")
                beta.sync_from_global(self.glib)
                sel = beta.select_guidance("python", "small-feature", "implementer")
                self.assertEqual(len(sel.cue_ids), expected)
                if learn_from == "both":
                    self.assertEqual(sel.cue_ids[0], "exp-own")
                if learn_from == "global":
                    self.assertTrue(sel.cue_ids[0].startswith("exp-g-"))

    def test_report_promotes_vetoes_and_shares_scores(self) -> None:
        alpha = self._project("alpha")
        self._own_active(alpha, LESSON, entry_id="exp-own",
                         stats={"applied": 3, "helped": 3, "hurt": 0, "neutral": 0})
        with mock.patch.object(harness_memory, "should_promote", return_value=True), \
                mock.patch.object(harness_memory, "global_eligibility",
                                  return_value=(True, "")):
            res = alpha.report_to_global(self.glib, "alpha", share=True)
        self.assertEqual(res["promoted"], ["exp-own"])
        gid = alpha.show_entry("exp-own")["global_id"]
        self.assertEqual(self.glib.get(gid)["projects"]["alpha"]["helped"], 3)

        beta = self._project("beta")
        beta.sync_from_global(self.glib)
        local = f"exp-{gid}"
        entry = beta.show_entry(local)
        entry["stats"] = {"applied": 2, "helped": 0, "hurt": 2, "neutral": 0}
        beta._save_entry(entry)
        with mock.patch.object(harness_memory, "should_veto", return_value=True):
            res = beta.report_to_global(self.glib, "beta", share=True)
        self.assertEqual(res["vetoed"], [local])
        self.assertEqual(beta.show_entry(local)["demote_reason"], "global_veto")
        self.assertTrue(self.glib.get(gid)["projects"]["beta"]["vetoed"])
        self.assertFalse(self.glib.get(gid)["projects"]["alpha"]["vetoed"])

    def test_share_false_and_ineligible_lessons_stay_home(self) -> None:
        alpha = self._project("alpha")
        self._own_active(alpha, LESSON, entry_id="exp-own")
        with mock.patch.object(harness_memory, "should_promote", return_value=True), \
                mock.patch.object(harness_memory, "global_eligibility",
                                  return_value=(False, "bookkeeping note")):
            self.assertEqual(alpha.report_to_global(self.glib, "alpha", share=False)["promoted"], [])
            res = alpha.report_to_global(self.glib, "alpha", share=True)
        self.assertEqual(res["refused"], {"exp-own": "bookkeeping note"})
        self.assertEqual(alpha.show_entry("exp-own")["global_refused"], "bookkeeping note")
        self.assertFalse(self.glib.root.exists())


class CaptureScopeTests(_Base):
    OUTCOME = ({"card_id": "CARD-1", "category": "small-feature", "language": "python",
                "final_state": "in-review", "attempts": 2},
               [{"attempt": 1, "phase": "implementer", "outcome": "fail",
                 "error_signature": "AssertionError"},
                {"attempt": 2, "phase": "implementer", "outcome": "pass"}])

    def _capture(self, markers):
        lib = self._project("alpha")
        res = lib.record_card_outcome(*self.OUTCOME, micromanager=Micromanager(NullMicromanager()),
                                      scope_markers=markers)
        return lib.show_entry(res["lessons_added"][0])

    def test_without_markers_every_lesson_is_project(self) -> None:
        entry = self._capture(None)
        self.assertEqual((entry["scope"], entry["origin"]), ("project", "project"))
        self.assertEqual(entry["scope_reason"], "no project markers to check against")

    def test_the_scope_check_decides_a_general_claim(self) -> None:
        with mock.patch.object(harness_memory, "scope_of",
                               return_value=("general", "")) as check:
            entry = self._capture({"alpha"})
        self.assertEqual(entry["scope"], "general")
        self.assertEqual(check.call_args.args[1], {"alpha"})


class BenchIsolationTests(unittest.TestCase):
    def test_a_bench_run_gets_its_own_global_library(self) -> None:
        from openfarm_core.bench import _openfarm_env
        env = _openfarm_env(Path("/x/run/global/harness"))
        self.assertEqual(env["OPENFARM_GLOBAL_HARNESS"], "/x/run/global/harness")


if __name__ == "__main__":
    unittest.main()
