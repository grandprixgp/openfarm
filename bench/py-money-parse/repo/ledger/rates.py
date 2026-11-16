"""Interest rates, held as whole basis points."""

import re

_RATE = re.compile(r"^(\d+)(?:\.(\d{1,2}))?%$")


class RateError(ValueError):
    """A rate could not be read."""


def parse_rate(text: str) -> int:
    """Read `"4.25%"` as 425 basis points; refuse anything else with `RateError`."""
    match = _RATE.match(text)
    if match is None:
        raise RateError(f"not a rate: {text!r}")
    whole, frac = match.groups()
    return int(whole) * 100 + int((frac or "0").ljust(2, "0"))
