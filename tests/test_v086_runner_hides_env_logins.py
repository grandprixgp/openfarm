"""Live check, 18 Nov 2026: the runner boundary showed logins kept outside ~/.claude.

With `[runner] confine = "bwrap"`, a card's tests could read the live Claude and
Codex logins when `CLAUDE_CONFIG_DIR` and `CODEX_HOME` pointed outside the
default folders (on that machine, a session manager's own profile folders), and
saw every token-like variable in the environment. The network is shared, so a
test could have sent either anywhere.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core import confine


def _pairs(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == flag]


class SecretEnvNamesTests(unittest.TestCase):
    def test_token_like_names_are_picked(self) -> None:
        env = {"PATH": "/bin", "HOME": "/h", "ANTHROPIC_API_KEY": "x", "GH_TOKEN": "x",
               "CLAUDE_CODE_OAUTH_TOKEN": "x", "AWS_SECRET_ACCESS_KEY": "x",
               "DB_PASSWORD": "x", "MY_TOKENIZER": "x", "CARGO_HOME": "/c"}
        self.assertEqual(confine.secret_env_names(env),
                         ["ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY",
                          "CLAUDE_CODE_OAUTH_TOKEN", "DB_PASSWORD", "GH_TOKEN"])

    def test_what_the_config_sets_on_purpose_stays(self) -> None:
        self.assertEqual(confine.secret_env_names({"A_TOKEN": "x", "B_TOKEN": "y"},
                                                  keep={"B_TOKEN": "y"}), ["A_TOKEN"])


class RunnerArgvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-runner-"))
        self.tree = self.tmp / "tree"
        self.tree.mkdir()
        self.claude = self.tmp / "profiles" / "claude"
        self.codex = self.tmp / "profiles" / "codex"
        for d in (self.claude, self.codex):
            d.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _argv(self, **kw) -> list[str]:
        environ = {"CLAUDE_CONFIG_DIR": str(self.claude), "CODEX_HOME": str(self.codex)}
        return confine.runner_argv(["true"], worktree=self.tree, bwrap="/usr/bin/bwrap",
                                   environ=environ, **kw)

    def test_login_folders_named_by_the_environment_are_hidden(self) -> None:
        tmpfs = _pairs(self._argv(), "--tmpfs")
        self.assertIn(str(self.claude.resolve()), tmpfs)
        self.assertIn(str(self.codex.resolve()), tmpfs)

    def test_named_variables_are_unset(self) -> None:
        argv = self._argv(unset_env=["ANTHROPIC_API_KEY", "GH_TOKEN"])
        self.assertEqual(_pairs(argv, "--unsetenv"), ["ANTHROPIC_API_KEY", "GH_TOKEN"])
        self.assertLess(argv.index("--unsetenv"), argv.index("--"))

    @unittest.skipUnless(shutil.which("bwrap") and confine.bwrap_status()[0],
                         "bubblewrap is not usable here")
    def test_a_confined_command_cannot_read_them(self) -> None:
        import subprocess
        (self.claude / ".credentials.json").write_text("secret")
        (self.codex / "auth.json").write_text("secret")
        script = (f'cat "{self.claude}/.credentials.json" "{self.codex}/auth.json" 2>/dev/null;'
                  ' echo "key=${ANTHROPIC_API_KEY:-unset}"')
        argv = confine.runner_argv(
            ["/bin/sh", "-c", script], worktree=self.tree,
            environ={"CLAUDE_CONFIG_DIR": str(self.claude), "CODEX_HOME": str(self.codex)},
            unset_env=["ANTHROPIC_API_KEY"])
        out = subprocess.run(argv, capture_output=True, text=True,
                             env={**os.environ, "ANTHROPIC_API_KEY": "sk-live"}).stdout
        self.assertNotIn("secret", out)
        self.assertIn("key=unset", out)


if __name__ == "__main__":
    unittest.main()
