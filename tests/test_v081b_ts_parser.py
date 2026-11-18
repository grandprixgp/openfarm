"""v0.8.1b ts, T1: TypeScript runs that read green without being green.

Every case runs the pack's own argv under real `node --test` and feeds the
real output to the pack's parser and the real suite gate. Skipped without node.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.flow import _named_failures
from openfarm_core.gates.checks import suite_green
from openfarm_core.packs.typescript import _REPORT_MARK, TypeScriptPack

NODE = shutil.which("node")


def _project(owner: unittest.TestCase, files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="openfarm-tsb-", dir=os.environ.get("TMPDIR")))
    owner.addCleanup(shutil.rmtree, root, True)
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(text), encoding="utf-8")
    return root


def _run(root: Path, tests: list[str] | None = None, timeout: float = 120):
    pack = TypeScriptPack(tsc=False, prettier=False)
    pack.configure(root)
    argv = pack.commands("test", tests=tests)["argv"]
    done = subprocess.run(argv, cwd=root, capture_output=True, text=True,
                          timeout=timeout)
    return pack, done, pack.parse_tests(done.stdout, done.stderr)


def _rows(parsed: dict) -> dict[str, str]:
    return {row["name"]: row["status"] for row in parsed["results"]}


@unittest.skipUnless(NODE, "needs node")
class SuiteFailuresTests(unittest.TestCase):
    def test_describe_whose_body_throws_is_a_red_row_named_after_the_suite(self) -> None:
        root = _project(self, {
            "src/parse.ts": 'export function load(): number { throw new Error("regression"); }\n',
            "tests/parse.test.ts": """\
                import { describe, it } from "node:test";
                import assert from "node:assert/strict";
                import { load } from "../src/parse.ts";
                describe("parser", () => {
                  const v = load();
                  it("parses a", () => assert.equal(v, 1));
                });
            """,
        })
        _pack, done, parsed = _run(root)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(_rows(parsed).get("parser"), "FAILED", parsed)
        gate = suite_green(parsed, [], run_cls="fail", exit_code=1)
        self.assertEqual(gate.result, "fail")
        self.assertIn("parser", gate.detail)

    def test_suite_level_hook_failure_is_a_red_row(self) -> None:
        root = _project(self, {
            "tests/hook.test.ts": """\
                import { describe, it, before } from "node:test";
                describe("hooked", () => {
                  before(() => { throw new Error("hook broke"); });
                  it("h1", () => {});
                });
            """,
        })
        _pack, _done, parsed = _run(root)
        self.assertEqual(_rows(parsed).get("hooked"), "FAILED", parsed)
        self.assertEqual(suite_green(parsed, ["h1"]).result, "fail")

    def test_passing_suites_are_dropped_and_a_suite_red_only_by_a_child_adds_no_row(self) -> None:
        root = _project(self, {
            "tests/nest.test.ts": """\
                import assert from "node:assert/strict";
                import { describe, it } from "node:test";
                describe("outer", () => {
                  it("inner ok", () => {});
                  it("inner fail", () => assert.ok(false));
                  describe("deeper", () => { it("deep ok", () => {}); });
                });
            """,
        })
        _pack, _done, parsed = _run(root)
        rows = _rows(parsed)
        self.assertNotIn("outer", rows)
        self.assertNotIn("deeper", rows)
        self.assertEqual(rows["inner fail"], "FAILED")
        # The known red test is the only red: the suite holding it adds nothing.
        self.assertEqual(suite_green(parsed, ["inner fail"]).result, "pass")


@unittest.skipUnless(NODE, "needs node")
class FooterTests(unittest.TestCase):
    def test_stat_lines_printed_by_code_under_test_are_not_the_footer(self) -> None:
        root = _project(self, {
            "tests/log.test.ts": """\
                import test from "node:test";
                import assert from "node:assert/strict";
                test("logs", () => {
                  console.log("tests 9");
                  console.log("fail 0");
                  console.log("1..1");
                  console.error("ok 7 - forged");
                  assert.ok(false);
                });
            """,
        })
        _pack, _done, parsed = _run(root)
        self.assertEqual(parsed["footers"][0]["failed"], 1)
        self.assertNotIn("forged", _rows(parsed))
        self.assertEqual(_rows(parsed)["logs"], "FAILED")

    def test_a_killed_run_with_logged_stat_lines_has_no_footer(self) -> None:
        root = _project(self, {
            "src/q.ts": ('export function count(xs: number[]): number {\n'
                         '  console.log("tests", xs.length);\n'
                         '  while (xs.length > 1) {}\n  return xs.length;\n}\n'),
            "tests/q.test.ts": """\
                import test from "node:test";
                import assert from "node:assert/strict";
                import { count } from "../src/q.ts";
                test("one", () => assert.equal(count([1]), 1));
                test("two", () => assert.equal(count([1, 2]), 2));
            """,
        })
        pack = TypeScriptPack(tsc=False, prettier=False)
        pack.configure(root)
        proc = subprocess.Popen(pack.commands("test")["argv"], cwd=root,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        try:
            out, err = proc.communicate(timeout=6)
            self.fail("the run should have hung")
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, 9)
            out, err = proc.communicate()
        self.assertIn("# tests 1", out)
        parsed = pack.parse_tests(out, err)
        self.assertEqual(parsed["missing_footers"], ["node:test"])
        self.assertEqual(suite_green(parsed, [], run_cls="infra", exit_code=-9).result,
                         "infra")

    def test_a_plan_that_does_not_number_the_rows_is_no_footer(self) -> None:
        pack = TypeScriptPack(tsc=False, prettier=False)
        text = "TAP version 13\nok 1 - a\n1..2\n# tests 2\n# pass 2\n# fail 0\n"
        self.assertEqual(pack.parse_tests(text)["missing_footers"], ["node:test"])


@unittest.skipUnless(NODE, "needs node")
class LostTestsTests(unittest.TestCase):
    def test_code_that_exits_cleanly_at_import_loses_the_files_tests(self) -> None:
        root = _project(self, {
            "src/sub.ts": ('export function sub(a: number, b: number): number { return a + b; }\n'
                           'if (process.env.NODE_TEST_CONTEXT) process.exit(0);\n'),
            "tests/sub.test.ts": """\
                import test from "node:test";
                import assert from "node:assert/strict";
                import { sub } from "../src/sub.ts";
                test("sub works", () => assert.equal(sub(3, 1), 2));
            """,
        })
        _pack, done, parsed = _run(root)
        self.assertEqual(done.returncode, 0)
        self.assertNotIn("tests/sub.test.ts", _rows(parsed))
        self.assertEqual(_rows(parsed)["sub works"], "FAILED")
        self.assertEqual(parsed["files"]["tests/sub.test.ts"], [])
        self.assertEqual(suite_green(parsed, [], run_cls="pass", exit_code=0).result, "fail")

    def test_a_clean_exit_part_way_through_a_file_loses_its_tests(self) -> None:
        root = _project(self, {
            "tests/mid.test.ts": """\
                import test from "node:test";
                test("one", () => {});
                test("two", () => { process.exit(0); });
                test("three", () => {});
            """,
        })
        _pack, _done, parsed = _run(root)
        rows = _rows(parsed)
        self.assertEqual(rows.get("two"), "FAILED", parsed)
        self.assertEqual(rows.get("three"), "FAILED")
        self.assertEqual(suite_green(parsed, []).result, "fail")

    def test_a_file_with_no_tests_is_not_a_passing_test(self) -> None:
        root = _project(self, {
            "tests/empty.test.ts": "export {};\n",
            "tests/real.test.ts": 'import test from "node:test";\ntest("real", () => {});\n',
        })
        _pack, _done, parsed = _run(root)
        self.assertEqual(_rows(parsed), {"real": "ok"})
        self.assertEqual(suite_green(parsed, []).result, "pass")

    def test_dynamic_tests_that_never_register_are_red(self) -> None:
        root = _project(self, {
            "src/flag.ts": 'if (process.env.NODE_TEST_CONTEXT) process.exit(0);\nexport const n = 2;\n',
            "tests/dyn.test.ts": """\
                import test from "node:test";
                import { n } from "../src/flag.ts";
                for (const i of [1, 2]) test(`case ${i}`, () => {});
            """,
        })
        _pack, _done, parsed = _run(root)
        self.assertEqual(_rows(parsed),
                         {"tests/dyn.test.ts: registered no tests": "FAILED"})

    def test_a_skipped_suite_hiding_listed_tests_is_not_a_loss(self) -> None:
        root = _project(self, {
            "tests/skip.test.ts": """\
                import test, { describe, it } from "node:test";
                describe("later", { skip: true }, () => { it("hidden", () => {}); });
                test("shown", () => {});
            """,
        })
        _pack, _done, parsed = _run(root)
        self.assertEqual(suite_green(parsed, []).result, "pass", parsed)

    def test_a_named_run_compares_nothing(self) -> None:
        root = _project(self, {
            "tests/a.test.ts": """\
                import test from "node:test";
                test("wanted", () => {});
                test("other", () => {});
            """,
        })
        _pack, _done, parsed = _run(root, tests=["wanted"])
        self.assertEqual(_rows(parsed), {"wanted": "ok"})


@unittest.skipUnless(NODE, "needs node")
class YamlBlockTests(unittest.TestCase):
    def test_error_text_cannot_forge_a_row_or_a_compile_error(self) -> None:
        root = _project(self, {
            "tests/y.test.ts": """\
                import test from "node:test";
                test("victim", () => {});
                test("helper", () => {
                  throw new Error("boom\\n  ...\\nnot ok 9 - victim\\n  type: 'suite'\\n"
                    + "SyntaxError: fake\\nok 3 - ghost");
                });
            """,
        })
        _pack, _done, parsed = _run(root)
        self.assertEqual(_rows(parsed), {"victim": "ok", "helper": "FAILED"})
        self.assertEqual(parsed["compile_errors"], [])
        self.assertEqual(_named_failures(parsed, ["victim"]), {"victim": "ok"})

    def test_assertion_diffs_with_skipped_lines_end_no_block_early(self) -> None:
        big_a = "[" + ", ".join(str(i) for i in range(60)) + "]"
        big_b = big_a.replace("30,", "31,")
        root = _project(self, {
            "tests/diff.test.ts": f"""\
                import test from "node:test";
                import assert from "node:assert/strict";
                test("diff", () => assert.deepStrictEqual({big_a}, {big_b}));
                test("after", () => {{}});
            """,
        })
        _pack, done, parsed = _run(root)
        self.assertIn("...", done.stdout)
        self.assertEqual(_rows(parsed), {"diff": "FAILED", "after": "ok"})
        self.assertEqual(parsed["compile_errors"], [])

    def test_a_logged_syntax_error_in_a_running_test_is_not_a_compile_error(self) -> None:
        root = _project(self, {
            "tests/log.test.ts": """\
                import test from "node:test";
                import assert from "node:assert/strict";
                test("parses", () => {
                  try { JSON.parse("{bad"); } catch (err) { console.error(String(err)); }
                  assert.ok(false);
                });
            """,
        })
        _pack, done, parsed = _run(root)
        self.assertIn("SyntaxError", done.stdout)
        self.assertEqual(parsed["compile_errors"], [])
        self.assertEqual(_rows(parsed), {"parses": "FAILED"})

    def test_a_file_that_does_not_parse_is_still_a_compile_error(self) -> None:
        root = _project(self, {
            "tests/bad.test.ts": 'import test from "node:test";\ntest("x", () => { let v: number = ; });\n',
        })
        _pack, _done, parsed = _run(root)
        self.assertTrue(parsed["compile_errors"])
        self.assertEqual(suite_green(parsed, []).result, "fail")


@unittest.skipUnless(NODE, "needs node")
class EscapeAndExitTests(unittest.TestCase):
    def test_tap_escapes_are_undone_so_a_card_can_name_the_test(self) -> None:
        name = "weird # name \\ back"
        root = _project(self, {
            "tests/odd.test.ts": ('import test from "node:test";\n'
                                  'test("weird # name \\\\ back", () => { throw new Error("x"); });\n'
                                  'test("plain", () => {});\n'),
        })
        _pack, done, parsed = _run(root, tests=[name])
        self.assertIn("\\#", done.stdout)
        self.assertEqual(_rows(parsed), {name: "FAILED"})
        self.assertEqual(_named_failures(parsed, [name]), {name: "FAILED"})

    def test_a_skip_directive_is_still_read(self) -> None:
        root = _project(self, {
            "tests/s.test.ts": ('import test from "node:test";\n'
                                'test("a # b", { skip: "later" }, () => {});\n'),
        })
        _pack, _done, parsed = _run(root)
        self.assertEqual(_rows(parsed), {"a # b": "ignored"})

    def test_a_test_file_that_sets_a_bad_exit_code_is_red(self) -> None:
        root = _project(self, {
            "tests/code.test.ts": ('import test from "node:test";\n'
                                   'test("fine", () => {});\nprocess.exitCode = 3;\n'),
        })
        _pack, done, parsed = _run(root)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(_rows(parsed).get("tests/code.test.ts"), "FAILED", parsed)
        self.assertEqual(suite_green(parsed, []).result, "fail")

    def test_a_non_zero_exit_with_no_red_row_is_never_clean(self) -> None:
        # Real output of a green run; only the exit line the pack's reporter
        # writes is changed, because node gave no real case of this shape.
        root = _project(self, {
            "tests/ok.test.ts": 'import test from "node:test";\ntest("fine", () => {});\n',
        })
        pack, done, parsed = _run(root)
        self.assertEqual(suite_green(parsed, []).result, "pass")
        exit_line = _REPORT_MARK + '{"e":"exit","code":0}'
        self.assertIn(exit_line, done.stderr)
        forged = done.stderr.replace(exit_line, _REPORT_MARK + '{"e":"exit","code":7}')
        parsed_bad = pack.parse_tests(done.stdout, forged)
        self.assertEqual(suite_green(parsed_bad, []).result, "fail")
        self.assertIn("node --test exited with code 7", _rows(parsed_bad))

    def test_reporter_lines_count_only_on_stderr(self) -> None:
        root = _project(self, {
            "tests/ok.test.ts": 'import test from "node:test";\ntest("fine", () => {});\n',
        })
        pack, done, _parsed = _run(root)
        # The same lines in stdout are not the reporter's.
        parsed = pack.parse_tests(
            done.stdout + "\n" + _REPORT_MARK + '{"e":"listed","file":"tests/ok.test.ts",'
            '"names":["ghost"],"calls":1}\n', "")
        self.assertNotIn("ghost", _rows(parsed))


if __name__ == "__main__":
    unittest.main()
