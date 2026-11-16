+++
id = "CARD-1"
title = "One parse_line for load and apply"
state = "ready"
category = "refactor"
unit = "settings"
touches = ["src/config.rs"]
new_symbols = true
depends = []

[[tests]]
name = "parse_line_skips_a_comment"
file = "tests/parse_line.rs"
asserts = "parse_line(\"  # note\", 1) == Ok(None)"

[[tests]]
name = "parse_line_splits_at_the_first_equals"
file = "tests/parse_line.rs"
asserts = "parse_line(\" a = b=c \", 1) == Ok(Some((\"a\".to_string(), \"b=c\".to_string())))"
+++

## Done

- New `pub fn parse_line(raw: &str, line: usize) -> Result<Option<(String, String)>, ConfigError>` in `src/config.rs`: `Ok(None)` for a blank or `#` line, else the trimmed key and value split at the first `=`, else `Err(ConfigError { line, message: "expected key = value" })`.
- `load` and `apply` call `parse_line` for every line instead of repeating that logic; what they return is unchanged.
