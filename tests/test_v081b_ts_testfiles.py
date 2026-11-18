"""v0.8.1b ts, T6: one rule for what a TypeScript test file is.

Eligibility, `classify_paths`, discovery and the list script agree, so a card
that passes `check` names a test node really runs and the list really sees.
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
from openfarm_core.config import load_config
from openfarm_core.config_template import render_config
from openfarm_core.gates.eligibility import check_eligibility
from openfarm_core.packs.typescript import TypeScriptPack, is_test_file

NODE = shutil.which("node")
BODY = 'import test from "node:test";\ntest("NAME", () => { throw new Error("red"); });\n'

CASES = {
    # path: (a test file, why not)
    "tests/ok.test.ts": True,
    "tests/math_test.ts": True,
    "src/math.spec.mts": True,
    "lib/old.test.cjs": True,
    "src/__tests__/helper.ts": False,
    "tests/plain.ts": False,
    "tests/view.test.tsx": False,
    "src/legacy.spec.jsx": False,
}


def _tmp(owner: unittest.TestCase) -> Path:
    root = Path(tempfile.mkdtemp(prefix="openfarm-tsf-", dir=os.environ.get("TMPDIR")))
    owner.addCleanup(shutil.rmtree, root, True)
    return root


def _cfg(root: Path):
    (root / ".openfarm").mkdir(parents=True, exist_ok=True)
    (root / ".openfarm" / "config.toml").write_text(render_config(
        name="t", language="typescript", base_branch="main",
        state_dir=root.parent / "state", key_file=root.parent / "k",
        typescript_tools={"typecheck": "syntax", "format": "off"}))
    return load_config(root)


def _card(path: str) -> Card:
    return Card("C-1", "t", done_text="`f()` returns `1`", touches=["src/f.ts"],
                fields={"category": "small-feature",
                        "tests": [{"name": path, "file": path}]})


class OneRuleTests(unittest.TestCase):
    def test_the_predicate(self) -> None:
        for path, want in CASES.items():
            with self.subTest(path=path):
                self.assertEqual(is_test_file(path), want)

    def test_eligibility_uses_it(self) -> None:
        root = _tmp(self) / "r"
        root.mkdir()
        cfg = _cfg(root)
        for path, want in CASES.items():
            with self.subTest(path=path):
                failures = {f["rule"]: f["detail"] for f in
                            check_eligibility(_card(path), cfg, worktree=root).failures}
                self.assertEqual("typescript-tests-in-test-files" not in failures, want,
                                 failures)
        tsx = {f["rule"]: f["detail"] for f in check_eligibility(
            _card("tests/view.test.tsx"), cfg, worktree=root).failures}
        self.assertIn("node --test cannot run", tsx["typescript-tests-in-test-files"])
        plain = {f["rule"]: f["detail"] for f in check_eligibility(
            _card("tests/plain.ts"), cfg, worktree=root).failures}
        self.assertIn("*.test.*, *.spec.* or *_test.*",
                      plain["typescript-tests-in-test-files"])

    def test_test_folders_stay_the_test_writers(self) -> None:
        kinds = TypeScriptPack(tsc=False, prettier=False).classify_paths(
            list(CASES) + ["src/f.ts", "src/view.tsx", "README.md"])
        for path in CASES:
            self.assertEqual(kinds[path], "test", path)
        self.assertEqual(kinds["src/f.ts"], "production")
        self.assertEqual(kinds["src/view.tsx"], "production")
        self.assertEqual(kinds["README.md"], "other")


@unittest.skipUnless(NODE, "needs node")
class DiscoveryAndListAgreeTests(unittest.TestCase):
    def test_eligible_listed_and_run_are_the_same_files(self) -> None:
        root = _tmp(self) / "r"
        for rel in CASES:
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            body = BODY.replace("NAME", rel)
            if rel.endswith(".cjs"):
                body = body.replace('import test from "node:test";',
                                    'const test = require("node:test");')
            target.write_text(body)
        (root / "package.json").write_text('{"name": "r", "type": "module"}\n')
        pack = TypeScriptPack(tsc=False, prettier=False)
        pack.configure(root)
        listing = subprocess.run(pack.commands("list")["argv"], cwd=root,
                                 capture_output=True, text=True)
        listed = {t["name"] for t in pack.parse_list(listing.stdout)["tests"]}
        run = subprocess.run(pack.commands("test")["argv"], cwd=root,
                             capture_output=True, text=True)
        ran = {r["name"] for r in pack.parse_tests(run.stdout, run.stderr)["results"]}
        wanted = {path for path, want in CASES.items() if want}
        self.assertEqual(listed, wanted)
        self.assertEqual(ran, wanted)
        self.assertEqual(set(pack.test_files()), wanted)


if __name__ == "__main__":
    unittest.main()
