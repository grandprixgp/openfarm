"""`openfarm bench`: the efficacy suite, farm against frontier-solo on hidden tests.

A skeleton for the v0.6 cards (CARD-008, CARD-009, CARD-010): each card appends its
part here. See `.openfarm/PROJECT_SPEC.md`.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .errors import Invalid

_ALLOWED_LANGUAGES = frozenset({"rust", "python"})
_ALLOWED_CATEGORIES = frozenset(
    {"small-feature", "pinned-bugfix", "pattern", "refactor"}
)
_REQUIRED_KEYS = ("id", "language", "category", "card", "hidden_tests")


@dataclass(frozen=True)
class BenchTask:
    id: str
    language: str
    category: str
    card: Path
    hidden_tests: tuple[Path, ...]
    root: Path


def load_task(task_dir: Path) -> BenchTask:
    root = Path(task_dir).resolve()
    manifest = root / "task.toml"

    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise Invalid("bad-bench-task", f"missing {manifest.name}") from exc
    except OSError as exc:
        raise Invalid(
            "bad-bench-task", f"cannot read {manifest.name}: {exc}"
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise Invalid(
            "bad-bench-task", f"invalid TOML in {manifest.name}: {exc}"
        ) from exc

    for key in _REQUIRED_KEYS:
        if key not in data:
            raise Invalid("bad-bench-task", f"missing required key: {key}")

    task_id = data["id"]
    if not isinstance(task_id, str) or not task_id:
        raise Invalid("bad-bench-task", "id must be a non-empty string")

    language = data["language"]
    if not isinstance(language, str) or language not in _ALLOWED_LANGUAGES:
        raise Invalid(
            "bad-bench-task",
            f"language must be one of {sorted(_ALLOWED_LANGUAGES)}: {language!r}",
        )

    category = data["category"]
    if not isinstance(category, str) or category not in _ALLOWED_CATEGORIES:
        raise Invalid(
            "bad-bench-task",
            f"category must be one of {sorted(_ALLOWED_CATEGORIES)}: {category!r}",
        )

    card_value = data["card"]
    if not isinstance(card_value, str) or not card_value:
        raise Invalid("bad-bench-task", "card must be a non-empty string")
    card = _resolve_task_member(root, card_value, "card")

    hidden_value = data["hidden_tests"]
    if (
        not isinstance(hidden_value, list)
        or not hidden_value
        or not all(isinstance(item, str) and item for item in hidden_value)
    ):
        raise Invalid(
            "bad-bench-task",
            "hidden_tests must be a non-empty list of relative path strings",
        )
    hidden_tests = tuple(
        _resolve_task_member(root, item, "hidden_tests") for item in hidden_value
    )

    return BenchTask(
        id=task_id,
        language=language,
        category=category,
        card=card,
        hidden_tests=hidden_tests,
        root=root,
    )


def score(records: list[dict]) -> dict:
    raise NotImplementedError


def _resolve_task_member(root: Path, raw: str, key: str) -> Path:
    path = Path(raw)
    if path.is_absolute():
        raise Invalid(
            "bad-bench-task", f"{key} must be a relative path: {raw}"
        )

    target = (root / path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise Invalid(
            "bad-bench-task", f"{key} escapes task directory: {raw}"
        ) from exc

    if not target.exists():
        raise Invalid("bad-bench-task", f"{key} path does not exist: {raw}")

    return target
