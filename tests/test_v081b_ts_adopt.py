"""v0.8.1b ts, T10: a repo v0.8 adopted as Rust or Python is adopted the same way
again, and the TypeScript template forbids the other lock and npm files."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adopt import adopt_config_text, adopt_project, detect_language
from openfarm_core.cards import Card
from openfarm_core.config import load_config
from openfarm_core.errors import Refused
from openfarm_core.gates.eligibility import check_eligibility
from openfarm_core.git import Git


def _repo(owner: unittest.TestCase, files: dict[str, str]) -> Path:
    base = Path(tempfile.mkdtemp(prefix="openfarm-tsa-", dir=os.environ.get("TMPDIR")))
    owner.addCleanup(shutil.rmtree, base, True)
    root = base / "repo"
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    git = Git(root)
    git.init(branch="main")
    git.commit_all("base")
    return root


class MixedRepoTests(unittest.TestCase):
    def test_rust_with_a_package_json_is_rust(self) -> None:
        root = _repo(self, {"Cargo.toml": '[package]\nname = "a"\n',
                            "package.json": '{"name": "docs"}\n'})
        self.assertEqual(detect_language(root), "rust")
        result = adopt_project(root, state_dir=root.parent / "s", key_file=root.parent / "k")
        self.assertEqual(result["language"], "rust")
        self.assertTrue(any("package.json found too" in n and "--lang typescript" in n
                            for n in result["notes"]), result)
        written = (root / ".openfarm" / "config.toml").read_text()
        self.assertEqual(written, adopt_config_text(
            root, "rust", base_branch="main", state_dir=root.parent / "s",
            key_file=root.parent / "k"))
        self.assertNotIn("typescript", written)

    def test_python_with_a_tsconfig_is_python(self) -> None:
        root = _repo(self, {"pyproject.toml": '[project]\nname = "a"\n',
                            "tsconfig.json": "{}\n", "package.json": "{}\n"})
        self.assertEqual(detect_language(root), "python")
        result = adopt_project(root, state_dir=root.parent / "s", key_file=root.parent / "k")
        self.assertEqual(result["language"], "python")
        self.assertTrue(any("package.json and tsconfig.json found too" in n
                            for n in result["notes"]), result)

    def test_rust_and_python_together_stay_ambiguous(self) -> None:
        root = _repo(self, {"Cargo.toml": "", "setup.py": ""})
        with self.assertRaises(Refused) as caught:
            detect_language(root)
        self.assertEqual(caught.exception.code, "ambiguous-language")
        self.assertEqual(caught.exception.message,
                         "both a Rust and a Python manifest are present; pass --lang rust "
                         "or --lang python")

    def test_a_repo_that_is_really_both_takes_lang(self) -> None:
        root = _repo(self, {"Cargo.toml": "", "package.json": '{"name": "a"}\n'})
        result = adopt_project(root, language="typescript", state_dir=root.parent / "s",
                               key_file=root.parent / "k")
        self.assertEqual(result["language"], "typescript")
        self.assertFalse(any("found too" in n for n in result.get("notes", [])))

    def test_typescript_alone_is_typescript(self) -> None:
        root = _repo(self, {"package.json": '{"name": "a"}\n'})
        self.assertEqual(detect_language(root), "typescript")


class TemplateTests(unittest.TestCase):
    def test_other_lock_and_npm_files_are_forbidden(self) -> None:
        root = _repo(self, {"package.json": '{"name": "a"}\n'})
        adopt_project(root, state_dir=root.parent / "s", key_file=root.parent / "k")
        cfg = load_config(root)
        for path in ("yarn.lock", "pnpm-lock.yaml", ".npmrc", "web/.npmrc",
                     "packages/b/yarn.lock"):
            with self.subTest(path):
                card = Card("C-1", "t", done_text="`f()` returns `1`",
                            touches=[path, "src/f.ts"],
                            fields={"category": "small-feature",
                                    "tests": [{"name": "f", "file": "tests/f.test.ts"}]})
                rules = [f["rule"] for f in check_eligibility(card, cfg).failures]
                self.assertIn("forbidden-file", rules)


if __name__ == "__main__":
    unittest.main()
