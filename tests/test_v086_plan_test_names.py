"""Pilot, 18 Nov 2026: card M3 stopped as cannot-split on a good plan.

The planner's brief lists each test as "`name` in file". The planner wrote its
steps' tests the same way ("op_symbol_spells_each_operator in
tests/test_op_symbol.rs"), and the plan was refused because only the bare name
was accepted, so the card stopped before any code was written.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.cards import Card
from openfarm_core.errors import Invalid
from openfarm_core.roles import parse_plan


def _card() -> Card:
    tests = [{"name": "op_symbol_spells_each_operator", "file": "tests/test_op_symbol.rs",
              "asserts": "x"},
             {"name": "op_symbol_matches_the_parsed_text", "file": "tests/test_op_symbol.rs",
              "asserts": "y"}]
    return Card(id="M3", title="Op::symbol", state="ready",
                touches=["src/parse.rs", "src/display.rs"],
                fields={"category": "mechanical", "tests": tests})


def _plan(*step_tests: list[str]) -> str:
    return json.dumps({"steps": [{"n": i, "does": f"step {i}", "tests": t}
                                 for i, t in enumerate(step_tests, start=1)]})


class PlanTestNameTests(unittest.TestCase):
    def test_the_pilots_plan_is_read(self) -> None:
        plan = parse_plan(_plan(
            ["op_symbol_spells_each_operator in tests/test_op_symbol.rs"],
            ["op_symbol_matches_the_parsed_text in tests/test_op_symbol.rs",
             "op_symbol_spells_each_operator in tests/test_op_symbol.rs"]), _card())
        self.assertEqual(plan.steps[0]["tests"], ["op_symbol_spells_each_operator"])
        self.assertEqual(plan.steps[1]["tests"], ["op_symbol_matches_the_parsed_text",
                                                  "op_symbol_spells_each_operator"])

    def test_backticked_forms_are_read(self) -> None:
        plan = parse_plan(_plan(["`op_symbol_spells_each_operator` in tests/test_op_symbol.rs"],
                                ["`op_symbol_matches_the_parsed_text`"]), _card())
        self.assertEqual(plan.steps[1]["tests"], ["op_symbol_matches_the_parsed_text"])

    def test_a_wrong_file_is_still_refused(self) -> None:
        with self.assertRaises(Invalid):
            parse_plan(_plan(["op_symbol_spells_each_operator in tests/other.rs"],
                             ["op_symbol_matches_the_parsed_text"]), _card())

    def test_an_unknown_test_is_still_refused(self) -> None:
        with self.assertRaises(Invalid):
            parse_plan(_plan(["made_up in tests/test_op_symbol.rs"],
                             ["op_symbol_matches_the_parsed_text"]), _card())



class CopiedBriefLineTests(unittest.TestCase):
    """The re-run on the fix above: the planner copied the whole brief line back."""

    def test_name_file_and_description(self) -> None:
        plan = parse_plan(_plan(
            ['op_symbol_spells_each_operator in tests/test_op_symbol.rs \u2014 '
             'Op::Caret.symbol() == "^", Op::Wildcard.symbol() == ""'],
            ['`op_symbol_matches_the_parsed_text` in `tests/test_op_symbol.rs`: y']), _card())
        self.assertEqual([s["tests"] for s in plan.steps],
                         [["op_symbol_spells_each_operator"],
                          ["op_symbol_matches_the_parsed_text"]])

    def test_another_file_or_more_text_is_still_refused(self) -> None:
        for bad in ("op_symbol_spells_each_operator in tests/other.rs \u2014 x",
                    "op_symbol_spells_each_operator and op_symbol_matches_the_parsed_text",
                    "cargo test op_symbol_spells_each_operator"):
            with self.subTest(bad=bad), self.assertRaises(Invalid):
                parse_plan(_plan([bad], ["op_symbol_matches_the_parsed_text"]), _card())


if __name__ == "__main__":
    unittest.main()
