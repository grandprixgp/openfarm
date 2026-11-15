"""The run's own bounds: `--for`, `--until`, and how a night reports itself."""

from __future__ import annotations

import datetime as dt
import unittest

from openfarm_core.cli import _deadline
from openfarm_core.errors import UsageError


class DeadlineTests(unittest.TestCase):
    def make(self, **kwargs):
        class Args:
            pass
        args = Args()
        args.for_duration = kwargs.get("for_duration")
        args.until = kwargs.get("until")
        return args

    def test_no_flag_means_no_deadline(self) -> None:
        self.assertIsNone(_deadline(self.make()))

    def test_for_duration_is_seconds_from_now(self) -> None:
        import time
        before = time.monotonic()
        deadline = _deadline(self.make(for_duration="45m"))
        self.assertIsNotNone(deadline)
        self.assertAlmostEqual(deadline - before, 2700.0, delta=2.0)

    def test_for_accepts_a_plain_number(self) -> None:
        import time
        before = time.monotonic()
        deadline = _deadline(self.make(for_duration="60"))
        self.assertAlmostEqual(deadline - before, 60.0, delta=2.0)

    def test_a_bad_duration_is_a_usage_error(self) -> None:
        with self.assertRaises(UsageError):
            _deadline(self.make(for_duration="soon"))

    def test_until_hh_mm_is_in_the_future(self) -> None:
        """A time already past today means tomorrow, not a deadline in the past.

        The deadline is a `time.monotonic()` value, so it is compared against now
        rather than against zero.
        """
        import time
        now = dt.datetime.now(dt.timezone.utc)
        deadline = _deadline(self.make(until=now.strftime("%H:%M")))
        seconds_left = deadline - time.monotonic()
        self.assertGreaterEqual(seconds_left, -2.0)
        self.assertLessEqual(seconds_left, 86400.0)

    def test_until_an_iso_time(self) -> None:
        import time
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)
        deadline = _deadline(self.make(until=future.strftime("%Y-%m-%dT%H:%M:%S")))
        self.assertAlmostEqual(deadline - time.monotonic(), 7200.0, delta=5.0)

    def test_the_deadline_is_in_the_same_clock_the_run_checks(self) -> None:
        """`_deadline` answers in `time.monotonic()` and the run compares to it.

        Mixing the two clocks (a monotonic deadline against `time.time()`) makes
        every card look overdue, which is how `--for` once turned a night into a
        no-op.
        """
        import time
        deadline = _deadline(self.make(for_duration="1h"))
        self.assertGreater(deadline, time.monotonic())
        self.assertLess(deadline, time.monotonic() + 3700)
        # A wall-clock value would be around 1.7e9, three orders larger.
        self.assertLess(deadline, 1e8)

    def test_a_bad_time_is_a_usage_error(self) -> None:
        with self.assertRaises(UsageError):
            _deadline(self.make(until="half past"))


if __name__ == "__main__":
    unittest.main()
