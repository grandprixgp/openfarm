"""The Python pack: real unittest output, pytest output shapes, files and patterns.

unittest cases run a real interpreter on a generated project, so the parser is
checked against what Python actually prints. pytest is not a dependency, so its
cases use output copied from pytest's documented `-v` and `--collect-only -q`
shapes; they are marked as such.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.errors import Invalid, Refused
from openfarm_core.gates.checks import banned_patterns, suite_green, test_loss
from openfarm_core.gates.eligibility import _units_of, check_eligibility
from openfarm_core.packs.base import pack_for
from openfarm_core.packs.python import PythonPack, detect_runner


def _project(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="openfarm-py-"))
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(content))
    return root


def _run(argv: list[str], cwd: Path) -> tuple[str, str]:
    result = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True)
    return result.stdout, result.stderr


MIXED_TESTS = '''\
import unittest


class TestMixed(unittest.TestCase):
    def test_passes(self):
        self.assertEqual(1, 1)

    def test_fails(self):
        self.assertEqual(1, 2)

    def test_raises(self):
        raise ValueError("boom")

    def test_documented(self):
        """A docstring moves the status to its own line."""
        self.assertTrue(True)

    def test_prints(self):
        import sys
        sys.stderr.write("noise ")
        self.assertTrue(True)

    @unittest.skip("not today")
    def test_skipped(self):
        pass
'''


class UnittestParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _project({"tests/__init__.py": "",
                              "tests/test_mixed.py": MIXED_TESTS})
        self.pack = PythonPack(ruff=False)
        self.pack.configure(self.root)

    def test_every_status_is_read_from_real_output(self) -> None:
        out, err = _run(self.pack.commands("test")["argv"], self.root)
        parsed = self.pack.parse_tests(out, err)
        statuses = {r["name"]: r["status"] for r in parsed["results"]}
        self.assertEqual(statuses, {
            "test_passes": "ok", "test_fails": "FAILED", "test_raises": "FAILED",
            "test_documented": "ok", "test_prints": "ok",
            "test_skipped": "ignored"})
        self.assertEqual(parsed["missing_footers"], [])
        self.assertEqual(parsed["compile_errors"], [])
        self.assertEqual(parsed["footers"][0]["failed"], 2)
        self.assertEqual(
            {r["binary"] for r in parsed["results"]}, {"tests.test_mixed.TestMixed"})

    def test_red_tests_fail_the_suite_unless_known(self) -> None:
        out, err = _run(self.pack.commands("test")["argv"], self.root)
        parsed = self.pack.parse_tests(out, err)
        self.assertEqual(suite_green(parsed, []).result, "fail")
        self.assertEqual(
            suite_green(parsed, ["test_fails", "test_raises"]).result, "pass")

    def test_named_tests_narrow_the_run(self) -> None:
        argv = self.pack.commands("test", tests=["test_passes"])["argv"]
        out, err = _run(argv, self.root)
        parsed = self.pack.parse_tests(out, err)
        self.assertEqual([r["name"] for r in parsed["results"]], ["test_passes"])

    def test_the_listing_matches_the_run(self) -> None:
        out, _ = _run(self.pack.commands("list")["argv"], self.root)
        listing = self.pack.parse_list(out)
        names = sorted(t["name"] for t in listing["tests"])
        self.assertEqual(names, ["test_documented", "test_fails", "test_passes",
                                 "test_prints", "test_raises", "test_skipped"])
        self.assertTrue(all(t["binary"] == "tests.test_mixed.TestMixed"
                            for t in listing["tests"]))

    def test_a_test_moved_to_another_class_is_a_loss(self) -> None:
        before = {"tests": [{"name": "test_a", "binary": "tests.test_x.A"}]}
        after = {"tests": [{"name": "test_a", "binary": "tests.test_x.B"}]}
        self.assertEqual(test_loss(before, after).result, "fail")


class ImportFailureTests(unittest.TestCase):
    """A module that cannot import is Python's compile error, never a pass."""

    def test_a_syntax_error_is_a_compile_error(self) -> None:
        root = _project({"tests/__init__.py": "",
                         "tests/test_ok.py": "import unittest\n\n"
                         "class T(unittest.TestCase):\n"
                         "    def test_a(self):\n        pass\n",
                         "tests/test_broken.py": "def f(:\n"})
        pack = PythonPack(ruff=False)
        pack.configure(root)
        out, err = _run(pack.commands("test")["argv"], root)
        parsed = pack.parse_tests(out, err)
        self.assertEqual([e["binary"] for e in parsed["compile_errors"]],
                         ["tests.test_broken"])
        gate = suite_green(parsed, [])
        self.assertEqual(gate.result, "fail")
        self.assertIn("did not compile", gate.detail)

    def test_a_missing_import_is_a_compile_error(self) -> None:
        root = _project({"tests/__init__.py": "",
                         "tests/test_imp.py": "from pkg.nothing import thing\n"})
        pack = PythonPack(ruff=False)
        pack.configure(root)
        out, err = _run(pack.commands("test")["argv"], root)
        parsed = pack.parse_tests(out, err)
        self.assertEqual(parsed["compile_errors"][0]["binary"], "tests.test_imp")

    def test_no_footer_means_the_run_died(self) -> None:
        pack = PythonPack(ruff=False)
        parsed = pack.parse_tests(
            "", "test_a (tests.test_x.T.test_a) ... ok\nKilled\n")
        self.assertEqual(parsed["missing_footers"], ["unittest"])
        self.assertEqual(suite_green(parsed, []).result, "infra")

    def test_the_listing_marks_an_import_failure(self) -> None:
        root = _project({"tests/__init__.py": "",
                         "tests/test_broken.py": "def f(:\n"})
        pack = PythonPack(ruff=False)
        out, _ = _run(pack.commands("list")["argv"], root)
        self.assertIn("ERROR tests.test_broken", out)
        self.assertEqual(pack.parse_list(out), {"tests": []})


class PytestShapeTests(unittest.TestCase):
    """Output shapes copied from pytest's `-v` and `--collect-only -q`."""

    VERBOSE = textwrap.dedent("""\
        ============================= test session starts ==============================
        collected 4 items

        tests/test_x.py::test_one PASSED                                         [ 25%]
        tests/test_x.py::TestK::test_two FAILED                                  [ 50%]
        tests/test_x.py::test_param[2] SKIPPED (no)                              [ 75%]
        tests/test_x.py::test_xf XFAIL                                           [100%]

        =================================== FAILURES ===================================
        ========================= short test summary info ==========================
        FAILED tests/test_x.py::TestK::test_two - assert 1 == 2
        =============== 1 failed, 1 passed, 1 skipped, 1 xfailed in 0.05s ===============
        """)

    def setUp(self) -> None:
        self.pack = PythonPack(runner="pytest", ruff=False)

    def test_results_and_footer(self) -> None:
        parsed = self.pack.parse_tests(self.VERBOSE)
        statuses = {(r["binary"], r["name"]): r["status"] for r in parsed["results"]}
        self.assertEqual(statuses, {
            ("tests/test_x.py", "test_one"): "ok",
            ("tests/test_x.py::TestK", "test_two"): "FAILED",
            ("tests/test_x.py", "test_param[2]"): "ignored",
            ("tests/test_x.py", "test_xf"): "ignored"})
        self.assertEqual(parsed["footers"][0]["failed"], 1)
        self.assertEqual(parsed["missing_footers"], [])

    def test_a_collection_error_is_a_compile_error(self) -> None:
        text = textwrap.dedent("""\
            ==================================== ERRORS ====================================
            ______________________ ERROR collecting tests/test_y.py _______________________
            E   ModuleNotFoundError: No module named 'nope'
            =========================== short test summary info ============================
            ERROR tests/test_y.py
            !!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
            =============================== 1 error in 0.04s ===============================
            """)
        parsed = self.pack.parse_tests(text)
        self.assertEqual(parsed["compile_errors"][0]["binary"], "tests/test_y.py")
        self.assertEqual(suite_green(parsed, []).result, "fail")

    def test_collect_only_listing(self) -> None:
        listing = self.pack.parse_list(
            "tests/test_x.py::test_one\ntests/test_x.py::TestK::test_two\n\n"
            "2 tests collected in 0.01s\n")
        self.assertEqual(listing["tests"], [
            {"name": "test_one", "binary": "tests/test_x.py"},
            {"name": "test_two", "binary": "tests/test_x.py::TestK"}])

    def test_named_tests_and_files_in_the_command(self) -> None:
        argv = self.pack.commands("test", tests=["a", "b"])["argv"]
        self.assertEqual(argv[-2:], ["-k", "a or b"])
        self.assertIn("no:cacheprovider", argv)
        argv = self.pack.commands("test", files=["tests/test_c.py"])["argv"]
        self.assertIn("tests/test_c.py", argv)


class RunnerDetectionTests(unittest.TestCase):
    def test_unittest_by_default(self) -> None:
        self.assertEqual(detect_runner(_project({"pyproject.toml": "[project]\n"})),
                         "unittest")

    def test_pytest_when_configured(self) -> None:
        for files in ({"pyproject.toml": "[tool.pytest.ini_options]\naddopts = ''\n"},
                      {"pytest.ini": "[pytest]\n"},
                      {"setup.cfg": "[tool:pytest]\n"}):
            self.assertEqual(detect_runner(_project(files)), "pytest", files)

    def test_configure_fixes_the_runner_and_pack_for_knows_python(self) -> None:
        root = _project({"pytest.ini": "[pytest]\n"})
        pack = pack_for("python", python="/usr/bin/python3")
        pack.configure(root)
        self.assertEqual(pack.runner, "pytest")
        self.assertEqual(pack.commands("test")["argv"][0], "/usr/bin/python3")


class CommandTests(unittest.TestCase):
    def test_counter_test_files_run_as_modules(self) -> None:
        pack = PythonPack(ruff=False)
        argv = pack.commands("test", files=["tests/test_openfarm_counter_1.py"])["argv"]
        self.assertEqual(argv[-1], "tests.test_openfarm_counter_1")

    def test_a_package_name_as_unit_means_the_root(self) -> None:
        root = _project({"pyproject.toml": "[project]\n", "tally/__init__.py": ""})
        pack = PythonPack(ruff=False)
        pack.configure(root)
        argv = pack.commands("test", unit="tally")["argv"]
        self.assertEqual(argv[argv.index("-s") + 1], "tests")
        self.assertEqual(argv[argv.index("-t") + 1], ".")

    def test_without_ruff_formatting_is_a_named_no_op(self) -> None:
        pack = PythonPack(ruff=False)
        self.assertEqual(pack.commands("fmt-check", files=["a.py"])["argv"][1:],
                         ["-c", "pass"])
        self.assertTrue(any("ruff is not installed" in n for n in pack.notes))

    def test_with_ruff_lint_and_format_use_it(self) -> None:
        pack = PythonPack(ruff=True)
        self.assertEqual(pack.commands("lint")["argv"][-1], "ruff")
        self.assertEqual(pack.commands("fmt-check", files=["a.py"])["argv"],
                         ["ruff", "format", "--check", "a.py"])
        self.assertEqual(pack.notes, [])

    def test_the_syntax_check_fails_on_a_broken_file(self) -> None:
        pack = PythonPack(ruff=False)
        good = _project({"a.py": "x = 1\n"})
        bad = _project({"a.py": "x = (\n"})
        self.assertEqual(subprocess.run(pack.commands("lint")["argv"],
                                        cwd=good).returncode, 0)
        result = subprocess.run(pack.commands("lint")["argv"], cwd=bad,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("SyntaxError", result.stderr)
        self.assertFalse((bad / "__pycache__").exists())

    def test_mutation_is_refused(self) -> None:
        with self.assertRaises(Invalid):
            PythonPack(ruff=False).commands("mutation", files=["a.py"])


class FileTests(unittest.TestCase):
    def test_classify(self) -> None:
        pack = PythonPack(ruff=False)
        self.assertEqual(pack.classify_paths([
            "openfarm_core/flow.py", "tests/test_flow.py", "pkg/tests/helpers.py",
            "conftest.py", "pkg/thing_test.py", "README.md", "pyproject.toml"]), {
            "openfarm_core/flow.py": "production", "tests/test_flow.py": "test",
            "pkg/tests/helpers.py": "test", "conftest.py": "test",
            "pkg/thing_test.py": "test", "README.md": "other",
            "pyproject.toml": "other"})

    def test_signatures_never_show_a_body(self) -> None:
        root = _project({"m.py": '''\
            """Module."""
            LIMIT = 10
            _private = 3


            def add(a: int, b: int = 2) -> int:
                """Add two numbers.

                More text.
                """
                secret_sum = a + b
                return secret_sum


            class Box(Base, metaclass=Meta):
                """A box."""
                size: int

                @property
                def area(self) -> int:
                    return self.size * self.size

                async def fetch(self, *, key: str) -> None:
                    hidden = key
            '''})
        sigs = "\n".join(PythonPack(ruff=False).signatures(root / "m.py")["signatures"])
        for expected in ("LIMIT = 10", "def add(a: int, b: int=2) -> int:",
                         '"""Add two numbers."""', "class Box(Base, metaclass=Meta):",
                         "size: int", "@property", "def area(self) -> int:",
                         "async def fetch(self, *, key: str) -> None:"):
            self.assertIn(expected, sigs)
        for hidden in ("secret_sum", "hidden", "self.size *", "_private", "More text"):
            self.assertNotIn(hidden, sigs)

    def test_append_stub(self) -> None:
        root = _project({"m.py": "def a():\n    return 1\n"})
        pack = PythonPack(ruff=False)
        with self.assertRaises(Refused):
            pack.append_stub(root / "m.py", "t", "def b():\n    return 2\n")
        with self.assertRaises(Refused):
            pack.append_stub(root / "m.py", "t", "def b(:\n")
        pack.append_stub(root / "m.py", "t",
                         "def b() -> int:\n    raise NotImplementedError\n")
        text = (root / "m.py").read_text()
        self.assertTrue(text.endswith(
            "    return 1\n\n\ndef b() -> int:\n    raise NotImplementedError\n"))
        compile(text, "m.py", "exec")


class PatternTests(unittest.TestCase):
    def _labels(self, path: str, lines: list[str]) -> list[str]:
        pack = PythonPack(ruff=False)
        added = [{"path": path, "line": i, "text": t} for i, t in enumerate(lines, 1)]
        return [b["pattern"] for b in pack.patterns(added)["banned"]]

    def test_banned_anywhere(self) -> None:
        self.assertEqual(self._labels("tests/test_a.py", [
            "try:", "    x()", "except:", "    y()",
            "z = 1  # noqa: E501", "w = f()  # type: ignore[attr]",
            "breakpoint()", "import pdb", "@unittest.skip('later')",
            "import subprocess", "os.system('ls')", "eval(text)",
        ]), ["bare-except", "suppression-noqa", "suppression-type-ignore",
             "debug-call", "debug-call", "test-skipped", "process-call",
             "process-call", "dynamic-exec"])

    def test_swallowed_exceptions(self) -> None:
        self.assertEqual(self._labels("pkg/a.py", [
            "try:", "    x()", "except ValueError: pass"]), ["swallowed-exception"])
        self.assertEqual(self._labels("pkg/a.py", [
            "try:", "    x()", "except (KeyError, ValueError):", "    pass"]),
            ["swallowed-exception"])
        self.assertEqual(self._labels("pkg/a.py", [
            "try:", "    x()", "except KeyError:", "    log()", "    pass"]), [])

    def test_stubs_are_banned_only_in_production(self) -> None:
        self.assertEqual(self._labels("pkg/a.py", ["    raise NotImplementedError"]),
                         ["leftover-stub"])
        self.assertEqual(self._labels("tests/test_a.py",
                                      ["    raise NotImplementedError"]), [])

    def test_text_inside_strings_and_comments_is_not_code(self) -> None:
        self.assertEqual(self._labels("pkg/a.py", [
            "msg = 'use # noqa sparingly'", "# breakpoint() is banned",
            "literal_eval(x)"]), [])

    def test_the_gate_compares_counts_per_file(self) -> None:
        class Cfg:
            flagged_patterns: list[str] = []
        pack = PythonPack(ruff=False)
        added = [{"path": "pkg/a.py", "line": 3, "text": "except:"}]
        same = banned_patterns(added, Cfg(), {"pkg/a.py"},
                               base_counts={"pkg/a.py": {"bare-except": 1}},
                               head_counts={"pkg/a.py": {"bare-except": 1}}, pack=pack)
        self.assertEqual(same.result, "pass")
        more = banned_patterns(added, Cfg(), {"pkg/a.py"},
                               base_counts={"pkg/a.py": {"bare-except": 1}},
                               head_counts={"pkg/a.py": {"bare-except": 2}}, pack=pack)
        self.assertEqual(more.result, "fail")


class ErrorSignatureTests(unittest.TestCase):
    def test_paths_and_line_numbers_do_not_change_the_signature(self) -> None:
        pack = PythonPack(ruff=False)
        a = ('FAIL: test_x (tests.test_a.T.test_x)\nTraceback (most recent call last):\n'
             '  File "/tmp/wt-1/tests/test_a.py", line 9, in test_x\n'
             'AssertionError: 1 != 2\nRan 1 test in 0.003s\n')
        b = a.replace("/tmp/wt-1", "/home/x/wt-2").replace("line 9", "line 12") \
            .replace("0.003s", "0.020s")
        self.assertEqual(pack.error_signature(a)["signature"],
                         pack.error_signature(b)["signature"])
        self.assertEqual(pack.error_signature(a)["codes"], ["AssertionError"])
        other = a.replace("AssertionError", "KeyError")
        self.assertNotEqual(pack.error_signature(a)["signature"],
                            pack.error_signature(other)["signature"])


class EligibilityTests(unittest.TestCase):
    def test_a_pyproject_folder_is_one_unit(self) -> None:
        root = _project({"pyproject.toml": "[project]\n", "pkg/a.py": "",
                         "tests/test_a.py": ""})
        self.assertEqual(_units_of(["pkg/a.py", "tests/test_a.py"], root), {"."})

    def test_python_card_tests_go_in_test_files(self) -> None:
        from openfarm_core.cards import Card

        class Cfg:
            languages = ["python"]
            known_failing: list[str] = []
            python_tests_in_test_files = True
            rust_tests_in_separate_files = True
            max_files = 6
            max_units = 1
            never_touch: list[str] = []
            forbidden_files: list[str] = []
            refuse_touches_prefixes: list[str] = []
            require_reference: list[str] = []
            categories = ["small-feature"]
            allow_type_changes = False
            max_context_files = 4
            micromanager_adapter = "off"

        def card(test_file: str) -> Card:
            return Card("C-1", "t", done_text="`f(1)` returns `2`",
                        touches=["pkg/a.py", test_file],
                        fields={"category": "small-feature", "tests": [
                            {"name": "test_f", "file": test_file}]})

        bad = check_eligibility(card("pkg/helpers.py"), Cfg())
        self.assertIn("python-tests-in-test-files", [f["rule"] for f in bad.failures])
        good = check_eligibility(card("tests/test_a.py"), Cfg())
        self.assertNotIn("python-tests-in-test-files",
                         [f["rule"] for f in good.failures])


if __name__ == "__main__":
    unittest.main()
