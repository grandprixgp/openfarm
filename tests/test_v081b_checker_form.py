"""v0.8.1b checker K1: the second pass's answer has one form, and it can be read.

The brief sends `pass2_answer_form(...)` and `parse_pass2` reads that form, so the
two are defined in one place. The answers the old parser read still parse. An
answer that cannot be read says so; it is never "no counter-tests".
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.briefs import HANDBACK_INSTRUCTIONS, ROLE_HEADERS, checker_pass2_brief
from openfarm_core.cards import Card
from openfarm_core.roles import (COUNTER_TEST_HEADER, MAX_COUNTER_TESTS,
                                 PASS2_SECTIONS, PASS2_VERDICTS, parse_counter_tests,
                                 parse_pass2, pass2_answer_form)

#: The real second-pass answer from the live run on the planted `slugify` bug
#: (DeepSeek V4.1 Flash, 17 Nov): it found the bug and wrote two counter-tests,
#: in markdown, and the old parser read "0 counter-tests".
LIVE_ANSWER = (
    "**Verdict: changes requested.** The diff fixes trailing dashes only. It does not "
    "satisfy “no dash at either end” because `.rstrip(\"-\")` leaves leading "
    "dashes intact.\n\n### Traceability table\n\n| ID | Requirement / source | "
    "Evidence in diff | Status |\n|---|---|---|---|\n| R1 | A slug has no dash at "
    "either end | Card title | ❌ **FAIL** |\n\n### Traced edge cases\n\n1. "
    "**Leading punctuation with trailing punctuation**\n   - Input: `\"  --Draft--  \"`\n"
    "   - Result: **FAIL** — leading dash remains.\n\n### Counter-tests expected "
    "to fail\n\n```python\ndef test_leading_and_trailing_punctuation_leaves_no_dashes"
    "(self):\n    self.assertEqual(slugify(\"  --Draft--  \"), \"draft\")\n```\n\n"
    "Expected `\"draft\"`, actual `\"-draft\"`.\n\n```python\ndef "
    "test_leading_punctuation_before_word_leaves_no_dash(self):\n    "
    "self.assertEqual(slugify(\"!!!Hello\"), \"hello\")\n```\n\nExpected `\"hello\"`, "
    "actual `\"-hello\"`.\n\n**Recommended fix:** replace `.rstrip(\"-\")` with "
    "`.strip(\"-\")`.")

COUNTER_FILE = '''import unittest

from textkit.slug import slugify


class SlugCounterTests(unittest.TestCase):
    def {name}(self):
        self.assertEqual(slugify({given!r}), {wanted!r})
'''


def counter_file(name: str, given: str, wanted: str) -> str:
    return COUNTER_FILE.format(name=name, given=given, wanted=wanted)


def form_answer(verdict: str = "changes-requested", tests=(), *,
                reasons: str = "`.rstrip(\"-\")` trims only the right end.") -> str:
    """An answer written exactly as the form says."""
    parts = [f"VERDICT: {verdict}", "", "REASONS:", reasons, "",
             "TRACEABILITY:",
             "| no dash at either end | `.rstrip(\"-\")` | fails at the start |", "",
             "EDGE CASES:",
             "1. \"  --Draft--  \": sub gives \"-draft-\", rstrip gives \"-draft\".",
             "2. \"!!!\": sub gives \"-\", rstrip gives \"\".", ""]
    for name, content in tests:
        parts += [f"{COUNTER_TEST_HEADER} {name}", content]
    return "\n".join(parts)


def _card() -> Card:
    return Card(id="CARD-1", title="A slug has no dash at either end", state="ready",
                done_text="`slugify` never returns a slug that starts or ends with `-`.",
                touches=["textkit/slug.py"],
                fields={"category": "pinned-bugfix", "unit": ".",
                        "tests": [{"name": "test_punctuation_at_the_ends_leaves_no_dash",
                                   "file": "tests/test_slug_ends.py",
                                   "asserts": "slugify(\"Hello, World!\") == \"hello-world\""}]})


class FormAndParserAgreeTests(unittest.TestCase):
    def test_the_form_names_every_section_verdict_and_the_block_header(self) -> None:
        form = pass2_answer_form("python", "unittest",
                                 ["tests/test_openfarm_counter_1.py",
                                  "tests/test_openfarm_counter_2.py"])
        for label in PASS2_SECTIONS:
            self.assertIn(f"\n{label}:", form)
        for verdict in PASS2_VERDICTS:
            self.assertIn(f"VERDICT: {verdict}", form)
        self.assertIn(f"{COUNTER_TEST_HEADER} <test name>", form)
        self.assertIn(f"up to {MAX_COUNTER_TESTS} counter-tests", form)
        self.assertIn("counter-test 1: tests/test_openfarm_counter_1.py", form)
        self.assertIn("unittest.TestCase", form)
        self.assertIn("complete test file", form)

    def test_the_form_says_which_runner_for_each_pack(self) -> None:
        self.assertIn("pytest", pass2_answer_form("python", "pytest", ["tests/t.py"]))
        rust = pass2_answer_form("rust", "", ["tests/openfarm_counter_1.rs"], "toy")
        self.assertIn("cargo test --test", rust)
        self.assertIn("`use toy::...;`", rust)
        self.assertIn("node:test", pass2_answer_form("typescript", "node", ["t.ts"]))

    def test_the_brief_carries_the_form_it_was_given(self) -> None:
        form = pass2_answer_form("python", "unittest", ["tests/test_openfarm_counter_1.py"])
        brief = checker_pass2_brief(_card(), "+x", [], {}, answer_form=form).body
        self.assertIn(form, brief)
        self.assertTrue(brief.startswith(ROLE_HEADERS["checker_pass2"]))
        # Without a form passed in the brief still says how to answer.
        self.assertIn(f"{COUNTER_TEST_HEADER} <test name>",
                      checker_pass2_brief(_card(), "+x", [], {}).body)

    def test_the_form_comes_before_the_hand_back_text(self) -> None:
        form = pass2_answer_form("python", "unittest", ["tests/a.py"])
        off = checker_pass2_brief(_card(), "+x", [], {}, answer_form=form).body
        on = checker_pass2_brief(_card(), "+x", [], {}, handback=True,
                                 answer_form=form).body
        self.assertEqual(on, off + "\n\n" + HANDBACK_INSTRUCTIONS)

    def test_asking_again_restates_the_problem_and_the_form(self) -> None:
        form = pass2_answer_form("python", "unittest", ["tests/a.py"])
        brief = checker_pass2_brief(_card(), "+x", [], {}, answer_form=form,
                                    retry_problem="no `VERDICT:` line was found").body
        self.assertIn("could not be read: no `VERDICT:` line was found", brief)
        self.assertIn(form, brief)

    def test_an_answer_in_the_form_reads_back_whole(self) -> None:
        first = counter_file("test_leading_dash_goes", "  --Draft--  ", "draft")
        second = counter_file("test_dash_before_a_word_goes", "!!!Hello", "hello")
        answer = parse_pass2(form_answer(tests=[("test_leading_dash_goes", first),
                                                ("test_dash_before_a_word_goes", second)]))
        self.assertTrue(answer.readable, answer.problem)
        self.assertEqual(answer.form, "blocks")
        self.assertEqual(answer.verdict, "changes-requested")
        self.assertIn("trims only the right end", answer.reasons)
        self.assertIn("no dash at either end", answer.traceability)
        self.assertIn("\"!!!\"", answer.edge_cases)
        self.assertEqual([t["name"] for t in answer.counter_tests],
                         ["test_leading_dash_goes", "test_dash_before_a_word_goes"])
        self.assertEqual(answer.counter_tests[0]["content"], first)
        self.assertEqual(answer.counter_tests[1]["content"], second)
        self.assertEqual(answer.missing, [])

    def test_an_accepting_answer_with_no_counter_tests_is_readable(self) -> None:
        answer = parse_pass2(form_answer("accept", reasons="Both ends are trimmed."))
        self.assertTrue(answer.readable)
        self.assertEqual(answer.verdict, "accept")
        self.assertEqual(answer.counter_tests, [])

    def test_a_fence_around_a_counter_test_and_bold_labels_are_tolerated(self) -> None:
        content = counter_file("test_leading_dash_goes", "-a", "a")
        text = ("**VERDICT:** Changes requested.\n\n## Reasons:\nleading dash kept\n\n"
                f"{COUNTER_TEST_HEADER} `test_leading_dash_goes`\n```python\n{content}```\n")
        answer = parse_pass2(text)
        self.assertTrue(answer.readable, answer.problem)
        self.assertEqual(answer.verdict, "changes-requested")
        self.assertEqual(answer.reasons, "leading dash kept")
        self.assertEqual(answer.counter_tests[0]["name"], "test_leading_dash_goes")
        self.assertEqual(answer.counter_tests[0]["content"], content)
        self.assertEqual(sorted(answer.missing), ["edge cases", "traceability"])

    def test_the_limit_of_two_counter_tests_stays(self) -> None:
        tests = [(f"test_n{i}", counter_file(f"test_n{i}", "-a", "a")) for i in range(3)]
        answer = parse_pass2(form_answer(tests=tests))
        self.assertEqual(len(answer.counter_tests), MAX_COUNTER_TESTS)
        self.assertEqual([d["name"] for d in answer.dropped], ["test_n2"])
        self.assertTrue(answer.dropped[0]["over_limit"])

    def test_a_block_with_no_file_is_dropped_with_a_reason(self) -> None:
        answer = parse_pass2(form_answer(tests=[("test_empty", "\n")]))
        self.assertTrue(answer.readable)
        self.assertEqual(answer.counter_tests, [])
        self.assertEqual(answer.dropped[0]["name"], "test_empty")
        self.assertIn("no file content", answer.dropped[0]["why"])


class UnreadableAnswerTests(unittest.TestCase):
    def test_the_live_markdown_answer_is_not_read_as_no_counter_tests(self) -> None:
        answer = parse_pass2(LIVE_ANSWER)
        self.assertFalse(answer.readable)
        self.assertIn("code fences", answer.problem)
        self.assertIn(COUNTER_TEST_HEADER, answer.problem)
        # What it did say is still kept for the record.
        self.assertEqual(answer.verdict, "changes-requested")
        self.assertEqual(parse_counter_tests(LIVE_ANSWER).tests, [])

    def test_prose_with_no_verdict_cannot_be_read(self) -> None:
        answer = parse_pass2("The diff looks fine to me, nothing to add.")
        self.assertFalse(answer.readable)
        self.assertIn("VERDICT", answer.problem)

    def test_an_empty_or_cut_off_answer_cannot_be_read(self) -> None:
        self.assertFalse(parse_pass2("").readable)
        cut = parse_pass2(form_answer("accept"), cut_off=True)
        self.assertFalse(cut.readable)
        self.assertIn("cut off", cut.problem)

    def test_a_json_snippet_quoted_in_prose_is_not_an_answer(self) -> None:
        self.assertFalse(parse_pass2('The config {"a": 1} is fine.').readable)

    def test_quoting_a_named_test_in_a_fence_is_not_a_counter_test(self) -> None:
        text = form_answer("accept") + (
            "\nThe card's own test:\n```python\ndef test_x(self):\n    pass\n```\n")
        self.assertTrue(parse_pass2(text).readable)


class OldFormsStillReadTests(unittest.TestCase):
    def test_the_old_json_shape(self) -> None:
        payload = {"traceability": [], "edge_cases": ["capacity 0"],
                   "counter_tests": [{"name": "a_counter_test", "file": "tests/c.rs",
                                      "content": "#[test]\nfn a_counter_test() {}\n"}]}
        answer = parse_pass2(json.dumps(payload))
        self.assertTrue(answer.readable)
        self.assertEqual(answer.form, "legacy-json")
        self.assertEqual(answer.verdict, "")
        self.assertEqual(answer.counter_tests[0]["file"], "tests/c.rs")
        self.assertEqual(parse_counter_tests(json.dumps(payload)).tests[0]["name"],
                         "a_counter_test")

    def test_the_old_json_shape_inside_prose(self) -> None:
        text = 'Here: {"counter_tests": [{"name": "t", "content": "x"}]} done.'
        answer = parse_pass2(text)
        self.assertTrue(answer.readable)
        self.assertEqual(answer.counter_tests[0]["name"], "t")

    def test_an_empty_json_object_is_the_old_no_counter_tests(self) -> None:
        answer = parse_pass2("{}")
        self.assertTrue(answer.readable)
        self.assertEqual(answer.counter_tests, [])

    def test_a_json_answer_with_the_new_fields(self) -> None:
        answer = parse_pass2(json.dumps({
            "verdict": "changes-requested", "reasons": "leading dash",
            "traceability": "t", "edge_cases": ["a", "b"],
            "counter_tests": [{"name": "t1", "content": "x"}]}))
        self.assertEqual(answer.form, "json")
        self.assertEqual(answer.verdict, "changes-requested")
        self.assertEqual(answer.edge_cases, "a\nb")

    def test_the_old_counter_test_blocks(self) -> None:
        text = ("COUNTER_TEST: first_one\n#[test]\nfn first_one() {}\n"
                "COUNTER_TEST: second_one\n#[test]\nfn second_one() {}\n"
                "COUNTER_TEST: third_one\nfn x() {}\n")
        answer = parse_pass2(text)
        self.assertTrue(answer.readable)
        self.assertEqual(answer.form, "legacy-blocks")
        self.assertEqual([t["name"] for t in answer.counter_tests],
                         ["first_one", "second_one"])
        self.assertEqual(parse_counter_tests(text).tests[1]["content"],
                         "#[test]\nfn second_one() {}")


if __name__ == "__main__":
    unittest.main()
