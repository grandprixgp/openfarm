+++
id = "CARD-1"
title = "Validate a port number like a username"
state = "ready"
category = "pattern"
unit = "."
touches = ["forms/validators.py"]
reference = "forms/validators.py"
new_symbols = true
depends = []

[[tests]]
name = "test_validate_port_accepts_8080"
file = "tests/test_port.py"
asserts = "validate_port(\"8080\") == 8080"

[[tests]]
name = "test_validate_port_refuses_zero"
file = "tests/test_port.py"
asserts = "validate_port(\"0\") raises ValidationError"
+++

## Done

- New `validate_port(value: str) -> int` in `forms/validators.py`, following `validate_username`: trim, check, return the cleaned value, or raise `ValidationError`.
- After trimming, the value must be one or more ASCII digits `0`-`9` and nothing else, so `"+80"`, `"-1"`, `"1_000"` and `""` are refused.
- The number must be 1 to 65535 inclusive; it is returned as an `int`.
- Ordinary-length inputs only; inputs past Python's `int()` string-conversion digit limit are out of scope.
