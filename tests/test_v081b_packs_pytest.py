"""The Python pack against the real pytest (v0.8.1 fix wave 2, P8, and P5 end to end).

Every case runs pytest through the pack's own commands and parsers, on a generated
project, under the Python that runs the suite. They skip themselves when that Python
cannot import pytest; to run them, use one that can, for example
`/var/tmp/openfarm-v081/tools/venv/bin/python -m unittest tests.test_v081b_packs_pytest`.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.gates.checks import (FAIL, INFRA, PASS, ignored_or_filtered,
                                        rows_for_test, suite_green)
from openfarm_core.packs.python import PythonPack, detect_runner
from tests.test_python_e2e import (GOOD_IMPLEMENTATION, build_python_toy, run_cli,
                                   script)

HAS_PYTEST = importlib.util.find_spec("pytest") is not None

TALLY = "def add(a, b):\n    return a + b\n"

EVERY_STATUS = '''\
import pytest

from tally.m import add


def test_adds():
    assert add(1, 2) == 3


def test_fails():
    assert add(1, 2) == 4


@pytest.mark.parametrize("a,b,c", [(1, 1, 2), (2, 2, 5)])
def test_param(a, b, c):
    assert add(a, b) == c


@pytest.mark.skip(reason="later")
def test_skipped():
    assert False


@pytest.mark.xfail
def test_xfail():
    assert False


@pytest.mark.xfail
def test_xpass():
    assert True


class TestGroup:
    def test_in_class(self):
        assert add(0, 0) == 0

    def test_raises(self):
        raise RuntimeError("boom")


@pytest.fixture
def broken():
    raise ValueError("fixture broke")


def test_fixture_error(broken):
    assert True


def test_adds_more():
    assert add(2, 2) == 4
'''


def _project(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="openfarm-pytest-"))
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(content))
    return root


@unittest.skipUnless(HAS_PYTEST, "the Python running the suite cannot import pytest")
class RealPytestTests(unittest.TestCase):
    def _repo(self, tests: dict[str, str],
              config: tuple[str, str] = ("pytest.ini", "[pytest]\ntestpaths = tests\n")
              ) -> tuple[Path, PythonPack]:
        files = {config[0]: config[1], "tally/__init__.py": "", "tally/m.py": TALLY}
        files.update(tests)
        root = _project(files)
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        pack = PythonPack(ruff=False)
        pack.configure(root)
        self.assertEqual(pack.runner, "pytest")
        return root, pack

    def _run(self, root: Path, pack: PythonPack, purpose: str, **kw):
        argv = pack.commands(purpose, **kw)["argv"]
        result = subprocess.run(argv, cwd=root, capture_output=True, text=True,
                                timeout=300)
        return result

    def _test(self, root, pack, **kw) -> tuple[subprocess.CompletedProcess, dict]:
        result = self._run(root, pack, "test", **kw)
        return result, pack.parse_tests(result.stdout, result.stderr)

    def test_both_config_forms_select_pytest(self) -> None:
        for name, text in (("pytest.ini", "[pytest]\n"),
                           ("pyproject.toml", "[tool.pytest.ini_options]\n"
                                              "testpaths = ['tests']\n")):
            with self.subTest(name):
                root, pack = self._repo({"tests/test_ok.py": "def test_one():\n"
                                                             "    assert True\n"},
                                        (name, text))
                self.assertEqual(detect_runner(root), "pytest")
                result, parsed = self._test(root, pack)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual([(r["name"], r["status"]) for r in parsed["results"]],
                                 [("test_one", "ok")])

    def test_the_listing_names_every_case_and_every_collection_error(self) -> None:
        root, pack = self._repo({
            "tests/test_ok.py": EVERY_STATUS,
            "tests/test_import_error.py": "from tally.m import nope\n\n"
                                          "def test_never():\n    assert True\n",
            "tests/test_syntax.py": "def test_bad(:\n    pass\n",
        })
        result = self._run(root, pack, "list")
        listing = pack.parse_list(result.stdout)
        names = [(t["binary"], t["name"]) for t in listing["tests"]]
        self.assertIn(("tests/test_ok.py", "test_param[1-1-2]"), names)
        self.assertIn(("tests/test_ok.py", "test_param[2-2-5]"), names)
        self.assertIn(("tests/test_ok.py::TestGroup", "test_in_class"), names)
        self.assertEqual(len(names), 11)
        self.assertEqual(sorted(listing["errors"]),
                         ["tests/test_import_error.py", "tests/test_syntax.py"])
        self.assertEqual(len(rows_for_test(listing["tests"], "test_param",
                                           "tests/test_ok.py", pack.row_file)), 2)

    def test_every_status_and_the_footer(self) -> None:
        root, pack = self._repo({"tests/test_ok.py": EVERY_STATUS})
        result, parsed = self._test(root, pack)
        self.assertEqual(result.returncode, 1)
        statuses = {(r["binary"], r["name"]): r["status"] for r in parsed["results"]}
        self.assertEqual(statuses, {
            ("tests/test_ok.py", "test_adds"): "ok",
            ("tests/test_ok.py", "test_fails"): "FAILED",
            ("tests/test_ok.py", "test_param[1-1-2]"): "ok",
            ("tests/test_ok.py", "test_param[2-2-5]"): "FAILED",
            ("tests/test_ok.py", "test_skipped"): "ignored",
            ("tests/test_ok.py", "test_xfail"): "ignored",
            ("tests/test_ok.py", "test_xpass"): "FAILED",
            ("tests/test_ok.py::TestGroup", "test_in_class"): "ok",
            ("tests/test_ok.py::TestGroup", "test_raises"): "FAILED",
            ("tests/test_ok.py", "test_fixture_error"): "FAILED",
            ("tests/test_ok.py", "test_adds_more"): "ok",
        })
        self.assertEqual(parsed["missing_footers"], [])
        self.assertEqual(parsed["compile_errors"], [])
        footer = parsed["footers"][0]
        self.assertEqual((footer["passed"], footer["failed"], footer["ignored"]),
                         (4, 5, 2))
        self.assertEqual(footer["status"], "FAILED")
        gate = suite_green(parsed, [])
        self.assertEqual(gate.result, FAIL)
        self.assertEqual(sorted(gate.evidence["unexpected"]),
                         sorted(["test_fails", "test_param[2-2-5]", "test_xpass",
                                 "test_raises", "test_fixture_error"]))
        # With every red test on known_failing, the run reads green.
        known = ["test_fails", "test_param[2-2-5]", "test_xpass", "test_raises",
                 "test_fixture_error"]
        self.assertEqual(suite_green(parsed, known).result, PASS)

    def test_a_collection_error_is_a_compile_error(self) -> None:
        root, pack = self._repo({
            "tests/test_ok.py": "def test_one():\n    assert True\n",
            "tests/test_import_error.py": "from tally.m import nope\n\n"
                                          "def test_never():\n    assert True\n",
        })
        result, parsed = self._test(root, pack)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([e["binary"] for e in parsed["compile_errors"]],
                         ["tests/test_import_error.py"])
        gate = suite_green(parsed, [], run_cls=FAIL, exit_code=result.returncode)
        self.assertEqual(gate.result, FAIL)
        self.assertIn("did not compile", gate.detail)
        named = ignored_or_filtered(parsed, ["test_one"], run_cls=FAIL,
                                    exit_code=result.returncode)
        self.assertEqual(named.result, FAIL)

    def test_a_named_run_selects_by_substring_and_the_gate_does_not(self) -> None:
        root, pack = self._repo({"tests/test_ok.py": EVERY_STATUS})
        result, parsed = self._test(root, pack, tests=["test_adds"])
        names = sorted(r["name"] for r in parsed["results"])
        # `-k` is a substring match: pytest runs test_adds_more too.
        self.assertEqual(names, ["test_adds", "test_adds_more"])
        self.assertEqual([r["name"] for r in rows_for_test(
            parsed["results"], "test_adds", "tests/test_ok.py", pack.row_file)],
            ["test_adds"])

    def test_a_named_parametrized_test_runs_every_case(self) -> None:
        root, pack = self._repo({"tests/test_ok.py": EVERY_STATUS})
        result, parsed = self._test(root, pack, tests=["test_param"])
        rows = rows_for_test(parsed["results"], "test_param", "tests/test_ok.py",
                             pack.row_file)
        self.assertEqual(sorted((r["name"], r["status"]) for r in rows),
                         [("test_param[1-1-2]", "ok"), ("test_param[2-2-5]", "FAILED")])
        gate = ignored_or_filtered(parsed, ["test_param"],
                                   files={"test_param": "tests/test_ok.py"},
                                   row_file=pack.row_file)
        self.assertEqual(gate.result, FAIL)
        # One exact case may be named, and `-k` takes the brackets.
        result, parsed = self._test(root, pack, tests=["test_param[1-1-2]"])
        self.assertEqual([r["name"] for r in parsed["results"]], ["test_param[1-1-2]"])
        self.assertEqual(ignored_or_filtered(parsed, ["test_param[1-1-2]"]).result,
                         PASS)

    def test_a_name_that_selects_nothing_is_never_reported(self) -> None:
        root, pack = self._repo({"tests/test_ok.py": EVERY_STATUS})
        result, parsed = self._test(root, pack, tests=["no_such_test"])
        self.assertEqual(result.returncode, 5)
        self.assertEqual(parsed["results"], [])
        self.assertEqual(parsed["missing_footers"], [])
        self.assertEqual(parsed["footers"][0]["filtered"], 11)
        gate = ignored_or_filtered(parsed, ["no_such_test"], run_cls=FAIL,
                                   exit_code=5)
        self.assertEqual(gate.result, FAIL)
        self.assertIn("never reported", gate.detail)

    def test_an_empty_suite_is_a_finished_run(self) -> None:
        root, pack = self._repo({"tests/test_empty.py": ""})
        result, parsed = self._test(root, pack)
        self.assertEqual(result.returncode, 5)
        self.assertIn("no tests ran", result.stdout)
        self.assertEqual(parsed["missing_footers"], [])
        self.assertEqual(suite_green(parsed, [], run_cls=FAIL, exit_code=5).result,
                         PASS)

    def test_a_run_killed_partway_is_infra(self) -> None:
        root, pack = self._repo({"tests/test_hang.py": textwrap.dedent('''\
            import time


            def test_first():
                assert True


            def test_hangs():
                time.sleep(60)
            ''')})
        argv = pack.commands("test")["argv"]
        try:
            subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=5)
            self.fail("the run did not time out")
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        parsed = pack.parse_tests(out, "")
        self.assertEqual(parsed["missing_footers"], ["pytest"])
        self.assertEqual(suite_green(parsed, [], run_cls=INFRA, exit_code=-1).result,
                         INFRA)


PYTEST_PYPROJECT = """\
[project]
name = "tally"
version = "0.1.0"
requires-python = ">=3.11"

[tool.pytest.ini_options]
testpaths = ["tests"]
"""

#: A plain pytest function, the shape a pytest project's test writer is told to use.
PLAIN_TEST = '''\
import pytest

from tally.bounded import Bounded, FullError


def test_a_full_bundle_refuses_a_push():
    bundle = Bounded(1)
    bundle.push(7)
    with pytest.raises(FullError):
        bundle.push(8)
'''

#: Every case fails on base (push ignores the capacity) and passes after the fix.
PARAM_TEST = '''\
import pytest

from tally.bounded import Bounded, FullError


@pytest.mark.parametrize("capacity", [1, 2, 3])
def test_a_full_bundle_refuses_a_push(capacity):
    bundle = Bounded(capacity)
    for value in range(capacity):
        bundle.push(value)
    with pytest.raises(FullError):
        bundle.push(99)
'''

#: The last case passes on base: the named test's rows disagree there.
MIXED_PARAM_TEST = '''\
import pytest

from tally.bounded import Bounded, FullError


@pytest.mark.parametrize("capacity,pushes,full", [(1, 1, True), (3, 1, False)])
def test_a_full_bundle_refuses_a_push(capacity, pushes, full):
    bundle = Bounded(capacity)
    for value in range(pushes):
        bundle.push(value)
    if full:
        with pytest.raises(FullError):
            bundle.push(99)
    else:
        assert bundle.push(99) == pushes
'''


@unittest.skipUnless(HAS_PYTEST, "the Python running the suite cannot import pytest")
class PytestCardTests(unittest.TestCase):
    """Scripted cards end to end on a pytest-configured toy, real pytest."""

    def _toy(self):
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        toy.write("pyproject.toml", PYTEST_PYPROJECT)
        toy.commit("tally: run the tests with pytest")
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

    def _run(self, test: str) -> tuple[dict, Path]:
        toy = self._toy()
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
        return outcome, next((toy.state_dir / "cards" / "CARD-1").iterdir())

    def _gate(self, run_dir: Path, name: str) -> dict:
        found = sorted(run_dir.glob(f"gates/*/{name}*.json"))
        self.assertTrue(found, f"no {name} gate recorded")
        return json.loads(found[-1].read_text())

    def test_a_plain_pytest_function_reaches_review(self) -> None:
        outcome, run_dir = self._run(PLAIN_TEST)
        self.assertEqual(outcome["final_state"], "in-review",
                         f"{outcome.get('stop_code')}: {outcome.get('detail')}")
        self.assertEqual(self._gate(run_dir, "suite")["result"], "pass")

    def test_a_parametrized_test_reaches_review_on_every_case(self) -> None:
        outcome, run_dir = self._run(PARAM_TEST)
        self.assertEqual(outcome["final_state"], "in-review",
                         f"{outcome.get('stop_code')}: {outcome.get('detail')}")
        rows = self._gate(run_dir, "fail-before")["evidence"]["rows"][
            "test_a_full_bundle_refuses_a_push"]
        self.assertEqual(sorted(r["name"] for r in rows), [
            "test_a_full_bundle_refuses_a_push[1]",
            "test_a_full_bundle_refuses_a_push[2]",
            "test_a_full_bundle_refuses_a_push[3]"])
        self.assertEqual({r["status"] for r in rows}, {"FAILED"})
        self.assertEqual(self._gate(run_dir, "pass-after")["result"], "pass")

    def test_a_case_passing_on_base_blocks_as_unprovable(self) -> None:
        outcome, run_dir = self._run(MIXED_PARAM_TEST)
        self.assertEqual(outcome["final_state"], "blocked")
        self.assertEqual(outcome["stop_code"], "tests-unprovable")
        gate = self._gate(run_dir, "fail-before")
        self.assertEqual(gate["result"], "fail")
        rows = gate["evidence"]["rows"]["test_a_full_bundle_refuses_a_push"]
        self.assertEqual(sorted((r["name"], r["status"]) for r in rows), [
            ("test_a_full_bundle_refuses_a_push[1-1-True]", "FAILED"),
            ("test_a_full_bundle_refuses_a_push[3-1-False]", "ok")])


if __name__ == "__main__":
    unittest.main()
