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


class CodexReadOnlyInsideTheBoundaryTests(unittest.TestCase):
    """Codex runs its shell in its own sandbox; where that cannot nest, read-only is refused."""

    def test_refused_when_a_sandbox_cannot_start_inside_the_boundary(self) -> None:
        d = confine.decide
        for mode in ("auto", "bwrap"):
            h = d("codex", transport="bridge", tool_policy="read-only", mode=mode,
                  bwrap_usable=True, nested_usable=False, nested_reason="no new namespaces")
            self.assertTrue(h.refused, mode)
            self.assertFalse(h.boundary)
            self.assertIn("no new namespaces", h.refused)
            self.assertIn('tool_policy = "none"', h.refused)
            self.assertIn('[micromanager] confine = "off"', h.refused)
            ok = d("codex", transport="bridge", tool_policy="read-only", mode=mode,
                   bwrap_usable=True, nested_usable=True)
            self.assertEqual(ok.describe(), "flags + boundary")

    def test_nothing_else_is_refused_for_it(self) -> None:
        d = confine.decide
        for prov, transport, policy, mode in (
                ("codex", "bridge", "none", "auto"), ("codex", "server", "read-only", "auto"),
                ("antigravity", "bridge", "read-only", "auto"),
                ("claude", "bridge", "read-only", "bwrap"),
                ("codex", "bridge", "read-only", "off")):
            h = d(prov, transport=transport, tool_policy=policy, mode=mode,
                  bwrap_usable=True, nested_usable=False)
            self.assertFalse(h.refused, (prov, transport, policy, mode))
            self.assertEqual(confine.needs_nested(prov, transport, policy, mode), False)
        self.assertTrue(confine.needs_nested("codex", "bridge", "read-only", "auto"))

    def test_the_probe_reads_the_inner_sandbox_result(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="of-v081-lead-nest-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.addCleanup(confine._reset_status_cache)
        # A fake bwrap: the outer call (it carries --die-with-parent) runs what follows
        # the first "--"; the inner call fails or passes as the file name says.
        for name, inner_exit in (("bwrap-nests", 0), ("bwrap-flat", 1)):
            fake = tmp / name
            fake.write_text(f"""#!/usr/bin/env python3
import subprocess, sys
args = sys.argv[1:]
if "--die-with-parent" not in args:
    if {inner_exit}:
        sys.stderr.write("bwrap: No permissions to create a new namespace\\n")
    sys.exit({inner_exit})
rest = args[args.index("--") + 1:]
sys.exit(subprocess.run(rest).returncode)
""")
            fake.chmod(0o755)
        self.assertEqual(confine.nested_status(str(tmp / "bwrap-nests"))[0], True)
        usable, why = confine.nested_status(str(tmp / "bwrap-flat"))
        self.assertFalse(usable)
        self.assertIn("No permissions to create a new namespace", why)
        self.assertEqual(confine.nested_status("")[0], False)

    def test_the_adapter_refuses_the_session_and_the_status_says_so(self) -> None:
        import sys
        from unittest import mock
        from openfarm_core.adapters.acp import ClaudeAcpAdapter
        from openfarm_core.errors import Refused
        tmp = Path(tempfile.mkdtemp(prefix="of-v081-lead-ro-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bridge = Path(confine.__file__).parent / "adapters" / "codex_native_acp.py"
        with mock.patch.object(confine, "bwrap_status", return_value=(True, "works")), \
                mock.patch.object(confine, "nested_status", return_value=(False, "no nesting")):
            with ClaudeAcpAdapter([sys.executable, str(bridge), "--codex-bin", "/nonexistent/codex"],
                                  cwd=tmp, provider="codex", auth_mode="api-key",
                                  confine="auto") as acp:
                summary = acp.confinement_summary()
                self.assertEqual(summary["none"]["held_by"], "flags + boundary")
                self.assertIn("no nesting", summary["read-only"]["refused"])
                with self.assertRaises(Refused) as ctx:
                    acp.new_session(cwd=tmp, tool_policy="read-only")
                self.assertIn("no nesting", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
