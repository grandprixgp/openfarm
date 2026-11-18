"""ruff is used only when the project configures it (v0.8.1 fix wave 2, P1).

With ruff installed and no ruff configuration, ruff's default rules judged code
nobody wrote for them, and toy cards blocked on import order. The pack now follows
the pytest rule: `[tool.ruff]` in `pyproject.toml`, a `ruff.toml` or a `.ruff.toml`
turns ruff on, and only when it is installed. The cases marked "real ruff" run the
installed ruff and skip themselves when it is not on PATH.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.packs.python import PythonPack, detect_ruff_config
from tests.test_python_e2e import (GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy,
                                   run_cli, script)

HAS_RUFF = shutil.which("ruff") is not None

RUFF_PYPROJECT = """\
[project]
name = "tally"
version = "0.1.0"
requires-python = ">=3.11"

[tool.ruff]
line-length = 88

[tool.ruff.lint]
select = ["F", "I"]
"""

#: Unsorted imports and an unused one: clean for the syntax check, red for ruff's
#: default rules (F401) and for `I` (I001).
UNSORTED = "import sys\nimport os\n\nprint(sys.argv)\n"


def _project(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="openfarm-ruff-"))
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(content))
    return root


def _pack(root: Path, ruff: bool | None = None) -> PythonPack:
    pack = PythonPack(ruff=ruff)
    pack.configure(root)
    return pack


class RuffConfigDetectionTests(unittest.TestCase):
    def test_each_config_file_turns_ruff_on(self) -> None:
        cases = {
            "ruff.toml": {"ruff.toml": "line-length = 90\n"},
            ".ruff.toml": {".ruff.toml": "line-length = 90\n"},
            "pyproject.toml": {"pyproject.toml": "[tool.ruff]\nline-length = 90\n"},
        }
        for name, files in cases.items():
            with self.subTest(name):
                root = _project(files)
                self.assertEqual(detect_ruff_config(root), root / name)

    def test_a_lint_only_table_counts(self) -> None:
        root = _project({"pyproject.toml": "[tool.ruff.lint]\nselect = ['F']\n"})
        self.assertEqual(detect_ruff_config(root), root / "pyproject.toml")

    def test_a_pyproject_without_tool_ruff_configures_nothing(self) -> None:
        for text in ("[project]\nname = 'x'\n",
                     "[tool.black]\nline-length = 90\n",
                     "[tool.ruff\nbroken",
                     "# [tool.ruff]\n"):
            with self.subTest(text):
                self.assertIsNone(detect_ruff_config(
                    _project({"pyproject.toml": text})))

    def test_no_file_configures_nothing(self) -> None:
        self.assertIsNone(detect_ruff_config(_project({"a.py": "x = 1\n"})))


class RuffChoiceTests(unittest.TestCase):
    def test_installed_but_not_configured_is_not_used(self) -> None:
        pack = _pack(_project({"pyproject.toml": "[project]\nname = 'x'\n"}),
                     ruff=True)
        self.assertFalse(pack.use_ruff)
        self.assertNotIn("ruff", pack.commands("lint")["argv"])
        self.assertNotIn("ruff", pack.commands("ws-lint")["argv"])
        self.assertEqual(pack.commands("format", files=["a.py"])["argv"][1:],
                         ["-c", "pass"])
        self.assertEqual(pack.commands("fmt-check", files=["a.py"])["argv"][1:],
                         ["-c", "pass"])
        note = " ".join(pack.notes)
        self.assertIn("ruff is not configured", note)
        self.assertIn("although it is installed", note)

    def test_not_installed_and_not_configured(self) -> None:
        pack = _pack(_project({"a.py": "x = 1\n"}), ruff=False)
        self.assertFalse(pack.use_ruff)
        ruff_notes = [n for n in pack.notes if "ruff" in n]
        self.assertEqual(len(ruff_notes), 1, pack.notes)
        self.assertIn("ruff is not configured", ruff_notes[0])
        self.assertNotIn("although", ruff_notes[0])

    def test_configured_but_not_installed_says_so(self) -> None:
        pack = _pack(_project({"ruff.toml": "line-length = 90\n"}), ruff=False)
        self.assertFalse(pack.use_ruff)
        self.assertNotIn("ruff", pack.commands("lint")["argv"])
        self.assertEqual(pack.commands("fmt-check", files=["a.py"])["argv"][1:],
                         ["-c", "pass"])
        ruff_notes = [n for n in pack.notes if "ruff" in n]
        self.assertEqual(len(ruff_notes), 1, pack.notes)
        self.assertIn("configured in ruff.toml but not installed", ruff_notes[0])

    def test_configured_and_installed_uses_it(self) -> None:
        root = _project({"ruff.toml": "line-length = 90\n"})
        pack = _pack(root, ruff=True)
        self.assertTrue(pack.use_ruff)
        self.assertEqual(pack.commands("lint")["argv"][-1], "ruff")
        self.assertEqual(pack.commands("ws-lint")["argv"][-1], "ruff")
        self.assertNotIn("ruff", pack.commands("typecheck")["argv"])
        self.assertEqual(pack.commands("format", files=["a.py"])["argv"],
                         ["ruff", "format", "--no-cache", "a.py"])
        self.assertEqual([n for n in pack.notes if "ruff" in n], [])

    def test_a_file_outside_the_worktree_gets_the_project_config(self) -> None:
        root = _project({"ruff.toml": "line-length = 90\n"})
        pack = _pack(root, ruff=True)
        outside = Path(tempfile.mkdtemp()) / "base.py"
        argv = pack.commands("fmt-check", files=[str(outside)])["argv"]
        self.assertEqual(argv[:5], ["ruff", "format", "--no-cache", "--config",
                                    str(root / "ruff.toml")])
        inside = pack.commands("fmt-check", files=[str(root / "a.py")])["argv"]
        self.assertNotIn("--config", inside)

    def test_reconfiguring_replaces_the_note(self) -> None:
        pack = PythonPack(ruff=True)
        pack.configure(_project({"a.py": ""}))
        pack.configure(_project({"ruff.toml": "line-length = 90\n"}))
        self.assertEqual([n for n in pack.notes if "ruff" in n], [])
        self.assertTrue(pack.use_ruff)

    def test_the_path_lookup_is_the_default(self) -> None:
        pack = _pack(_project({"ruff.toml": "line-length = 90\n"}))
        self.assertEqual(pack.has_ruff, HAS_RUFF)
        self.assertEqual(pack.use_ruff, HAS_RUFF)


@unittest.skipUnless(HAS_RUFF, "real ruff is not on PATH")
class RealRuffTests(unittest.TestCase):
    """The pack's own argv, run with the installed ruff."""

    def _lint(self, root: Path) -> subprocess.CompletedProcess:
        pack = _pack(root)
        return subprocess.run(pack.commands("lint")["argv"], cwd=root,
                              capture_output=True, text=True)

    def test_without_config_ruff_defaults_never_judge(self) -> None:
        root = _project({"a.py": UNSORTED})
        result = self._lint(root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((root / ".ruff_cache").exists())

    def test_with_config_a_real_finding_fails_lint(self) -> None:
        root = _project({"a.py": UNSORTED, "pyproject.toml": RUFF_PYPROJECT})
        result = self._lint(root)
        self.assertEqual(result.returncode, 1)
        self.assertIn("F401", result.stdout + result.stderr)
        self.assertFalse((root / ".ruff_cache").exists())

    def test_with_config_clean_code_passes_lint(self) -> None:
        root = _project({"a.py": "import sys\n\nprint(sys.argv)\n",
                         "pyproject.toml": RUFF_PYPROJECT})
        result = self._lint(root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_format_runs_and_the_base_check_reads_the_project_config(self) -> None:
        config = 'line-length = 88\n[format]\nquote-style = "single"\n'
        root = _project({"ruff.toml": config, "a.py": 'x = "a"\n'})
        pack = _pack(root)
        subprocess.run(pack.commands("format", files=["a.py"])["argv"], cwd=root,
                       check=True, capture_output=True)
        self.assertEqual((root / "a.py").read_text(), "x = 'a'\n")
        # The flow checks the base blob from a scratch copy outside the worktree.
        scratch = Path(tempfile.mkdtemp()) / "a.py"
        scratch.write_text("x = 'a'\n")
        check = subprocess.run(
            pack.commands("fmt-check", files=[str(scratch)])["argv"], cwd=root,
            capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertFalse((root / ".ruff_cache").exists())


#: Imports `os` and never uses it: ruff's F401, invisible to the syntax check.
UNUSED_IMPORT_IMPLEMENTATION = '''\
=== search_replace: tally/bounded.py
<<< SEARCH
"""A bounded list of segments. The subject of the cards."""
>>> REPLACE
"""A bounded list of segments. The subject of the cards."""

import os
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

#: The same fix with unsorted imports, which ruff's default rule set flags.
UNSORTED_IMPORT_IMPLEMENTATION = '''\
=== search_replace: tally/bounded.py
<<< SEARCH
"""A bounded list of segments. The subject of the cards."""
>>> REPLACE
"""A bounded list of segments. The subject of the cards."""

import sys
import os

_ARGS = (os.sep, sys.byteorder)
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


@unittest.skipUnless(HAS_RUFF, "real ruff is not on PATH")
class RuffCardTests(unittest.TestCase):
    """A scripted card end to end, with the real ruff on PATH."""

    def _toy(self, configured: bool):
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        if configured:
            toy.write("pyproject.toml", RUFF_PYPROJECT)
            toy.commit("tally: configure ruff")
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

    def _run(self, toy, implementer: str) -> tuple[dict, str]:
        script_path = script(**{
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
            "CARD-1/implementer": implementer,
            "CARD-1/checker_pass1": "RESTATEMENT: a full bundle refuses a push",
            "CARD-1/checker_pass2": "{}",
        })
        result = run_cli(toy.root, "run", "--card", "CARD-1",
                         "--script", str(script_path), "--cards", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        outcome = json.loads(result.stdout)["outcomes"][0]
        run_dirs = list((toy.state_dir / "cards" / "CARD-1").iterdir())
        dossier = (run_dirs[0] / "dossier.md").read_text()
        return outcome, dossier

    def test_the_base_is_clean_for_the_configured_rules(self) -> None:
        toy = self._toy(configured=True)
        result = subprocess.run(["ruff", "check", "--no-cache", "."], cwd=toy.root,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_configured_ruff_passes_a_clean_card(self) -> None:
        toy = self._toy(configured=True)
        outcome, dossier = self._run(toy, GOOD_IMPLEMENTATION)
        self.assertEqual(outcome["final_state"], "in-review",
                         f"{outcome.get('stop_code')}: {outcome.get('detail')}")
        self.assertNotIn("ruff is not configured", dossier)

    def test_configured_ruff_blocks_a_real_finding(self) -> None:
        toy = self._toy(configured=True)
        outcome, dossier = self._run(toy, UNUSED_IMPORT_IMPLEMENTATION)
        self.assertEqual(outcome["final_state"], "blocked")
        self.assertIn("F401", json.dumps(outcome) + dossier)

    def test_unconfigured_ruff_never_blocks_on_its_defaults(self) -> None:
        toy = self._toy(configured=False)
        outcome, dossier = self._run(toy, UNSORTED_IMPORT_IMPLEMENTATION)
        self.assertEqual(outcome["final_state"], "in-review",
                         f"{outcome.get('stop_code')}: {outcome.get('detail')}")
        self.assertIn("ruff is not configured", dossier)


if __name__ == "__main__":
    unittest.main()
