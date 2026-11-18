"""v0.8.1b ts, T3: the syntax fall-back checks what it says, and says what it can't.

Build output is skipped only at a unit root; node_modules, .git and .openfarm
anywhere. `.tsx`/`.jsx` are named as unchecked and cards touching them refused
while there is no tsc. adopt names what node cannot run, with what to do, and
the eligibility gate record says when checks are syntax only.
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
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.cards import Card
from openfarm_core.config import load_config
from openfarm_core.config_template import render_config
from openfarm_core.gates.eligibility import check_eligibility
from openfarm_core.git import Git
from openfarm_core.packs.typescript import TypeScriptPack, adopt_findings, resolve_tooling

NODE = shutil.which("node")
GARBAGE = "export const = (;\n"
RED = 'import test from "node:test";\ntest("NAME", () => { throw new Error("red"); });\n'


def _tmp(owner: unittest.TestCase) -> Path:
    root = Path(tempfile.mkdtemp(prefix="openfarm-tsy-", dir=os.environ.get("TMPDIR")))
    owner.addCleanup(shutil.rmtree, root, True)
    return root


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(text), encoding="utf-8")


def _pack(root: Path) -> TypeScriptPack:
    pack = TypeScriptPack(tsc=False, prettier=False)
    pack.configure(root)
    return pack


def _cfg(root: Path, typecheck: str = "syntax"):
    (root / ".openfarm").mkdir(parents=True, exist_ok=True)
    (root / ".openfarm" / "config.toml").write_text(render_config(
        name="t", language="typescript", base_branch="main",
        state_dir=root.parent / "state", key_file=root.parent / "k",
        typescript_tools={"typecheck": typecheck, "format": "off"}))
    return load_config(root)


@unittest.skipUnless(NODE, "needs node")
class WalkTests(unittest.TestCase):
    def _check(self, root: Path) -> subprocess.CompletedProcess:
        return subprocess.run(_pack(root).commands("typecheck")["argv"], cwd=root,
                              capture_output=True, text=True)

    def test_build_named_source_folders_are_checked(self) -> None:
        root = _tmp(self)
        _write(root, {"package.json": '{"name": "a"}\n', "src/build/garbage.ts": GARBAGE})
        done = self._check(root)
        self.assertEqual(done.returncode, 1)
        self.assertIn("src/build/garbage.ts", done.stderr)

    def test_build_output_at_a_unit_root_is_skipped(self) -> None:
        root = _tmp(self)
        _write(root, {
            "package.json": '{"name": "a"}\n',
            "dist/out.js": GARBAGE,
            "coverage/x.js": GARBAGE,
            "packages/b/package.json": '{"name": "b"}\n',
            "packages/b/dist/out.js": GARBAGE,
            "src/node_modules/dep/index.ts": GARBAGE,
            "src/.openfarm/x.ts": GARBAGE,
            "src/ok.ts": "export const ok = 1;\n",
        })
        done = self._check(root)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_tests_under_a_build_named_folder_are_found_listed_and_run(self) -> None:
        root = _tmp(self)
        _write(root, {"package.json": '{"name": "a", "type": "module"}\n',
                      "tests/build/x.test.ts": RED.replace("NAME", "in build"),
                      "dist/y.test.js": RED.replace("NAME", "in dist")})
        pack = _pack(root)
        self.assertEqual(pack.test_files(), ["tests/build/x.test.ts"])
        listing = subprocess.run(pack.commands("list")["argv"], cwd=root,
                                 capture_output=True, text=True)
        self.assertEqual([t["name"] for t in pack.parse_list(listing.stdout)["tests"]],
                         ["in build"])
        run = subprocess.run(pack.commands("test")["argv"], cwd=root,
                             capture_output=True, text=True)
        rows = {r["name"]: r["status"] for r in pack.parse_tests(run.stdout,
                                                                 run.stderr)["results"]}
        self.assertEqual(rows, {"in build": "FAILED"})


class JsxTests(unittest.TestCase):
    def _card(self, touch: str) -> Card:
        return Card("C-1", "t", done_text="`f()` returns `1`", touches=[touch],
                    fields={"category": "small-feature",
                            "tests": [{"name": "f", "file": "tests/f.test.ts"}]})

    def test_unchecked_jsx_is_named(self) -> None:
        root = _tmp(self) / "r"
        _write(root, {"package.json": '{"name": "a"}\n', "src/ui/view.tsx": GARBAGE})
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            tooling = resolve_tooling(root, {})
        self.assertTrue(tooling["syntax_only"])
        self.assertTrue(any("src/ui/view.tsx" in note and "node cannot parse JSX" in note
                            for note in tooling["notes"]), tooling["notes"])

    def test_a_card_touching_jsx_is_refused_while_syntax_only(self) -> None:
        root = _tmp(self) / "r"
        root.mkdir()
        cfg = _cfg(root)
        failures = {f["rule"]: f["detail"] for f in
                    check_eligibility(self._card("src/ui/view.tsx"), cfg,
                                      worktree=root).failures}
        self.assertIn("typescript-jsx-unchecked", failures)
        self.assertIn("node cannot parse JSX", failures["typescript-jsx-unchecked"])
        ok = check_eligibility(self._card("src/f.ts"), cfg, worktree=root)
        self.assertNotIn("typescript-jsx-unchecked", [f["rule"] for f in ok.failures])

    def test_with_tsc_jsx_cards_are_allowed(self) -> None:
        root = _tmp(self) / "r"
        bin_dir = root / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "tsc").write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / "tsc").chmod(0o755)
        _write(root, {"tsconfig.json": "{}\n"})
        cfg = _cfg(root, typecheck="tsc")
        rules = [f["rule"] for f in check_eligibility(
            self._card("src/ui/view.tsx"), cfg, worktree=root).failures]
        self.assertNotIn("typescript-jsx-unchecked", rules)
        self.assertNotIn("typescript-tools", rules)

    def test_the_gate_record_says_syntax_only(self) -> None:
        root = _tmp(self) / "r"
        root.mkdir()
        cfg = _cfg(root)
        record = check_eligibility(self._card("src/f.ts"), cfg, worktree=root).as_dict()
        self.assertTrue(record["typescript"]["syntax_only"])
        self.assertEqual(record["typescript"]["typecheck"], "syntax")
        self.assertTrue(any("syntax only" in n for n in record["typescript"]["notes"]))


@unittest.skipUnless(NODE, "needs node")
class AdoptFindingsTests(unittest.TestCase):
    def test_what_node_cannot_run_is_named_with_what_to_do(self) -> None:
        root = _tmp(self)
        _write(root, {
            "package.json": '{"name": "a", "type": "module"}\n',
            "src/color.ts": "export enum Color { Red, Green }\n",
            "src/space.ts": "export namespace Space { export const x = 1; }\n",
            "src/point.ts": ("export class Point {\n"
                             "  constructor(public x: number) {}\n}\n"),
            "src/main.ts": 'import { Color } from "./color";\nexport const c = Color.Red;\n',
            "src/fine.ts": ('import { c } from "./main.ts";\nimport json from "./d.json" '
                            'with { type: "json" };\nexport const d = c;\n'),
            "src/types.d.ts": 'export type { C } from "./other";\n',
        })
        notes = adopt_findings(root, NODE)
        text = "\n".join(notes)
        self.assertIn("enums (1 file(s), first: src/color.ts)", text)
        self.assertIn("as const", text)
        self.assertIn("namespaces (1 file(s), first: src/space.ts)", text)
        self.assertIn("parameter properties (1 file(s), first: src/point.ts)", text)
        self.assertIn("1 file(s) import a relative path without its extension (first: "
                      "src/main.ts imports './color')", text)
        self.assertIn("allowImportingTsExtensions", text)
        self.assertEqual(len(notes), 4, notes)

    def test_a_clean_repo_has_no_findings(self) -> None:
        root = _tmp(self)
        _write(root, {"package.json": '{"name": "a", "type": "module"}\n',
                      "src/a.ts": 'import { b } from "./b.ts";\nexport const a = b;\n',
                      "src/b.ts": "export const b = 1;\n"})
        self.assertEqual(adopt_findings(root, NODE), [])

    def test_adopt_reports_them(self) -> None:
        from openfarm_core.adopt import adopt_project

        base = _tmp(self)
        root = base / "repo"
        _write(root, {"package.json": '{"name": "a", "type": "module"}\n',
                      "src/color.ts": "export enum Color { Red }\n"})
        git = Git(root)
        git.init(branch="main")
        git.commit_all("base")
        result = adopt_project(root, state_dir=base / "s", key_file=base / "k")
        self.assertTrue(any("node cannot run TypeScript enums" in n
                            for n in result["notes"]), result["notes"])


if __name__ == "__main__":
    unittest.main()
