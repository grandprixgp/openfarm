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


_REQUIRED_RECORD_KEYS = (
    "task",
    "category",
    "arm",
    "passed",
    "spend",
    "tokens",
    "seconds",
    "diff_lines",
)
_ALLOWED_ARMS = frozenset({"farm", "solo"})


def score(records: list[dict]) -> dict:
    for record in records:
        _check_record(record)

    arm_totals: dict[str, dict] = {}
    category_totals: dict[str, dict] = {}

    for record in records:
        arm = record["arm"]
        category = record["category"]
        passed = bool(record["passed"])

        arm_stats = arm_totals.setdefault(
            arm, {"runs": 0, "passed": 0, "spend": 0.0}
        )
        arm_stats["runs"] += 1
        if passed:
            arm_stats["passed"] += 1
        arm_stats["spend"] += record["spend"]

        category_arms = category_totals.setdefault(category, {})
        category_stats = category_arms.setdefault(arm, {"runs": 0, "passed": 0})
        category_stats["runs"] += 1
        if passed:
            category_stats["passed"] += 1

    arms: dict[str, dict] = {}
    for arm, stats in arm_totals.items():
        runs = stats["runs"]
        passed = stats["passed"]
        spend = round(stats["spend"], 4)
        arms[arm] = {
            "runs": runs,
            "passed": passed,
            "pass_rate": round(passed / runs, 4),
            "spend": spend,
            "cost_per_pass": round(spend / passed, 4) if passed else None,
        }

    categories: dict[str, dict[str, float]] = {}
    for category, arm_stats_by_arm in category_totals.items():
        categories[category] = {
            arm: round(stats["passed"] / stats["runs"], 4)
            for arm, stats in arm_stats_by_arm.items()
        }

    return {"arms": arms, "categories": categories}


def render_report(summary: dict) -> str:
    def percent(value: float) -> str:
        return f"{value * 100:.1f}%"

    def money(value: float | None) -> str:
        if value is None:
            return "—"
        return f"${value:.2f}"

    lines = [
        "# openfarm bench report",
        "",
        "| Arm | Runs | Passed | Pass rate | Spend | Cost per pass |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for arm in sorted(summary.get("arms", {})):
        stats = summary["arms"][arm]
        lines.append(
            f"| {arm} | {stats['runs']} | {stats['passed']} | "
            f"{percent(stats['pass_rate'])} | {money(stats['spend'])} | "
            f"{money(stats['cost_per_pass'])} |"
        )

    lines += [
        "",
        "## By category",
        "",
        "| Category | farm | solo |",
        "| --- | --- | --- |",
    ]
    for category in sorted(summary.get("categories", {})):
        arms = summary["categories"][category]
        farm = percent(arms["farm"]) if "farm" in arms else "—"
        solo = percent(arms["solo"]) if "solo" in arms else "—"
        lines.append(f"| {category} | {farm} | {solo} |")

    return "\n".join(lines)


def _check_record(record: dict) -> None:
    for key in _REQUIRED_RECORD_KEYS:
        if key not in record:
            raise Invalid("bad-bench-record", f"missing required key: {key}")

    arm = record["arm"]
    if not isinstance(arm, str) or arm not in _ALLOWED_ARMS:
        raise Invalid("bad-bench-record", f"unknown arm: {arm!r}")


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
