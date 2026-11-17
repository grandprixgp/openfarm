"""The Rust pack's parsing, against real cargo output shapes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openfarm_core.packs.rust import RustPack, added_lines_from_diff


TEST_OUTPUT = """\
   Compiling toy v0.1.0 (/tmp/toy)
    Finished `test` profile [unoptimized + debuginfo] target(s) in 0.42s
     Running unittests src/lib.rs (/tmp/toy/target/debug/deps/toy-abc123)

running 1 test
test spine::tests::a_new_spine_is_empty ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

     Running tests/existing.rs (/tmp/toy/target/debug/deps/existing-def456)

running 1 test
test an_empty_fascicle_has_length_zero ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
"""

FAILING_OUTPUT = """\
     Running tests/full.rs (/tmp/toy/target/debug/deps/full-111)

running 2 tests
test a_full_fascicle_refuses_a_push ... FAILED
test another_one ... ok

test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out
"""

LIST_OUTPUT = """\
an_empty_fascicle_has_length_zero: test
a_full_fascicle_refuses_a_push: test
a_benchmark: bench
spine::tests::a_new_spine_is_empty: test
"""


class ParseTestsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = RustPack()

    def test_reports_each_result_with_its_binary(self) -> None:
        parsed = self.pack.parse_tests(TEST_OUTPUT)
        names = {r["name"]: r for r in parsed["results"]}
        self.assertEqual(names["an_empty_fascicle_has_length_zero"]["status"], "ok")
        self.assertEqual(names["spine::tests::a_new_spine_is_empty"]["status"], "ok")
        self.assertIn("tests/existing.rs", names["an_empty_fascicle_has_length_zero"]["binary"])
        self.assertEqual(parsed["missing_footers"], [])

    def test_a_failed_test_is_failed(self) -> None:
        parsed = self.pack.parse_tests(FAILING_OUTPUT)
        statuses = {r["name"]: r["status"] for r in parsed["results"]}
        self.assertEqual(statuses["a_full_fascicle_refuses_a_push"], "FAILED")
        self.assertEqual(statuses["another_one"], "ok")

    def test_footers_are_counted(self) -> None:
        parsed = self.pack.parse_tests(TEST_OUTPUT)
        self.assertEqual(len(parsed["footers"]), 2)
        self.assertEqual(parsed["footers"][0]["passed"], 1)

    def test_a_binary_without_a_footer_is_reported(self) -> None:
        # The run died partway: no footer after the running line.
        truncated = """\
     Running tests/one.rs (/tmp/toy/target/debug/deps/one-abc)

running 1 test
test only_one ... ok
"""
        parsed = self.pack.parse_tests(truncated)
        self.assertEqual(len(parsed["missing_footers"]), 1)
        self.assertIn("tests/one.rs", parsed["missing_footers"][0])

    def test_compile_error_is_reported(self) -> None:
        broken = """\
   Compiling toy v0.1.0 (/tmp/toy)
error[E0425]: cannot find function `missing` in this scope
 --> tests/full.rs:4:5
error: could not compile `toy` (test "full") due to 1 previous error
"""
        parsed = self.pack.parse_tests(broken)
        self.assertTrue(parsed["compile_errors"])
        self.assertIn("full", parsed["missing_footers"])

    def test_ignored_status_is_kept(self) -> None:
        text = """\
     Running tests/x.rs (/tmp/x)

running 1 test
test skipped_one ... ignored

test result: ok. 0 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out
"""
        parsed = self.pack.parse_tests(text)
        self.assertEqual(parsed["results"][0]["status"], "ignored")


class ParseListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = RustPack()

    def test_reads_test_lines_and_drops_benches(self) -> None:
        listing = self.pack.parse_list(LIST_OUTPUT)
        names = [t["name"] for t in listing["tests"]]
        self.assertIn("an_empty_fascicle_has_length_zero", names)
        self.assertIn("spine::tests::a_new_spine_is_empty", names)
        self.assertNotIn("a_benchmark", names)

    def test_empty_output(self) -> None:
        self.assertEqual(self.pack.parse_list(""), {"tests": []})


class CommandsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = RustPack()

    def test_test_command_shape(self) -> None:
        argv = self.pack.commands("test", unit="toy")["argv"]
        self.assertEqual(argv[:5], ["cargo", "test", "-p", "toy", "--locked"])
        self.assertIn("--no-fail-fast", argv)
        self.assertIn("--lib", argv)
        self.assertIn("--bins", argv)
        self.assertIn("--tests", argv)

    def test_locked_always_precedes_the_separator(self) -> None:
        for purpose in ("test", "list", "lint", "ws-lint", "build"):
            argv = self.pack.commands(purpose, unit="toy")["argv"]
            if "--" not in argv:
                continue
            separator = argv.index("--")
            self.assertLess(argv.index("--locked"), separator,
                            f"{purpose}: --locked must come before --")

    def test_list_compiles_the_tests(self) -> None:
        argv = self.pack.commands("list", unit="toy")["argv"]
        self.assertIn("--list", argv)
        self.assertEqual(argv[argv.index("--list") - 1], "--")

    def test_lint_turns_warnings_into_errors(self) -> None:
        argv = self.pack.commands("lint", unit="toy")["argv"]
        self.assertEqual(argv[-2:], ["-D", "warnings"])

    def test_workspace_lint_shape(self) -> None:
        argv = self.pack.commands("ws-lint")["argv"]
        self.assertIn("--workspace", argv)
        self.assertIn("--all-targets", argv)

    def test_unknown_purpose_refuses(self) -> None:
        from openfarm_core.errors import Invalid
        with self.assertRaises(Invalid):
            self.pack.commands("teleport")


class ClassifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = RustPack()

    def test_paths(self) -> None:
        result = self.pack.classify_paths([
            "tests/full.rs", "src/fascicle.rs", "src/lib.rs",
            "benches/bench.rs", "README.md", "crates/a/tests/x.rs",
        ])
        self.assertEqual(result["tests/full.rs"], "test")
        self.assertEqual(result["src/fascicle.rs"], "production")
        self.assertEqual(result["src/lib.rs"], "production")
        self.assertEqual(result["benches/bench.rs"], "other")
        self.assertEqual(result["README.md"], "other")
        self.assertEqual(result["crates/a/tests/x.rs"], "test")


class PatternsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = RustPack()

    def test_production_unwrap_is_banned_but_test_unwrap_is_not(self) -> None:
        lines = [{"path": "src/a.rs", "line": 2, "text": "    let x = y.unwrap();"}]
        found = self.pack.patterns(lines, [], {"src/a.rs"})
        self.assertTrue(any(b["pattern"] == "production-unwrap"
                            for b in found["banned"]))

        test_lines = [{"path": "tests/a.rs", "line": 2, "text": "    let x = y.unwrap();"}]
        found = self.pack.patterns(test_lines, [], set())
        self.assertFalse(any(b["pattern"] == "production-unwrap"
                             for b in found["banned"]))

    def test_suppressions_and_stubs_are_banned_everywhere(self) -> None:
        lines = [
            {"path": "tests/a.rs", "line": 1, "text": "#[allow(dead_code)]"},
            {"path": "tests/a.rs", "line": 2, "text": "#[ignore]"},
            {"path": "src/a.rs", "line": 3, "text": "todo!()"},
            {"path": "src/a.rs", "line": 4, "text": "unsafe { }"},
            {"path": "src/a.rs", "line": 5, "text": "dbg!(x);"},
        ]
        found = self.pack.patterns(lines, [], {"src/a.rs"})
        labels = {b["pattern"] for b in found["banned"]}
        for expected in ("suppression-allow", "test-ignored", "leftover-stub",
                         "unsafe", "debug-macro"):
            self.assertIn(expected, labels)

    def test_flagged_patterns_are_reported_not_banned(self) -> None:
        lines = [{"path": "tests/a.rs", "line": 1,
                  "text": "    std::fs::write(p, b\"x\")"}]
        found = self.pack.patterns(lines, ["std::fs::write"], set())
        self.assertTrue(found["flagged"])
        self.assertFalse(found["banned"])


class ErrorSignatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = RustPack()

    def test_line_numbers_and_paths_are_stripped(self) -> None:
        first = self.pack.error_signature(
            "error[E0308]: mismatched types\n --> src/a.rs:12:5")["signature"]
        second = self.pack.error_signature(
            "error[E0308]: mismatched types\n --> src/a.rs:99:1")["signature"]
        self.assertEqual(first, second)

    def test_different_errors_differ(self) -> None:
        first = self.pack.error_signature("error[E0308]: mismatched types")["signature"]
        second = self.pack.error_signature("error[E0425]: not found")["signature"]
        self.assertNotEqual(first, second)


class AppendStubTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = RustPack()
        self.tmp = Path(tempfile.mkdtemp())

    def _file(self, text: str) -> Path:
        path = self.tmp / "lib.rs"
        path.write_text(text)
        return path

    def test_stub_goes_above_the_only_test_module(self) -> None:
        path = self._file(
            "pub fn a() {}\n"
            "\n"
            "#[cfg(test)]\n"
            "mod tests {\n"
            "    #[test]\n"
            "    fn t() {}\n"
            "}\n")
        result = self.pack.append_stub(path, "t", "pub fn missing() { todo!() }")
        text = path.read_text()
        self.assertLess(text.index("pub fn missing"), text.index("#[cfg(test)]"))
        self.assertGreater(result["line"], 1)

    def test_stub_steps_back_over_attribute_lines(self) -> None:
        path = self._file(
            "pub fn a() {}\n"
            "\n"
            "#[allow(unused)]\n"
            "#[cfg(test)]\n"
            "mod tests {}\n")
        self.pack.append_stub(path, "t", "pub fn missing() { todo!() }")
        text = path.read_text()
        self.assertLess(text.index("pub fn missing"), text.index("#[allow(unused)]"))

    def test_stub_appends_at_the_end_without_a_test_module(self) -> None:
        path = self._file("pub fn a() {}\n")
        self.pack.append_stub(path, "t", "pub fn missing() { todo!() }")
        text = path.read_text()
        self.assertIn("pub fn a() {}", text)
        self.assertIn("pub fn missing() { todo!() }", text)
        self.assertLess(text.index("pub fn a"), text.index("pub fn missing"))

    def test_two_test_modules_refuse(self) -> None:
        from openfarm_core.errors import Refused
        path = self._file(
            "#[cfg(test)]\nmod one {}\n"
            "#[cfg(test)]\nmod two {}\n")
        with self.assertRaises(Refused) as caught:
            self.pack.append_stub(path, "t", "pub fn missing() {}")
        self.assertEqual(caught.exception.code, "multiple-test-modules")


class MutationParseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = RustPack()

    def test_summary_line(self) -> None:
        # cargo-mutants 27 prints text for a run; `--json` is for `--list` only.
        parsed = self.pack.parse_mutation(
            "Found 10 mutants to test\nok       Unmutated baseline\n"
            "MISSED   src/lib.rs:1:1: replace f -> bool with true\n"
            "MISSED   src/lib.rs:2:1: replace g -> bool with true\n"
            "10 mutants tested: 2 missed, 8 caught\n")
        self.assertEqual(parsed["killed"], 8)
        self.assertEqual(parsed["survived"], 2)
        self.assertEqual(parsed["total"], 10)
        self.assertFalse(parsed["capped"])

    def test_empty_output_is_an_invalid_answer(self) -> None:
        from openfarm_core.errors import Invalid
        with self.assertRaises(Invalid):
            self.pack.parse_mutation("")


class DiffTests(unittest.TestCase):
    def test_added_lines_carry_new_file_line_numbers(self) -> None:
        diff = (
            "diff --git a/src/a.rs b/src/a.rs\n"
            "--- a/src/a.rs\n"
            "+++ b/src/a.rs\n"
            "@@ -1,2 +1,3 @@\n"
            " one\n"
            "-two\n"
            "+two changed\n"
            "+three\n")
        lines = added_lines_from_diff(diff)
        texts = [line["text"] for line in lines]
        self.assertEqual(texts, ["two changed", "three"])
        self.assertEqual(lines[0]["path"], "src/a.rs")
        self.assertEqual([line["line"] for line in lines], [2, 3])


if __name__ == "__main__":
    unittest.main()
