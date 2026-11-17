"""v0.8.1c glue, G5: the checker's first pass answers in a form that can be read.

The brief states the form (`pass1_answer_form`) and `parse_pass1` reads it: the
restatement, whether the tests would catch a bad implementation, and what they
miss. Old answers still read. An answer that cannot be told apart from a critique
of the tests is asked for once more, then recorded as unreadable, never taken as
the restatement. The live first-pass answers from 24 runs are the fixtures.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.briefs import HANDBACK_INSTRUCTIONS, ROLE_HEADERS, checker_pass1_brief
from openfarm_core.cards import Card
from openfarm_core.errors import StopCode
from openfarm_core.roles import (PASS1_CATCHES, PASS1_SECTIONS, parse_pass1,
                                 parse_restatement, pass1_answer_form)
from tests.test_v081b_checker_flow import HB, _SlugHarness
from tests.test_v081b_checker_form import form_answer

LIVE = Path(__file__).resolve().parent.parent / ".lead" / "repro" / "live-answers"

DONE = ("- `slugify` never returns a slug that starts or ends with `-`: "
        "`slugify(\"Hello, World!\")` is `\"hello-world\"`.\n"
        "- Text with no letters or digits gives `\"\"`.\n")

#: The live first-pass answer of the `py-slug-ends` repro, kept here so the test
#: does not depend on the `.lead` folder: a critique first, then the restatement
#: under a label of the model's own.
LIVE_SLUG = """\
**Would the tests catch a bad implementation?**
Only partially. The test diff contains one assertion:

```python
slugify("Hello, World!") == "hello-world"
```

It would miss bad implementations such as ones that:

- leave a leading dash for inputs beginning with punctuation or dashes,
- fail to return `""` for all-punctuation input like `"!!!"`.

**Done text restated in my own words:**
- `slugify` must never produce a slug that begins or ends with `-`.
- If the input contains no letters or digits, the result should be the empty string.
"""

#: A live answer with no restatement label at all (bug/py-money-parse's shape).
LIVE_NO_LABEL = """\
**Verdict: No — the tests are much too thin to catch most bad implementations.**

The only test checks `parse_cents("12.34") == 1234`. That means a bad
implementation could still pass easily, for example one that ignores the sign.
"""

FORM = """\
RESTATEMENT:
slugify never gives a slug that starts or ends with a dash; text with no letters
or digits gives an empty slug.

CATCHES: partly

MISSES:
- a leading dash: only the trailing end is tested
- "!!!" giving "" is not tested
"""


def _card() -> Card:
    return Card(id="CARD-1", title="A slug has no dash at either end", state="ready",
                done_text=DONE, touches=["textkit/slug.py"],
                fields={"category": "pinned-bugfix", "unit": ".",
                        "tests": [{"name": "test_punctuation_at_the_ends_leaves_no_dash",
                                   "file": "tests/test_slug_ends.py"}]})


class FormAndParserTests(unittest.TestCase):
    def test_the_form_names_every_section_and_answer(self) -> None:
        form = pass1_answer_form()
        for label in PASS1_SECTIONS:
            self.assertIn(f"\n{label}:", "\n" + form)
        for value in PASS1_CATCHES:
            self.assertIn(f"CATCHES: {value}", form)
        self.assertIn("does not pass or block", form)

    def test_an_answer_in_the_form_is_read_in_full(self) -> None:
        answer = parse_pass1(FORM, _card())
        self.assertTrue(answer.readable)
        self.assertEqual(answer.form, "form")
        self.assertEqual(answer.catches, "partly")
        self.assertIn("starts or ends with a dash", answer.restatement.text)
        self.assertNotIn("CATCHES", answer.restatement.text)
        self.assertNotIn("leading dash: only", answer.restatement.text)
        self.assertIn("a leading dash: only the trailing end is tested", answer.misses)
        self.assertFalse(answer.restatement.mismatch)

    def test_bold_and_headings_around_labels_are_tolerated(self) -> None:
        text = ("**RESTATEMENT:** slugs never start or end with a dash\n\n"
                "## Catches: no\n\n**MISSES:**\n- everything but one input\n")
        answer = parse_pass1(text, _card())
        self.assertEqual((answer.readable, answer.catches), (True, "no"))
        self.assertEqual(answer.restatement.text, "slugs never start or end with a dash")
        self.assertIn("everything but one input", answer.misses)

    def test_the_mismatch_rule_reads_the_restatement_alone(self) -> None:
        # The misses repeat every word of the card; the restatement drifts.
        drift = ("RESTATEMENT:\nthe function sorts a list of numbers\n\nCATCHES: no\n\n"
                 "MISSES:\n" + DONE + "\ntest_punctuation_at_the_ends_leaves_no_dash\n")
        self.assertTrue(parse_pass1(drift, _card()).restatement.mismatch)
        # And the other way: a faithful restatement, unrelated misses.
        faithful = ("RESTATEMENT:\n" + DONE + "\nCATCHES: no\n\nMISSES:\nthe sorting\n")
        self.assertFalse(parse_pass1(faithful, _card()).restatement.mismatch)

    def test_old_answers_are_still_read_as_before(self) -> None:
        card = _card()
        for old in ("RESTATEMENT: slugs have no dash at either end",
                    "Here it is. RESTATEMENT: slugs have no dash at either end",
                    "slugs have no dash at either end, and !!! gives empty",
                    "{}"):
            with self.subTest(old):
                answer = parse_pass1(old, card)
                self.assertTrue(answer.readable, answer.problem)
                before = parse_restatement(old, card)
                self.assertEqual(answer.restatement.as_dict(), before.as_dict())

    def test_unreadable_answers_are_never_taken_as_the_restatement(self) -> None:
        for text, cut_off, why in (("", False, "empty"),
                                   ("RESTATEMENT: half an ans", True, "cut off"),
                                   (LIVE_NO_LABEL, False, "no `RESTATEMENT:`")):
            with self.subTest(why):
                answer = parse_pass1(text, _card(), cut_off=cut_off)
                self.assertFalse(answer.readable)
                self.assertIn(why, answer.problem)
                self.assertEqual(answer.restatement.text, "")

    def test_the_live_repro_answer_splits_into_restatement_and_misses(self) -> None:
        answer = parse_pass1(LIVE_SLUG, _card())
        self.assertTrue(answer.readable)
        self.assertTrue(answer.restatement.text.startswith("- `slugify` must never"))
        self.assertNotIn("bad implementation", answer.restatement.text)
        self.assertIn("leave a leading dash", answer.misses)

    @unittest.skipUnless(LIVE.is_dir(), "the live answers are not in this checkout")
    def test_no_live_answer_has_its_critique_taken_as_the_restatement(self) -> None:
        files = sorted(LIVE.glob("*/*/checker-pass1.json")) + sorted(
            LIVE.glob("*/responses/checker-pass1.json"))
        self.assertGreaterEqual(len(files), 24)
        for path in files:
            content = json.loads(path.read_text())["content"]
            with self.subTest(str(path.relative_to(LIVE))):
                answer = parse_pass1(content, _card())
                if answer.readable:
                    self.assertNotIn("bad implementation", answer.restatement.text)
                    self.assertTrue(answer.misses)
                else:
                    self.assertEqual(answer.restatement.text, "")


class BriefTests(unittest.TestCase):
    def test_the_brief_states_the_form_and_the_header_points_at_it(self) -> None:
        brief = checker_pass1_brief(_card(), "+def test_x(): pass")
        self.assertIn(pass1_answer_form(), brief.body)
        self.assertIn("form given at the end of this brief", ROLE_HEADERS["checker_pass1"])

    def test_the_retry_says_why_and_the_hand_back_text_comes_last(self) -> None:
        retry = checker_pass1_brief(_card(), "+x", retry_problem="the answer is empty")
        self.assertIn("could not be read: the answer is empty", retry.body)
        off = checker_pass1_brief(_card(), "+x")
        on = checker_pass1_brief(_card(), "+x", handback=True)
        self.assertTrue(on.body.endswith(HANDBACK_INSTRUCTIONS))
        self.assertIn(pass1_answer_form(), on.body)
        self.assertLess(on.body.index(pass1_answer_form()),
                        on.body.index(HANDBACK_INSTRUCTIONS))
        self.assertTrue(off.body.endswith(pass1_answer_form()))


class FirstPassFlowTests(_SlugHarness):
    def pass1_calls(self, run) -> list[dict]:
        return [c for c in run.backend.calls if c["role"] == "checker_pass1"]

    def test_an_answer_in_the_form_is_recorded_with_the_gate_and_in_the_dossier(self) -> None:
        run = self.card_run(self.script(form_answer("accept"),
                                        **{"CARD-1/checker_pass1": FORM}))
        result = run.run()
        self.assertEqual(result["final_state"], "in-review", result.get("detail"))
        self.assertEqual(len(self.pass1_calls(run)), 1)
        self.assertIn(pass1_answer_form(),
                      self.pass1_calls(run)[0]["messages"][0]["content"])
        gate = self.gate(run, "checker-pass1")
        self.assertEqual(gate.result, "pass")
        self.assertIn("would partly catch a bad implementation", gate.detail)
        self.assertEqual(gate.evidence["checker"]["catches"], "partly")
        self.assertIn("only the trailing end is tested", gate.evidence["checker"]["misses"])
        self.assertNotIn("CATCHES", gate.evidence["restatement"]["text"])
        payload, text = self.dossier(run, result)
        self.assertEqual(payload["6_checker"]["pass1"]["catches"], "partly")
        self.assertIn("would the tests catch a bad implementation: partly", text)
        self.assertIn("What the first pass says the tests miss", text)
        self.assertIn("only the trailing end is tested", text)

    def test_an_unreadable_answer_is_asked_for_again_and_the_second_is_used(self) -> None:
        run = self.card_run(self.script(form_answer("accept"), **{
            "CARD-1/checker_pass1/1": LIVE_NO_LABEL,
            "CARD-1/checker_pass1/2": FORM}))
        result = run.run()
        self.assertEqual(result["final_state"], "in-review", result.get("detail"))
        calls = self.pass1_calls(run)
        self.assertEqual([c["attempt"] for c in calls], [1, 2])
        again = calls[1]["messages"][0]["content"]
        self.assertIn("could not be read: it has no `RESTATEMENT:` section", again)
        self.assertIn(pass1_answer_form(), again)
        self.assertTrue(run.checker_pass1["asked_again"])
        self.assertEqual(run.checker_pass1["catches"], "partly")
        self.assertTrue((run.card_layout / "responses" / "checker-pass1-again.json").exists())
        _, text = self.dossier(run, result)
        self.assertIn("First pass asked again once", text)

    def test_an_answer_unreadable_twice_is_recorded_so_and_blocks_nothing(self) -> None:
        run = self.card_run(self.script(form_answer("accept"), **{
            "CARD-1/checker_pass1": LIVE_NO_LABEL}), counter_tests="reject")
        result = run.run()
        self.assertEqual(result["final_state"], "in-review", result.get("detail"))
        self.assertEqual(len(self.pass1_calls(run)), 2)
        gate = self.gate(run, "checker-pass1")
        self.assertEqual(gate.result, "pass")
        self.assertIn("could not be read, even when asked again", gate.detail)
        self.assertEqual(gate.evidence["restatement"]["text"], "")
        self.assertFalse(gate.evidence["checker"]["readable"])
        payload, text = self.dossier(run, result)
        self.assertIn("First pass: **could not be read**", text)
        self.assertNotIn("bad implementations", payload["6_checker"]["restatement"]["text"])
        self.assertTrue(any("first pass could not be read" in line
                            for line in payload["8_not_checked"]))

    def test_an_old_marker_answer_is_read_once(self) -> None:
        run = self.card_run(self.script(form_answer("accept")))
        result = run.run()
        self.assertEqual(result["final_state"], "in-review", result.get("detail"))
        self.assertEqual(len(self.pass1_calls(run)), 1)
        self.assertFalse(run.checker_pass1["asked_again"])
        self.assertEqual(run.checker_restatement.text,
                         "slugs have no dash at either end")

    def test_a_hand_back_is_read_first_on_either_ask(self) -> None:
        for script, calls in (({"CARD-1/checker_pass1": HB}, 1),
                              ({"CARD-1/checker_pass1/1": LIVE_NO_LABEL,
                                "CARD-1/checker_pass1/2": HB}, 2)):
            with self.subTest(calls=calls):
                run = self.card_run(self.script(form_answer("accept"), **script),
                                    swarm_on=True)
                result = run.run()
                self.assertEqual(result["stop_code"], StopCode.HANDED_BACK.value)
                self.assertEqual([h["role"] for h in run.handbacks], ["checker_pass1"])
                self.assertEqual(len(self.pass1_calls(run)), calls)
                self.assertFalse([g for g in run.gates if g.name == "checker-pass1"])


if __name__ == "__main__":
    unittest.main()
