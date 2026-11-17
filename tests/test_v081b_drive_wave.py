"""v0.8.1 fix wave, unit drive, D2: `--wave --record-verdict` never makes a verdict up.

One managed drive brings CARD-1 to in-review; each test then attaches a fresh
scripted driver to that ended run, which goes straight to the wave review.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openfarm_core.config import load_config
from openfarm_core.driver import DriverAgent, drive_run
from openfarm_core.state import StateDir
from tests.test_python_e2e import GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy

_RUN_SCRIPT = {
    "driver/plan": {"actions": [{"action": "start", "card": "CARD-1"}], "answers": []},
    "driver/question/shape_and_plan": {"actions": [], "answers": [
        {"seq": "auto", "data": {"verdict": "ready", "clarified_notes": "ok"}}]},
    "driver/question/pre_review": {"actions": [], "answers": [
        {"seq": "auto", "data": {"verdict": "approve", "reviewer_brief": "ok"}}]},
    "driver/question/reflect_harness": {"actions": [], "answers": [
        {"seq": "auto", "data": {"lessons": []}}]},
}


def _card_1(toy) -> None:
    toy.write_card(
        "CARD-1",
        title="A full bundle refuses a push",
        category="pinned-bugfix",
        unit=".",
        touches=["tally/bounded.py"],
        tests=[{
            "name": "test_a_full_bundle_refuses_a_push",
            "file": "tests/test_full.py",
            "asserts": "push raises FullError",
        }],
        done_text="push on a full bundle raises `FullError`.",
    )
    toy.commit("add CARD-1")


def _drive_to_in_review(toy, test_text: str) -> str:
    sp = Path(toy.base) / "run-script.json"
    sp.write_text(json.dumps({
        "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + test_text,
        "CARD-1/implementer": GOOD_IMPLEMENTATION,
        "CARD-1/checker_pass1": "RESTATEMENT: full bundle refuses push with FullError",
        "CARD-1/checker_pass2": "{}",
        **_RUN_SCRIPT,
    }), encoding="utf-8")
    res = drive_run(toy.root, load_config(toy.root), StateDir(toy.state_dir), agent="claude",
                    seat="both", card_ids=["CARD-1"], script=sp)
    assert res["outcomes"][0]["final_state"] == "in-review", res["outcomes"]
    return res["run_id"]


class _WaveFixture(unittest.TestCase):
    test_text = GOOD_TEST

    @classmethod
    def setUpClass(cls) -> None:
        cls.toy = build_python_toy()
        _card_1(cls.toy)
        cls.run_id = _drive_to_in_review(cls.toy, cls.test_text)
        cls.vfile = next((cls.toy.state_dir / "cards" / "CARD-1").glob("*/card-record.json")).parent / "verdict.json"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.toy.cleanup()

    def setUp(self) -> None:
        self.vfile.unlink(missing_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v081b-wave-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _review(self, entries: dict, *, max_turns: int = 40, record: bool = True) -> dict:
        sp = self.tmp / "wave-script.json"
        sp.write_text(json.dumps({"driver/*": {"actions": [], "answers": []}, **entries}),
                      encoding="utf-8")
        res = drive_run(self.toy.root, load_config(self.toy.root), StateDir(self.toy.state_dir),
                        agent="claude", seat="both", run_id=self.run_id, wave=True,
                        record_verdict=record, max_turns=max_turns, script=sp)
        self.assertEqual(len(res["reviews"]), 1, res["reviews"])
        return res["reviews"][0]


class WaveReviewVerdictTests(_WaveFixture):
    def test_unusable_answers_record_nothing_and_say_not_reviewed(self) -> None:
        rev = self._review({
            "driver/wave/CARD-1": "I could not review this, sorry.",
            "driver/wave/CARD-1/correction": "still no JSON",
        })
        self.assertFalse(rev["reviewed"])
        self.assertEqual(rev["verdict"], "")
        self.assertFalse(rev["recorded"])
        self.assertIn("could not be used, twice", rev["not_reviewed_reason"])
        self.assertFalse(self.vfile.exists(), "a verdict nobody made was recorded")

    def test_no_turn_left_records_nothing(self) -> None:
        # One turn: the review gets rubbish and no turn is left to ask again.
        rev = self._review({"driver/wave/CARD-1": "rubbish"}, max_turns=1)
        self.assertFalse(rev["reviewed"])
        self.assertIn("no driver turn was left", rev["not_reviewed_reason"])
        self.assertFalse(self.vfile.exists())

    def test_failed_review_turn_records_nothing_and_does_not_abort(self) -> None:
        rev = self._review({"driver/wave/CARD-1": {"fail": "driver-timeout", "message": "slow"}})
        self.assertFalse(rev["reviewed"])
        self.assertIn("turn failed (driver-timeout)", rev["not_reviewed_reason"])
        self.assertFalse(self.vfile.exists())

    def test_a_persons_verdict_is_kept(self) -> None:
        person = {"card": "CARD-1", "verdict": "bounced", "review_tokens": 900, "minutes": 12,
                  "note": "person: wrong error type", "at": "2026-11-17T10:00:00Z"}
        self.vfile.write_text(json.dumps(person), encoding="utf-8")
        for entries in (
            {"driver/wave/CARD-1": "rubbish", "driver/wave/CARD-1/correction": "rubbish"},
            {"driver/wave/CARD-1": {"content": json.dumps({"verdict": "clean", "note": "fine"}),
                                    "tokens_in": 100, "tokens_out": 10}},
        ):
            with self.subTest(entries=list(entries)):
                rev = self._review(entries, max_turns=2)
                self.assertFalse(rev["recorded"])
                self.assertEqual(json.loads(self.vfile.read_text(encoding="utf-8")), person)
        self.assertTrue(rev["reviewed"])
        self.assertEqual(rev["kept_existing"]["verdict"], "bounced")
        self.assertIn("never overwrites", rev["not_recorded_reason"])

    def test_recorded_verdict_says_who_and_carries_measured_tokens(self) -> None:
        rev = self._review({
            "driver/wave/CARD-1": {"content": "not json", "tokens_in": 100, "tokens_out": 10},
            "driver/wave/CARD-1/correction": {
                "content": json.dumps({"verdict": "minor-fix", "note": "rename x", "review_tokens": 5}),
                "tokens_in": 200, "tokens_out": 20,
            },
        })
        self.assertTrue(rev["recorded"], rev)
        data = json.loads(self.vfile.read_text(encoding="utf-8"))
        self.assertEqual(data["verdict"], "minor-fix")
        self.assertEqual(data["by"], "driver/claude")
        self.assertEqual(data["review_tokens"], 330, "tokens of the two review turns, not the model's claim")
        self.assertFalse(data["review_tokens_estimated"])
        self.assertFalse(data["diff_truncated"])
        self.assertEqual(data["run"], self.run_id)
        # Older readers' fields are all still there.
        for key in ("card", "verdict", "review_tokens", "minutes", "note", "at"):
            self.assertIn(key, data)

    def test_guessed_tokens_are_marked_estimated(self) -> None:
        self._review({"driver/wave/CARD-1": json.dumps({"verdict": "clean", "note": "ok"})})
        data = json.loads(self.vfile.read_text(encoding="utf-8"))
        self.assertEqual(data["verdict"], "clean")
        self.assertTrue(data["review_tokens_estimated"])
        self.assertGreater(data["review_tokens"], 0)


WAVE_DIFF_LIMIT = 24000  # the cut the driver applies (driver.WAVE_DIFF_LIMIT)
_PADDING = "PADDING = '''\n" + ("padding line for a long diff\n" * 1200) + "'''\n"


class WaveReviewCutDiffTests(_WaveFixture):
    test_text = GOOD_TEST + "\n" + _PADDING

    def _prompts(self, entries: dict) -> tuple[dict, list[str]]:
        seen: list[str] = []
        real = DriverAgent.complete

        def spy(agent, prompt, **kwargs):
            seen.append(prompt)
            return real(agent, prompt, **kwargs)

        with mock.patch.object(DriverAgent, "complete", spy):
            rev = self._review(entries)
        return rev, seen

    def test_clean_on_a_cut_diff_is_not_recorded(self) -> None:
        self.assertGreater(len(_PADDING), WAVE_DIFF_LIMIT)
        rev, prompts = self._prompts(
            {"driver/wave/CARD-1": json.dumps({"verdict": "clean", "note": "looks fine"})})
        sent = json.loads(prompts[0])
        self.assertTrue(sent["diff_truncated"])
        self.assertEqual(sent["diff_chars_shown"], WAVE_DIFF_LIMIT)
        self.assertGreater(sent["diff_chars"], WAVE_DIFF_LIMIT)
        self.assertFalse(rev["reviewed"])
        self.assertEqual(rev["proposed_verdict"], "clean")
        self.assertIn("clean verdict cannot stand", rev["not_reviewed_reason"])
        self.assertFalse(self.vfile.exists())

    def test_other_verdict_on_a_cut_diff_is_recorded_as_cut(self) -> None:
        rev, _ = self._prompts(
            {"driver/wave/CARD-1": json.dumps({"verdict": "bounced", "note": "too big to judge"})})
        self.assertTrue(rev["recorded"], rev)
        data = json.loads(self.vfile.read_text(encoding="utf-8"))
        self.assertEqual(data["verdict"], "bounced")
        self.assertTrue(data["diff_truncated"])


if __name__ == "__main__":
    unittest.main()
