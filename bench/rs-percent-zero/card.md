+++
id = "CARD-1"
title = "percent never panics"
state = "ready"
category = "pinned-bugfix"
unit = "stats"
touches = ["src/percent.rs"]
depends = []

[[tests]]
name = "a_zero_total_gives_zero"
file = "tests/zero.rs"
asserts = "percent(5, 0) == 0"
+++

## Done

- `percent(part, 0)` returns 0 instead of panicking.
- `percent` never overflows: it works in `u128`, so `percent(u64::MAX, u64::MAX)` is 100, and a result too large for `u64` saturates at `u64::MAX`, so `percent(u64::MAX, 1)` is `u64::MAX`.
- Every other result is unchanged: `part * 100 / total`, rounded down.
