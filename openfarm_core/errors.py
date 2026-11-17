"""Stop codes and adapter error kinds.

Every way a card can stop has a code from the spec's stop-condition table, so a
record, a note and a log line all name the same thing.
"""

from __future__ import annotations

import enum


class StopCode(str, enum.Enum):
    """Why a card stopped. Values are the spec's stop codes."""

    INELIGIBLE = "ineligible"
    DIRTY_BASELINE = "dirty-baseline"
    TESTS_UNPROVABLE = "tests-unprovable"
    ATTEMPTS = "attempts"
    NO_PROGRESS = "no-progress"
    BUILD_CAP = "build-cap"
    MUTATION_CAP = "mutation-cap"
    BUDGET = "budget"
    CANNOT_SPLIT = "cannot-split"
    CHECKER_REJECT = "checker-reject"
    SUPERVISOR_REJECT = "supervisor-reject"
    EXTRA_GATE = "extra-gate"
    CARD_CHANGED = "card-changed"
    STALE_CONFLICT = "stale-conflict"
    REFUSED = "refused"
    INFRA = "infra"
    LEASE_EXPIRED = "lease-expired"
    HANDED_BACK = "handed-back"
    SEAT_SILENT = "seat-silent"

    # Farm-wide and operator codes, outside the per-card table.
    KILLED = "killed"
    RELEASED = "released"
    PAUSED = "paused"


#: Codes that move a card to `blocked` rather than releasing it.
RELEASING_CODES = frozenset({StopCode.INELIGIBLE})


class AdapterError(Exception):
    """An adapter answered with an error or a refusal."""

    KIND = "infra"

    def __init__(self, code: str, message: str, kind: str | None = None,
                 retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.kind = kind or self.KIND
        self.retry_after_s = retry_after_s

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "code": self.code,
            "message": self.message,
            "retry_after_s": self.retry_after_s,
        }


class Refused(AdapterError):
    """A rule said no. Never retried, and the card or step stops with the code."""

    KIND = "refused"


class Infra(AdapterError):
    """A timeout, a lost machine, an unusable answer. Never spends an attempt."""

    KIND = "infra"


class Invalid(AdapterError):
    """The adapter and the core disagree about the request or the response."""

    KIND = "invalid"


class Unsupported(AdapterError):
    """The op is not offered; the core uses its fallback or refuses."""

    KIND = "unsupported"


class ConfigError(Exception):
    """The config file is missing, malformed or refers outside the repository."""


class UsageError(Exception):
    """The command was called wrongly."""


class GateFailure(Exception):
    """A gate failed in a way that stops the card outright."""

    def __init__(self, code: StopCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
