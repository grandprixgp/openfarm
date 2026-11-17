"""The gates that read a test run (v0.8.1 fix wave 2, P3).

P3: a run the runner classed as infra (timed out, killed, a machine error) is never
green, whatever its partial output looks like, in the suite gate and in pass-after.
P4: a named test fails before only when every row reported for it fails, and passes
after only when every row passes; mixed rows prove nothing.
P5: a card's test name matches its parameter cases (`test_param[1-2]`), never a
substring hit of `-k` (`test_adds_more` for `test_adds`), and never a same-named test
in another file when the pack can tell the file.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adapters.direct import DirectRunner
from openfarm_core.gates.checks import FAIL, INFRA, PASS, ignored_or_filtered, suite_green
from openfarm_core.packs.python import PythonPack
from openfarm_core.packs.rust import RustPack
from openfarm_core.packs.typescript import TypeScriptPack

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



if __name__ == "__main__":
    unittest.main()
