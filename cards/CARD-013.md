+++
id = "CARD-013"
title = "README: working on an existing project and on Python"
state = "ready"
touches = ["README.md"]
depends = ["CARD-006", "CARD-012"]
category = "mechanical"
frontier_estimate = 0.25
new_symbols = false
unit = "."
type_changes = false
+++

## Done

- Add a `## Existing projects and Python` section to `README.md` after the install steps, with the three commands `openfarm adopt --dir .`, `/openfarm-grill-me --batch`, and `openfarm check CARD-001`, in that order, each in a fenced `bash` or `text` block.
- State in one sentence that Python repositories use `unittest` unless they configure pytest, and that formatting is checked only when `ruff` is installed.
- Change no other section.
