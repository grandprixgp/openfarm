+++
id = "CARD-1"
title = "A full ring drops its oldest reading"
state = "ready"
category = "pinned-bugfix"
unit = "ringbuf"
touches = ["src/ring.rs"]
depends = []

[[tests]]
name = "a_full_ring_drops_the_oldest"
file = "tests/overwrite.rs"
asserts = "Ring::new(2) after push(1), push(2), push(3) has items() == [2, 3]"
+++

## Done

- `Ring::push` on a ring already holding `capacity` readings drops the oldest one first, so `items()` never holds more than `capacity` readings, oldest first.
- A ring made with `Ring::new(0)` keeps nothing: `push` returns without storing and does not panic.
