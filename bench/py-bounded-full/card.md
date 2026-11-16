+++
id = "CARD-1"
title = "A full bundle refuses a push"
state = "ready"
category = "pinned-bugfix"
unit = "."
touches = ["tally/bounded.py"]
depends = []

[[tests]]
name = "test_a_full_bundle_refuses_a_push"
file = "tests/test_full.py"
asserts = "Bounded(1).push(7) then .push(8) raises FullError"
+++

## Done

- `Bounded.push` raises `FullError` when the bundle already holds `capacity` segments, and leaves `segments` unchanged.
- Below capacity it still appends and returns the new segment's index.
