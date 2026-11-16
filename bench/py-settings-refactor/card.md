+++
id = "CARD-1"
title = "One parse_line for both settings loaders"
state = "ready"
category = "refactor"
unit = "."
touches = ["config/loader.py"]
new_symbols = true
depends = []

[[tests]]
name = "test_parse_line_skips_a_comment"
file = "tests/test_parse_line.py"
asserts = "parse_line(\"  # note\", 1) is None"

[[tests]]
name = "test_parse_line_splits_key_and_value"
file = "tests/test_parse_line.py"
asserts = "parse_line(\" a = b=c \", 1) == (\"a\", \"b=c\")"
+++

## Done

- New `parse_line(raw: str, number: int) -> tuple[str, str] | None` in `config/loader.py`: `None` for a blank or `#` line, else the stripped key and value split at the first `=`, else `LoadError("line <number>: expected key = value")`.
- `load_settings` and `load_overrides` call `parse_line` for every line instead of repeating that logic; what they return and raise is unchanged.
