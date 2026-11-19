+++
id = "CARD-1"
title = "Read an amount of money into cents"
state = "ready"
category = "small-feature"
unit = "."
touches = ["ledger/money.py"]
reference = "ledger/rates.py"
new_symbols = true
depends = []

[[tests]]
name = "test_parse_cents_reads_dollars_and_cents"
file = "tests/test_parse_cents.py"
asserts = "parse_cents(\"12.34\") == 1234"

[[tests]]
name = "test_parse_cents_refuses_three_decimals"
file = "tests/test_parse_cents.py"
asserts = "parse_cents(\"1.234\") raises MoneyError"
+++

## Done

- New `parse_cents(text: str) -> int` in `ledger/money.py`, the inverse of `format_cents`.
- It reads an optional `-`, one or more digits, and optionally `.` with one or two digits: `"12.34"` is 1234, `"7"` is 700, `"0.5"` is 50, `"-1.25"` is -125.
- Anything else raises `MoneyError`, including `""`, `"1.234"`, `"abc"`, `"1.2.3"`, `"+1.00"` and text with spaces.
- Ordinary-length inputs only; inputs past Python's `int()` string-conversion digit limit are out of scope.
