"""v0.8.1b ts, T5: the TypeScript banned patterns close the reviewer's gaps.

Each Python rule has its TypeScript counterpart; ordinary code near them stays
allowed.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.packs.typescript import TypeScriptPack

SRC = "src/a.ts"
TEST = "tests/a.test.ts"

CAUGHT = [
    (SRC, "// @ts-expect-error", "suppression-ts-expect-error"),
    (SRC, "/* @ts-expect-error: wrong on purpose */", "suppression-ts-expect-error"),
    (SRC, "/* c8 ignore next */", "suppression-coverage"),
    (SRC, "/* istanbul ignore next */", "suppression-coverage"),
    (TEST, 'test("old", { skip: true }, () => {});', "test-skipped"),
    (TEST, 'test("old", { todo: true }, () => {});', "test-todo"),
    (TEST, 'it("old", { skip: "flaky" }, () => {});', "test-skipped"),
    (TEST, 'describe("group", { skip: process.env.CI }, () => {});', "test-skipped"),
    (TEST, 'suite("group", { only: true }, () => {});', "test-only"),
    (TEST, 'test("odd", { expectFailure: true }, () => {});', "test-skipped"),
    (TEST, "t.todo();", "test-todo"),
    (TEST, "describe.todo(\"later\");", "test-todo"),
    (SRC, 'export function f(): number { throw Error("not implemented"); }', "leftover-stub"),
    (SRC, 'export function f(): number { throw new Error("Not yet implemented"); }',
     "leftover-stub"),
    (SRC, 'export function f(): number { throw new TypeError("not implemented"); }',
     "leftover-stub"),
    (SRC, "export function f(): number { throw new Error(`unimplemented`); }",
     "leftover-stub"),
    (SRC, 'import http from "node:http";', "network-call"),
    (SRC, 'import https from "https";', "network-call"),
    (SRC, 'import { connect } from "node:net";', "network-call"),
    (SRC, 'const dgram = require("dgram");', "network-call"),
    (SRC, 'const tls = await import("node:tls");', "network-call"),
    (SRC, "const ws = new WebSocket(url);", "network-call"),
    (SRC, 'const cp = await import("node:child_" + "process");', "dynamic-exec"),
    (SRC, "const m = require(name);", "dynamic-exec"),
    (SRC, 'const r = (0, eval)("1+1");', "dynamic-exec"),
    (SRC, 'const g = Function("return 1")();', "dynamic-exec"),
    (SRC, 'const g = new Function("return 1")();', "dynamic-exec"),
    (SRC, 'import vm from "node:vm";', "dynamic-exec"),
    (SRC, 'globalThis["fetch"]("http://x");', "dynamic-exec"),
    (SRC, 'import inspector from "node:inspector";', "debug-call"),
    (SRC, "try { f(); } catch {}", "swallowed-exception"),
    (SRC, "try { f(); } catch (err) { }", "swallowed-exception"),
    (SRC, "p.catch(() => {});", "swallowed-exception"),
    (SRC, "p.catch((e) => undefined);", "swallowed-exception"),
    (SRC, 'process.binding("spawn_sync");', "process-call"),
]

ALLOWED = [
    (SRC, "// a ts-expect-error would hide this, so none"),
    (TEST, 'test("runs", { skip: false }, () => {});'),
    (TEST, "const page = list({ skip: 10, take: 5 });"),
    (TEST, "  skip: 10,"),
    (SRC, "const options = { skip: true };"),
    (SRC, 'const scheme = "http";'),
    (SRC, 'import { get } from "./http.ts";'),
    (SRC, 'const m = await import("./plugin.ts");'),
    (SRC, "type M = typeof import(\"./m.ts\");"),
    (SRC, "const self = this; self[key] = 1;"),
    (SRC, "const out = evaluate(expr);"),
    (SRC, "function callFunction(f: Function) { return f(); }"),
    (SRC, "try { f(); } catch (err) { log(err); }"),
    (SRC, "p.catch((e) => report(e));"),
    (SRC, 'throw new Error("negative values are not allowed");'),
    (TEST, 'throw new Error("not implemented");'),
]


def _scan(lines: list[tuple[str, int, str]]) -> list[str]:
    pack = TypeScriptPack(tsc=False, prettier=False)
    found = pack.patterns([{"path": p, "line": n, "text": t} for p, n, t in lines], [], None)
    return [entry["pattern"] for entry in found["banned"]]


class BannedPatternTests(unittest.TestCase):
    def test_each_gap_is_caught(self) -> None:
        for path, text, label in CAUGHT:
            with self.subTest(text=text):
                self.assertIn(label, _scan([(path, 1, text)]))

    def test_ordinary_code_stays_allowed(self) -> None:
        for path, text in ALLOWED:
            with self.subTest(text=text):
                self.assertEqual(_scan([(path, 1, text)]), [])

    def test_a_stub_throw_split_across_lines(self) -> None:
        for lines in (["throw new Error(", '  "not implemented",', ");"],
                      ["throw new Error(", '  "not implemented");'],
                      ["throw (", '  new Error("TODO")', ");"]):
            with self.subTest(lines=lines):
                self.assertIn("leftover-stub",
                              _scan([(SRC, n + 1, t) for n, t in enumerate(lines)]))
        # Not consecutive: a different statement in between.
        self.assertEqual(_scan([(SRC, 1, "throw new Error("), (SRC, 5, '"not implemented"')]),
                         [])

    def test_an_empty_catch_split_across_lines(self) -> None:
        self.assertIn("swallowed-exception",
                      _scan([(SRC, 1, "} catch (err) {"), (SRC, 2, "}")]))
        self.assertEqual(_scan([(SRC, 1, "} catch (err) {"), (SRC, 2, "  log(err);"),
                                (SRC, 3, "}")]), [])

    def test_test_options_on_their_own_line(self) -> None:
        self.assertIn("test-skipped", _scan([(TEST, 1, 'test("old", {'),
                                            (TEST, 2, "  skip: true,"),
                                            (TEST, 3, "}, () => {});")]))
        self.assertIn("test-todo", _scan([(TEST, 1, '  todo: "later",')]))


if __name__ == "__main__":
    unittest.main()
