from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adopt import adopt_project
from openfarm_core.errors import Refused
from openfarm_core.git import Git


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _snapshot(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = _sha256(path)
    return result


def _make_committed_python_repo(root: Path, branch: str = "main") -> None:
    (root / "pyproject.toml").write_text('name = "widget"\n', encoding="utf-8")
    (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
    git = Git(root)
    git.init(branch=branch)
    git.commit_all("initial")


class AdoptProjectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.root = self.tmp / "widget"
        self.root.mkdir()
        self.state_dir = self.tmp / "state"
        self.key_file = self.tmp / "keys" / "widget.key"

    def test_adopt_writes_only_its_three_files(self) -> None:
        _make_committed_python_repo(self.root)
        before = _snapshot(self.root)
        result = adopt_project(
            self.root, state_dir=self.state_dir, key_file=self.key_file
        )
        self.assertEqual(
            result,
            {
                "ok": True,
                "language": "python",
                "written": [
                    ".openfarm/config.toml",
                    ".openfarm/PROJECT_SPEC.md",
                    "cards/.gitkeep",
                ],
            },
        )
        after = _snapshot(self.root)
        for rel, digest in before.items():
            self.assertIn(rel, after)
            self.assertEqual(after[rel], digest)

    def test_a_folder_that_is_not_a_repo_is_refused(self) -> None:
        (self.root / "pyproject.toml").write_text('name = "widget"\n', encoding="utf-8")
        with self.assertRaises(Refused) as ctx:
            adopt_project(
                self.root, state_dir=self.state_dir, key_file=self.key_file
            )
        self.assertEqual(ctx.exception.code, "not-a-git-repo")

    def test_uncommitted_changes_are_refused(self) -> None:
        Git(self.root).init()
        (self.root / "scratch.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaises(Refused) as ctx:
            adopt_project(
                self.root, state_dir=self.state_dir, key_file=self.key_file
            )
        self.assertEqual(ctx.exception.code, "dirty-worktree")

    def test_a_second_adopt_is_refused(self) -> None:
        _make_committed_python_repo(self.root)
        adopt_project(self.root, state_dir=self.state_dir, key_file=self.key_file)
        with self.assertRaises(Refused) as ctx:
            adopt_project(
                self.root, state_dir=self.state_dir, key_file=self.key_file
            )
        self.assertEqual(ctx.exception.code, "already-adopted")

    def test_the_base_branch_comes_from_git(self) -> None:
        _make_committed_python_repo(self.root, branch="trunk")
        adopt_project(self.root, state_dir=self.state_dir, key_file=self.key_file)
        config = (self.root / ".openfarm" / "config.toml").read_text(encoding="utf-8")
        lines = [line.strip() for line in config.splitlines()]
        self.assertIn("base_branch = \"trunk\"", lines)
        section = config.split("[project]", 1)[1]
        project_lines = [line.strip() for line in section.splitlines()]
        self.assertIn('base_branch = "trunk"', project_lines)


if __name__ == "__main__":
    unittest.main()
