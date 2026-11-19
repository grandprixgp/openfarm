"""The runner adapter: every build, test, lint and typecheck goes through here.

The `infra` class is the point of this module. A build timeout must never reach a
model as a code failure, and never spends an attempt. The runner decides infra; the
core never guesses it from output.
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import subprocess
import threading
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
    # What cargo 1.100 really prints when the compiler itself is killed (seen live):
    # the signal is in the "Caused by" block under the "could not compile" line.
    r"error: could not compile `[^`]+`[^\n]*\n\s*\nCaused by:\n\s+process didn't exit "
    r"successfully: [^\n]*\(signal: \d+",
    r"No space left on device",
    r"Out of memory",
    r"Cannot allocate memory",
    r"error: failed to download",
    r"failed to clone into",
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

#: cargo prints this while another cargo holds a lock, then carries on. Parallel
#: cards make it routine, so on its own it says nothing about the machine. It is
#: infra only when cargo never got past it: the wait is the last thing it said.
_LOCK_WAIT = re.compile(r"Blocking waiting for file lock")


def _stuck_on_lock(stdout: str, stderr: str) -> bool:
    for text in (stderr, stdout):
        lines = [line for line in text.splitlines() if line.strip()]
        if lines and _LOCK_WAIT.search(lines[-1]):
            return True
    return False


class RunResult:
    """One runner call's answer."""

    __slots__ = ("cls", "exit_code", "duration_s", "stdout", "stderr",
                 "stdout_path", "stderr_path", "argv", "kind",
                 "confined", "confine_network", "runner_argv")

    def __init__(self, cls: str, exit_code: int, duration_s: float,
                 stdout: str = "", stderr: str = "",
                 stdout_path: Path | None = None,
                 stderr_path: Path | None = None,
                 argv: list[str] | None = None, kind: str = "",
                 confined: bool = False, confine_network: str = "",
                 runner_argv: list[str] | None = None) -> None:
        self.cls = cls
        self.exit_code = exit_code
        self.duration_s = duration_s
        self.stdout = stdout
        self.stderr = stderr
        self.stdout_path = stdout_path
        self.stderr_path = stderr_path
        self.argv = list(argv or [])
        self.kind = kind
        self.confined = bool(confined)
        self.confine_network = str(confine_network or "")
        self.runner_argv = list(runner_argv or [])

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


class BuildLimiter:
    """Semaphore capping concurrent runner builds across parallel cards."""

    def __init__(self, limit: int) -> None:
        cap = int(limit or 0)
        self.limit = cap
        self._sem = threading.Semaphore(cap) if cap > 0 else None
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    @contextlib.contextmanager
    def slot(self, budget=None):
        if self._sem is not None:
            pause_cm = (
                budget.pause_clock()
                if budget is not None and hasattr(budget, "pause_clock")
                else contextlib.nullcontext()
            )
            with pause_cm:
                self._sem.acquire()
        try:
            with self._lock:
                self.active += 1
                if self.active > self.max_active:
                    self.max_active = self.active
            yield
        finally:
            with self._lock:
                self.active = max(0, self.active - 1)
            if self._sem is not None:
                self._sem.release()


def _with_limiter(method):
    """Acquire `self.limiter.slot(self.budget)` around a runner method when set."""

    def wrapper(self, *args, **kwargs):
        limiter = getattr(self, "limiter", None)
        if limiter is not None:
            with limiter.slot(getattr(self, "budget", None)):
                return method(self, *args, **kwargs)
        return method(self, *args, **kwargs)

    return wrapper


class DirectRunner:
    """`cargo` or `npm`/`pnpm` run directly, with its own infra classification."""

    name = "direct"

    def __init__(self, repo: Path, timeout_s: float = 1800.0,
                 output_dir: Path | None = None,
                 limiter: BuildLimiter | None = None,
                 budget=None, confine: dict | None = None) -> None:
        self.repo = Path(repo)
        #: `{"write_roots": [...], "hide_paths": [...]}` to run every command in a
        #: boundary (`[runner] confine = "bwrap"`, v0.8.5); None runs them as they are.
        self.confine = confine
        self.timeout_s = timeout_s
        self.output_dir = Path(output_dir) if output_dir else None
        self.limiter = limiter
        self.budget = budget
        self._call = 0
        self._save_lock = threading.Lock()

    def hello(self) -> dict:
        return {"name": self.name, "versions": [1], "ops": ["hello", "exec"]}

    @_with_limiter
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

        run_argv = list(argv)
        is_confined = False
        net_mode = ""
        if self.confine is not None:
            from .. import confine as confine_lib

            write_roots = list(self.confine.get("write_roots") or ())
            if target_dir:
                write_roots.append(str(target_dir))
            if kind == "mutation":
                # A mutation tool writes its results to the card's evidence folder.
                for flag in ("--out", "--output"):
                    if flag in run_argv[:-1]:
                        write_roots.append(run_argv[run_argv.index(flag) + 1])
            net_mode = str(self.confine.get("network") or "off")
            try:
                run_argv = confine_lib.runner_argv(
                    run_argv, worktree=Path(cwd or self.repo), write_roots=write_roots,
                    hide_paths=self.confine.get("hide_paths") or (),
                    unset_env=confine_lib.secret_env_names(run_env, keep=(env or {})),
                    environ=run_env, network=net_mode)
                is_confined = True
            except confine_lib.ConfineError as exc:
                raise Refused("runner-confine-unavailable", exc.message) from exc

        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                run_argv, cwd=str(cwd or self.repo),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=run_env, start_new_session=True)
            try:
                stdout, stderr = proc.communicate(timeout=timeout_s or self.timeout_s)
                stdout = stdout or ""
                stderr = stderr or ""
                exit_code = proc.returncode
                timed_out = False
            except subprocess.TimeoutExpired:
                import signal
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
                out_raw, err_raw = proc.communicate()
                stdout = _decode_output(out_raw)
                stderr = _decode_output(err_raw)
                exit_code = -1
                timed_out = True
        except OSError as exc:
            raise Infra("spawn-failed", f"{argv[0]} could not start: {exc}") from exc

        duration = time.monotonic() - started
        stdout_path, stderr_path = self._save(kind, stdout, stderr)

        if timed_out:
            return RunResult(INFRA, exit_code, duration, stdout, stderr,
                             stdout_path, stderr_path, argv, kind,
                             confined=is_confined, confine_network=net_mode,
                             runner_argv=run_argv[:40])
        cls = self.classify(exit_code, stdout, stderr, kind)
        return RunResult(cls, exit_code, duration, stdout, stderr,
                         stdout_path, stderr_path, argv, kind,
                         confined=is_confined, confine_network=net_mode,
                         runner_argv=run_argv[:40])

    def classify(self, exit_code: int, stdout: str, stderr: str,
                 kind: str = "") -> str:
        """pass, fail or infra. Infra wins, because a broken machine looks red.

        A compile error is a code failure, not an infra one: `error[E0449]` says the
        edit was wrong. Only the machine-shaped failures below are infra.
        """
        if exit_code == 0:
            return PASS
        if exit_code == -9:
            return INFRA
        combined = f"{stdout}\n{stderr}"
        for pattern in INFRA_PATTERNS:
            if re.search(pattern, combined):
                return INFRA
        if _stuck_on_lock(stdout, stderr):
            return INFRA
        if exit_code in (-6, -11):
            return FAIL if kind == "test" else INFRA
        return FAIL

    def _save(self, kind: str, stdout: str,
              stderr: str) -> tuple[Path | None, Path | None]:
        if self.output_dir is None:
            return None, None
        with self._save_lock:
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

    def __init__(self, spec: str, repo: Path, timeout_s: float = 3600.0,
                 limiter: BuildLimiter | None = None, budget=None) -> None:
        from ..protocol import CmdAdapter
        self.adapter = CmdAdapter(spec, repo, timeout_s=timeout_s)
        self.repo = Path(repo)
        self.timeout_s = timeout_s
        self.limiter = limiter
        self.budget = budget

    def hello(self) -> dict:
        result = self.adapter.hello("runner", {"exec"})
        return result

    @_with_limiter
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


def _decode_output(data) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def open_runner(cfg, worktree: Path, output_dir: Path | None = None,
                limiter: BuildLimiter | None = None, budget=None):
    """The runner the config names."""
    spec = cfg.runner_adapter
    if spec == "direct":
        confine = None
        if getattr(cfg, "runner_confine", "off") == "bwrap":
            from .. import confine as confine_lib

            usable, why = confine_lib.bwrap_status()
            if not usable:
                raise Refused("runner-confine-unavailable",
                              f'[runner] confine = "bwrap", but bubblewrap is not usable '
                              f'here ({why}); install it or set confine = "off"')
            hide: list[str] = []
            for p in (getattr(cfg, "key_file", None),
                      getattr(cfg, "state_dir", None),
                      getattr(getattr(cfg, "micromanager", None), "auth_file", None)):
                if p:
                    hide.append(str(p))
            confine_net = str(getattr(cfg, "runner_confine_network", "off") or "off")
            net_arg = "off" if confine_net == "on" else "on"
            confine = {"write_roots": list(getattr(cfg, "runner_write_roots", []) or []),
                       "hide_paths": hide,
                       "network": net_arg}
        return DirectRunner(worktree, output_dir=output_dir,
                            limiter=limiter, budget=budget, confine=confine)
    if spec.startswith("cmd:"):
        return CmdRunner(spec, worktree, limiter=limiter, budget=budget)
    raise Invalid("bad-runner", f"{spec!r} is not a runner adapter")
