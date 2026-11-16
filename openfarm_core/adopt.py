"""`openfarm adopt`: add openfarm to an existing repository without touching its source.

A skeleton for the v0.6 cards (CARD-003, CARD-004, CARD-005, CARD-007): each card
appends its function here. See `.openfarm/PROJECT_SPEC.md`.
"""

from __future__ import annotations

from pathlib import Path

from .errors import Refused


def detect_language(root: Path) -> str:
    """Return "rust" or "python" from the manifests at the top level of `root`.

    Raises `openfarm_core.errors.Refused` with code "ambiguous-language" when both
    kinds of manifest are present, or "unsupported-language" when neither is.
    Only the top level of `root` is looked at; nothing is written.
    """
    root = Path(root)
    has_cargo = (root / "Cargo.toml").is_file()
    has_python = ((root / "pyproject.toml").is_file()
                  or (root / "setup.py").is_file())

    if has_cargo and has_python:
        raise Refused(
            "ambiguous-language",
            "both a Rust and a Python manifest are present; pass --lang rust or "
            "--lang python",
        )
    if has_cargo:
        return "rust"
    if has_python:
        return "python"
    if (root / "package.json").is_file():
        raise Refused(
            "unsupported-language",
            "no supported language manifest found; TypeScript has no pack yet",
        )
    raise Refused(
        "unsupported-language",
        "no supported language manifest found: expected Cargo.toml, pyproject.toml, "
        "or setup.py",
    )


def adopt_config_text(
    root: Path,
    language: str,
    *,
    base_branch: str,
    state_dir: Path,
    key_file: Path,
) -> str:
    """Return the text of `.openfarm/config.toml` for adopting `root` as `language`.

    Raises `openfarm_core.errors.Refused` with code "state-inside-repo" when
    `state_dir` or `key_file` is inside `root`, or "unsupported-language" when
    `language` is not "rust" or "python". Nothing is written.
    """
    raise NotImplementedError
