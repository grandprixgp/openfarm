"""Budgets and caps.

The split matters and is not an accident:

- **Model-driven builds** count against `model_build_calls_per_card`: every attempt's
  suite and lint, and counter-test runs.
- **Fixed gates** sit outside that cap — baseline, the named-test listing,
  fail-before, the final workspace lint and the trial merge — so a healthy card isn't
  stopped by the gates themselves.
- **Mutation** has its own cap, because one mutation run is many builds.
"""

from __future__ import annotations

import contextlib
import threading
import time

from .errors import GateFailure, StopCode


class BudgetExceeded(GateFailure):
    def __init__(self, detail: str, code: StopCode = StopCode.BUDGET) -> None:
        super().__init__(code, detail)


class Budget:
    """One card's budget."""

    def __init__(
        self,
        model_builds: int = 10,
        mutation_builds: int = 40,
        tokens: int = 400_000,
        wall_clock_s: float = 2700.0,
        spend: float = 0.0,
        frontier_estimate: float | None = None,
        frontier_ratio: float | None = None,
        implementer_attempts: int = 3,
        test_writer_attempts: int = 2,
    ) -> None:
        self.model_build_cap = model_builds
        self.mutation_build_cap = mutation_builds
        self.token_cap = tokens
        self.wall_clock_cap_s = wall_clock_s
        self.spend_cap = spend
        self.frontier_estimate = frontier_estimate
        self.frontier_ratio = frontier_ratio
        self.implementer_attempt_cap = implementer_attempts
        self.test_writer_attempt_cap = test_writer_attempts

        self.model_builds = 0
        self.fixed_builds = 0
        self.mutation_builds = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.spend = 0.0
        self.implementer_attempts = 0
        self.test_writer_attempts = 0

        self._started = time.monotonic()
        self._stopped: float | None = None
        self._paused_total_s = 0.0
        self._pause_started: float | None = None
        self._pause_depth = 0
        self._clock_lock = threading.Lock()

    # -- clocks ---------------------------------------------------------------

    def stop_clock(self) -> None:
        with self._clock_lock:
            if self._stopped is None:
                self._stopped = time.monotonic()

    @contextlib.contextmanager
    def pause_clock(self):
        """Pause the card's wall clock while waiting on an external seat."""
        with self._clock_lock:
            self._pause_depth += 1
            if self._pause_depth == 1 and self._stopped is None:
                self._pause_started = time.monotonic()
        try:
            yield
        finally:
            with self._clock_lock:
                self._pause_depth -= 1
                if self._pause_depth == 0 and self._pause_started is not None:
                    now = self._stopped if self._stopped is not None else time.monotonic()
                    self._paused_total_s += max(0.0, now - self._pause_started)
                    self._pause_started = None

    @property
    def wall_clock_s(self) -> float:
        with self._clock_lock:
            end = self._stopped if self._stopped is not None else time.monotonic()
            extra = (
                (end - self._pause_started)
                if self._pause_started is not None and end > self._pause_started
                else 0.0
            )
            return max(0.0, (end - self._started) - self._paused_total_s - extra)

    def wall_clock_left_s(self) -> float:
        return max(0.0, self.wall_clock_cap_s - self.wall_clock_s)

    # -- build accounting -----------------------------------------------------

    def note_model_build(self, count: int = 1) -> None:
        """A build a model's edit caused."""
        self.model_builds += count
        if self.model_builds > self.model_build_cap:
            raise BudgetExceeded(
                f"model-driven builds reached {self.model_builds}, over the cap of "
                f"{self.model_build_cap}", StopCode.BUILD_CAP)

    def note_fixed_build(self, count: int = 1) -> None:
        """A gate's own build. Outside the model cap."""
        self.fixed_builds += count

    def note_mutation_build(self, count: int = 1) -> None:
        self.mutation_builds += count
        if self.mutation_builds > self.mutation_build_cap:
            raise BudgetExceeded(
                f"mutation builds reached {self.mutation_builds}, over the cap of "
                f"{self.mutation_build_cap}", StopCode.MUTATION_CAP)

    def mutation_capped(self) -> bool:
        return self.mutation_builds > self.mutation_build_cap

    # -- tokens, spend, time --------------------------------------------------

    def note_tokens(self, tokens_in: int, tokens_out: int, spend: float = 0.0) -> None:
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.spend += spend
        self._check_tokens()
        self._check_spend()

    def _check_tokens(self) -> None:
        total = self.tokens_in + self.tokens_out
        if total > self.token_cap:
            raise BudgetExceeded(
                f"{total} tokens used, over the cap of {self.token_cap}")

    def _check_spend(self) -> None:
        if self.spend_cap and self.spend > self.spend_cap:
            raise BudgetExceeded(
                f"spend reached {self.spend:.4f}, over the cap of {self.spend_cap:.4f}")
        if self.frontier_estimate and self.frontier_ratio:
            limit = self.frontier_estimate * self.frontier_ratio
            if self.spend > limit:
                raise BudgetExceeded(
                    f"spend reached {self.spend:.4f}, over "
                    f"{self.frontier_ratio:.0%} of the frontier estimate "
                    f"({limit:.4f})")

    def check_clock(self) -> None:
        if self.wall_clock_s > self.wall_clock_cap_s:
            raise BudgetExceeded(
                f"wall clock reached {self.wall_clock_s:.0f}s, over the cap of "
                f"{self.wall_clock_cap_s:.0f}s")

    # -- attempts -------------------------------------------------------------

    def take_implementer_attempt(self) -> int:
        self.implementer_attempts += 1
        if self.implementer_attempts > self.implementer_attempt_cap:
            raise BudgetExceeded(
                f"{self.implementer_attempts} implementer attempts used, over "
                f"{self.implementer_attempt_cap}", StopCode.ATTEMPTS)
        return self.implementer_attempts

    def implementer_attempts_left(self) -> int:
        return max(0, self.implementer_attempt_cap - self.implementer_attempts)

    def take_test_writer_attempt(self) -> int:
        self.test_writer_attempts += 1
        if self.test_writer_attempts > self.test_writer_attempt_cap:
            raise BudgetExceeded(
                f"{self.test_writer_attempts} test writer attempts used, over "
                f"{self.test_writer_attempt_cap}", StopCode.TESTS_UNPROVABLE)
        return self.test_writer_attempts

    # -- records --------------------------------------------------------------

    def builds(self) -> dict:
        return {"model": self.model_builds, "fixed": self.fixed_builds,
                "mutation": self.mutation_builds}

    @classmethod
    def from_config(cls, cfg, frontier_estimate: float | None = None) -> "Budget":
        return cls(
            model_builds=cfg.model_build_calls_per_card,
            mutation_builds=cfg.mutation_build_calls_per_card,
            tokens=cfg.tokens_per_card,
            wall_clock_s=cfg.wall_clock_per_card,
            spend=cfg.spend_per_night,
            frontier_estimate=frontier_estimate,
            frontier_ratio=cfg.frontier_cost_ratio,
            implementer_attempts=cfg.implementer_attempts,
            test_writer_attempts=cfg.test_writer_attempts,
        )


class NightBudget:
    """The per-night cap and the kill switch, across all cards.

    Three kinds of spend count against the cap: the workers', the supervisor's
    (`micromanager_spend`) and, in a swarm run with a model coordinator, the
    coordinator's own calls.
    """

    def __init__(self, spend_cap: float, cards_cap: int | None = None) -> None:
        self.spend_cap = spend_cap
        self.cards_cap = cards_cap
        self.spend = 0.0
        self.micromanager_spend = 0.0
        self.coordinator_spend = 0.0
        self.cards_claimed = 0
        self._lock = threading.Lock()

    def note_card(self) -> None:
        with self._lock:
            self.cards_claimed += 1

    def note_spend(self, amount: float) -> None:
        with self._lock:
            self.spend += amount

    def note_micromanager(self, amount: float) -> None:
        with self._lock:
            self.micromanager_spend += amount

    def note_coordinator(self, amount: float) -> None:
        with self._lock:
            self.coordinator_spend += amount

    def total_spend(self) -> float:
        with self._lock:
            return self.spend + self.micromanager_spend + self.coordinator_spend

    def exhausted(self) -> str | None:
        # A zero cap means no cap, as everywhere: `[budget] spend_per_night = 0`
        # turns the money limit off (the card's own token and attempt caps stay).
        with self._lock:
            total = self.spend + self.micromanager_spend + self.coordinator_spend
            if self.spend_cap > 0 and total >= self.spend_cap:
                return f"night spend {total:.4f} reached its cap of {self.spend_cap:.4f}"
            if self.cards_cap is not None and self.cards_claimed >= self.cards_cap:
                return f"{self.cards_claimed} cards claimed, the run's cap"
            return None


def token_split(worker: int, supervisor: int, coordinator: int = 0) -> dict:
    """Who used a run's tokens: the worker model, the supervisor, the coordinator.

    Reports compare runs by tokens, not by dollars: on a subscription the
    supervisor's API list price says nothing about what it costs. The
    coordinator is a model only in a swarm run with `coordinator = "tier:..."`
    or an ACP coordinator; otherwise its part is zero and the worker and
    supervisor shares are what they were before it was counted.
    """
    worker = max(0, int(worker or 0))
    supervisor = max(0, int(supervisor or 0))
    coordinator = max(0, int(coordinator or 0))
    total = worker + supervisor + coordinator

    def share(part: int) -> float:
        return round(part / total, 4) if total else 0.0

    return {"worker": worker, "supervisor": supervisor, "coordinator": coordinator,
            "total": total,
            "worker_share": share(worker),
            "supervisor_share": share(supervisor),
            "coordinator_share": share(coordinator)}


def describe_split(split: dict) -> str:
    """`"68,212 tokens: 54% worker, 46% supervisor"`, plus the coordinator when it spent.

    Reads older splits too, which have no coordinator part.
    """
    total = int(split.get("total", 0) or 0)
    text = (f"{total:,} tokens: "
            f"{float(split.get('worker_share', 0.0) or 0.0) * 100:.0f}% worker, "
            f"{float(split.get('supervisor_share', 0.0) or 0.0) * 100:.0f}% supervisor")
    if int(split.get("coordinator", 0) or 0) > 0:
        text += (f", {float(split.get('coordinator_share', 0.0) or 0.0) * 100:.0f}% "
                 f"coordinator")
    return text
