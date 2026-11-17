"""`openfarm adopt`: add openfarm to an existing repository without touching its source.

A skeleton for the v0.6 cards (CARD-003, CARD-004, CARD-005, CARD-007): each card
appends its function here. See `.openfarm/PROJECT_SPEC.md`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .config_template import render_config
from .errors import Refused
from .git import Git


def detect_language(root: Path) -> str:
    """Return `"rust"`, `"python"`, or `"typescript"` from top-level manifests.

    Raises `openfarm_core.errors.Refused` with code "ambiguous-language" when
    multiple kinds of manifest are present, or "unsupported-language" when none
    is. Only the top level of `root` is looked at; nothing is written.
    """
    root = Path(root)
    has_cargo = (root / "Cargo.toml").is_file()
    has_python = ((root / "pyproject.toml").is_file()
                  or (root / "setup.py").is_file())
    has_ts = ((root / "package.json").is_file()
              or (root / "tsconfig.json").is_file())

    detected: list[str] = []
    if has_cargo:
        detected.append("rust")
    if has_python:
        detected.append("python")
    if has_ts:
        detected.append("typescript")

    if len(detected) > 1:
        raise Refused(
            "ambiguous-language",
            f"multiple language manifests are present ({', '.join(detected)}); "
            f"pass --lang {' or --lang '.join(detected)}",
        )
    if len(detected) == 1:
        return detected[0]
    raise Refused(
        "unsupported-language",
        "no supported language manifest found: expected Cargo.toml, pyproject.toml, "
        "setup.py, package.json, or tsconfig.json",
    )


def baseline_known_failing(root: Path, language: str, *, target_dir: Path,
                           timeout_s: float = 1800.0) -> dict:
    """Run the suite once on the clean base and propose its red tests.

    Returns {"ran": bool, "known_failing": [...], "notes": [...]}. A Python repo
    with no tests/ folder is not run. Raises `Refused` "suite-does-not-compile"
    when the suite does not build, or "suite-did-not-run" when it could not run
    or failed without naming a failed test.
    """
    from .packs.base import pack_for

    root = Path(root)
    cargo_flags = ["--locked"] if (root / "Cargo.lock").is_file() else []
    pack = pack_for(language, cargo_flags, cwd=root)
    pack.configure(root)
    notes = [str(note) for note in getattr(pack, "notes", [])]
    if (language == "python" and getattr(pack, "runner", "") == "unittest"
            and not (root / getattr(pack, "test_dir", "tests")).is_dir()):
        return {"ran": False, "known_failing": [], "notes": notes}

    argv = pack.commands("test")["argv"]
    env = dict(os.environ)
    # Cargo builds into openfarm's state, not into the repository being adopted.
    env["CARGO_TARGET_DIR"] = str(target_dir)
    try:
        done = subprocess.run(argv, cwd=root, capture_output=True, text=True,
                              env=env, timeout=timeout_s, check=False)
    except FileNotFoundError as exc:
        raise Refused("suite-did-not-run",
                      f"could not run the suite: {argv[0]} was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise Refused("suite-did-not-run",
                      f"the suite took longer than {timeout_s:.0f}s; pass --no-suite "
                      "to adopt without running it") from exc
    parsed = pack.parse_tests(done.stdout, done.stderr)
    proposed = propose_known_failing(parsed)
    if done.returncode != 0 and not proposed:
        tail = (done.stderr or done.stdout).strip().splitlines()[-3:]
        raise Refused("suite-did-not-run",
                      "the suite failed without naming a failed test: "
                      + " | ".join(tail))
    return {"ran": True, "known_failing": proposed, "notes": notes}


def adopt_config_text(
    root: Path,
    language: str,
    *,
    base_branch: str,
    state_dir: Path,
    key_file: Path,
    proposed_known_failing: list[str] | None = None,
    typescript_tools: dict | None = None,
) -> str:
    """Return the text of `.openfarm/config.toml` for adopting `root` as `language`.

    Raises `openfarm_core.errors.Refused` with code "state-inside-repo" when
    `state_dir` or `key_file` is inside `root`, or "unsupported-language" when
    `language` is not "rust" or "python". Nothing is written.
    """
    if language not in ("rust", "python", "typescript"):
        raise Refused(
            "unsupported-language",
            f"unsupported language {language!r}: expected 'rust', 'python', or 'typescript'",
        )

    resolved_root = Path(root).expanduser().resolve()
    resolved_state = Path(state_dir).expanduser().resolve()
    resolved_key = Path(key_file).expanduser().resolve()

    if resolved_state.is_relative_to(resolved_root):
        raise Refused(
            "state-inside-repo",
            f"state_dir must be outside the repository: {resolved_state}",
        )
    if resolved_key.is_relative_to(resolved_root):
        raise Refused(
            "state-inside-repo",
            f"key_file must be outside the repository: {resolved_key}",
        )

    cargo_flags = ["--locked"] if (resolved_root / "Cargo.lock").is_file() else []
    return render_config(
        name=resolved_root.name, language=language, base_branch=base_branch,
        state_dir=resolved_state, key_file=resolved_key, cargo_flags=cargo_flags,
        proposed_known_failing=proposed_known_failing,
        typescript_tools=typescript_tools)


def adopt_project(
    root: Path,
    *,
    language: str | None = None,
    state_dir: Path | None = None,
    key_file: Path | None = None,
    run_suite: bool = False,
    suite_timeout_s: float = 1800.0,
) -> dict:
    """Adopt the existing git repository at `root` without touching its source.

    Raises `openfarm_core.errors.Refused` with code "not-a-git-repo" when
    `root/.git` does not exist, "dirty-worktree" when the worktree is not clean,
    or "already-adopted" when `root/.openfarm/config.toml` exists, in that order.
    Writes `.openfarm/config.toml`, `.openfarm/PROJECT_SPEC.md` and
    `cards/.gitkeep`, creates `state_dir` with mode 0o700, never commits, and
    returns {"ok": True, "language": ..., "written": [...]}.
    """
    root = Path(root).expanduser().resolve()

    if not (root / ".git").exists():
        raise Refused(
            "not-a-git-repo",
            f"{root} is not a git repository: .git is missing",
        )

    # Adopting never commits, so a second adopt always meets a dirty tree; the
    # already-adopted check has to come first to be the one it reports.
    config_path = root / ".openfarm" / "config.toml"
    spec_path = root / ".openfarm" / "PROJECT_SPEC.md"
    gitkeep_path = root / "cards" / ".gitkeep"
    # Checked before anything is written, so a refusal never leaves half an adoption.
    for path in (config_path, spec_path, gitkeep_path):
        if path.exists():
            raise Refused(
                "already-adopted",
                f"{root} is already adopted: {path} exists",
            )

    git = Git(root)
    if not git.is_clean():
        raise Refused(
            "dirty-worktree",
            f"{root} has uncommitted changes; commit or stash them before adopting",
        )

    resolved_language = language or detect_language(root)
    base_branch = git.current_branch()

    name = root.name
    if state_dir is None:
        resolved_state = (Path.home() / ".openfarm" / name).expanduser().resolve()
    else:
        resolved_state = Path(state_dir).expanduser().resolve()
    if key_file is None:
        resolved_key = (
            Path.home() / ".config" / "openfarm" / f"{name}.key"
        ).expanduser().resolve()
    else:
        resolved_key = Path(key_file).expanduser().resolve()

    typescript_tools = None
    adopt_notes: list[str] = []
    if resolved_language == "typescript":
        typescript_tools, adopt_notes = typescript_adopt_checks(root)

    baseline = None
    if run_suite:
        # Before anything is written: a suite that does not build refuses adoption.
        baseline = baseline_known_failing(
            root, resolved_language, target_dir=resolved_state / "adopt-target",
            timeout_s=suite_timeout_s)

    config_text = adopt_config_text(
        root,
        resolved_language,
        base_branch=base_branch,
        state_dir=resolved_state,
        key_file=resolved_key,
        proposed_known_failing=baseline["known_failing"] if baseline else None,
        typescript_tools=typescript_tools,
    )

    resolved_state.mkdir(parents=True, exist_ok=True)
    os.chmod(resolved_state, 0o700)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("x", encoding="utf-8") as handle:
        handle.write(config_text)

    spec_path.parent.mkdir(parents=True, exist_ok=True)
    with spec_path.open("x", encoding="utf-8") as handle:
        handle.write(f"# {name}\n")

    gitkeep_path.parent.mkdir(parents=True, exist_ok=True)
    with gitkeep_path.open("x", encoding="utf-8") as handle:
        handle.write("")

    result = {
        "ok": True,
        "language": resolved_language,
        "written": [
            ".openfarm/config.toml",
            ".openfarm/PROJECT_SPEC.md",
            "cards/.gitkeep",
        ],
    }
    if typescript_tools is not None:
        result["typescript"] = {key: typescript_tools[key]
                                for key in ("runner", "typecheck", "format")}
    if adopt_notes:
        result["notes"] = adopt_notes
    if baseline is not None:
        result["suite_ran"] = baseline["ran"]
        result["known_failing_proposed"] = baseline["known_failing"]
        result["suite_notes"] = baseline["notes"]
    return result


def typescript_adopt_checks(root: Path) -> tuple[dict, list[str]]:
    """The runner and tools a TypeScript repo is adopted with, and what to tell.

    Decided here, in the checkout being adopted, and recorded in the config so
    that `check` and every card use the same. Raises `Refused`
    "unsupported-test-runner" when the test script is not a `node --test` one.
    """
    import shutil

    from .packs.typescript import adopt_findings, resolve_tooling

    tooling = resolve_tooling(root, {})
    if tooling["test_script"]:
        raise Refused("unsupported-test-runner", tooling["test_script"])
    notes = [f"tests run with {tooling['runner']}; typecheck: {tooling['typecheck']}; "
             f"format: {tooling['format']}"]
    notes += tooling["notes"]
    node = shutil.which("node")
    if node:
        notes += adopt_findings(root, node)
    else:
        notes.append("node was not found, so adopt could not check what node --test "
                     "can run")
    return tooling, notes


def propose_known_failing(parsed: dict) -> list[str]:
    """Return the sorted, de-duplicated names of failed tests.

    Raises `openfarm_core.errors.Refused` with code "suite-does-not-compile"
    when the pack's parser reported compile errors.
    """
    compile_errors = parsed.get("compile_errors") or []
    if compile_errors:
        first = compile_errors[0]
        binary = first.get("binary", "") if isinstance(first, dict) else str(first)
        raise Refused(
            "suite-does-not-compile",
            f"the suite does not compile: {binary}",
        )

    names = [
        result["name"]
        for result in parsed.get("results") or []
        if result.get("status") == "FAILED"
    ]
    return sorted(set(names))
