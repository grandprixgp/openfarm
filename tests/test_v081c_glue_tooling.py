"""v0.8.1c glue, G1 and G2: `check` and a card say what tools they judge with,
and both decide from the one config the command was given.

G1: `openfarm check` shows the runner, the type check or lint, the formatter and
the pack's notes, in the JSON payload and in the human output, the same way for
TypeScript, Python and Rust. A card's record carries the same block.

G2: with `--config` naming a file that differs from the base checkout's own
config, eligibility, the TypeScript pack and the card's gates all follow the
named file. A fake `tsc` in the base's `node_modules/.bin` shows which one ran.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adopt import adopt_config_text
from openfarm_core.git import Git
from openfarm_core.human_cli import Style, format_human_strict

OPENFARM = Path(__file__).resolve().parent.parent / "openfarm"
NODE = shutil.which("node")

MATH_TS = """\
/** The sum of two numbers. */
export function add(a: number, b: number): number {
  return a - b;
}
"""

EXISTING_TEST = """\
import test from "node:test";
import assert from "node:assert/strict";
import { add } from "../src/math.ts";

test("adding zero keeps the number", () => {
  assert.equal(add(4, 0), 4);
});
"""

NAMED_TEST = """\
import test from "node:test";
import assert from "node:assert/strict";
import { add } from "../src/math.ts";

test("two and three make five", () => {
  assert.equal(add(2, 3), 5);
});
"""

FIX = """\
=== search_replace: src/math.ts
<<< SEARCH
  return a - b;
>>> REPLACE
  return a + b;
"""

CARD = """\
+++
id = "CARD-1"
title = "add adds"
state = "ready"
category = "pinned-bugfix"
unit = "."
touches = ["src/math.ts"]
depends = []
tests = [{name = "two and three make five", file = "tests/add.test.ts", asserts = "add(2, 3) is 5"}]
+++

`add(2, 3)` returns `5`, and `add(4, 0)` returns `4`.
"""

TSCONFIG = '{"compilerOptions": {"noEmit": true, "allowImportingTsExtensions": true}}\n'


def _tmp(owner: unittest.TestCase) -> Path:
    root = Path(tempfile.mkdtemp(prefix="of-v081c-tool-", dir=os.environ.get("TMPDIR")))
    owner.addCleanup(shutil.rmtree, root, True)
    return root


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


def _cli(repo: Path, *args: str, config: Path | None = None,
         human: bool = False) -> subprocess.CompletedProcess:
    head = [sys.executable, str(OPENFARM), "--repo", str(repo)]
    if config is not None:
        head += ["--config", str(config)]
    if not human:
        head.append("--json")
    env = dict(os.environ)
    env["PATH"] = "/usr/bin:/bin" + (":" + str(Path(NODE).parent) if NODE else "")
    env.pop("NO_COLOR", None)
    return subprocess.run([*head, *args], capture_output=True, text=True, env=env)


def _offline(text: str) -> str:
    text = text.replace('model = "openai-compatible"', 'model = "scripted"')
    text = text.replace('micromanager = "claude-acp"', 'micromanager = "off"')
    return text.replace('mode = "active"', 'mode = "off"')


@unittest.skipUnless(NODE and shutil.which("git"), "needs node and git")
class TypeScriptToolingTests(unittest.TestCase):
    """The base checkout's own config says syntax only; `other.toml` says auto,
    and the base has a (fake) tsc and a tsconfig.json, so `auto` means tsc."""

    def setUp(self) -> None:
        base = _tmp(self)
        root = base / "repo"
        root.mkdir()
        _write(root, {"package.json": '{"name": "mathkit", "type": "module"}\n',
                      ".gitignore": "node_modules/\n", "tsconfig.json": TSCONFIG,
                      "src/math.ts": MATH_TS, "tests/existing.test.ts": EXISTING_TEST})
        self.log = base / "tsc.log"
        tsc = root / "node_modules" / ".bin" / "tsc"
        tsc.parent.mkdir(parents=True)
        tsc.write_text(f'#!/bin/sh\necho "$PWD $*" >> "{self.log}"\nexit 0\n')
        tsc.chmod(0o755)
        state = base / "state"
        text = adopt_config_text(root, "typescript", base_branch="main",
                                 state_dir=state, key_file=base / "k",
                                 typescript_tools={"runner": "node --test",
                                                   "typecheck": "syntax",
                                                   "format": "off"})
        text = _offline(text)
        self.assertIn('typescript_typecheck = "syntax"', text)
        _write(root, {".openfarm/config.toml": text, "cards/CARD-1.md": CARD})
        self.other = base / "other.toml"
        self.other.write_text(text.replace('typescript_typecheck = "syntax"',
                                           'typescript_typecheck = "auto"'))
        git = Git(root)
        git.init(branch="main")
        git.commit_all("mathkit")
        self.root, self.state, self.base = root, state, base
        script = base / "script.json"
        script.write_text(json.dumps({
            "CARD-1/test_writer": "=== whole_file: tests/add.test.ts\n" + NAMED_TEST,
            "CARD-1/implementer": FIX,
            "CARD-1/checker_pass1": "RESTATEMENT: add(2, 3) returns 5",
            "CARD-1/checker_pass2": "{}",
        }))
        self.script = script

    def _check(self, config: Path | None) -> dict:
        res = _cli(self.root, "check", "CARD-1", config=config)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        return json.loads(res.stdout)

    def _run(self, config: Path | None) -> tuple[dict, list[dict]]:
        res = _cli(self.root, "run", "--card", "CARD-1", "--script", str(self.script),
                   "--cards", "1", config=config)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        outcome = json.loads(res.stdout)["outcomes"][0]
        run_dir = sorted((self.state / "cards" / "CARD-1").iterdir())[-1]
        gates = [json.loads(p.read_text()) for p in sorted((run_dir / "gates").rglob("*.json"))]
        return outcome, gates

    def test_check_shows_the_runner_the_type_check_and_the_notes(self) -> None:
        payload = self._check(None)
        self.assertEqual(payload["tooling"]["pack"], "typescript")
        self.assertEqual(payload["tooling"]["runner"], "node --test")
        self.assertEqual(payload["tooling"]["typecheck"], "syntax")
        self.assertTrue(payload["tooling"]["syntax_only"])
        self.assertEqual(payload["tooling"]["format"], "off")
        self.assertTrue(any("syntax" in note for note in payload["tooling"]["notes"]),
                        payload["tooling"]["notes"])
        # Eligibility's own record of the same decision.
        self.assertEqual(payload["typescript"]["typecheck"], "syntax")
        human = _cli(self.root, "check", "CARD-1", human=True)
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn("Tooling", human.stdout)
        self.assertIn("node --test", human.stdout)
        self.assertIn("syntax only (no types are checked)", human.stdout)
        self.assertIn("Tooling Notes", human.stdout)
        for note in payload["tooling"]["notes"]:
            self.assertIn(note, human.stdout)

    def test_config_named_by_flag_decides_check_and_the_cards_gates_alike(self) -> None:
        payload = self._check(self.other)
        self.assertEqual(payload["typescript"]["typecheck"], "tsc")
        self.assertEqual(payload["tooling"]["typecheck"], "tsc")
        outcome, gates = self._run(self.other)
        self.assertEqual(outcome["final_state"], "in-review",
                         f"{outcome.get('stop_code')}: {outcome.get('detail')}")
        self.assertEqual(outcome["tooling"]["typecheck"], "tsc")
        # The card's lint gate ran the same tsc the check named.
        self.assertTrue(self.log.exists(), "tsc never ran for the card")
        self.assertIn("--noEmit", self.log.read_text())

    def test_without_the_flag_the_base_checkouts_config_decides_both(self) -> None:
        payload = self._check(None)
        self.assertEqual(payload["typescript"]["typecheck"], "syntax")
        outcome, _ = self._run(None)
        self.assertEqual(outcome["final_state"], "in-review",
                         f"{outcome.get('stop_code')}: {outcome.get('detail')}")
        self.assertEqual(outcome["tooling"]["typecheck"], "syntax")
        self.assertFalse(self.log.exists(), self.log.read_text() if self.log.exists() else "")

    def _lint_records(self, gates: list[dict]) -> dict[str, list[dict]]:
        found: dict[str, list[dict]] = {}
        for gate in gates:
            if gate.get("name") in ("lint", "workspace-lint"):
                found.setdefault(gate["name"], []).append(gate)
        self.assertEqual(sorted(found), ["lint", "workspace-lint"], found)
        return found

    def test_the_lint_gate_records_say_the_check_was_syntax_only(self) -> None:
        """G3: no types were checked, and the lint gates' own records say so."""
        outcome, gates = self._run(None)
        self.assertEqual(outcome["final_state"], "in-review", outcome.get("detail"))
        for name, records in self._lint_records(gates).items():
            for record in records:
                with self.subTest(name):
                    self.assertTrue(record["evidence"]["syntax_only"], record)
                    self.assertEqual(record["evidence"]["typecheck"], "syntax")
                    self.assertIn("no types were checked", record["evidence"]["note"])

    def test_a_runs_notes_carry_the_packs_notes_once(self) -> None:
        """Lead: what the pack could not use is said once per run, in its notes."""
        res = _cli(self.root, "run", "--card", "CARD-1", "--script", str(self.script),
                   "--cards", "1")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        payload = json.loads(res.stdout)
        pack_notes = payload["outcomes"][0]["tooling"]["notes"]
        self.assertTrue(pack_notes, payload["outcomes"][0]["tooling"])
        for note in pack_notes:
            self.assertEqual(payload["notes"].count(note), 1, payload["notes"])
        run_json = json.loads(sorted((self.state / "runs").rglob("run.json"))[-1].read_text())
        for note in pack_notes:
            self.assertIn(note, run_json["notes"])

    def test_the_lint_gate_records_say_tsc_checked_the_types(self) -> None:
        outcome, gates = self._run(self.other)
        self.assertEqual(outcome["final_state"], "in-review", outcome.get("detail"))
        for name, records in self._lint_records(gates).items():
            for record in records:
                with self.subTest(name):
                    self.assertFalse(record["evidence"]["syntax_only"], record)
                    self.assertEqual(record["evidence"]["typecheck"], "tsc")

    def test_a_flag_config_that_turns_the_link_off_reaches_the_card(self) -> None:
        self.other.write_text(self.other.read_text().replace(
            "link_node_modules = true", "link_node_modules = false"))
        self.assertIn("link_node_modules = false", self.other.read_text())
        outcome, _ = self._run(self.other)
        self.assertTrue(any("link_node_modules is false" in note
                            for note in outcome["tooling"]["notes"]),
                        outcome["tooling"]["notes"])


class PythonAndRustToolingTests(unittest.TestCase):
    def _python_repo(self, extra: dict[str, str]) -> Path:
        base = _tmp(self)
        root = base / "repo"
        root.mkdir()
        files = {"pyproject.toml": '[project]\nname = "pk"\nversion = "0.1.0"\n',
                 "pk/__init__.py": "", "pk/core.py": "def one():\n    return 1\n",
                 "tests/__init__.py": "",
                 "tests/test_core.py": "import unittest\n\n\nclass T(unittest.TestCase):\n"
                                       "    def test_one(self):\n        pass\n",
                 **extra}
        _write(root, files)
        text = _offline(adopt_config_text(root, "python", base_branch="main",
                                          state_dir=base / "state", key_file=base / "k"))
        _write(root, {".openfarm/config.toml": text, "cards/CARD-1.md": textwrap.dedent("""\
            +++
            id = "CARD-1"
            title = "one is two"
            state = "ready"
            category = "pinned-bugfix"
            unit = "."
            touches = ["pk/core.py"]
            depends = []
            tests = [{name = "test_two", file = "tests/test_two.py", asserts = "one() == 2"}]
            +++

            `one()` returns `2`.
            """)})
        git = Git(root)
        git.init(branch="main")
        git.commit_all("pk")
        return root

    def test_python_check_names_the_runner_and_the_ruff_case(self) -> None:
        root = self._python_repo({})
        res = _cli(root, "check", "CARD-1")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        tooling = json.loads(res.stdout)["tooling"]
        self.assertEqual((tooling["pack"], tooling["runner"], tooling["lint"],
                          tooling["format"]), ("python", "unittest", "syntax", "off"))
        self.assertTrue(any("ruff is not configured" in n for n in tooling["notes"]),
                        tooling["notes"])
        human = _cli(root, "check", "CARD-1", human=True)
        self.assertIn("unittest", human.stdout)
        self.assertIn("ruff is not configured", human.stdout)
        self.assertIn("off (formatting is not checked)", human.stdout)

    def test_python_check_names_pytest_where_the_project_configures_it(self) -> None:
        root = self._python_repo({"pytest.ini": "[pytest]\n"})
        tooling = json.loads(_cli(root, "check", "CARD-1").stdout)["tooling"]
        self.assertEqual(tooling["runner"], "pytest")

    def test_packs_other_than_typescript_add_nothing_to_gate_records(self) -> None:
        from openfarm_core.packs.base import pack_for
        for language in ("python", "rust"):
            with self.subTest(language):
                pack = pack_for(language)
                self.assertEqual(pack.gate_evidence("lint"), {})
                self.assertEqual(pack.gate_evidence("workspace-lint"), {})

    def test_the_human_tooling_block_reads_alike_for_rust(self) -> None:
        payload = {"card": "CARD-1", "eligible": True, "failures": [],
                   "tooling": {"pack": "rust", "notes": [
                       "Cargo.lock is not committed, so --locked is not passed"]}}
        text = format_human_strict("check", payload, style=Style(enabled=False))
        self.assertIn("Tooling", text)
        self.assertIn("language", text)
        self.assertIn("rust", text)
        self.assertIn("Cargo.lock is not committed", text)
        # A payload from before this change still renders.
        old = format_human_strict("check", {"card": "CARD-1", "eligible": True,
                                            "failures": []}, style=Style(enabled=False))
        self.assertNotIn("Tooling", old)


if __name__ == "__main__":
    unittest.main()
