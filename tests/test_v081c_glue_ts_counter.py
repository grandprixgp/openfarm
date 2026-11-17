"""v0.8.1c glue, G4: counter-tests through the TypeScript pack, with real node.

A TypeScript card whose scripted diff breaks a line of its Done text. The checker's
second pass answers in the new form with counter-tests; they are run alone by the
pack's own `node --test` and the worktree comes back exactly as it was. The project
links the base checkout's git-ignored `node_modules` into the card worktree, and the
source imports a package from it, so the link is really used by every run.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adapters.direct import DirectRunner
from openfarm_core.adapters.files import open_card_source
from openfarm_core.adapters.jsonl import MemoryTelemetry
from openfarm_core.adapters.openai_compat import ScriptedBackend
from openfarm_core.adopt import adopt_config_text
from openfarm_core.briefs import BriefBook
from openfarm_core.config import Config, load_config
from openfarm_core.dossier import build_dossier, render_markdown
from openfarm_core.flow import CardRun
from openfarm_core.git import Git
from openfarm_core.packs.base import pack_for
from openfarm_core.state import StateDir

NODE = shutil.which("node")
COUNTER_TEST_HEADER = "=== counter_test:"

CLAMP_TS = """\
import { describeRange } from "rangekit";

/** Keep `value` within `lo`..`hi`, both ends included. */
export function clamp(value: number, lo: number, hi: number): number {
  void describeRange(lo, hi);
  return value;
}
"""

#: A package the source imports, reached only through the linked `node_modules`.
RANGEKIT = {
    "node_modules/rangekit/package.json":
        '{"name": "rangekit", "type": "module", "exports": "./index.js"}\n',
    "node_modules/rangekit/index.js":
        "export function describeRange(lo, hi) { return `${lo}..${hi}`; }\n",
    "node_modules/@scope/util/index.js": "export const util = 1;\n",
}

EXISTING_TEST = """\
import test from "node:test";
import assert from "node:assert/strict";
import { clamp } from "../src/clamp.ts";

test("a value inside the range is kept", () => {
  assert.equal(clamp(2, 0, 3), 2);
});
"""

NAMED_TEST = """\
import test from "node:test";
import assert from "node:assert/strict";
import { clamp } from "../src/clamp.ts";

test("a value above the range becomes the top", () => {
  assert.equal(clamp(5, 0, 3), 3);
});
"""

#: The planted bug: only the top of the range is enforced.
BUGGY = """\
=== search_replace: src/clamp.ts
<<< SEARCH
  return value;
>>> REPLACE
  return value > hi ? hi : value;
"""

FIXED = """\
=== search_replace: src/clamp.ts
<<< SEARCH
  return value;
>>> REPLACE
  return value > hi ? hi : value < lo ? lo : value;
"""

DONE = """\
- `clamp(value, lo, hi)` never returns a number outside `lo..hi`: `clamp(5, 0, 3)` \
is `3` and `clamp(-1, 0, 3)` is `0`.
- A value inside the range is returned unchanged.
"""


def counter_file(title: str, given: str, wanted: str) -> str:
    return textwrap.dedent(f"""\
        import test from "node:test";
        import assert from "node:assert/strict";
        import {{ clamp }} from "../src/clamp.ts";

        test("{title}", () => {{
          assert.equal(clamp({given}, 0, 3), {wanted});
        }});
        """)


BELOW = "a value below the range becomes the bottom"
INSIDE = "a value at the bottom is kept"
FAILING = (BELOW, counter_file(BELOW, "-1", "0"))
PASSING = (INSIDE, counter_file(INSIDE, "0", "0"))
#: Does not parse: node's type stripping refuses it.
UNPARSABLE = ("does not parse", 'import test from "node:test";\ntest("x", () => {{{\n')


def form_answer(tests=(), verdict: str = "changes-requested") -> str:
    parts = [f"VERDICT: {verdict}", "", "REASONS:",
             "`clamp` enforces only the top of the range.", "", "TRACEABILITY:",
             "| never outside lo..hi | `value > hi ? hi : value` | fails below lo |", "",
             "EDGE CASES:", "1. clamp(-1, 0, 3): -1 > 3 is false, so -1.",
             "2. clamp(5, 0, 3): 5 > 3, so 3.", ""]
    for name, content in tests:
        parts += [f"{COUNTER_TEST_HEADER} {name}", content]
    return "\n".join(parts)


def tree_state(root: Path) -> dict:
    """Every entry under `root` (ignored ones too, `.git` aside): files by hash,
    links by target, folders as folders. Links are never followed."""
    entries: dict[str, str] = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        rel_dir = Path(current).relative_to(root)
        if rel_dir.parts[:1] == (".git",):
            continue
        for name in list(dirs):
            rel = rel_dir / name
            if rel.parts[:1] == (".git",):
                continue
            full = Path(current) / name
            entries[str(rel)] = ("link:" + os.readlink(full) if full.is_symlink()
                                 else "dir")
        for name in files:
            full = Path(current) / name
            rel = str(rel_dir / name)
            if rel == ".git":
                continue
            entries[rel] = ("link:" + os.readlink(full) if full.is_symlink()
                            else hashlib.sha256(full.read_bytes()).hexdigest())
    return entries


def git_state(worktree: Path) -> dict:
    git = Git(worktree)
    return {"head": git.head(), "branch": git.current_branch(),
            "status": git._run(["status", "--porcelain", "--ignored", "-uall"]).stdout}


@unittest.skipUnless(NODE and shutil.which("git"), "needs node and git")
class TypeScriptCounterTests(unittest.TestCase):
    def setUp(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="of-v081c-ts-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, base, True)
        self.base = base
        root = base / "repo"
        root.mkdir()
        files = {
            "package.json": '{"name": "clampkit", "type": "module"}\n',
            ".gitignore": "node_modules/\n",
            "src/clamp.ts": CLAMP_TS,
            "tests/existing.test.ts": EXISTING_TEST,
        }
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        git = Git(root)
        git.init(branch="main")
        git.commit_all("clampkit: base")
        # Installed packages live only in the base checkout, ignored by git.
        for rel, text in RANGEKIT.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        (root / "node_modules" / ".bin").mkdir()
        os.symlink("../rangekit/index.js", root / "node_modules" / ".bin" / "rangekit")
        state = base / "state"
        text = adopt_config_text(root, "typescript", base_branch="main",
                                 state_dir=state, key_file=base / "k")
        (root / ".openfarm").mkdir(exist_ok=True)
        (root / ".openfarm" / "config.toml").write_text(text)
        (root / "cards").mkdir(exist_ok=True)
        (root / "cards" / "CARD-1.md").write_text(textwrap.dedent("""\
            +++
            id = "CARD-1"
            title = "clamp keeps a value within its range"
            state = "ready"
            category = "pinned-bugfix"
            unit = "."
            touches = ["src/clamp.ts"]
            depends = []
            tests = [{name = "a value above the range becomes the top", file = "tests/above.test.ts", asserts = "clamp(5, 0, 3) is 3"}]
            +++

            """) + DONE)
        git.commit_all("adopt and CARD-1")
        self.root = root
        self.state_dir = state
        self.git = git
        self.snapshots: list[dict] = []
        self._seq = 0

    def card_run(self, script: dict, *, counter_tests: str = "report") -> CardRun:
        self._seq += 1
        state = StateDir(self.state_dir)
        data = dict(load_config(self.root)._data)
        adapters = dict(data.get("adapters") or {})
        adapters.update({"model": "scripted", "micromanager": "off"})
        gates = dict(data.get("gates") or {})
        gates.update({"counter_tests": counter_tests, "planner": "off"})
        data.update({"adapters": adapters, "gates": gates})
        micromanager = dict(data.get("micromanager") or {})
        micromanager["mode"] = "off"
        data["micromanager"] = micromanager
        cfg = Config(data, self.root)
        run_id = f"run-v081c-ts-{self._seq}"
        state.create_run(run_id)
        layout = state.create_card("CARD-1", run_id)
        source = open_card_source(cfg, self.state_dir)
        source.prepare()
        card = next(c for c in source.all_cards() if c.id == "CARD-1")
        worktree = self.base / f"wt-{run_id}"
        branch = f"of/{run_id}/CARD-1"
        self.git.create_branch(branch, "main")
        self.git.worktree_add(worktree, branch)
        self.addCleanup(self.git.worktree_remove, worktree)
        pack = pack_for("typescript")
        pack.apply_settings(cfg)
        pack.configure(worktree)
        book = BriefBook(worktree, cfg.never_touch, cfg.forbidden_files)
        book.pack = pack
        run = CardRun(card, cfg, state, layout, worktree, DirectRunner(worktree),
                      pack, ScriptedBackend(script), book, run_id, MemoryTelemetry())
        real = run._run_counter_tests

        def watched(tests):
            before = (tree_state(run.worktree), git_state(run.worktree),
                      tree_state(self.root / "node_modules"))
            try:
                return real(tests)
            finally:
                self.snapshots.append({
                    "before": before,
                    "after": (tree_state(run.worktree), git_state(run.worktree),
                              tree_state(self.root / "node_modules"))})

        run._run_counter_tests = watched
        return run

    @staticmethod
    def script(pass2, implementer=BUGGY, **extra) -> dict:
        script = {
            "CARD-1/test_writer": "=== whole_file: tests/above.test.ts\n" + NAMED_TEST,
            "CARD-1/implementer": implementer,
            "CARD-1/checker_pass1": "RESTATEMENT: clamp never returns a value outside lo..hi",
            "CARD-1/checker_pass2": pass2,
        }
        script.update(extra)
        return script

    def assert_worktree_and_base_untouched(self, run: CardRun) -> None:
        self.assertTrue(self.snapshots, "no counter-test ran")
        for snap in self.snapshots:
            self.assertEqual(snap["before"][0], snap["after"][0])
            self.assertEqual(snap["before"][1], snap["after"][1])
            # The base checkout's node_modules: nothing changed, nothing removed.
            self.assertEqual(snap["before"][2], snap["after"][2])
        links = run.worktree / "node_modules"
        self.assertTrue((links / "rangekit").is_symlink())
        self.assertEqual((links / "rangekit").resolve(),
                         (self.root / "node_modules" / "rangekit").resolve())
        self.assertTrue((self.root / "node_modules" / "rangekit" / "index.js").is_file())

    def test_the_card_uses_the_linked_node_modules(self) -> None:
        run = self.card_run(self.script(form_answer([FAILING])))
        run.run()
        self.assertTrue((run.worktree / "node_modules" / "rangekit").is_symlink())

    def test_a_failing_and_a_passing_counter_test_are_recorded(self) -> None:
        run = self.card_run(self.script(form_answer([FAILING, PASSING])))
        result = run.run()
        self.assertEqual(result["final_state"], "in-review", result.get("detail"))
        summary = result["counter_tests"]
        self.assertEqual((summary["written"], summary["failed"], summary["passed"],
                          summary["dropped_uncompiled"], summary["not_run"]),
                         (2, 1, 1, 0, 0), run.checker_pass2["counter_results"])
        outcomes = run.checker_pass2["counter_results"]
        self.assertEqual([(r["name"], r["outcome"]) for r in outcomes],
                         [(BELOW, "failed"), (INSIDE, "passed")])
        self.assertTrue(outcomes[0]["file"].endswith(".test.ts"), outcomes[0]["file"])
        kept = json.loads((run.card_layout / "gates" / "counter-test-1.json").read_text())
        self.assertEqual(kept["outcome"], "failed")
        # Its output is kept: node's assertion names the actual and expected values.
        self.assertIn("-1", kept["output"])
        self.assertIn("not ok", kept["output"])
        self.assertTrue(outcomes[0]["output_head"])
        self.assert_worktree_and_base_untouched(run)
        shown = self.git._run(["log", "--name-only", "--format=",
                               f"main..of/{run.run_id}/CARD-1"]).stdout
        self.assertNotIn("openfarm_counter", shown)
        _, text = (lambda p: (p, render_markdown(p)))(build_dossier(run, result["final_state"]))
        self.assertIn(f"`{BELOW}`", text)
        self.assertIn("**failed on the diff**", text)
        self.assertIn(f"`{INSIDE}`", text)

    def test_a_counter_test_that_does_not_parse_is_dropped(self) -> None:
        run = self.card_run(self.script(form_answer([UNPARSABLE, FAILING])))
        result = run.run()
        self.assertEqual(result["final_state"], "in-review", result.get("detail"))
        outcomes = run.checker_pass2["counter_results"]
        self.assertEqual([r["outcome"] for r in outcomes], ["dropped", "failed"],
                         outcomes)
        self.assertEqual(result["counter_tests"]["dropped_uncompiled"], 1)
        self.assert_worktree_and_base_untouched(run)

    def test_reject_bounces_the_card_and_the_fixed_diff_reaches_review(self) -> None:
        run = self.card_run(self.script(
            form_answer([FAILING, PASSING]), implementer=BUGGY,
            **{"CARD-1/implementer/2": FIXED}), counter_tests="reject")
        result = run.run()
        self.assertEqual(result["final_state"], "in-review", result.get("detail"))
        impl = [c for c in run.backend.calls if c["role"] == "implementer"]
        self.assertEqual(len(impl), 2)
        self.assertIn(BELOW, impl[1]["messages"][0]["content"])
        self.assertIn("value < lo ? lo : value",
                      (run.worktree / "src" / "clamp.ts").read_text())
        self.assertEqual([r["outcome"] for r in run.checker_pass2["counter_results"]],
                         ["passed", "passed"])
        self.assert_worktree_and_base_untouched(run)

    def test_a_counter_test_that_breaks_the_links_leaves_nothing_behind(self) -> None:
        """Code under test can remove, re-point or replace the links. The worktree
        still comes back exactly, and the base's packages are never touched."""
        def tamper(title: str, code: str) -> tuple[str, str]:
            return (title, 'import fs from "node:fs";\n' + code + "\n"
                    + counter_file(title, "-1", "0"))

        cases = [
            tamper("removes a link", 'fs.unlinkSync("node_modules/rangekit");'),
            tamper("re-points a link",
                   'fs.unlinkSync("node_modules/rangekit");\n'
                   'fs.symlinkSync("../src", "node_modules/rangekit");'),
            tamper("replaces a link with a folder",
                   'fs.unlinkSync("node_modules/@scope");\n'
                   'fs.mkdirSync("node_modules/@scope/util", {recursive: true});\n'
                   'fs.writeFileSync("node_modules/@scope/util/index.js", "x");'),
            tamper("removes the whole folder",
                   'fs.rmSync("node_modules", {recursive: true, force: true});'),
            tamper("adds a file and a link",
                   'fs.writeFileSync("node_modules/.cache", "x");\n'
                   'fs.symlinkSync("../src", "node_modules/extra");'),
        ]
        for title, content in cases:
            with self.subTest(title):
                self.snapshots.clear()
                run = self.card_run(self.script(form_answer([(title, content)])))
                result = run.run()
                self.assertEqual(result["final_state"], "in-review", result.get("detail"))
                self.assertEqual(run.checker_pass2["counter_results"][0]["outcome"],
                                 "failed", run.checker_pass2["counter_results"])
                self.assert_worktree_and_base_untouched(run)
                self.assertTrue((run.worktree / "node_modules" / ".openfarm-linked")
                                .is_file())

    def test_reject_blocks_when_the_counter_test_still_fails(self) -> None:
        run = self.card_run(self.script(form_answer([FAILING])), counter_tests="reject")
        result = run.run()
        self.assertEqual(result["final_state"], "blocked")
        self.assert_worktree_and_base_untouched(run)


if __name__ == "__main__":
    unittest.main()
