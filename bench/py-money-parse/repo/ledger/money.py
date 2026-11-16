"""Amounts of money, held as whole cents."""


class MoneyError(ValueError):
    """An amount could not be read."""


def format_cents(cents: int) -> str:
    """Show whole cents as `"12.34"`, or `"-0.05"` below zero."""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{whole}.{frac:02d}"
