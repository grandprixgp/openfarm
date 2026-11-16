"""End-to-end on a Python repository: real git, real interpreter, a scripted model.

These run by default (no `OPENFARM_E2E` needed): a Python card builds nothing, so a
whole run takes a second or two.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.toy import ToyRepo, _run

OPENFARM = Path(__file__).resolve().parent.parent / "openfarm"

PYPROJECT = """\
[project]
name = "tally"
version = "0.1.0"
requires-python = ">=3.11"
"""

COUNTER_PY = '''\
"""A counter: the reference slice pattern cards imitate."""


class CounterError(Exception):
    """A counter refused a change."""


class Counter:
    """A count that never goes below zero."""

    def __init__(self) -> None:
        self.value = 0

    def add(self, amount: int) -> int:
        """Add `amount` and return the new value."""
        if amount < 0:
            raise CounterError("negative amount")
        self.value += amount
        return self.value
'''

#: The base has a real bug: `push` ignores the capacity.
BOUNDED_PY = '''\
"""A bounded list of segments. The subject of the cards."""


class FullError(Exception):
    """The bundle is at capacity."""


class Bounded:
    """Up to `capacity` segments."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.segments: list[int] = []

    def push(self, value: int) -> int:
        """Add a segment and return its index."""
        self.segments.append(value)
        return len(self.segments) - 1
'''

SEAM_PY = '''\
"""A seam module. Cards must never touch it."""


def sealed_gate() -> int:
    return 41
'''

EXISTING_TEST = '''\
import unittest

from tally.bounded import Bounded


class EmptyTests(unittest.TestCase):
    def test_an_empty_bundle_has_no_segments(self):
        self.assertEqual(Bounded(3).segments, [])
'''

CONFIG = """\
[project]
name = "tally"
lane = "farm"
base_branch = "main"
state_dir = "{state_dir}"

[adapters]
cards = "files:cards/"
runner = "direct"
languages = ["python"]
model = "scripted"

[model]
endpoint = "https://scripted.invalid/v1"
allowed_endpoints = ["https://scripted.invalid/v1"]
test_writer = {{ model = "stub", temperature = 0.2 }}
implementer = {{ model = "stub", temperature = 0.3, vary_seed = true }}
checker     = {{ model = "stub", temperature = 0.4, vary_seed = true }}

[policy]
never_touch = ["tally/seam.py"]
forbidden_files = ["pyproject.toml", ".openfarm/**"]
known_failing = []
flagged_patterns = []
categories = ["pattern", "pinned-bugfix", "small-feature"]

[eligibility]
max_files = 6
max_units = 1
require_reference = ["pattern", "small-feature"]
refuse_touches_prefixes = ["seam:"]

[runner]
python = "{python}"

[budget]
implementer_attempts = 3
test_writer_attempts = 2
model_build_calls_per_card = 10
mutation_build_calls_per_card = 40
tokens_per_card = 400000
wall_clock_per_card = "10m"
spend_per_night = 20.0
concurrent_builds = 1
concurrent_cards = 1
lease_ttl = "20m"

[gates]
revert_check = "required"
mutation = "off"
mutation_required_for = []
counter_tests = "report"
coverage = "off"
workspace_lint = true
baseline_cache = true
catch_up = "check"
findings_per_card = 2
circuit_breaker = 3
"""

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

GOOD_IMPLEMENTATION = '''\
=== search_replace: tally/bounded.py
<<< SEARCH
        """Add a segment and return its index."""
        self.segments.append(value)
>>> REPLACE
        """Add a segment and return its index, or raise `FullError`."""
        if len(self.segments) >= self.capacity:
            raise FullError(f"capacity {self.capacity} reached")
        self.segments.append(value)
'''

#: Swallows the error instead of raising it: the banned-pattern gate must stop it.
SWALLOWING_IMPLEMENTATION = '''\
=== search_replace: tally/bounded.py
<<< SEARCH
        """Add a segment and return its index."""
        self.segments.append(value)
>>> REPLACE
        """Add a segment and return its index."""
        try:
            if len(self.segments) >= self.capacity:
                raise FullError("full")
        except:
            raise FullError("full")
        self.segments.append(value)
'''

#: A test that passes on base proves nothing.
TEST_PASSES_ON_BASE = '''\
import unittest

from tally.bounded import Bounded


class FullTests(unittest.TestCase):
    def test_a_full_bundle_refuses_a_push(self):
        self.assertEqual(Bounded(1).capacity, 1)
'''


def build_python_toy() -> ToyRepo:
    base = Path(tempfile.mkdtemp(prefix="openfarm-toy-"))
    root = base / "repo"
    state = base / "repo-state"
    root.mkdir()
    state.mkdir()
    repo = ToyRepo(root, state, base=base)
    repo.write("pyproject.toml", PYPROJECT)
    repo.write("tally/__init__.py", "")
    repo.write("tally/counter.py", COUNTER_PY)
    repo.write("tally/bounded.py", BOUNDED_PY)
    repo.write("tally/seam.py", SEAM_PY)
    repo.write("tests/__init__.py", "")
    repo.write("tests/test_existing.py", EXISTING_TEST)
    repo.write(".gitignore", "__pycache__/\n")
    repo.write(".openfarm/config.toml",
               CONFIG.format(state_dir=state, python=sys.executable))
    _run(["git", "init", "-q", "-b", "main"], root)
    repo.commit("tally: base")
    origin = base / "origin.git"
    _run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], base)
    _run(["git", "remote", "add", "origin", str(origin)], root)
    repo.origin = origin
    return repo


def script(**entries) -> Path:
    path = Path(tempfile.mkdtemp()) / "script.json"
    path.write_text(json.dumps(entries))
    return path


def run_cli(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(OPENFARM), "--repo", str(repo), "--json", *args],
        capture_output=True, text=True)


class PythonCardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy = build_python_toy()
        self.toy.write_card(
            "CARD-1",
            title="A full bundle refuses a push",
            category="pinned-bugfix",
            unit=".",
            touches=["tally/bounded.py"],
            tests=[{"name": "test_a_full_bundle_refuses_a_push",
                    "file": "tests/test_full.py",
                    "asserts": "push raises FullError"}],
            done_text="`Bounded(1).push(8)` after one push raises `FullError`.")
        self.toy.commit("tally: CARD-1")

    def tearDown(self) -> None:
        self.toy.cleanup()

    def _run_card(self, test_writer: str, implementer: str) -> dict:
        script_path = script(**{
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + test_writer,
            "CARD-1/implementer": implementer,
            "CARD-1/checker_pass1": "RESTATEMENT: a full bundle refuses a push",
            "CARD-1/checker_pass2": "{}",
        })
        result = run_cli(self.toy.root, "run", "--card", "CARD-1",
                         "--script", str(script_path), "--cards", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["outcomes"][0]

    def _dossier(self) -> str:
        run_dirs = list((self.toy.state_dir / "cards" / "CARD-1").iterdir())
        return (run_dirs[0] / "dossier.md").read_text()

    def test_the_card_checks_and_dry_runs(self) -> None:
        check = run_cli(self.toy.root, "check", "CARD-1")
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertTrue(json.loads(check.stdout)["eligible"], check.stdout)

    def test_a_clean_card_reaches_in_review(self) -> None:
        outcome = self._run_card(GOOD_TEST, GOOD_IMPLEMENTATION)
        self.assertEqual(outcome["final_state"], "in-review",
                         f"{outcome.get('stop_code')}: {outcome.get('detail')}")
        dossier = self._dossier()
        self.assertNotIn("doc-tests", dossier)
        self.assertIn("## 9. Diffs", dossier)

        branch = subprocess.run(
            ["git", "branch", "--list", "openfarm/CARD-1*", "--format=%(refname:short)"],
            cwd=self.toy.root, capture_output=True, text=True).stdout.split()[0]
        shown = subprocess.run(["git", "show", f"{branch}:tally/bounded.py"],
                               cwd=self.toy.root, capture_output=True, text=True)
        self.assertIn("raise FullError", shown.stdout)
        main = subprocess.run(["git", "show", "main:tally/bounded.py"],
                              cwd=self.toy.root, capture_output=True, text=True)
        self.assertEqual(main.stdout, BOUNDED_PY)

    def test_a_bare_except_is_banned(self) -> None:
        outcome = self._run_card(GOOD_TEST, SWALLOWING_IMPLEMENTATION)
        # The scripted model sends the same banned diff again, so the card ends on
        # no-progress; the gate that sent it back is what this case is about.
        self.assertEqual(outcome["final_state"], "blocked")
        gates = json.dumps(outcome) + self._dossier()
        self.assertIn("bare-except", gates)

    def test_a_test_that_passes_on_base_is_not_proof(self) -> None:
        outcome = self._run_card(TEST_PASSES_ON_BASE, GOOD_IMPLEMENTATION)
        self.assertEqual(outcome["final_state"], "blocked")
        self.assertEqual(outcome["stop_code"], "tests-unprovable")


if __name__ == "__main__":
    unittest.main()
