"""v0.8.1b ts, T8: signatures survive a regex after a keyword, and keep a class's
decorators."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.packs.typescript import TypeScriptPack, _TSScanner, _has_balanced_braces


def _sigs(owner: unittest.TestCase, text: str) -> list[str]:
    root = Path(tempfile.mkdtemp(prefix="openfarm-tsg-", dir=os.environ.get("TMPDIR")))
    owner.addCleanup(shutil.rmtree, root, True)
    path = root / "a.ts"
    path.write_text(text)
    return TypeScriptPack(tsc=False, prettier=False).signatures(path)["signatures"]


class RegexAfterKeywordTests(unittest.TestCase):
    def test_the_reviewers_file(self) -> None:
        sigs = _sigs(self, (
            "export function isBrace(s: string): boolean {\n"
            "  return /\\{/.test(s);\n"
            "}\n"
            "export function second(x: number): number { return x; }\n"
            "export class Third { run(): void {} }\n"))
        self.assertEqual(sigs[0], "export function isBrace(s: string): boolean { ... }")
        self.assertIn("export function second(x: number): number { ... }", sigs)
        self.assertTrue(any(s.startswith("export class Third") for s in sigs), sigs)

    def test_every_keyword_that_can_precede_a_regex(self) -> None:
        for word in ("return", "typeof", "case", "do", "else", "in", "of", "new",
                     "delete", "void", "throw", "yield", "await", "instanceof"):
            with self.subTest(word):
                masked = _TSScanner().mask_line(f"  {word} /{{/;")
                self.assertNotIn("{", masked)
                self.assertTrue(_has_balanced_braces(f"function f() {{ {word} /{{/; }}"))

    def test_a_property_named_like_a_keyword_is_still_division(self) -> None:
        masked = _TSScanner().mask_line("const h = o.return / 2 / 4;")
        self.assertEqual(masked, "const h = o.return / 2 / 4;")
        masked = _TSScanner().mask_line("const k = returned / 2 / 4;")
        self.assertEqual(masked, "const k = returned / 2 / 4;")

    def test_later_declarations_are_kept(self) -> None:
        sigs = _sigs(self, (
            "export function a(s: string): boolean { return s.length > 2 && /[{]/.test(s); }\n"
            "export function b(s: string) {\n  if (s) return /{/.test(s);\n  return false;\n}\n"
            "export function c(): number { return 3; }\n"))
        self.assertEqual([s.split("(")[0] for s in sigs],
                         ["export function a", "export function b", "export function c"])


class DecoratorTests(unittest.TestCase):
    def test_decorators_before_a_class_are_kept(self) -> None:
        sigs = _sigs(self, (
            "export const before = 1;\n"
            '@Component({ selector: "x" })\n'
            "@Injectable()\n"
            "export class Widget {\n"
            "  @Input() value: string = \"secret\";\n"
            "  run(): void { this.secret(); }\n"
            "}\n"
            "@Other({\n  multi: true,\n})\n"
            "export class Second {}\n"))
        widget = next(s for s in sigs if "class Widget" in s)
        self.assertTrue(widget.startswith('@Component({ selector: "x" })\n@Injectable()\n'
                                          "export class Widget {"), widget)
        self.assertNotIn("secret", widget)
        second = next(s for s in sigs if "class Second" in s)
        self.assertTrue(second.startswith("@Other({\n  multi: true,\n})\nexport class Second"),
                        second)

    def test_a_decorator_not_before_a_class_is_not_carried(self) -> None:
        sigs = _sigs(self, "@odd()\nconst x = 1;\nexport class Plain {}\n")
        plain = next(s for s in sigs if "class Plain" in s)
        self.assertNotIn("@odd", plain)


if __name__ == "__main__":
    unittest.main()
