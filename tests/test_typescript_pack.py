"""The TypeScript pack: real Node v26 `node --test`, TAP/spec parsing, files,
signatures, stubs, patterns, eligibility, and end-to-end `adopt` + `run`.
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

from openfarm_core.adopt import adopt_config_text, adopt_project
from openfarm_core.bench import _suite_cmd, load_task
from openfarm_core.cards import Card
from openfarm_core.config import load_config
from openfarm_core.errors import Invalid, Refused
from openfarm_core.gates.checks import banned_patterns, suite_green, test_loss
from openfarm_core.gates.eligibility import _units_of, check_eligibility
from openfarm_core.git import Git
from openfarm_core.packs.base import pack_for
from openfarm_core.packs.typescript import (
    TypeScriptPack,
    detect_runner,
    test_script_problem,
)

OPENFARM = Path(__file__).resolve().parent.parent / "openfarm"


def _project(files: dict[str, str], cleanup_owner: unittest.TestCase | None = None) -> Path:
    root = Path(tempfile.mkdtemp(prefix="openfarm-ts-", dir=os.environ.get("TMPDIR")))
    if cleanup_owner is not None:
        cleanup_owner.addCleanup(shutil.rmtree, root, True)
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(content), encoding="utf-8")
    return root


def _run(argv: list[str], cwd: Path) -> tuple[int, str, str]:
    result = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


MIXED_TS_TESTS = """\
import test, { describe, it } from "node:test";
import assert from "node:assert/strict";

test("passes math", () => {
  assert.strictEqual(1 + 2, 3);
});

test("fails math", () => {
  assert.strictEqual(1 + 2, 4);
});

test("skipped case", { skip: "later" }, () => {
  assert.strictEqual(1, 2);
});

test("todo case", { todo: true }, () => {});

describe("suite", () => {
  it("runs inside describe", () => {
    assert.ok(true);
  });
});
"""


class NodeTestParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _project({
            "package.json": '{"name": "demo", "type": "module"}\n',
            "tests/mixed.test.ts": MIXED_TS_TESTS,
        }, self)
        self.pack = TypeScriptPack(tsc=False, prettier=False)
        self.pack.configure(self.root)

    def test_real_tap_output_parses_all_statuses_and_suite(self) -> None:
        _, out, err = _run(self.pack.commands("test")["argv"], self.root)
        parsed = self.pack.parse_tests(out, err)
        statuses = {r["name"]: r["status"] for r in parsed["results"]}
        self.assertEqual(statuses, {
            "passes math": "ok",
            "fails math": "FAILED",
            "skipped case": "ignored",
            "todo case": "ignored",
            "runs inside describe": "ok",
        })
        self.assertEqual(parsed["missing_footers"], [])
        self.assertEqual(parsed["compile_errors"], [])
        self.assertEqual(parsed["footers"][0]["failed"], 1)
        self.assertEqual(parsed["footers"][0]["ignored"], 2)
        self.assertEqual(suite_green(parsed, []).result, "fail")
        self.assertEqual(suite_green(parsed, ["fails math"]).result, "pass")

    def test_named_tests_filter_with_regex_escaping(self) -> None:
        root = _project({
            "tests/pat.test.ts": """\
                import test from "node:test";
                import assert from "node:assert/strict";
                test("adds (1 + 2)", () => assert.ok(true));
                test("adds (1 + 2) extra", () => assert.strictEqual(1, 2));
            """,
        }, self)
        pack = TypeScriptPack(tsc=False, prettier=False)
        pack.configure(root)
        argv = pack.commands("test", tests=["adds (1 + 2)"])["argv"]
        rc, out, err = _run(argv, root)
        self.assertEqual(rc, 0, out + err)
        parsed = pack.parse_tests(out, err)
        self.assertEqual(
            [(r["name"], r["status"]) for r in parsed["results"]],
            [("adds (1 + 2)", "ok")],
        )
        self.assertEqual(parsed["missing_footers"], [])

    def test_empty_project_finishes_cleanly(self) -> None:
        root = _project({"package.json": '{"name": "empty"}\n'}, self)
        pack = TypeScriptPack(tsc=False, prettier=False)
        pack.configure(root)
        rc, out, err = _run(pack.commands("test")["argv"], root)
        self.assertEqual(rc, 0, err)
        parsed = pack.parse_tests(out, err)
        self.assertEqual(parsed["results"], [])
        self.assertEqual(parsed["missing_footers"], [])
        self.assertEqual(parsed["footers"][0]["failed"], 0)

    def test_real_spec_output_parses_all_statuses(self) -> None:
        _, out, err = _run(
            ["node", "--test", "tests/mixed.test.ts"], self.root,
        )
        parsed = self.pack.parse_tests(out, err)
        statuses = {r["name"]: r["status"] for r in parsed["results"]}
        self.assertEqual(statuses["passes math"], "ok")
        self.assertEqual(statuses["fails math"], "FAILED")
        self.assertEqual(statuses["skipped case"], "ignored")
        self.assertEqual(statuses["todo case"], "ignored")
        self.assertEqual(statuses["runs inside describe"], "ok")
        self.assertEqual(parsed["missing_footers"], [])
        self.assertEqual(parsed["footers"][0]["failed"], 1)
        # Ensure the `✖ failing tests:` section does not duplicate `fails math`.
        self.assertEqual(
            sum(1 for r in parsed["results"] if r["name"] == "fails math"), 1,
        )

    def test_missing_footer_is_infra(self) -> None:
        aborted_tap = "TAP version 13\nok 1 - one test\nKilled\n"
        parsed = self.pack.parse_tests(aborted_tap)
        self.assertEqual(parsed["missing_footers"], ["node:test"])
        self.assertEqual(suite_green(parsed, []).result, "infra")

        aborted_top_plan = "TAP version 13\n1..3\nok 1 - one test\n"
        parsed_top = self.pack.parse_tests(aborted_top_plan)
        self.assertEqual(parsed_top["missing_footers"], ["node:test"])
        self.assertEqual(suite_green(parsed_top, []).result, "infra")

        aborted_spec = "✔ one test (0.5ms)\n"
        parsed_spec = self.pack.parse_tests(aborted_spec)
        self.assertEqual(parsed_spec["missing_footers"], ["node:test"])
        self.assertEqual(suite_green(parsed_spec, []).result, "infra")


class CompileAndListTests(unittest.TestCase):
    def test_syntax_and_missing_import_produce_compile_errors(self) -> None:
        root = _project({
            "package.json": '{"name": "demo", "type": "module"}\n',
            "tests/bad_syntax.test.ts": "export function broken(: number { return 1; }\n",
            "tests/bad_import.test.ts": 'import { x } from "./nope.ts";\n',
        }, self)
        pack = TypeScriptPack(tsc=False, prettier=False)
        pack.configure(root)
        _, out, err = _run(pack.commands("test")["argv"], root)
        parsed = pack.parse_tests(out, err)
        self.assertTrue(parsed["compile_errors"], out + err)
        gate = suite_green(parsed, [])
        self.assertEqual(gate.result, "fail")
        self.assertIn("did not compile", gate.detail)

    def test_ts_error_text_in_output_is_compile_error(self) -> None:
        pack = TypeScriptPack(tsc=False, prettier=False)
        parsed = pack.parse_tests(
            "",
            "src/a.ts(2,5): error TS2322: Type 'string' is not assignable to type 'number'.\n",
        )
        self.assertTrue(parsed["compile_errors"])
        self.assertEqual(suite_green(parsed, []).result, "fail")

    def test_runtime_syntax_error_inside_test_is_not_compile_error(self) -> None:
        root = _project({
            "tests/json.test.ts": """\
                import test from "node:test";
                test("throws SyntaxError at runtime", () => {
                  JSON.parse("{bad");
                });
            """,
        }, self)
        pack = TypeScriptPack(tsc=False, prettier=False)
        pack.configure(root)
        _, out, err = _run(pack.commands("test")["argv"], root)
        parsed = pack.parse_tests(out, err)
        self.assertEqual(parsed["compile_errors"], [])
        self.assertEqual(parsed["results"][0]["status"], "FAILED")

    def test_list_walks_repo_skips_vendor_and_catches_syntax_errors(self) -> None:
        root = _project({
            "node_modules/pkg/ignored.test.ts": 'test("ignored vendor", () => {});\n',
            "dist/built.test.js": 'test("ignored dist", () => {});\n',
            "tests/a.test.ts": """\
                import test, { describe, it } from "node:test";
                // test("commented out", () => {});
                test("alpha", () => {});
                describe("grp", () => {
                  it('beta', () => {});
                });
                test.todo("gamma");
            """,
            "src/b.spec.mts": 'import { it } from "node:test";\nit(`delta`, () => {});\n',
            "tests/broken.test.ts": "export const x: number = (;\n",
        }, self)
        pack = TypeScriptPack(tsc=False, prettier=False)
        pack.configure(root)
        rc, out, err = _run(pack.commands("list")["argv"], root)
        self.assertEqual(rc, 0, err)
        listing = pack.parse_list(out)
        names = [t["name"] for t in listing["tests"]]
        self.assertEqual(names, ["delta", "alpha", "beta", "gamma"])
        self.assertNotIn("commented out", names)
        self.assertNotIn("ignored vendor", names)
        self.assertNotIn("ignored dist", names)
        self.assertEqual(len(listing.get("errors", [])), 1)
        self.assertIn("tests/broken.test.ts", listing["errors"][0])

        # Moving a test to another file is caught by test_loss.
        moved = {"tests": [
            {"name": "alpha", "binary": "tests/other.test.ts"},
            *listing["tests"][1:],
        ]}
        self.assertEqual(test_loss(listing, moved).result, "fail")


class RunnerAndToolingTests(unittest.TestCase):
    def test_detect_runner_npm_vs_node(self) -> None:
        # node --test is the only runner; a script that isn't one is told plainly.
        plain = _project({"package.json": '{"name": "a"}\n'}, self)
        self.assertEqual(detect_runner(plain), "node")
        self.assertEqual(test_script_problem(plain), "")

        node_script = _project({
            "package.json": '{"scripts": {"test": "tsc && node --test"}}\n',
            "node_modules/x/index.js": "",
        }, self)
        self.assertEqual(detect_runner(node_script), "node")
        self.assertEqual(test_script_problem(node_script), "")

        custom_npm = _project({
            "package.json": '{"scripts": {"test": "vitest run"}}\n',
            "node_modules/vitest/index.js": "",
        }, self)
        self.assertEqual(detect_runner(custom_npm), "node")
        problem = test_script_problem(custom_npm)
        self.assertIn("'vitest run'", problem)
        self.assertIn("only with node --test", problem)

    def test_configure_notes_and_local_bins(self) -> None:
        root = _project({"package.json": '{"name": "a"}\n'}, self)
        pack = TypeScriptPack()
        pack.configure(root)
        if shutil.which("tsc") is None:
            self.assertIn(
                "tsc is not installed, so typecheck and lint check syntax only",
                pack.notes,
            )

        with_bins = _project({
            "package.json": '{"name": "a", "prettier": {}}\n',
            "tsconfig.json": "{}\n",
            "node_modules/.bin/tsc": "#!/bin/sh\n",
            "node_modules/.bin/prettier": "#!/bin/sh\n",
        }, self)
        pack2 = TypeScriptPack()
        pack2.configure(with_bins)
        self.assertTrue(pack2.has_tsc)
        self.assertTrue(pack2.has_prettier)
        self.assertEqual(pack2.notes, [])
        self.assertIn("--noEmit", pack2.commands("typecheck")["argv"])
        self.assertIn("--check", pack2.commands("fmt-check", files=["a.ts"])["argv"])

    def test_npm_and_tool_commands(self) -> None:
        pack_npm = TypeScriptPack(runner="npm", tsc=True, prettier=True)
        with self.assertRaises(Invalid) as npm:
            pack_npm.commands("test", tests=["adds"], files=["tests/a.test.ts"])
        self.assertEqual(npm.exception.code, "unsupported-test-runner")
        self.assertEqual(
            pack_npm.commands("format", files=["src/a.ts"])["argv"],
            ["prettier", "--write", "src/a.ts"],
        )
        pack_plain = TypeScriptPack(tsc=False, prettier=False)
        self.assertEqual(
            pack_plain.commands("fmt-check", files=["src/a.ts"])["argv"],
            ["node", "-e", ""],
        )
        with self.assertRaises(Invalid) as caught:
            pack_plain.commands("mutation", files=["src/a.ts"])
        self.assertEqual(caught.exception.code, "no-typescript-mutation")
        with self.assertRaises(Invalid):
            pack_plain.parse_mutation("")

    def test_node_syntax_check_fallback(self) -> None:
        good = _project({
            "src/math.ts": "export function add(a: number, b: number): number { return a + b; }\n",
            "src/util.mjs": "export const y = 2;\n",
        }, self)
        bad_ts = _project({"src/bad.ts": "export function f(: number {}\n"}, self)
        bad_js = _project({"src/bad.js": "const x = (;\n"}, self)
        pack = TypeScriptPack(tsc=False, prettier=False)
        for p in ("build", "typecheck", "lint", "ws-lint"):
            rc, out, err = _run(pack.commands(p)["argv"], good)
            self.assertEqual(rc, 0, f"{p}: {out}{err}")
        rc_ts, _, err_ts = _run(pack.commands("lint")["argv"], bad_ts)
        self.assertEqual(rc_ts, 1)
        self.assertIn("SyntaxError", err_ts)
        rc_js, _, err_js = _run(pack.commands("typecheck")["argv"], bad_js)
        self.assertEqual(rc_js, 1)
        self.assertIn("SyntaxError", err_js)


class FilesSignaturesStubsAndPatternsTests(unittest.TestCase):
    def test_classify_paths(self) -> None:
        pack = TypeScriptPack(tsc=False, prettier=False)
        self.assertEqual(
            pack.classify_paths([
                "src/math.ts",
                "lib/index.mts",
                "tests/math.test.ts",
                "src/math.test.ts",
                "src/math.spec.mts",
                "src/__tests__/helper.ts",
                "package.json",
                "README.md",
            ]),
            {
                "src/math.ts": "production",
                "lib/index.mts": "production",
                "tests/math.test.ts": "test",
                "src/math.test.ts": "test",
                "src/math.spec.mts": "test",
                "src/__tests__/helper.ts": "test",
                "package.json": "other",
                "README.md": "other",
            },
        )

    def test_signatures_preserve_contracts_and_strip_bodies(self) -> None:
        root = _project({
            "src/mod.ts": """\
                export interface Options {
                  readonly limit: number;
                  name?: string;
                }

                export type Result = "ok" | "err";

                export enum Mode {
                  Fast = 1,
                  Slow = 2,
                }

                export const DEFAULT_LIMIT: number = 10;
                const secretPrivate = 999;

                export function add(a: number, b: number): number {
                  const secretSum = a + b;
                  return secretSum;
                }

                export async function fetchItem({ id }: { id: string }): Promise<{ ok: boolean }> {
                  const secretFetch = id;
                  return { ok: true };
                }

                export const mul = (a: number, b: number): number => a * b;

                export class Box<T> {
                  readonly cap: number;
                  private items: T[] = [];

                  constructor(cap: number) {
                    const secretInit = cap;
                    this.cap = secretInit;
                  }

                  push(item: T): number {
                    const secretPush = 123;
                    this.items.push(item);
                    return this.items.length;
                  }
                }
            """,
        }, self)
        pack = TypeScriptPack(tsc=False, prettier=False)
        sigs = "\n\n".join(pack.signatures(root / "src/mod.ts")["signatures"])
        for expected in (
            "export interface Options",
            "readonly limit: number;",
            'export type Result = "ok" | "err";',
            "export enum Mode",
            "export const DEFAULT_LIMIT: number = 10;",
            "export function add(a: number, b: number): number { ... }",
            "export async function fetchItem({ id }: { id: string }): Promise<{ ok: boolean }> { ... }",
            "export const mul = (a: number, b: number): number => { ... };",
            "export class Box<T> {",
            "private items: T[];",
            "constructor(cap: number) { ... }",
            "push(item: T): number { ... }",
        ):
            self.assertIn(expected, sigs)
        for hidden in (
            "secretPrivate",
            "secretSum",
            "secretFetch",
            "a * b",
            "secretInit",
            "secretPush",
            "this.items.push",
        ):
            self.assertNotIn(hidden, sigs)

    def test_append_stub_validation(self) -> None:
        root = _project({"src/a.ts": "export function one(): number { return 1; }\n"}, self)
        pack = TypeScriptPack(tsc=False, prettier=False)
        with self.assertRaises(Refused) as c1:
            pack.append_stub(root / "src/a.ts", "t", "export function two() { return 2; }")
        self.assertEqual(c1.exception.code, "non-panicking-stub")

        with self.assertRaises(Refused) as c2:
            pack.append_stub(root / "src/a.ts", "t", 'export function two() { throw new Error("x");')
        self.assertEqual(c2.exception.code, "non-panicking-stub")

        with self.assertRaises(Refused) as c3:
            pack.append_stub(root / "src/a.ts", "t", '// throw new Error("x")\nexport function two() {}')
        self.assertEqual(c3.exception.code, "non-panicking-stub")

        res = pack.append_stub(
            root / "src/a.ts",
            "t",
            'export function two(): number {\n  throw new Error("not implemented");\n}\n',
        )
        self.assertGreater(res["line"], 1)
        rc, _, err = _run(pack.commands("lint")["argv"], root)
        self.assertEqual(rc, 0, err)

    def test_patterns_and_production_only_stubs(self) -> None:
        pack = TypeScriptPack(tsc=False, prettier=False)

        def labels(path: str, lines: list[str]) -> list[str]:
            added = [{"path": path, "line": i, "text": t} for i, t in enumerate(lines, 1)]
            return [b["pattern"] for b in pack.patterns(added)["banned"]]

        self.assertEqual(
            labels("tests/a.test.ts", [
                "const a = 1; // @ts-ignore",
                "// @ts-nocheck",
                "/* eslint-disable no-console */",
                "debugger;",
                'test.skip("a", () => {});',
                'it.only("b", () => {});',
                'test.todo("c");',
                'import { exec } from "node:child_process";',
                'await fetch("https://example.invalid");',
                'eval("1+1");',
                'new Function("return 1");',
            ]),
            [
                "suppression-ts-ignore",
                "suppression-ts-nocheck",
                "suppression-eslint",
                "debug-call",
                "test-skipped",
                "test-only",
                "test-todo",
                "process-call",
                "network-call",
                "dynamic-exec",
                "dynamic-exec",
            ],
        )

        # Leftover stubs banned in production, allowed in tests; real domain Errors allowed!
        self.assertEqual(
            labels("src/a.ts", [
                'throw new Error("not implemented");',
                "throw new Error('TODO: finish');",
                'throw new Error("negative amount");',
            ]),
            ["leftover-stub", "leftover-stub"],
        )
        self.assertEqual(
            labels("tests/a.test.ts", ['throw new Error("not implemented");']),
            [],
        )
        # Strings containing suppressed/debug words do not false-positive.
        self.assertEqual(
            labels("src/a.ts", [
                'const hint = "never use @ts-ignore or debugger or eval(";',
                "// eval('ok in comment');",
            ]),
            [],
        )

        # Gate count comparison works cleanly with TypeScriptPack.
        class Cfg:
            flagged_patterns = ["setTimeout\\("]

        added = [{"path": "src/a.ts", "line": 1, "text": "setTimeout(() => {}, 10); // @ts-ignore"}]
        same = banned_patterns(
            added, Cfg(), {"src/a.ts"},
            base_counts={"src/a.ts": {"suppression-ts-ignore": 1}},
            head_counts={"src/a.ts": {"suppression-ts-ignore": 1}},
            pack=pack,
        )
        self.assertEqual(same.result, "pass")
        self.assertEqual(same.evidence["flagged_count"], 2)  # 1 flagged + 1 pre-existing

    def test_error_signature_normalises_volatile_parts(self) -> None:
        pack = TypeScriptPack(tsc=False, prettier=False)
        a = (
            "not ok 1 - bounded push rejects overflow\n"
            "  location: '/tmp/wt-1/tests/bounded.test.ts:12:5'\n"
            "  AssertionError [ERR_ASSERTION]: 1 !== 2\n"
            "  duration_ms: 1.23ms\n"
        )
        b = (
            a.replace("/tmp/wt-1", "/home/u/wt-2")
            .replace(":12:5", ":99:1")
            .replace("1.23ms", "45.67ms")
        )
        sig_a = pack.error_signature(a)
        sig_b = pack.error_signature(b)
        self.assertEqual(sig_a["signature"], sig_b["signature"])
        self.assertIn("ERR_ASSERTION", sig_a["codes"])
        other = a.replace("ERR_ASSERTION", "ERR_INVALID_ARG_TYPE")
        self.assertNotEqual(sig_a["signature"], pack.error_signature(other)["signature"])


class WiringTests(unittest.TestCase):
    def test_pack_for_and_eligibility_and_config_and_bench(self) -> None:
        for alias in ("typescript", "ts", "javascript", "js"):
            self.assertIsInstance(pack_for(alias), TypeScriptPack)

        root = _project({
            "package.json": '{"name": "widget"}\n',
            "src/a.ts": "export const x = 1;\n",
            "tests/a.test.ts": "",
        }, self)
        self.assertEqual(_units_of(["src/a.ts", "tests/a.test.ts"], root), {"."})

        cfg_text = adopt_config_text(
            root,
            "typescript",
            base_branch="main",
            state_dir=root.parent / "ts-state",
            key_file=root.parent / "ts.key",
        )
        (root / ".openfarm").mkdir()
        (root / ".openfarm/config.toml").write_text(cfg_text, encoding="utf-8")
        cfg = load_config(root)
        self.assertEqual(cfg.languages, ["typescript"])

        def card(test_file: str) -> Card:
            return Card(
                "C-1",
                "t",
                done_text="`f(1)` returns `2`",
                touches=["src/a.ts", test_file],
                fields={
                    "category": "small-feature",
                    "tests": [{"name": "test_f", "file": test_file}],
                },
            )

        for bad_test in ("src/helper.ts", "src/__tests__/a.ts", "tests/a.test.tsx"):
            # Never given to node --test, so a test named there could never be proven.
            bad = check_eligibility(card(bad_test), cfg)
            self.assertIn("typescript-tests-in-test-files",
                          [f["rule"] for f in bad.failures], bad_test)
        for valid_test in (
            "tests/a.test.ts",
            "src/a.test.ts",
            "src/a.spec.mts",
            "src/__tests__/a.test.ts",
        ):
            good = check_eligibility(card(valid_test), cfg)
            self.assertNotIn(
                "typescript-tests-in-test-files",
                [f["rule"] for f in good.failures],
                valid_test,
            )

        task_dir = _project({
            "task.toml": (
                'id = "ts-demo"\n'
                'language = "typescript"\n'
                'category = "pinned-bugfix"\n'
                'card = "card.md"\n'
                'hidden_tests = ["hidden/tests/h.test.ts"]\n'
            ),
            "card.md": "# card\n",
            "hidden/tests/h.test.ts": 'import test from "node:test";\n',
        }, self)
        task = load_task(task_dir)
        self.assertEqual(task.language, "typescript")
        # Scored by the pack's own run and parser, never by a bare node --test.
        with self.assertRaises(Invalid):
            _suite_cmd("typescript")


COUNTER_TS = """\
export class CounterError extends Error {}

export class Counter {
  value = 0;

  add(amount: number): number {
    if (amount < 0) {
      throw new CounterError("negative amount");
    }
    this.value += amount;
    return this.value;
  }
}
"""

BOUNDED_TS = """\
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

SEAM_TS = """\
export function sealedGate(): number {
  return 41;
}
"""

EXISTING_TS_TEST = """\
import test from "node:test";
import assert from "node:assert/strict";
import { Bounded } from "../src/bounded.ts";

test("empty bundle has no segments", () => {
  assert.deepStrictEqual(new Bounded(3).segments, []);
});
"""

GOOD_TS_TEST = """\
import test from "node:test";
import assert from "node:assert/strict";
import { Bounded, FullError } from "../src/bounded.ts";

test("full bundle refuses a push", () => {
  const bundle = new Bounded(1);
  bundle.push(7);
  assert.throws(() => bundle.push(8), FullError);
});
"""

GOOD_TS_IMPL = """\
=== search_replace: src/bounded.ts
<<< SEARCH
  push(value: number): number {
    this.segments.push(value);
    return this.segments.length - 1;
  }
>>> REPLACE
  push(value: number): number {
    if (this.segments.length >= this.capacity) {
      throw new FullError(`capacity ${this.capacity} reached`);
    }
    this.segments.push(value);
    return this.segments.length - 1;
  }
"""

BANNED_TS_IMPL = """\
=== search_replace: src/bounded.ts
<<< SEARCH
  push(value: number): number {
    this.segments.push(value);
    return this.segments.length - 1;
  }
>>> REPLACE
  push(value: number): number {
    // @ts-ignore
    eval("1 + 1");
    this.segments.push(value);
    return this.segments.length - 1;
  }
"""

BASE_PASSING_TS_TEST = """\
import test from "node:test";
import assert from "node:assert/strict";
import { Bounded } from "../src/bounded.ts";

test("full bundle refuses a push", () => {
  assert.strictEqual(new Bounded(1).capacity, 1);
});
"""


@unittest.skipUnless(shutil.which("node") and shutil.which("git"),
                     "real node --test end to end with a scripted model; needs node and git")
class TypeScriptE2ETests(unittest.TestCase):
    def _build_repo(self) -> tuple[Path, Path, Path]:
        base = Path(tempfile.mkdtemp(prefix="openfarm-ts-e2e-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, base, True)
        root = base / "repo"
        state = base / "state"
        key = base / "demo.key"
        root.mkdir()
        (root / "package.json").write_text(
            '{\n  "name": "tally-ts",\n  "version": "0.1.0",\n  "type": "module"\n}\n',
            encoding="utf-8",
        )
        (root / "src").mkdir()
        (root / "tests").mkdir()
        (root / "src" / "counter.ts").write_text(COUNTER_TS, encoding="utf-8")
        (root / "src" / "bounded.ts").write_text(BOUNDED_TS, encoding="utf-8")
        (root / "src" / "seam.ts").write_text(SEAM_TS, encoding="utf-8")
        (root / "tests" / "existing.test.ts").write_text(EXISTING_TS_TEST, encoding="utf-8")
        git = Git(root)
        git.init(branch="main")
        git.commit_all("tally-ts: base")
        return root, state, key

    def _run_cli(self, repo: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(OPENFARM), "--repo", str(repo), "--json", *args],
            capture_output=True, text=True,
        )

    def _prepare_adopted_repo_with_card(self, *, via_cli: bool = False) -> tuple[Path, Path]:
        root, state, key = self._build_repo()
        key.write_text("dummy-key\n", encoding="utf-8")
        if via_cli:
            res = subprocess.run(
                [
                    sys.executable, str(OPENFARM), "--json", "adopt",
                    "--dir", str(root), "--state-dir", str(state), "--key-file", str(key),
                ],
                capture_output=True, text=True,
            )
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            adopted = json.loads(res.stdout)
        else:
            adopted = adopt_project(root, state_dir=state, key_file=key, run_suite=True)
        self.assertEqual(adopted["language"], "typescript")
        self.assertTrue(adopted["suite_ran"])
        self.assertEqual(adopted["known_failing_proposed"], [])

        # Switch [adapters] model to scripted + [micromanager] mode = off for offline E2E.
        cfg_path = root / ".openfarm" / "config.toml"
        cfg_text = cfg_path.read_text(encoding="utf-8")
        cfg_text = cfg_text.replace('model = "openai-compatible"', 'model = "scripted"')
        cfg_text = cfg_text.replace('micromanager = "claude-acp"', 'micromanager = "off"')
        cfg_text = cfg_text.replace('mode = "active"', 'mode = "off"')
        cfg_path.write_text(cfg_text, encoding="utf-8")

        card_text = textwrap.dedent("""\
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
        """)
        (root / "cards" / "CARD-1.md").write_text(card_text, encoding="utf-8")
        Git(root).commit_all("adopt and add CARD-1")
        return root, state

    def _run_card(self, root: Path, test_content: str, impl_text: str) -> dict:
        script_path = root.parent / "script.json"
        script_path.write_text(json.dumps({
            "CARD-1/test_writer": "=== whole_file: tests/full.test.ts\n" + test_content,
            "CARD-1/implementer": impl_text,
            "CARD-1/checker_pass1": "RESTATEMENT: a full bundle refuses a push",
            "CARD-1/checker_pass2": "{}",
        }), encoding="utf-8")
        res = self._run_cli(
            root, "run", "--card", "CARD-1", "--script", str(script_path), "--cards", "1",
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        return json.loads(res.stdout)["outcomes"][0]

    def test_adopt_check_and_run_card_end_to_end(self) -> None:
        root, state = self._prepare_adopted_repo_with_card(via_cli=True)
        check = self._run_cli(root, "check", "CARD-1")
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertTrue(json.loads(check.stdout)["eligible"], check.stdout)

        outcome = self._run_card(root, GOOD_TS_TEST, GOOD_TS_IMPL)
        self.assertEqual(
            outcome["final_state"], "in-review",
            f"{outcome.get('stop_code')}: {outcome.get('detail')}",
        )
        run_dirs = list((state / "cards" / "CARD-1").iterdir())
        dossier = (run_dirs[0] / "dossier.md").read_text(encoding="utf-8")
        self.assertIn("Judged by openfarm", dossier)

        branch = subprocess.run(
            ["git", "branch", "--list", "openfarm/CARD-1*", "--format=%(refname:short)"],
            cwd=root, capture_output=True, text=True,
        ).stdout.split()[0]
        shown = subprocess.run(
            ["git", "show", f"{branch}:src/bounded.ts"],
            cwd=root, capture_output=True, text=True,
        )
        self.assertIn("throw new FullError", shown.stdout)
        main_blob = subprocess.run(
            ["git", "show", "main:src/bounded.ts"],
            cwd=root, capture_output=True, text=True,
        )
        self.assertEqual(main_blob.stdout, BOUNDED_TS)

    def test_banned_pattern_and_base_passing_test_are_blocked(self) -> None:
        root1, state1 = self._prepare_adopted_repo_with_card()
        out_banned = self._run_card(root1, GOOD_TS_TEST, BANNED_TS_IMPL)
        self.assertEqual(out_banned["final_state"], "blocked")
        run_dirs = list((state1 / "cards" / "CARD-1").iterdir())
        dossier = (run_dirs[0] / "dossier.md").read_text(encoding="utf-8")
        self.assertIn("suppression-ts-ignore", json.dumps(out_banned) + dossier)

        root2, _ = self._prepare_adopted_repo_with_card()
        out_unprovable = self._run_card(root2, BASE_PASSING_TS_TEST, GOOD_TS_IMPL)
        self.assertEqual(out_unprovable["final_state"], "blocked")
        self.assertEqual(out_unprovable["stop_code"], "tests-unprovable")

    def test_typescript_brief_runner_hint_and_scaffold_validation(self) -> None:
        from openfarm_core.briefs import BriefBook, test_writer_brief
        from openfarm_core.micromanager import Micromanager, ScriptedMicromanager

        root = _project({
            "package.json": json.dumps({"name": "app", "type": "module", "scripts": {"test": "node --test --test-reporter=tap"}}),
            "src/slug.ts": "export const x = 1;\n",
        }, self)
        card = Card(
            id="TS-1",
            title="Add slug",
            state="ready",
            done_text="adds slug",
            touches=["src/slug.ts"],
            fields={
                "category": "small-feature",
                "unit": ".",
                "new_symbols": True,
                "tests": [{"name": "adds slug", "file": "tests/slug.test.ts", "asserts": "x == 1"}],
            },
        )
        pack = TypeScriptPack()
        pack.configure(root)
        brief = test_writer_brief(card, BriefBook(root, [], []), pack).body
        self.assertIn("node:test", brief)
        self.assertIn('throw new Error("not implemented")', brief)

        mm = Micromanager(ScriptedMicromanager({
            "scaffold_contract": {
                "edits": [{
                    "format": "whole_file",
                    "path": "src/slug.ts",
                    "content": 'export function s(): string {\n  throw new Error("not implemented");\n}\n',
                }],
            },
        }))
        edits = mm.scaffold_contract(card, {}, {})
        self.assertEqual([e.path for e in edits], ["src/slug.ts"])

        mm_bad = Micromanager(ScriptedMicromanager({
            "scaffold_contract": {
                "edits": [{
                    "format": "whole_file",
                    "path": "src/slug.ts",
                    "content": 'export function s(): string { return "implemented"; }\n',
                }],
            },
        }))
        with self.assertRaises(Refused) as caught:
            mm_bad.scaffold_contract(card, {}, {})
        self.assertEqual(caught.exception.code, "scaffold-missing-todo")


if __name__ == "__main__":
    unittest.main()
