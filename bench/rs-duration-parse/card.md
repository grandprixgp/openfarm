+++
id = "CARD-1"
title = "Read a duration in seconds"
state = "ready"
category = "small-feature"
unit = "units"
touches = ["src/parse.rs"]
reference = "src/parse.rs"
new_symbols = true
depends = []

[[tests]]
name = "parses_minutes"
file = "tests/duration.rs"
asserts = "parse_duration(\"5m\") == Ok(300)"

[[tests]]
name = "refuses_an_unknown_suffix"
file = "tests/duration.rs"
asserts = "parse_duration(\"5x\").is_err()"
+++

## Done

- New `pub fn parse_duration(text: &str) -> Result<u64, UnitError>` in `src/parse.rs`, written like `parse_size`.
- It reads digits with an optional suffix `s` (seconds), `m` (minutes) or `h` (hours) into seconds: `"90s"` is 90, `"5m"` is 300, `"2h"` is 7200, and bare digits `"45"` are 45.
- Anything else is an `Err(UnitError(..))`: `""`, `"m"`, `"5x"`, `"-5s"`, `"1.5h"`, and a value whose seconds overflow `u64`.
