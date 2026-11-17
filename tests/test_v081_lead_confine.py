"""The boundary hides git history: a hidden file's committed versions are in it."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from openfarm_core import confine


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@localhost", *args],
                   cwd=cwd, check=True, capture_output=True)


class ScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v081-lead-git-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_every_git_entry_is_listed_to_hide(self) -> None:
        root = self.tmp / "s"
        (root / ".git" / "objects").mkdir(parents=True)
        (root / "sub").mkdir()
        (root / "sub" / ".git").write_text("gitdir: ../.git/modules/sub\n")
        (root / "src").mkdir()
        (root / "src" / "a.py").write_text("x = 1\n")
        files, dirs = confine.scan_masks(root, [], hide_git=True)
        self.assertEqual([p.relative_to(root).as_posix() for p in dirs], [".git"])
        self.assertEqual([p.relative_to(root).as_posix() for p in files], ["sub/.git"])
        # Asked for nothing, it still hides nothing.
        self.assertEqual(confine.scan_masks(root, []), ([], []))


@unittest.skipUnless(confine.bwrap_status()[0] and shutil.which("git"),
                     "needs a working bwrap and git")
class RealBoundaryGitTests(unittest.TestCase):
    def test_a_child_with_a_shell_cannot_read_a_hidden_file_back_from_history(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="of-v081-lead-bw-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        session = tmp / "repo"
        (session / "seams").mkdir(parents=True)
        (session / "seams" / "core.rs").write_text("NEVER_TOUCH_CONTENT\n")
        (session / "src.txt").write_text("ORDINARY\n")
        _git(session, "init", "-q", "-b", "main")
        _git(session, "add", "-A")
        _git(session, "commit", "-q", "-m", "start")
        home = tmp / "home"
        home.mkdir()
        script = ("cd " + str(session) + "; "
                  "echo ordinary=$(cat src.txt 2>&1); "
                  "echo seam=$(cat seams/core.rs 2>&1 | head -c 60); "
                  "echo show=$(git show HEAD:seams/core.rs 2>&1 | head -c 60); "
                  "echo head=$(cat .git/HEAD 2>&1 | head -c 60); "
                  "echo objects=$(ls .git/objects 2>&1 | head -c 60)")
        argv = confine.boundary_argv(
            ["/bin/sh", "-c", script], session_dir=session, home_dir=home,
            hide_globs=["seams/**"])
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        out = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
        self.assertEqual(out.get("ordinary"), "ORDINARY", done.stderr)
        self.assertNotIn("NEVER_TOUCH_CONTENT", done.stdout)
        self.assertNotIn("ref:", out.get("head", ""))
        self.assertNotIn("pack", out.get("objects", ""))
        # Outside the boundary the history really does hold it.
        plain = subprocess.run(["git", "show", "HEAD:seams/core.rs"], cwd=session,
                               capture_output=True, text=True)
        self.assertIn("NEVER_TOUCH_CONTENT", plain.stdout)


if __name__ == "__main__":
    unittest.main()
