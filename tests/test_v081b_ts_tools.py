"""v0.8.1b ts, T2: one runner and one set of tools for adopt, check and run.

The tools are decided from the base checkout, recorded by adopt, and a card
worktree reaches the base's git-ignored `node_modules` through links. The last
class runs real `tsc` and `prettier` (skipped when they are not installed).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adopt import adopt_config_text, adopt_project
from openfarm_core.cards import Card
from openfarm_core.config import ConfigError, load_config
from openfarm_core.errors import Refused
from openfarm_core.gates.eligibility import check_eligibility
from openfarm_core.git import Git
from openfarm_core.packs.typescript import (
    TypeScriptPack,
    base_checkout,
    resolve_tooling,
)

OPENFARM = Path(__file__).resolve().parent.parent / "openfarm"
NODE = shutil.which("node")
TOOLS = Path(os.environ.get("OPENFARM_TS_TOOLS",
                            "/var/tmp/openfarm-v081/tools/node/node_modules"))
HAVE_TOOLS = all((TOOLS / name).exists() for name in
                 ("typescript", "prettier", "@types/node", ".bin/tsc", ".bin/prettier"))


def _tmp(owner: unittest.TestCase) -> Path:
    root = Path(tempfile.mkdtemp(prefix="openfarm-tst-", dir=os.environ.get("TMPDIR")))
    owner.addCleanup(shutil.rmtree, root, True)
    return root


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(text), encoding="utf-8")


def _fake_bin(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _git_repo(owner: unittest.TestCase, files: dict[str, str]) -> Path:
    base = _tmp(owner)
    root = base / "repo"
    root.mkdir()
    _write(root, files)
    git = Git(root)
    git.init(branch="main")
    git.commit_all("base")
    return root


def _card(**fields) -> Card:
    data = {"category": "small-feature",
            "tests": [{"name": "adds", "file": "tests/math.test.ts"}]}
    data.update(fields)
    return Card("C-1", "t", done_text="`add(1, 2)` returns `3`",
                touches=["src/math.ts", "tests/math.test.ts"], fields=data)


def _adopted_cfg(owner: unittest.TestCase, root: Path, tools: dict | None = None):
    text = adopt_config_text(root, "typescript", base_branch="main",
                             state_dir=root.parent / "state", key_file=root.parent / "k",
                             typescript_tools=tools)
    (root / ".openfarm").mkdir(exist_ok=True)
    (root / ".openfarm" / "config.toml").write_text(text, encoding="utf-8")
    return load_config(root)


class ToolDecisionTests(unittest.TestCase):
    def test_a_worktree_uses_the_base_checkouts_tools(self) -> None:
        root = _git_repo(self, {
            "package.json": '{"name": "a", "type": "module"}\n',
            "tsconfig.json": "{}\n",
            ".gitignore": "node_modules/\n",
        })
        tsc = _fake_bin(root / "node_modules" / ".bin", "tsc")
        worktree = root.parent / "wt"
        Git(root).worktree_add(worktree, "HEAD")
        self.addCleanup(Git(root).worktree_remove, worktree)
        self.assertEqual(base_checkout(worktree).resolve(), root.resolve())
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            pack = TypeScriptPack()
            pack.configure(worktree)
        self.assertEqual(pack.tooling["typecheck"], "tsc")
        self.assertEqual(pack.commands("typecheck")["argv"][:2], [str(tsc), "--noEmit"])
        self.assertNotIn("syntax only", " ".join(pack.notes))

    def test_prettier_is_used_only_when_the_project_configures_it(self) -> None:
        bin_dir = _tmp(self) / "bin"
        _fake_bin(bin_dir, "prettier")
        plain = _tmp(self)
        _write(plain, {"package.json": '{"name": "a"}\n'})
        configured = _tmp(self)
        _write(configured, {"package.json": '{"name": "a"}\n', ".prettierrc": "{}\n"})
        keyed = _tmp(self)
        _write(keyed, {"package.json": '{"name": "a", "prettier": {"semi": false}}\n'})
        with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:/usr/bin:/bin"}):
            for root, used in ((plain, False), (configured, True), (keyed, True)):
                pack = TypeScriptPack()
                pack.configure(root)
                argv = pack.commands("fmt-check", files=["src/a.ts"])["argv"]
                self.assertEqual("--check" in argv, used, root)
            pack = TypeScriptPack()
            pack.configure(plain)
            self.assertIn("prettier is not configured in this project, so formatting is "
                          "not checked", pack.notes)

    def test_tsc_stays_tied_to_a_tsconfig(self) -> None:
        bin_dir = _tmp(self) / "bin"
        _fake_bin(bin_dir, "tsc")
        root = _tmp(self)
        _write(root, {"package.json": '{"name": "a"}\n'})
        with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:/usr/bin:/bin"}):
            pack = TypeScriptPack()
            pack.configure(root)
        self.assertEqual(pack.tooling["typecheck"], "syntax")
        self.assertIn("there is no tsconfig.json, so typecheck and lint check syntax only",
                      pack.notes)

    def test_the_recorded_choice_wins_over_what_is_installed(self) -> None:
        root = _git_repo(self, {"package.json": '{"name": "a"}\n', "tsconfig.json": "{}\n"})
        _fake_bin(root / "node_modules" / ".bin", "tsc")
        _adopted_cfg(self, root, {"typecheck": "syntax", "format": "off"})
        pack = TypeScriptPack()
        pack.configure(root)
        self.assertEqual(pack.tooling["typecheck"], "syntax")
        self.assertIn("-e", pack.commands("typecheck")["argv"])
        self.assertTrue(any('typescript_typecheck is "syntax"' in note for note in pack.notes))


class ConfigAndAdoptTests(unittest.TestCase):
    def test_runner_settings_parse_and_npm_is_refused_plainly(self) -> None:
        root = _tmp(self) / "r"
        root.mkdir()
        cfg = _adopted_cfg(self, root, {"typecheck": "tsc", "format": "prettier"})
        self.assertEqual((cfg.runner_typescript, cfg.typescript_typecheck,
                          cfg.typescript_format, cfg.link_node_modules),
                         ("node", "tsc", "prettier", True))
        text = (root / ".openfarm" / "config.toml").read_text()
        self.assertIn('typescript = "node"', text)
        self.assertIn('typescript_typecheck = "tsc"', text)
        self.assertIn('typescript_format = "prettier"', text)
        (root / ".openfarm" / "config.toml").write_text(
            text.replace('typescript = "node"', 'typescript = "npm"'))
        with self.assertRaises(ConfigError) as caught:
            load_config(root)
        self.assertIn("only with node --test", str(caught.exception))

    def test_adopt_refuses_a_test_script_that_is_not_node_test(self) -> None:
        root = _git_repo(self, {
            "package.json": '{"name": "a", "scripts": {"test": "vitest run"}}\n',
        })
        with self.assertRaises(Refused) as caught:
            adopt_project(root, state_dir=root.parent / "s", key_file=root.parent / "k")
        self.assertEqual(caught.exception.code, "unsupported-test-runner")
        self.assertIn("'vitest run'", caught.exception.message)
        self.assertIn("only with node --test", caught.exception.message)
        self.assertFalse((root / ".openfarm").exists())

    def test_adopt_records_the_tools_it_decided_and_says_so(self) -> None:
        root = _git_repo(self, {"package.json": '{"name": "a"}\n'})
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            result = adopt_project(root, state_dir=root.parent / "s",
                                   key_file=root.parent / "k")
        self.assertEqual(result["typescript"],
                         {"runner": "node --test", "typecheck": "syntax", "format": "off"})
        self.assertTrue(any("syntax only" in note for note in result["notes"]))
        text = (root / ".openfarm" / "config.toml").read_text()
        self.assertIn('typescript_typecheck = "syntax"', text)
        self.assertIn('typescript_format = "off"', text)

    def test_check_refuses_an_unsupported_test_script(self) -> None:
        root = _tmp(self) / "r"
        root.mkdir()
        cfg = _adopted_cfg(self, root)
        _write(root, {"package.json": '{"scripts": {"test": "jest"}}\n'})
        result = check_eligibility(_card(), cfg, worktree=root)
        rules = {f["rule"]: f["detail"] for f in result.failures}
        self.assertIn("typescript-test-runner", rules)
        self.assertIn("'jest'", rules["typescript-test-runner"])
        self.assertEqual(result.as_dict()["typescript"]["runner"], "node --test")

    def test_check_refuses_a_recorded_tool_that_is_missing(self) -> None:
        root = _tmp(self) / "r"
        root.mkdir()
        cfg = _adopted_cfg(self, root, {"typecheck": "tsc", "format": "prettier"})
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            result = check_eligibility(_card(), cfg, worktree=root)
        details = [f["detail"] for f in result.failures if f["rule"] == "typescript-tools"]
        self.assertEqual(len(details), 3, result.failures)  # no tsc, no tsconfig, no prettier


@unittest.skipUnless(shutil.which("git"), "needs git")
class NodeModulesLinkTests(unittest.TestCase):
    def _repo_with_modules(self, ignore: str = "node_modules/\n") -> Path:
        files = {"package.json": '{"name": "a", "type": "module"}\n',
                 "src/a.ts": "export const a = 1;\n"}
        if ignore:
            files[".gitignore"] = ignore
        root = _git_repo(self, files)
        _write(root, {"node_modules/pkg/index.js": "export const v = 1;\n",
                      "node_modules/@types/x/index.d.ts": "export {};\n"})
        return root

    def _worktree(self, root: Path) -> Path:
        worktree = root.parent / "wt"
        Git(root).worktree_add(worktree, "HEAD")
        return worktree

    def test_a_card_worktree_gets_links_that_git_never_sees(self) -> None:
        root = self._repo_with_modules()
        worktree = self._worktree(root)
        pack = TypeScriptPack(tsc=False, prettier=False)
        pack.configure(worktree)
        link = worktree / "node_modules" / "pkg"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), (root / "node_modules" / "pkg").resolve())
        self.assertTrue((worktree / "node_modules" / "@types" / "x").is_dir())
        git = Git(worktree)
        self.assertTrue(git.is_clean(), git.status_lines())
        self.assertEqual(git.changed_files(), [])
        pack.configure(worktree)  # a second configure (a resumed card) is harmless
        self.assertTrue(git.is_clean())
        Git(root).worktree_remove(worktree)
        self.assertFalse(worktree.exists())
        self.assertTrue((root / "node_modules" / "pkg" / "index.js").is_file())

    def test_no_link_when_git_does_not_ignore_node_modules(self) -> None:
        root = self._repo_with_modules(ignore="")
        # Untracked in the base, so git would see it: never linked.
        worktree = self._worktree(root)
        self.addCleanup(Git(root).worktree_remove, worktree)
        pack = TypeScriptPack(tsc=False, prettier=False)
        pack.configure(worktree)
        self.assertFalse((worktree / "node_modules").exists())
        self.assertTrue(any("git does not ignore it" in note for note in pack.notes))

    def test_the_setting_turns_the_link_off(self) -> None:
        root = self._repo_with_modules()
        _adopted_cfg(self, root)
        cfg_path = root / ".openfarm" / "config.toml"
        cfg_path.write_text(cfg_path.read_text().replace(
            "link_node_modules = true", "link_node_modules = false"))
        worktree = self._worktree(root)
        self.addCleanup(Git(root).worktree_remove, worktree)
        pack = TypeScriptPack(tsc=False, prettier=False)
        pack.configure(worktree)
        self.assertFalse((worktree / "node_modules").exists())
        self.assertIn("node_modules is not linked into card worktrees: [runner] "
                      "link_node_modules is false", pack.notes)

    def test_a_card_cannot_touch_the_base_through_the_link(self) -> None:
        root = self._repo_with_modules()
        cfg = _adopted_cfg(self, root)
        worktree = self._worktree(root)
        self.addCleanup(Git(root).worktree_remove, worktree)
        TypeScriptPack(tsc=False, prettier=False).configure(worktree)
        card = Card("C-1", "t", done_text="`v` returns `2`",
                    touches=["node_modules/pkg/index.js", "src/a.ts"],
                    fields={"category": "small-feature",
                            "tests": [{"name": "v", "file": "tests/a.test.ts"}]})
        rules = [f["rule"] for f in check_eligibility(card, cfg, worktree=worktree).failures]
        self.assertIn("forbidden-file", rules)
        self.assertIn("path-escape", rules)


BOUNDED = """\
export class FullError extends Error {}

export class Bounded {
  readonly capacity: number;
  readonly segments: number[] = [];

  constructor(capacity: number) {
    this.capacity = capacity;
  }

  push(value: number): number {
    this.segments.push(value);
    return this.segments.length - 1;
  }
}
"""

EXISTING_TEST = """\
import test from "node:test";
import assert from "node:assert/strict";
import { Bounded } from "../src/bounded.ts";

test("empty bundle has no segments", () => {
  assert.deepStrictEqual(new Bounded(3).segments, []);
});
"""

NEW_TEST = """\
import test from "node:test";
import assert from "node:assert/strict";
import { Bounded, FullError } from "../src/bounded.ts";

test("full bundle refuses a push", () => {
  const bundle = new Bounded(1);
  bundle.push(7);
  assert.throws(() => bundle.push(8), FullError);
});
"""

GOOD_IMPL = """\
=== search_replace: src/bounded.ts
<<< SEARCH
  push(value: number): number {
    this.segments.push(value);
>>> REPLACE
  push(value: number): number {
    if(this.segments.length>=this.capacity){throw new FullError("full")}
    this.segments.push(value);
"""

TYPE_ERROR_IMPL = """\
=== search_replace: src/bounded.ts
<<< SEARCH
  push(value: number): number {
    this.segments.push(value);
>>> REPLACE
  push(value: number): number {
    const limit: number = "one";
    if (this.segments.length >= this.capacity) {
      throw new FullError(`full at ${limit}`);
    }
    this.segments.push(value);
"""

TSCONFIG = """\
{
  "compilerOptions": {
    "target": "es2022",
    "module": "nodenext",
    "strict": true,
    "noEmit": true,
    "allowImportingTsExtensions": true,
    "types": ["node"],
    "skipLibCheck": true
  },
  "include": ["src", "tests"]
}
"""


@unittest.skipUnless(NODE and HAVE_TOOLS and shutil.which("git"),
                     f"needs node, git and typescript, prettier, @types/node in {TOOLS}")
class RealToolsEndToEndTests(unittest.TestCase):
    """adopt, check and a scripted card with real tsc and prettier, no network."""

    def _repo(self) -> tuple[Path, Path]:
        base = _tmp(self)
        root = base / "repo"
        state = base / "state"
        root.mkdir()
        _write(root, {
            "package.json": '{"name": "tally", "type": "module", "prettier": {}}\n',
            "tsconfig.json": TSCONFIG,
            ".gitignore": "node_modules/\n",
            "src/bounded.ts": BOUNDED,
            "tests/existing.test.ts": EXISTING_TEST,
        })
        modules = root / "node_modules"
        modules.mkdir()
        for entry in TOOLS.iterdir():
            os.symlink(entry, modules / entry.name)
        fmt = subprocess.run([str(modules / ".bin" / "prettier"), "--check", "src", "tests"],
                             cwd=root, capture_output=True, text=True)
        self.assertEqual(fmt.returncode, 0, fmt.stdout + fmt.stderr)
        git = Git(root)
        git.init(branch="main")
        git.commit_all("base")
        (base / "k").write_text("dummy\n")
        res = self._cli(None, "adopt", "--dir", str(root), "--state-dir", str(state),
                        "--key-file", str(base / "k"))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        adopted = json.loads(res.stdout)
        self.assertEqual(adopted["typescript"],
                         {"runner": "node --test", "typecheck": "tsc", "format": "prettier"})
        cfg_path = root / ".openfarm" / "config.toml"
        text = cfg_path.read_text()
        text = text.replace('model = "openai-compatible"', 'model = "scripted"')
        text = text.replace('micromanager = "claude-acp"', 'micromanager = "off"')
        text = text.replace('mode = "active"', 'mode = "off"')
        cfg_path.write_text(text)
        (root / "cards" / "CARD-1.md").write_text(textwrap.dedent("""\
            +++
            id = "CARD-1"
            title = "A full bundle refuses a push"
            state = "ready"
            category = "pinned-bugfix"
            unit = "."
            touches = ["src/bounded.ts"]
            depends = []
            tests = [{name = "full bundle refuses a push", file = "tests/full.test.ts", asserts = "push throws FullError"}]
            +++

            Calling `push` on a full `Bounded(1)` throws `FullError`.
        """))
        git.commit_all("adopt")
        return root, state

    def _cli(self, repo: Path | None, *args: str) -> subprocess.CompletedProcess:
        head = [sys.executable, str(OPENFARM)]
        if repo is not None:
            head += ["--repo", str(repo)]
        env = dict(os.environ)
        env["PATH"] = "/usr/bin:/bin:" + str(Path(NODE).parent)
        return subprocess.run([*head, "--json", *args], capture_output=True, text=True,
                              env=env)

    def _run_card(self, root: Path, impl: str) -> dict:
        script = root.parent / "script.json"
        script.write_text(json.dumps({
            "CARD-1/test_writer": "=== whole_file: tests/full.test.ts\n" + NEW_TEST,
            "CARD-1/implementer": impl,
            "CARD-1/checker_pass1": "RESTATEMENT: a full bundle refuses a push",
            "CARD-1/checker_pass2": "{}",
        }))
        res = self._cli(root, "run", "--card", "CARD-1", "--script", str(script),
                        "--cards", "1")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        return json.loads(res.stdout)["outcomes"][0]

    def _dossier(self, state: Path) -> str:
        run_dirs = sorted((state / "cards" / "CARD-1").iterdir())
        return (run_dirs[-1] / "dossier.md").read_text()

    def test_a_card_reaches_review_with_real_tsc_and_prettier(self) -> None:
        root, state = self._repo()
        check = self._cli(root, "check", "CARD-1")
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertTrue(json.loads(check.stdout)["eligible"])
        outcome = self._run_card(root, GOOD_IMPL)
        self.assertEqual(outcome["final_state"], "in-review",
                         f"{outcome.get('stop_code')}: {outcome.get('detail')}")
        branch = Git(root).branches("openfarm/CARD-1-*")[0]
        shown = subprocess.run(["git", "show", f"{branch}:src/bounded.ts"], cwd=root,
                               capture_output=True, text=True).stdout
        # Real prettier formatted the implementer's line on the branch.
        self.assertIn("    if (this.segments.length >= this.capacity) {\n"
                      '      throw new FullError("full");\n    }\n', shown)
        dossier = self._dossier(state)
        self.assertNotIn("syntax only", dossier)
        self.assertNotIn("formatting is not checked", dossier)

    def test_a_type_error_blocks_the_card(self) -> None:
        root, state = self._repo()
        outcome = self._run_card(root, TYPE_ERROR_IMPL)
        self.assertEqual(outcome["final_state"], "blocked", outcome)
        gates = sorted((state / "cards" / "CARD-1").iterdir())[-1] / "gates"
        lint = [json.loads(p.read_text()) for p in gates.rglob("lint*.json")]
        self.assertTrue(lint, list(gates.rglob("*.json")))
        self.assertTrue(any("TS2322" in str(g.get("detail", "")) for g in lint), lint)


if __name__ == "__main__":
    unittest.main()
