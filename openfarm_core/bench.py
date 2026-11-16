"""`openfarm bench`: the efficacy suite, farm against frontier-solo on hidden tests.

A skeleton for the v0.6 cards (CARD-008, CARD-009, CARD-010): each card appends its
part here. See `.openfarm/PROJECT_SPEC.md`.
"""

from __future__ import annotations


from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BenchTask:
    id: str
    language: str
    category: str
    card: Path
    hidden_tests: tuple[Path, ...]
    root: Path


def load_task(task_dir: Path) -> BenchTask:
    raise NotImplementedError("load_task")
