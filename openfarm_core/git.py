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
import threading
from pathlib import Path

from .errors import Infra

_GIT_LOCKS_GUARD = threading.Lock()
_GIT_LOCKS: dict[Path, threading.RLock] = {}


def _common_git_dir(cwd: Path) -> Path:
    cur = Path(cwd).resolve()
    for candidate in (cur, *cur.parents):
        dot_git = candidate / ".git"
        if dot_git.is_dir():
            return dot_git.resolve()
        if dot_git.is_file():
            try:
                text = dot_git.read_text(encoding="utf-8").strip()
            except OSError:
                break
            if text.startswith("gitdir:"):
                raw = text.split(":", 1)[1].strip()
                gitdir = Path(raw)
                if not gitdir.is_absolute():
                    gitdir = (candidate / gitdir).resolve()
                common = gitdir / "commondir"
                if common.is_file():
                    try:
                        rel = common.read_text(encoding="utf-8").strip()
                        return (gitdir / rel).resolve()
                    except OSError:
                        return gitdir.resolve()
                return gitdir.resolve()
    return cur


def _git_lock_for(cwd: Path) -> threading.RLock:
    key = _common_git_dir(cwd)
    with _GIT_LOCKS_GUARD:
        lock = _GIT_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _GIT_LOCKS[key] = lock
        return lock

#: Every git subcommand the core is allowed to run.
ALLOWED_SUBCOMMANDS = frozenset({
    "init", "add", "commit", "status", "diff", "rev-parse", "rev-list",
    "checkout", "switch", "branch", "worktree", "merge", "merge-base", "reset",
    "clean", "ls-files", "show", "cat-file", "hash-object", "for-each-ref",
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


#: Options that consume the next argument, so that argument is a value, not a
#: subcommand. `-m` is here because a commit message can be any word at all.
_OPTIONS_TAKING_A_VALUE = frozenset({
    "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path",
    "--config-env", "-c", "--super-prefix", "--shallow-file",
    "-m", "--message", "-F", "--file", "--author", "--date", "-t",
})


#: Subcommands whose arguments are paths or refs, never further subcommands. For
#: these the scan stops at the subcommand, so a file named `remote` is a file.
_LEAF_SUBCOMMANDS = frozenset({
    "add", "commit", "diff", "show", "ls-files", "log", "status", "reset",
    "clean", "checkout", "switch", "branch", "worktree", "merge", "merge-base",
    "rev-parse", "rev-list", "cat-file", "hash-object", "for-each-ref",
    "update-ref", "symbolic-ref", "stash", "rm", "mv", "describe", "tag",
    "apply", "restore", "init", "config",
})


_FORBIDDEN_CONFIG_PREFIXES = (
    "alias.",
    "core.hookspath",
    "core.sshcommand",
    "core.fsmonitor",
    "core.pager",
)


def _check_config_args(args: list[str]) -> None:
    """Reject global/system/executable-hook git config invocations."""
    after_double_dash = False
    for token in args:
        if token == "--" and not after_double_dash:
            after_double_dash = True
            continue
        if not after_double_dash and token in {"--global", "--system"}:
            raise GitRefused(f"git config flag {token!r} is refused")
        if after_double_dash or not token.startswith("-"):
            low = token.lower()
            for prefix in _FORBIDDEN_CONFIG_PREFIXES:
                if low.startswith(prefix):
                    raise GitRefused(f"git config key {token!r} is refused")


def _check_subcommand(args: list[str]) -> str:
    """Check the words git will read as a subcommand, and only those.

    Three things this must do, because git's own grammar is the only thing that
    decides which word is a subcommand:

    - Refuse `push` (and the rest) wherever it *is* the subcommand, including after
      global options: `git --namespace config push` runs `push`.
    - Stop scanning after the subcommand when that subcommand takes paths, so a
      repository with a file called `push` or `remote` works.
    - Skip option values, because a commit message can be any word at all.
    """
    subcommands: list[str] = []
    subcommand_end = len(args)
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            break
        if not subcommands:
            if (token == "-c" or token.startswith("-c")
                    or token == "--config-env" or token.startswith("--config-env=")
                    or token == "--exec-path" or token.startswith("--exec-path=")):
                raise GitRefused(f"git global option {token!r} is refused")
            if token.startswith("-C") and len(token) > 2:
                index += 1
                continue
        if token in _OPTIONS_TAKING_A_VALUE:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        subcommands.append(token)
        index += 1
        # A leaf subcommand's remaining words are its arguments, not subcommands.
        if token in _LEAF_SUBCOMMANDS:
            subcommand_end = index
            break

    if not subcommands:
        raise GitRefused("git was called with no subcommand")
    for word in subcommands:
        if word in FORBIDDEN_SUBCOMMANDS:
            raise GitRefused(
                f"openfarm never runs git {word}: the core has no code path that "
                f"reaches another repository")
    for word in subcommands:
        if word in ALLOWED_SUBCOMMANDS:
            if word == "config":
                _check_config_args(args[subcommand_end:])
            return word
    raise GitRefused(f"git {subcommands[0]} is not on openfarm's allowlist")


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

    def init(self, branch: str = "main") -> None:
        with _git_lock_for(self.cwd):
            self._run(["init", "-q", "-b", branch])

    def head(self, ref: str = "HEAD") -> str:
        return self._run(["rev-parse", ref]).stdout.strip()

    def current_branch(self) -> str:
        return self._run(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()

    def branches(self, pattern: str) -> list[str]:
        """Local branch names matching a `refs/heads/` glob such as `openfarm/CARD-1-*`."""
        with _git_lock_for(self.cwd):
            out = self._run(["for-each-ref", "--format=%(refname:short)",
                             f"refs/heads/{pattern}"]).stdout
        return sorted(line for line in out.splitlines() if line)

    def branch_exists(self, name: str) -> bool:
        with _git_lock_for(self.cwd):
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
        with _git_lock_for(self.cwd):
            self._run(["commit", "-q", "-m", message])
            return self.head()

    def commit_all(self, message: str) -> str:
        with _git_lock_for(self.cwd):
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
        with _git_lock_for(self.cwd):
            self._run(args)

    def create_branch(self, name: str, start: str) -> None:
        """Make or move a branch to `start`.

        `start` is resolved to a commit first, because passing a ref name straight
        to `git branch` can pick up a different worktree's idea of it. Git refuses
        to move a branch that a worktree has checked out, so any worktree holding it
        is removed first rather than leaving the branch silently on an old commit.
        """
        with _git_lock_for(self.cwd):
            commit = self.head(start)
            result = self._run(["branch", "--force", name, commit], check=False)
            if result.returncode == 0:
                return
            holder = self._worktree_holding(name)
            if holder is not None:
                self.worktree_remove(holder)
                self._run(["branch", "--force", name, commit])

    def worktree_add(self, path: Path, ref: str) -> None:
        path = Path(path)
        with _git_lock_for(self.cwd):
            path.parent.mkdir(parents=True, exist_ok=True)
            self._run(["worktree", "add", "--force", str(path), ref])

    def worktree_remove(self, path: Path) -> None:
        with _git_lock_for(self.cwd):
            self._run(["worktree", "remove", "--force", str(path)], check=False)
            self._run(["worktree", "prune"], check=False)

    def worktree_list(self) -> list[str]:
        with _git_lock_for(self.cwd):
            out = self._run(["worktree", "list", "--porcelain"]).stdout
        return [line[len("worktree "):] for line in out.splitlines()
                if line.startswith("worktree ")]

    def _worktree_holding(self, branch: str) -> Path | None:
        """The worktree that has `branch` checked out, if any."""
        with _git_lock_for(self.cwd):
            out = self._run(["worktree", "list", "--porcelain"]).stdout
        current = None
        for line in out.splitlines():
            if line.startswith("worktree "):
                current = line[len("worktree "):]
            elif line.startswith("branch ") and current:
                if line[len("branch "):] == f"refs/heads/{branch}":
                    return Path(current)
            elif not line.strip():
                current = None
        return None

    def merge_no_commit(self, ref: str) -> tuple[bool, str]:
        """Trial merge: stops before committing. Never leaves a merge commit."""
        with _git_lock_for(self.cwd):
            result = self._run(["merge", "--no-commit", "--no-ff", ref], check=False)
            output = (result.stdout or "") + (result.stderr or "")
            if result.returncode != 0:
                self._run(["merge", "--abort"], check=False)
                return False, output.strip()
            return True, output.strip()

    def abort_merge(self) -> None:
        with _git_lock_for(self.cwd):
            self._run(["merge", "--abort"], check=False)

    def merge_no_ff(self, ref: str, message: str = "") -> tuple[bool, str]:
        """Local-only `--no-ff` merge into the current branch. Never pushes."""
        with _git_lock_for(self.cwd):
            args = ["merge", "--no-ff", ref]
            if message:
                args += ["-m", message]
            result = self._run(args, check=False)
            output = (result.stdout or "") + (result.stderr or "")
            if result.returncode != 0:
                self._run(["merge", "--abort"], check=False)
                return False, output.strip()
            return True, output.strip()

    def diff(self, paths: list[str] | None = None, ref: str = "HEAD") -> str:
        """A unified diff against `ref`, optionally limited to paths."""
        args = ["diff", ref]
        if paths:
            args += ["--", *paths]
        return self._run(args).stdout

    def diff_name_only(self, ref: str = "HEAD") -> list[str]:
        """Tracked paths that differ from `ref`, by name."""
        return [line.strip() for line in
                self._run(["diff", "--name-only", ref]).stdout.splitlines()
                if line.strip()]

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
        with _git_lock_for(self.cwd):
            return self.head(f"refs/heads/{name}")
