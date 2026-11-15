"""The runner adapter: every build, test, lint and typecheck goes through here.

The `infra` class is the point of this module. A build timeout must never reach a
model as a code failure, and never spends an attempt. The runner decides infra; the
core never guesses it from output.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from ..errors import Infra, Invalid, Refused
from ..state import write_json

#: Kinds the runner accepts.
KINDS = ("build", "test", "lint", "typecheck", "list", "extra", "mutation")

PASS = "pass"
FAIL = "fail"
INFRA = "infra"

#: Output that means the machine, not the code, went wrong. Every pattern here is
#: about the machine: a missing toolchain, a full disk, a lost build box. A compile
#: error is deliberately absent, because that is the model's edit being wrong.
INFRA_PATTERNS = (
    r"error: could not compile `[^`]+` \(signal",
    r"No space left on device",
    r"Out of memory",
    r"Cannot allocate memory",
    r"error: failed to download",
    r"failed to clone into",
    r"Blocking waiting for file lock",
    r"linker command failed",
    r"internal compiler error",
    r"network failure",
    r"connection refused",
    r"Text file busy",
    r"cannot execute binary file",
    r"error: no such command",
    r"error: could not find `cargo`",
    r"error: toolchain .* is not installed",
    r"Permission denied \(os error 13\).*cargo",
)


class RunResult:
    """One runner call's answer."""

    __slots__ = ("cls", "exit_code", "duration_s", "stdout", "stderr",
                 "stdout_path", "stderr_path", "argv", "kind")

    def __init__(self, cls: str, exit_code: int, duration_s: float,
                 stdout: str = "", stderr: str = "",
                 stdout_path: Path | None = None,
                 stderr_path: Path | None = None,
                 argv: list[str] | None = None, kind: str = "") -> None:
        self.cls = cls
        self.exit_code = exit_code
        self.duration_s = duration_s
        self.stdout = stdout
        self.stderr = stderr
        self.stdout_path = stdout_path
        self.stderr_path = stderr_path
        self.argv = list(argv or [])
        self.kind = kind

    @property
    def ok(self) -> bool:
        return self.cls == PASS

    def as_dict(self) -> dict:
        return {"class": self.cls, "exit_code": self.exit_code,
                "duration_s": self.duration_s,
                "stdout_path": str(self.stdout_path or ""),
                "stderr_path": str(self.stderr_path or "")}

    def __repr__(self) -> str:
        return f"RunResult({self.kind!r}, {self.cls}, exit={self.exit_code})"


class DirectRunner:
    """`cargo` or `npm`/`pnpm` run directly, with its own infra classification."""

    name = "direct"

    def __init__(self, repo: Path, timeout_s: float = 1800.0,
                 output_dir: Path | None = None) -> None:
        self.repo = Path(repo)
        self.timeout_s = timeout_s
        self.output_dir = Path(output_dir) if output_dir else None
        self._call = 0

    def hello(self) -> dict:
        return {"name": self.name, "versions": [1], "ops": ["hello", "exec"]}

    def exec(self, kind: str, argv: list[str], cwd: Path | None = None,
             target_dir: Path | None = None, unit: str = "",
             env: dict[str, str] | None = None,
             timeout_s: float | None = None) -> RunResult:
        if kind not in KINDS:
            raise Invalid("bad-kind", f"{kind!r} is not a runner kind")
        if not argv:
            raise Invalid("no-argv", "the runner needs a command to run")

        run_env = dict(os.environ)
        if env:
            run_env.update({str(k): str(v) for k, v in env.items()})
        if target_dir:
            run_env["CARGO_TARGET_DIR"] = str(target_dir)
            Path(target_dir).mkdir(parents=True, exist_ok=True)

        started = time.monotonic()
        try:
            completed = subprocess.run(
                argv, cwd=str(cwd or self.repo), capture_output=True, text=True,
                env=run_env, timeout=timeout_s or self.timeout_s)
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            exit_code = completed.returncode
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
            exit_code = -1
            timed_out = True
        except OSError as exc:
            raise Infra("spawn-failed", f"{argv[0]} could not start: {exc}") from exc

        duration = time.monotonic() - started
        stdout_path, stderr_path = self._save(kind, stdout, stderr)

        if timed_out:
            return RunResult(INFRA, exit_code, duration, stdout, stderr,
                             stdout_path, stderr_path, argv, kind)
        cls = self.classify(exit_code, stdout, stderr, kind)
        return RunResult(cls, exit_code, duration, stdout, stderr,
                         stdout_path, stderr_path, argv, kind)

    def classify(self, exit_code: int, stdout: str, stderr: str,
                 kind: str = "") -> str:
        """pass, fail or infra. Infra wins, because a broken machine looks red.

        A compile error is a code failure, not an infra one: `error[E0449]` says the
        edit was wrong. Only the machine-shaped failures below are infra.
        """
        if exit_code == 0:
            return PASS
        combined = f"{stdout}\n{stderr}"
        for pattern in INFRA_PATTERNS:
            if re.search(pattern, combined):
                return INFRA
        return FAIL

    def _save(self, kind: str, stdout: str,
              stderr: str) -> tuple[Path | None, Path | None]:
        if self.output_dir is None:
            return None, None
        self._call += 1
        stem = f"{self._call:03d}-{kind}"
        out_path = self.output_dir / f"{stem}.stdout.txt"
        err_path = self.output_dir / f"{stem}.stderr.txt"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(stdout)
        err_path.write_text(stderr)
        return out_path, err_path


class CmdRunner:
    """`cmd:<argv>` runner: one JSON request per job on stdin and stdout."""

    name = "cmd"

    def __init__(self, spec: str, repo: Path, timeout_s: float = 3600.0) -> None:
        from ..protocol import CmdAdapter
        self.adapter = CmdAdapter(spec, repo, timeout_s=timeout_s)
        self.repo = Path(repo)

    def hello(self) -> dict:
        result = self.adapter.hello("runner", {"exec"})
        return result

    def exec(self, kind: str, argv: list[str], cwd: Path | None = None,
             target_dir: Path | None = None, unit: str = "",
             env: dict[str, str] | None = None,
             timeout_s: float | None = None) -> RunResult:
        result = self.adapter.call("runner", "exec", {
            "kind": kind, "argv": list(argv),
            "cwd": str(cwd or self.repo),
            "target_dir": str(target_dir or ""),
            "unit": unit, "env": dict(env or {}),
            "timeout_s": timeout_s or self.timeout_s,
        })
        cls = str(result.get("class", INFRA))
        if cls not in (PASS, FAIL, INFRA):
            raise Invalid("bad-class", f"runner answered class {cls!r}")
        return RunResult(
            cls=cls,
            exit_code=int(result.get("exit_code", -1)),
            duration_s=float(result.get("duration_s", 0.0)),
            stdout_path=(Path(result["stdout_path"])
                         if result.get("stdout_path") else None),
            stderr_path=(Path(result["stderr_path"])
                         if result.get("stderr_path") else None),
            argv=list(argv), kind=kind)

    def read_output(self, result: RunResult) -> tuple[str, str]:
        """For a cmd: runner: the output lives in the files it names."""
        out = ""
        err = ""
        if result.stdout_path and result.stdout_path.exists():
            out = result.stdout_path.read_text(errors="replace")
        if result.stderr_path and result.stderr_path.exists():
            err = result.stderr_path.read_text(errors="replace")
        return out, err


def open_runner(cfg, worktree: Path, output_dir: Path | None = None):
    """The runner the config names."""
    spec = cfg.runner_adapter
    if spec == "direct":
        return DirectRunner(worktree, output_dir=output_dir)
    if spec.startswith("cmd:"):
        return CmdRunner(spec, worktree)
    raise Invalid("bad-runner", f"{spec!r} is not a runner adapter")
