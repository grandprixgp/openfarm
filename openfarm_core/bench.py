"""`openfarm bench`: the efficacy suite, farm against frontier-solo on hidden tests.

A task (`bench/<task>/`) holds `task.toml`, the card, a starting project in `repo/`,
hidden tests under `hidden/`, and `script.json` with scripted answers for the
zero-cost dry run. An arm file sets only `[adapters]` model/micromanager, `[model]`
and `[micromanager]`; `bench/arms/` has examples. Each run starts from a fresh copy
of `repo/`, runs the card, then copies the hidden tests onto the result and runs the
whole suite. Nothing leaves the machine but the arms' own model calls.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .errors import Invalid
from .git import Git

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
    # The project the arms start from, and the scripted answers for a dry run.
    # Both are optional for loading; `run_bench` needs `repo`.
    repo: Path | None = None
    script: Path | None = None


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

    repo_dir = root / "repo"
    script_file = root / "script.json"
    return BenchTask(
        id=task_id,
        language=language,
        category=category,
        card=card,
        hidden_tests=hidden_tests,
        root=root,
        repo=repo_dir if repo_dir.is_dir() else None,
        script=script_file if script_file.is_file() else None,
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
            arm, {"runs": 0, "passed": 0, "spend": 0.0, "tokens": 0, "worker_tokens": 0}
        )
        arm_stats["runs"] += 1
        if passed:
            arm_stats["passed"] += 1
        arm_stats["spend"] += record["spend"]
        arm_stats["tokens"] += int(record["tokens"])
        # Older records have no split; count all their tokens as the worker's.
        arm_stats["worker_tokens"] += int(record.get("worker_tokens", record["tokens"]))

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
        tokens = stats["tokens"]
        arms[arm] = {
            "runs": runs,
            "passed": passed,
            "pass_rate": round(passed / runs, 4),
            "tokens": tokens,
            "tokens_per_pass": round(tokens / passed) if passed else None,
            "worker_share": round(stats["worker_tokens"] / tokens, 4) if tokens else 0.0,
            # Dollars at API list prices; on a subscription they are not a cost.
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

    def count(value: int | None) -> str:
        if value is None:
            return "—"
        return f"{value:,}"

    lines = [
        "# openfarm bench report",
        "",
        "| Arm | Runs | Passed | Pass rate | Tokens | Tokens per pass | Worker share |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for arm in sorted(summary.get("arms", {})):
        stats = summary["arms"][arm]
        lines.append(
            f"| {arm} | {stats['runs']} | {stats['passed']} | "
            f"{percent(stats['pass_rate'])} | {count(stats['tokens'])} | "
            f"{count(stats['tokens_per_pass'])} | {percent(stats['worker_share'])} |"
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


# -- running the bench ----------------------------------------------------------

ARM_ADAPTER_KEYS = frozenset({"model", "micromanager"})
_HEADER_RE = re.compile(r"^\[\s*([A-Za-z0-9_.\-]+)\s*\]\s*$")
_CARD_ID_RE = re.compile(r'^id\s*=\s*"([^"]+)"', re.MULTILINE)


def _sections(text: str) -> list[tuple[str, list[str]]]:
    """A TOML text split at its `[table]` headers: ("", preamble) first."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in text.splitlines():
        match = _HEADER_RE.match(line)
        if match:
            sections.append((match.group(1), [line]))
        else:
            sections[-1][1].append(line)
    return sections


def _arm_owns(table: str) -> bool:
    return table in ("model", "micromanager") or table.startswith(
        ("model.", "micromanager."))


ARM_HARNESS_KEYS = frozenset({"learn_from", "share", "global_dir"})


def load_arm(path: Path) -> str:
    """An arm file's text, checked: only `[adapters]` model/micromanager, `[model]`
    and `[micromanager]` (with their sub-tables), and `[harness]` learn_from, share
    and global_dir are allowed in it."""
    try:
        text = Path(path).read_text(encoding="utf-8")
        data = tomllib.loads(text)
    except OSError as exc:
        raise Invalid("bad-bench-arm", f"cannot read arm file {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise Invalid("bad-bench-arm", f"invalid TOML in arm file {path}: {exc}") from exc
    extra = sorted(set(data) - {"adapters", "model", "micromanager", "harness"})
    if extra:
        raise Invalid("bad-bench-arm",
                      f"{path}: an arm sets only [adapters], [model], "
                      f"[micromanager] and [harness], not {extra}")
    stray_harness = sorted(set(data.get("harness", {})) - ARM_HARNESS_KEYS)
    if stray_harness:
        raise Invalid("bad-bench-arm",
                      f"{path}: an arm's [harness] sets only learn_from, share and "
                      f"global_dir, not {stray_harness}")
    adapters = data.get("adapters", {})
    stray = sorted(set(adapters) - ARM_ADAPTER_KEYS)
    if stray:
        raise Invalid("bad-bench-arm",
                      f"{path}: an arm's [adapters] sets only model and "
                      f"micromanager, not {stray}")
    return text


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(str(value))


def _merge_harness_lines(lines: list[str], arm_harness: dict) -> list[str]:
    """The base's `[harness]` lines with the arm's keys replaced or added at the end."""
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in arm_harness:
            line = f"{key} = {_toml_value(arm_harness[key])}"
            seen.add(key)
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    out.extend(f"{key} = {_toml_value(value)}" for key, value in arm_harness.items()
               if key not in seen)
    return out


def arm_config_text(base: str, arm: str) -> str:
    """`base` (a full config) with the arm's adapters, `[model]` and `[micromanager]`.

    The arm's tables replace the base's whole; its `[adapters]` keys replace the
    matching lines and leave the rest of the base's `[adapters]` alone.
    """
    arm_sections = _sections(arm)
    arm_data = tomllib.loads(arm)
    arm_adapters = arm_data.get("adapters", {})
    arm_harness = arm_data.get("harness", {})
    arm_tables = [(name, lines) for name, lines in arm_sections if _arm_owns(name)]
    replaced = {name.split(".")[0] for name, _lines in arm_tables}

    out: list[str] = []
    for name, lines in _sections(base):
        if _arm_owns(name) and name.split(".")[0] in replaced:
            continue
        if name == "adapters":
            for line in lines:
                key = line.split("=", 1)[0].strip()
                if key in arm_adapters:
                    line = f"{key} = {json.dumps(str(arm_adapters[key]))}"
                out.append(line)
            continue
        if name == "harness" and arm_harness:
            out.extend(_merge_harness_lines(lines, arm_harness))
            continue
        out.extend(lines)
    for _name, lines in arm_tables:
        out.append("")
        out.extend(lines)
    text = "\n".join(out).strip("\n") + "\n"
    tomllib.loads(text)  # a broken merge fails here, not in the run
    return text


def card_id_of(card_text: str) -> str:
    match = _CARD_ID_RE.search(card_text)
    if not match:
        raise Invalid("bad-bench-task", "the task's card has no id")
    return match.group(1)


def _openfarm_cmd() -> list[str]:
    return [sys.executable, "-m", "openfarm_core.cli"]


def _openfarm_env(global_dir: Path | None = None) -> dict:
    env = dict(os.environ)
    if global_dir is not None:
        # A bench run never reads or writes the owner's global lessons.
        env["OPENFARM_GLOBAL_HARNESS"] = str(global_dir)
    root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"]
                                if env.get("PYTHONPATH") else "")
    return env


def _suite_cmd(language: str) -> list[str]:
    if language == "rust":
        return ["cargo", "test", "--quiet"]
    return [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."]


def _hidden_target(task: BenchTask, hidden: Path) -> Path:
    """Where a hidden test lands in the repo: its path below `hidden/`."""
    rel = hidden.relative_to(task.root)
    parts = rel.parts[1:] if rel.parts and rel.parts[0] == "hidden" else rel.parts
    if not parts:
        raise Invalid("bad-bench-task", f"hidden test {rel} has no file name")
    return Path(*parts)


def run_once(task: BenchTask, arm: str, arm_text: str | None, repeat: int,
             work: Path, *, dry_run: bool = False,
             timeout_s: float = 3 * 3600.0) -> dict:
    """Run one task once in one arm, in a fresh repo under `work`, and score it.

    The hidden tests are copied in only after the arm has finished, onto the
    branch the card produced, and the whole suite must pass for `passed`.
    """
    if task.repo is None:
        raise Invalid("bad-bench-task", f"{task.id}: no repo/ folder to start from")
    if dry_run and task.script is None:
        raise Invalid("bad-bench-task", f"{task.id}: a dry run needs script.json")

    run_dir = work / task.id / f"{arm}-{repeat}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    repo = run_dir / "repo"
    state = run_dir / "state"
    shutil.copytree(task.repo, repo)
    state.mkdir(parents=True)

    card_text = task.card.read_text(encoding="utf-8")
    card_id = card_id_of(card_text)
    (repo / "cards").mkdir(exist_ok=True)
    (repo / "cards" / f"{card_id}.md").write_text(card_text, encoding="utf-8")

    # Late import: adopt imports git and errors, nothing from here.
    from .adopt import adopt_config_text
    base = adopt_config_text(repo, task.language, base_branch="main",
                             state_dir=state, key_file=run_dir / "no.key")
    if dry_run:
        arm_text = ('[adapters]\nmodel = "scripted"\nmicromanager = "off"\n')
    if arm_text is None:
        raise Invalid("bad-bench-arm", f"no arm file for {arm}")
    config = arm_config_text(base, arm_text)
    (repo / ".openfarm").mkdir(exist_ok=True)
    (repo / ".openfarm" / "config.toml").write_text(config, encoding="utf-8")

    git = Git(repo)
    git.init(branch="main")
    git.commit_all("bench start")

    cmd = [*_openfarm_cmd(), "--repo", str(repo), "--json", "run",
           "--card", card_id, "--cards", "1"]
    if dry_run:
        cmd += ["--script", str(task.script)]
    started = time.monotonic()
    done = subprocess.run(cmd, cwd=repo, capture_output=True, text=True,
                          env=_openfarm_env(run_dir / "global" / "harness"),
                          timeout=timeout_s, check=False)
    seconds = round(time.monotonic() - started, 1)
    (run_dir / "run.out.json").write_text(done.stdout, encoding="utf-8")
    (run_dir / "run.err.log").write_text(done.stderr, encoding="utf-8")

    record = {"task": task.id, "language": task.language,
              "category": task.category, "arm": arm, "repeat": repeat,
              "passed": False, "spend": 0.0, "tokens": 0, "seconds": seconds,
              "diff_lines": 0, "final_state": "", "stop_code": "",
              "run_dir": str(run_dir)}
    try:
        outcome = (json.loads(done.stdout).get("outcomes") or [{}])[0]
    except ValueError:
        record["stop_code"] = f"openfarm-exit-{done.returncode}"
        return record

    record["final_state"] = str(outcome.get("final_state", ""))
    record["stop_code"] = str(outcome.get("stop_code", "") or "")
    record["spend"] = round(float(outcome.get("spend", 0.0) or 0.0)
                            + float(outcome.get("micromanager_spend", 0.0) or 0.0), 6)
    record["worker_tokens"] = sum(int(outcome.get(k, 0) or 0)
                                  for k in ("tokens_in", "tokens_out"))
    record["supervisor_tokens"] = sum(int(outcome.get(k, 0) or 0) for k in (
        "micromanager_tokens_in", "micromanager_tokens_out"))
    record["tokens"] = record["worker_tokens"] + record["supervisor_tokens"]
    record["diff_lines"] = (int(outcome.get("lines_added", 0) or 0)
                            + int(outcome.get("lines_removed", 0) or 0))
    if record["final_state"] != "in-review":
        return record

    branch = git.branches(f"openfarm/{card_id}-*")
    if not branch:
        record["stop_code"] = "no-card-branch"
        return record
    git.checkout(branch[0])
    for hidden in task.hidden_tests:
        target = repo / _hidden_target(task, hidden)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(hidden, target)
    scored = subprocess.run(_suite_cmd(task.language), cwd=repo, capture_output=True,
                            text=True, timeout=1800, check=False)
    (run_dir / "hidden.log").write_text(scored.stdout + scored.stderr, encoding="utf-8")
    record["passed"] = scored.returncode == 0
    return record


def run_bench(tasks: list[BenchTask], arms: dict[str, str | None], repeats: int,
              out: Path, *, dry_run: bool = False,
              log=lambda message: None) -> dict:
    """Every task in every arm `repeats` times; writes the report into `out`."""
    out.mkdir(parents=True, exist_ok=True)
    work = out / "runs"
    records: list[dict] = []
    with (out / "records.jsonl").open("a", encoding="utf-8") as sink:
        for task in tasks:
            for arm, arm_text in arms.items():
                for repeat in range(1, repeats + 1):
                    log(f"bench: {task.id} {arm} #{repeat}")
                    record = run_once(task, arm, arm_text, repeat, work,
                                      dry_run=dry_run)
                    log(f"bench: {task.id} {arm} #{repeat} -> "
                        f"{'pass' if record['passed'] else 'fail'} "
                        f"({record['final_state'] or record['stop_code']})")
                    records.append(record)
                    sink.write(json.dumps(record) + "\n")
                    sink.flush()
    summary = score(records)
    report = {"dry_run": dry_run, "repeats": repeats,
              "summary": summary, "records": records}
    (out / "bench-report.json").write_text(json.dumps(report, indent=2) + "\n",
                                           encoding="utf-8")
    (out / "bench-report.md").write_text(render_report(summary) + "\n",
                                         encoding="utf-8")
    return report
