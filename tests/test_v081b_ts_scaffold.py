"""v0.8.1b ts, T4: a TypeScript scaffold holds only declarations and failing stubs.

Every function, method, accessor or arrow body is exactly one
`throw new Error(...)`, and a `search_replace` is judged on what it adds.
Runs in the normal suite; the node check is skipped without node.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.cards import Card
from openfarm_core.errors import Refused
from openfarm_core.micromanager import Micromanager, ScriptedMicromanager
from openfarm_core.packs.typescript import (
    typescript_scaffold_edit_problem,
    typescript_scaffold_problem,
)

NODE = shutil.which("node")

ACCEPTED = {
    "function stub": 'export function s(): string {\n  throw new Error("not implemented");\n}\n',
    "types": ('import type { B } from "./b";\nexport type T = { a: number } | string;\n'
              "export interface I<T> extends J { x: T; m(): void }\n"
              'type U =\n  | "a"\n  | "b";\n'),
    "class": (
        "export class Ring<T> implements I {\n"
        "  readonly cap: number;\n"
        "  count = 0;\n"
        "  neg = -1;\n"
        '  constructor(cap: number) { throw new Error("todo"); }\n'
        "  push(v: T): number { throw new Error(); }\n"
        '  get size(): number { throw new Error("x") }\n'
        '  set size(v: number) { throw new Error("x") }\n'
        "  static of<T>(xs: T[]): Ring<T> { throw new Error(`no`); }\n"
        "  [key: string]: unknown;\n"
        "  #secret?: string;\n"
        "  handler = (e: Event): void => { throw new Error(); };\n"
        "}\n"
        "export abstract class A { abstract f(): void; }\n"),
    "arrows and overloads": (
        'export const f = (a: number): number => { throw new Error("x"); };\n'
        "export const g = async <T,>(x: T) => { throw new Error() }\n"
        "export function h(a: string): void;\n"
        "export function h(a: unknown): void { throw new Error(); }\n"),
    "ambient and re-exports": (
        "declare const VERSION: string;\n"
        "declare global { var Z: number }\n"
        'export * from "./z.ts";\nexport { a as b } from "./a.ts";\n'
        "export default class {}\nlet later: number;\n"),
    "object return type": 'export function r(): { a: number } { throw new Error("x"); }\n',
}

REFUSED = {
    "logic beside a stub": (
        'export function slug(s: string): string { return s.toLowerCase().replace(/ /g, "-"); }\n'
        'export function other(): never { throw new Error("not implemented"); }\n'),
    "code after the throw": 'export function f() { throw new Error("x"); return 1; }\n',
    "a throw inside an if": 'export function f() { if (x) { throw new Error("x"); } }\n',
    "a module constant": "export const LIMIT = 10;\n",
    "an enum": "export enum C { A }\n",
    "a field set by a call": "export class X { m = evil(); }\n",
    "a static block": "export class X { static { evil(); } }\n",
    "a parameter property": "export class X { constructor(private a: number) { throw new Error(); } }\n",
    "a parameter default": "export function f(a = evil()) { throw new Error(); }\n",
    "a decorator": "@dec\nexport class X {}\n",
    "code after a type alias": "type A = B\nconsole.log(1)\n",
    "code after a declare": "declare const x: number\nevil()\n",
    "a computed base class": "export class X extends mixin(Base) {}\n",
    "an expression arrow": "export const f = () => evil();\n",
    "a substitution in the message": "export function f() { throw new Error(`a ${evil()}`); }\n",
    "a side-effect import": 'import "./side-effect";\n',
    "export default of a call": "export default evil();\n",
    "another error type": 'export function f() { throw new TypeError("x"); }\n',
    "a computed member": "export class X { [evil()]: number; }\n",
    "a namespace": "namespace N { evil(); }\n",
    "a bare call": "main();\n",
    "an empty scaffold": "",
}


def _card() -> Card:
    return Card(id="CARD-8", title="t", state="ready", done_text="d",
                touches=["src/slug.ts", "tests/slug.test.ts"],
                fields={"category": "feature", "new_symbols": True,
                        "tests": [{"name": "slug works", "file": "tests/slug.test.ts"}]})


def _scaffold(edit: dict) -> list:
    mm = Micromanager(ScriptedMicromanager(
        {"scaffold_contract": {"edits": [dict(edit, path="src/slug.ts")]}}))
    return mm.scaffold_contract(_card(), {}, {})


class ScaffoldGrammarTests(unittest.TestCase):
    def test_declarations_and_stubs_are_accepted(self) -> None:
        for label, text in ACCEPTED.items():
            with self.subTest(label):
                self.assertEqual(typescript_scaffold_problem(text), "")
                self.assertEqual(len(_scaffold({"format": "whole_file", "content": text})), 1)

    def test_anything_else_is_refused(self) -> None:
        for label, text in REFUSED.items():
            with self.subTest(label):
                self.assertNotEqual(typescript_scaffold_problem(text), "")
                if label == "a parameter default":
                    continue  # the grammar refuses it; the stub is repaired, see the test below
                with self.assertRaises(Refused) as caught:
                    _scaffold({"format": "whole_file", "content": text})
                self.assertEqual(caught.exception.code, "scaffold-missing-todo")

    def test_a_parameter_default_is_taken_out_of_the_stub_not_refused(self) -> None:
        # the v0.20 live run: the plan check had made `n` optional in the task, the stub came back with
        # `n: number = 3` anyway, was refused, and the task stopped three times over
        from openfarm_core.packs.typescript import typescript_without_param_defaults

        stub = ('// n = 3 by default\n'
                'export function pickNotable(entries: WikiEntry[], n: number = 3): Notable[] {\n'
                '  throw new Error("not = implemented");\n}\n'
                'export async function get(kind: "a" | "b", opts: { ua?: string } = {}, f: typeof fetch = fetch, sep = ","): '
                'Promise<Map<string, number>> { throw new Error("no"); }\n')
        [edit] = _scaffold({"format": "whole_file", "content": stub})
        self.assertIn("pickNotable(entries: WikiEntry[], n?: number): Notable[]", edit.content)
        self.assertIn('get(kind: "a" | "b", opts?: { ua?: string }, f?: typeof fetch, sep?): Promise<Map<string, number>>',
                      edit.content)
        self.assertIn("// n = 3 by default", edit.content)  # comments and strings are left alone
        self.assertIn('"not = implemented"', edit.content)
        self.assertEqual(typescript_scaffold_problem(edit.content), "")
        [edit] = _scaffold({"format": "whole_file", "content": "export function f(a = evil()) { throw new Error(); }\n"})
        self.assertEqual(edit.content, "export function f(a?) { throw new Error(); }\n")  # the code that ran is gone
        self.assertEqual(typescript_without_param_defaults("export const f = (a: number): void => { throw new Error('x'); };"),
                         "export const f = (a: number): void => { throw new Error('x'); };")
        # the same run, next task: `void name;` lines before the throw, to quiet an unused-name warning
        [edit] = _scaffold({"format": "whole_file", "content": (
            'import { escapeHtml } from "./html.ts";\n'
            'export function renderPage(input: Input): string {\n  void escapeHtml;\n  void input;\n'
            '  throw new Error("not implemented");\n}\n')})
        self.assertEqual(edit.content, 'import { escapeHtml } from "./html.ts";\n'
                                       'export function renderPage(input: Input): string {\n'
                                       '  throw new Error("not implemented");\n}\n')
        with self.assertRaises(Refused):  # a call is not a `void name;`
            _scaffold({"format": "whole_file", "content": "export function f() {\n  void evil();\n  throw new Error();\n}\n"})
        with self.assertRaises(Refused):  # anything else wrong with it is still refused
            _scaffold({"format": "whole_file", "content": "export function f(a = 1) { return a; }\n"})


class SearchReplaceTests(unittest.TestCase):
    CLASS = "export class Ring {\n  push(v: number): void { throw new Error(); }\n"

    def test_a_member_added_to_an_existing_interface_is_a_declaration(self) -> None:
        # v0.20 live run: `home?: (now: Date) => Promise<string>;` added to an interface was refused as
        # "a statement starting 'home'", the stub was dropped and the task stopped
        from openfarm_core.packs.typescript import typescript_scaffold_edit_problem

        before = "export interface ServerDeps {\n  now: () => Date;\n}\n"
        after = "export interface ServerDeps {\n  now: () => Date;\n  home?: (now: Date) => Promise<string>;\n}\n"
        self.assertEqual(typescript_scaffold_edit_problem("search_replace", before, after, None), "")
        sneaky = "export interface ServerDeps {\n  now: () => Date;\n  x: number;\n}\nevil();\ninterface Y {\n}\n"
        self.assertNotEqual(typescript_scaffold_edit_problem("search_replace", before, sneaky, None), "")
        code = "export function f() {\n  go();\n}\n"
        self.assertNotEqual(typescript_scaffold_edit_problem("search_replace", code, code.replace("go();", "go();\n  more();"), None), "")

    def test_rewriting_existing_logic_is_refused(self) -> None:
        with self.assertRaises(Refused):
            _scaffold({"format": "search_replace", "search": "return a + b;",
                       "replace": "return a - b; if (a < 0) { throw new Error('x'); }"})

    def test_only_what_is_added_is_judged(self) -> None:
        search = "export function add(a: number, b: number): number {\n  return a + b;\n}\n"
        replace = search + "\nexport function sub(a: number, b: number): number {\n" \
                           '  throw new Error("not implemented");\n}\n'
        self.assertEqual(len(_scaffold({"format": "search_replace", "search": search,
                                        "replace": replace})), 1)
        with self.assertRaises(Refused):
            _scaffold({"format": "search_replace", "search": search,
                       "replace": search + "export const n = add(1, 2);\n"})

    def test_members_added_inside_a_class(self) -> None:
        edit = typescript_scaffold_edit_problem(
            "search_replace", self.CLASS,
            self.CLASS + "  pop(): number { throw new Error(); }\n  size = 0;\n", None)
        self.assertEqual(edit, "")
        refused = typescript_scaffold_edit_problem(
            "search_replace", self.CLASS, self.CLASS + "  pop(): number { return 1; }\n", None)
        self.assertIn("throw new Error", refused)

    def test_member_shapes_outside_a_class_are_statements(self) -> None:
        # At the top level `deleteAll(db)` is a call and `x: evil()` a labelled one.
        top = "export const a = 1;\n"
        for added in ("deleteAll(db)\n", "x: evil()\n", "size = 0;\n"):
            with self.subTest(added):
                self.assertNotEqual(
                    typescript_scaffold_edit_problem("search_replace", top, top + added,
                                                     None), "")

    def test_an_insertion_inside_a_statement_is_refused(self) -> None:
        problem = typescript_scaffold_edit_problem(
            "search_replace", "const x =\n  1;\n",
            "const x =\nfunction f() { throw new Error(); }\n  1;\n", None)
        self.assertEqual(problem, "it is inserted in the middle of a statement")

    def test_changing_a_stub_line_is_judged_whole(self) -> None:
        old = "export function f(a: number): void { throw new Error(); }\n"
        new = "export function f(a: string): void { throw new Error(); }\n"
        self.assertEqual(typescript_scaffold_edit_problem("search_replace", old, new, None), "")
        self.assertNotEqual(typescript_scaffold_edit_problem(
            "search_replace", old, "export function f(a: string): void { go(); }\n", None), "")


@unittest.skipUnless(NODE, "needs node")
class NodeAgreesTests(unittest.TestCase):
    """Each accepted scaffold is TypeScript node strips and loads with nothing run."""

    def test_accepted_scaffolds_load_under_node(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="openfarm-tss-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, root, True)
        for name in ("b", "z", "a"):
            (root / f"{name}.ts").write_text("export const a = 1;\nexport type B = 1;\n")
        for index, (label, text) in enumerate(ACCEPTED.items()):
            with self.subTest(label):
                path = root / f"s{index}.ts"
                path.write_text(text.replace("extends J ", ""))
                done = subprocess.run(
                    [NODE, "--input-type=module", "-e",
                     f"await import({str(path)!r}); console.log('loaded');"],
                    capture_output=True, text=True, cwd=root)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(done.stdout.strip(), "loaded")


if __name__ == "__main__":
    unittest.main()
