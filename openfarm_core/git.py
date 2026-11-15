"""The only place the core runs git.

Two rules hold here, and nowhere else can they be broken:

1. **No code path pushes.** The allowlist below has no `push`, `remote`, `fetch`,
   `pull` or `submodule` in it, and a test asserts that. Nothing in the core shells
   out to git except through this module.
2. **One worktree, one branch, one build target per card.** `worktree_add` takes
   the target directory from the caller, and the caller takes it from the card's
   own state folder.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .errors import Infra

#: Every git subcommand the core is allowed to run.
ALLOWED_SUBCOMMANDS = frozenset({
    "init", "add", "commit", "status", "diff", "rev-parse", "rev-list",
    "checkout", "switch", "branch", "worktree", "merge", "reset", "clean",
    "ls-files", "show", "cat-file", "hash-object", "for-each-ref",
    "update-ref", "symbolic-ref", "log", "config", "stash", "rm", "mv",
    "describe", "tag", "apply", "restore",
})

#: Subcommands that would reach another repository or change what a gate means.
FORBIDDEN_SUBCOMMANDS = frozenset({
    "push", "fetch", "pull", "remote", "submodule", "clone", "send-email",
    "request-pull", "archive", "daemon", "instaweb", "credential",
})

#: Identity forced on harness commits, so a run never depends on the machine.
_ENV = {
    "GIT_AUTHOR_NAME": "openfarm",
    "GIT_AUTHOR_EMAIL": "openfarm@localhost",
    "GIT_COMMITTER_NAME": "openfarm",
    "GIT_COMMITTER_EMAIL": "openfarm@localhost",
    "GIT_TERMINAL_PROMPT": "0",
}


class GitRefused(Exception):
    """A git call was outside the allowlist. Nothing was run."""


def _check_subcommand(args: list[str]) -> str:
    for arg in args:
        if arg.startswith("-"):
            continue
        if arg in FORBIDDEN_SUBCOMMANDS:
            raise GitRefused(
                f"openfarm never runs git {arg}: the core has no code path that "
                f"reaches another repository")
        if arg not in ALLOWED_SUBCOMMANDS:
            raise GitRefused(
                f"git {arg} is not on openfarm's allowlist")
        return arg
    raise GitRefused("git was called with no subcommand")


class Git:
    """Git calls in one repository, through the allowlist."""

    def __init__(self, cwd: Path, env: dict[str, str] | None = None) -> None:
        self.cwd = Path(cwd)
        self.extra_env = dict(env or {})

    def _run(self, args: list[str], check: bool = True,
             timeout: float = 300.0) -> subprocess.CompletedProcess:
        _check_subcommand(list(args))
        env = dict(os.environ)
        env.update(_ENV)
        env.update(self.extra_env)
        try:
            result = subprocess.run(
                ["git", *args], cwd=self.cwd, capture_output=True, text=True,
                env=env, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Infra("git-failed", f"git {args[0]} could not run: {exc}") from exc
        if check and result.returncode != 0:
            raise Infra("git-failed",
                        f"git {' '.join(args)} failed: {result.stderr.strip()[:500]}")
        return result

    # -- queries --------------------------------------------------------------

    def head(self, ref: str = "HEAD") -> str:
        return self._run(["rev-parse", ref]).stdout.strip()

    def current_branch(self) -> str:
        return self._run(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()

    def branch_exists(self, name: str) -> bool:
        result = self._run(["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
                           check=False)
        return result.returncode == 0

    def ref_exists(self, ref: str) -> bool:
        result = self._run(["rev-parse", "--verify", "--quiet", ref], check=False)
        return result.returncode == 0

    def is_clean(self) -> bool:
        result = self._run(["status", "--porcelain"])
        return not result.stdout.strip()

    def status_lines(self) -> list[str]:
        return [line for line in self._run(["status", "--porcelain"]).stdout.splitlines()
                if line.strip()]

    def changed_files(self) -> list[str]:
        """Tracked changes plus untracked files, repository-relative."""
        out: list[str] = []
        seen: set[str] = set()
        diff = self._run(["diff", "--name-only", "HEAD"]).stdout
        for line in diff.splitlines():
            path = line.strip()
            if path and path not in seen:
                seen.add(path)
                out.append(path)
        untracked = self._run(["ls-files", "--others", "--exclude-standard"]).stdout
        for line in untracked.splitlines():
            path = line.strip()
            if path and path not in seen:
                seen.add(path)
                out.append(path)
        return sorted(out)

    def staged_changes(self) -> list[str]:
        return [line.strip() for line in
                self._run(["diff", "--name-only", "--cached"]).stdout.splitlines()
                if line.strip()]

    def unstaged_changes(self) -> list[str]:
        """Tracked files whose unstaged content differs from the index."""
        return [line.strip() for line in self._run(["diff", "--name-only"]).stdout.splitlines()
                if line.strip()]

    def has_staged_changes(self) -> bool:
        return self._run(["diff", "--cached", "--quiet"], check=False).returncode == 1

    def tracked_files(self) -> list[str]:
        return [line.strip() for line in
                self._run(["ls-files"]).stdout.splitlines() if line.strip()]

    def commit(self, message: str) -> str:
        self._run(["commit", "-q", "-m", message])
        return self.head()

    def commit_all(self, message: str) -> str:
        self._run(["add", "-A"])
        if not self.has_staged_changes():
            return self.head()
        return self.commit(message)

    def merge_base(self, other: str) -> str:
        return self._run(["merge-base", "HEAD", other]).stdout.strip()

    def count_commits(self, spec: str, base: str = "HEAD") -> int:
        result = self._run(["rev-list", "--count", f"{base}..{spec}"], check=False)
        if result.returncode != 0:
            return 0
        return int(result.stdout.strip() or 0)

    # -- changes --------------------------------------------------------------

    def reset_hard(self, ref: str = "HEAD") -> None:
        """Between attempts: no leftovers, but ignored build caches survive."""
        self._run(["reset", "--hard", ref])
        self._run(["clean", "-fd"])

    def checkout(self, ref: str, detach: bool = False) -> None:
        args = ["checkout"] + (["--detach"] if detach else []) + [ref]
        self._run(args)

    def create_branch(self, name: str, start: str) -> None:
        self._run(["branch", name, start])

    def worktree_add(self, path: Path, ref: str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._run(["worktree", "add", "--force", str(path), ref])

    def worktree_remove(self, path: Path) -> None:
        self._run(["worktree", "remove", "--force", str(path)], check=False)
        self._run(["worktree", "prune"], check=False)

    def worktree_list(self) -> list[str]:
        return [line for line in
                self._run(["worktree", "list", "--porcelain"]).stdout.splitlines()
                if line.startswith("worktree ")]

    def merge_no_commit(self, ref: str) -> tuple[bool, str]:
        """Trial merge: stops before committing. Never leaves a merge commit."""
        result = self._run(["merge", "--no-commit", "--no-ff", ref], check=False)
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            self._run(["merge", "--abort"], check=False)
            return False, output.strip()
        return True, output.strip()

    def abort_merge(self) -> None:
        self._run(["merge", "--abort"], check=False)

    def diff(self, paths: list[str] | None = None, ref: str = "HEAD") -> str:
        """A unified diff against `ref`, optionally limited to paths."""
        args = ["diff", ref]
        if paths:
            args += ["--", *paths]
        return self._run(args).stdout

    def show_blob(self, spec: str) -> str | None:
        """A file's content at a commit, or None when it did not exist."""
        result = self._run(["show", spec], check=False)
        return result.stdout if result.returncode == 0 else None

    def untracked_files(self, paths: list[str] | None = None) -> list[str]:
        args = ["ls-files", "--others", "--exclude-standard"]
        if paths:
            args += ["--", *paths]
        return [line.strip() for line in self._run(args).stdout.splitlines()
                if line.strip()]

    def stage_all(self) -> None:
        self._run(["add", "-A"])

    def branch_head(self, name: str) -> str:
        return self.head(f"refs/heads/{name}")
