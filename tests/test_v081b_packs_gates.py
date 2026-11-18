"""The gates that read a test run (v0.8.1 fix wave 2, P3, P4 and P5).

P3: a run the runner classed as infra (timed out, killed, a machine error) is never
green, whatever its partial output looks like, in the suite gate and in pass-after.
P4: a named test fails before only when every row reported for it fails, and passes
after only when every row passes; mixed rows prove nothing.
P5: a card's test name matches its parameter cases (`test_param[1-2]`), never a
substring hit of `-k` (`test_adds_more` for `test_adds`), and never a same-named test
in another file when the pack can tell the file.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adapters.direct import DirectRunner
from openfarm_core.flow import _named_failures, _named_rows, _status_of
from openfarm_core.gates.checks import (FAIL, INFRA, PASS, combined_status,
                                        ignored_or_filtered, rows_for_test,
                                        same_test_file, suite_green)
from openfarm_core.packs.python import PythonPack
from openfarm_core.packs.rust import RustPack
from openfarm_core.packs.typescript import TypeScriptPack
from tests.test_python_e2e import (GOOD_IMPLEMENTATION, build_python_toy, run_cli,
                                   script)

RUST_GREEN = """\
     Running unittests src/lib.rs (target/debug/deps/toy-1)

running 1 test
test tests::adds ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

"""

UNITTEST_GREEN = """\
test_one (tests.test_a.A.test_one) ... ok

----------------------------------------------------------------------
Ran 1 test in 0.000s

OK
"""

PYTEST_GREEN = """\
tests/test_a.py::test_one PASSED                                         [100%]

============================== 1 passed in 0.01s ===============================
"""

#: What node's TAP mode makes of `console.log("tests", n)` before a hang: the code
#: under test's own line, shaped like the footer (rev-ts finding 2).
TAP_PARTIAL = "TAP version 13\n# tests 1\n# tests 2\n"


def _pt(name: str, status: str, binary: str = "tests/test_a.py") -> dict:
    return {"name": name, "binary": binary, "status": status}


class InfraIsNeverGreenTests(unittest.TestCase):
    """P3, for every pack: the runner's infra class wins over the parsed output."""

    def _parsed(self) -> dict:
        return {
            "rust": RustPack().parse_tests(RUST_GREEN, ""),
            "unittest": PythonPack(ruff=False).parse_tests("", UNITTEST_GREEN),
            "pytest": PythonPack(runner="pytest", ruff=False).parse_tests(
                PYTEST_GREEN, ""),
            "typescript": TypeScriptPack().parse_tests(TAP_PARTIAL, ""),
        }

    def test_the_outputs_are_green_on_their_own(self) -> None:
        for name, parsed in self._parsed().items():
            if name == "typescript":
                continue  # its parser is unit ts's; the gate must not care
            with self.subTest(name):
                self.assertEqual(suite_green(parsed, []).result, PASS)

    def test_the_suite_gate_reads_infra(self) -> None:
        for name, parsed in self._parsed().items():
            for code in (-1, -9, 101, 0):
                with self.subTest(name, exit_code=code):
                    gate = suite_green(parsed, [], run_cls=INFRA, exit_code=code)
                    self.assertEqual(gate.result, INFRA, gate.detail)

    def test_pass_after_reads_infra(self) -> None:
        names = {"rust": ["tests::adds"], "unittest": ["test_one"],
                 "pytest": ["test_one"], "typescript": ["one"]}
        for name, parsed in self._parsed().items():
            with self.subTest(name):
                gate = ignored_or_filtered(parsed, names[name], run_cls=INFRA,
                                           exit_code=-1)
                self.assertEqual(gate.result, INFRA, gate.detail)
                if name != "typescript":
                    self.assertEqual(
                        ignored_or_filtered(parsed, names[name]).result, PASS)

    def test_pass_after_never_reads_a_run_that_died_partway(self) -> None:
        parsed = {"results": [_pt("test_one", "ok")], "footers": [],
                  "missing_footers": ["pytest"], "compile_errors": []}
        self.assertEqual(ignored_or_filtered(parsed, ["test_one"],
                                             run_cls=FAIL, exit_code=1).result, INFRA)
        crashed = ignored_or_filtered(parsed, ["test_one"], run_cls=FAIL,
                                      exit_code=-11)
        self.assertEqual(crashed.result, FAIL)
        self.assertIn("signal 11", crashed.detail)

    def test_pass_after_on_a_compile_error_is_a_failure(self) -> None:
        parsed = {"results": [], "footers": [], "missing_footers": ["toy"],
                  "compile_errors": [{"pkg": "toy", "target": "", "binary": "toy"}]}
        gate = ignored_or_filtered(parsed, ["x"], run_cls=FAIL, exit_code=101)
        self.assertEqual(gate.result, FAIL)
        self.assertIn("did not compile", gate.detail)

    def test_a_compile_error_stays_the_edit_s_fault(self) -> None:
        parsed = {"results": [], "footers": [], "missing_footers": ["toy"],
                  "compile_errors": [{"pkg": "toy", "target": "", "binary": "toy"}]}
        self.assertEqual(suite_green(parsed, [], run_cls=INFRA).result, FAIL)


class RealKilledRunTests(unittest.TestCase):
    """P3 with a real interpreter: a hung run that printed a footer first."""

    def test_a_timed_out_python_run_with_a_planted_footer_is_infra(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="openfarm-hang-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "tests").mkdir()
        (root / "tests" / "__init__.py").write_text("")
        (root / "tests" / "test_hang.py").write_text(textwrap.dedent('''\
            import sys
            import time
            import unittest


            class Hang(unittest.TestCase):
                def test_hangs(self):
                    sys.stderr.write("ok\\n\\n" + "-" * 70 + "\\n"
                                     "Ran 1 test in 0.000s\\n\\nOK\\n")
                    sys.stderr.flush()
                    time.sleep(60)
            '''))
        pack = PythonPack(ruff=False)
        pack.configure(root)
        runner = DirectRunner(root)
        result = runner.exec("test", pack.commands("test")["argv"], cwd=root,
                             timeout_s=3)
        self.assertEqual(result.cls, INFRA)
        parsed = pack.parse_tests(result.stdout, result.stderr)
        self.assertFalse(parsed["missing_footers"], parsed)  # the plant worked
        gate = suite_green(parsed, [], run_cls=result.cls,
                           exit_code=result.exit_code)
        self.assertEqual(gate.result, INFRA, gate.detail)


class RowMatchingTests(unittest.TestCase):
    """P5: which rows belong to a card's named test."""

    ROWS = [
        _pt("test_param[1-1-2]", "ok"),
        _pt("test_param[2-2-5]", "FAILED"),
        _pt("test_adds", "ok"),
        _pt("test_adds_more", "FAILED"),
        _pt("test_param", "ok", binary="tests/test_b.py"),
        _pt("test_paramx[1]", "FAILED"),
    ]

    def test_parameter_cases_belong_to_their_function(self) -> None:
        rows = rows_for_test(self.ROWS, "test_param", "tests/test_a.py",
                             PythonPack(ruff=False).row_file)
        self.assertEqual([r["name"] for r in rows],
                         ["test_param[1-1-2]", "test_param[2-2-5]"])
        self.assertEqual(combined_status(rows), "mixed")

    def test_an_exact_parameter_id_matches_that_case_only(self) -> None:
        rows = rows_for_test(self.ROWS, "test_param[2-2-5]")
        self.assertEqual([r["name"] for r in rows], ["test_param[2-2-5]"])
        self.assertEqual(combined_status(rows), "FAILED")

    def test_a_substring_is_another_test(self) -> None:
        rows = rows_for_test(self.ROWS, "test_adds")
        self.assertEqual([r["name"] for r in rows], ["test_adds"])
        self.assertEqual(combined_status(rows), "ok")

    def test_another_file_is_another_test(self) -> None:
        pack = PythonPack(ruff=False)
        in_a = rows_for_test(self.ROWS, "test_param", "tests/test_a.py",
                             pack.row_file)
        self.assertNotIn("tests/test_b.py", [r["binary"] for r in in_a])
        in_b = rows_for_test(self.ROWS, "test_param", "tests/test_b.py",
                             pack.row_file)
        self.assertEqual([r["binary"] for r in in_b], ["tests/test_b.py"])
        # No file, or a pack that cannot tell, keeps every same-named row.
        self.assertEqual(len(rows_for_test(self.ROWS, "test_param")), 3)
        self.assertEqual(len(rows_for_test(self.ROWS, "test_param",
                                           "tests/test_a.py", RustPack().row_file)),
                         3)

    def test_python_row_files(self) -> None:
        pack = PythonPack(ruff=False)
        self.assertEqual(pack.row_file({"binary": "tests/test_a.py::TestA"}),
                         "tests/test_a.py")
        self.assertEqual(pack.row_file({"binary": "tests/test_a.py"}),
                         "tests/test_a.py")
        self.assertEqual(pack.row_file({"binary": "tests.test_a.TestA"}),
                         "tests/test_a.py")
        self.assertIsNone(pack.row_file({"binary": ""}))
        self.assertIsNone(pack.row_file({"binary": "unittest"}))

    def test_file_spellings(self) -> None:
        self.assertTrue(same_test_file("tests/test_a.py", "./tests/test_a.py"))
        self.assertTrue(same_test_file("tests/test_a.py", "pkg/tests/test_a.py"))
        self.assertFalse(same_test_file("tests/test_a.py", "tests/test_b.py"))
        self.assertFalse(same_test_file("a.py", "xa.py"))

    def test_pass_after_needs_every_parameter_case(self) -> None:
        parsed = {"results": self.ROWS, "footers": [{"binary": "pytest"}],
                  "missing_footers": [], "compile_errors": []}
        gate = ignored_or_filtered(parsed, ["test_param"],
                                   files={"test_param": "tests/test_a.py"},
                                   row_file=PythonPack(ruff=False).row_file)
        self.assertEqual(gate.result, FAIL)
        self.assertIn("test_param", gate.detail)
        self.assertEqual(len(gate.evidence["rows"]["test_param"]), 2)
        green = {"results": [_pt("test_param[1-1-2]", "ok"),
                             _pt("test_param[2-2-5]", "ok"),
                             _pt("test_param_more", "FAILED")]}
        self.assertEqual(ignored_or_filtered(green, ["test_param"]).result, PASS)
        self.assertEqual(ignored_or_filtered(green, ["test_param_more"]).result,
                         FAIL)


class EveryRowTests(unittest.TestCase):
    """P4: the status of a named test over every row reported for it."""

    def test_statuses(self) -> None:
        cases = {
            "FAILED": [_pt("t", "FAILED"), _pt("t", "FAILED", "tests/test_b.py")],
            "ok": [_pt("t", "ok"), _pt("t", "ok", "tests/test_b.py")],
            "mixed": [_pt("t", "FAILED"), _pt("t", "ok", "tests/test_b.py")],
            "not-run": [_pt("u", "FAILED")],
        }
        for wanted, rows in cases.items():
            with self.subTest(wanted):
                self.assertEqual(_named_failures({"results": rows}, ["t"]),
                                 {"t": wanted})
        # The order of the rows never decides: the last row used to.
        self.assertEqual(_named_failures(
            {"results": [_pt("t", "ok"), _pt("t", "FAILED", "x")]}, ["t"]),
            {"t": "mixed"})
        self.assertEqual(_named_failures(
            {"results": [_pt("t", "FAILED"), _pt("t", "ok", "x")]}, ["t"]),
            {"t": "mixed"})

    def test_status_of_agrees(self) -> None:
        self.assertEqual(_status_of({"results": [_pt("t", "ok"),
                                                  _pt("t", "FAILED", "x")]}, "t"),
                         "mixed")
        self.assertEqual(_status_of({"results": [_pt("t[1]", "ok"),
                                                  _pt("t[2]", "ok")]}, "t"), "ok")

    def test_the_file_keeps_another_file_out(self) -> None:
        rows = [_pt("t", "FAILED", "tests/test_a.py"),
                _pt("t", "ok", "tests/test_b.py")]
        statuses = _named_failures({"results": rows}, ["t"],
                                   {"t": "tests/test_a.py"},
                                   PythonPack(ruff=False).row_file)
        self.assertEqual(statuses, {"t": "FAILED"})
        self.assertEqual(len(_named_rows({"results": rows}, ["t"])["t"]), 2)

    def test_a_forged_tap_row_never_proves_fail_before(self) -> None:
        # rev-ts finding 4: text inside one test's error message adds a
        # `not ok 9 - victim` row after victim's real `ok` row.
        parsed = TypeScriptPack().parse_tests(FORGED_TAP, "")
        self.assertNotEqual(_named_failures(parsed, ["victim"])["victim"], "FAILED")


#: Real node v26 output from rev-ts's repro (`out/yaml.tap`): `helper` fails with
#: an error message whose text ends the YAML block early and reads as a second,
#: failing `victim` row. `victim` really passed.
FORGED_TAP = """\
TAP version 13
# Subtest: victim
ok 1 - victim
  ---
  duration_ms: 0.854666
  type: 'test'
  ...
# Subtest: helper
not ok 2 - helper
  ---
  duration_ms: 0.247409
  type: 'test'
  location: '/var/tmp/openfarm-v081/tmp-rev-ts/p1/y/tests/y.test.ts:3:1'
  failureType: 'testCodeFailure'
  error: |-
    boom
      ...
    not ok 9 - victim
      type: 'suite'
  code: 'ERR_TEST_FAILURE'
  stack: |-
    TestContext.<anonymous> (file:///var/tmp/openfarm-v081/tmp-rev-ts/p1/y/tests/y.test.ts:3:30)
    Test.runInAsyncScope (node:async_hooks:226:14)
    Test.run (node:internal/test_runner/test:1402:25)
    Test.processPendingSubtests (node:internal/test_runner/test:974:18)
    Test.postRun (node:internal/test_runner/test:1542:19)
    Test.run (node:internal/test_runner/test:1467:12)
    async startSubtestAfterBootstrap (node:internal/test_runner/harness:387:3)
  ...
1..2
# tests 2
# suites 0
# pass 1
# fail 1
# cancelled 0
# skipped 0
# todo 0
# duration_ms 110.334644
"""


#: The card's test file: the named test fails on base in one class and passes in
#: another, so its rows disagree.
MIXED_TEST = '''\
import unittest

from tally.bounded import Bounded, FullError


class FullTests(unittest.TestCase):
    def test_a_full_bundle_refuses_a_push(self):
        bundle = Bounded(1)
        bundle.push(7)
        with self.assertRaises(FullError):
            bundle.push(8)


class MoreFullTests(unittest.TestCase):
    def test_a_full_bundle_refuses_a_push(self):
        self.assertEqual(Bounded(1).capacity, 1)
'''

GOOD_TEST = '''\
import unittest

from tally.bounded import Bounded, FullError


class FullTests(unittest.TestCase):
    def test_a_full_bundle_refuses_a_push(self):
        bundle = Bounded(1)
        bundle.push(7)
        with self.assertRaises(FullError):
            bundle.push(8)
'''

#: An existing test in another file with the card's test name, passing on base.
OTHER_FILE_TEST = '''\
import unittest

from tally.bounded import Bounded


class OtherTests(unittest.TestCase):
    def test_a_full_bundle_refuses_a_push(self):
        self.assertEqual(Bounded(2).capacity, 2)
'''


class NamedRowsCardTests(unittest.TestCase):
    """P4 and P5 through a scripted card on the Python toy."""

    def _toy(self, extra: dict[str, str] | None = None):
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        for path, content in (extra or {}).items():
            toy.write(path, content)
        toy.write_card(
            "CARD-1",
            title="A full bundle refuses a push",
            category="pinned-bugfix",
            unit=".",
            touches=["tally/bounded.py"],
            tests=[{"name": "test_a_full_bundle_refuses_a_push",
                    "file": "tests/test_full.py",
                    "asserts": "push raises FullError"}],
            done_text="`Bounded(1).push(8)` after one push raises `FullError`.")
        toy.commit("tally: CARD-1")
        return toy

    def _run(self, toy, test: str) -> tuple[dict, list[dict]]:
        script_path = script(**{
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + test,
            "CARD-1/implementer": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: a full bundle refuses a push",
            "CARD-1/checker_pass2": "{}",
        })
        result = run_cli(toy.root, "run", "--card", "CARD-1",
                         "--script", str(script_path), "--cards", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        outcome = json.loads(result.stdout)["outcomes"][0]
        run_dir = next((toy.state_dir / "cards" / "CARD-1").iterdir())
        gates = [json.loads(p.read_text())
                 for p in sorted(run_dir.glob("gates/*/fail-before*.json"))]
        return outcome, gates

    def test_mixed_rows_on_base_block_as_unprovable_with_the_rows(self) -> None:
        outcome, gates = self._run(self._toy(), MIXED_TEST)
        self.assertEqual(outcome["final_state"], "blocked")
        self.assertEqual(outcome["stop_code"], "tests-unprovable")
        self.assertTrue(gates)
        gate = gates[-1]
        self.assertEqual(gate["result"], "fail")
        self.assertIn("both failing and not failing", gate["detail"])
        rows = gate["evidence"]["rows"]["test_a_full_bundle_refuses_a_push"]
        self.assertEqual(sorted(r["status"] for r in rows), ["FAILED", "ok"])
        self.assertEqual(sorted(r["binary"] for r in rows),
                         ["tests.test_full.FullTests", "tests.test_full.MoreFullTests"])

    def test_a_same_named_test_in_another_file_is_not_the_card_s(self) -> None:
        toy = self._toy({"tests/test_other.py": OTHER_FILE_TEST})
        outcome, gates = self._run(toy, GOOD_TEST)
        self.assertEqual(outcome["final_state"], "in-review",
                         f"{outcome.get('stop_code')}: {outcome.get('detail')}")
        rows = gates[-1]["evidence"]["rows"]["test_a_full_bundle_refuses_a_push"]
        self.assertEqual([r["binary"] for r in rows], ["tests.test_full.FullTests"])


if __name__ == "__main__":
    unittest.main()
