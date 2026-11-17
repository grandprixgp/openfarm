+++
id = "CARD-020"
title = "README: global learning"
state = "ready"
touches = ["README.md"]
depends = []
category = "mechanical"
frontier_estimate = 0.25
new_symbols = false
unit = "."
type_changes = false

[[tests]]
name = "test_the_readme_explains_global_learning"
file = "tests/test_readme_global_learning.py"
asserts = "README.md holds a heading line \"## Global learning\" placed before the line \"## Efficacy bench\", and the text between them contains \"learn_from\", \"share\", \"global_dir\", \"~/.openfarm/global/harness\", \"--learn-from\", \"harness status --global\" and \"untested\""
+++

## Done

Add a `## Global learning` section to `README.md`, immediately before the `## Efficacy bench` heading. Write in the first person ("I") or plainly, in short sentences. No code changes. It says:

- Every project learns lessons from its own cards, as before. Each lesson is marked `project` or `general`: Claude claims one, and plain code keeps it `project` if it names one of the project's files, functions, cards or its name.
- A general lesson that has helped on 3 or more cards, with fewer hurts than helps, goes to a global library at `~/.openfarm/global/harness` (`[harness] global_dir` moves it), unless the project sets `share = false`. Notes about openfarm's own bookkeeping, fixed-rule hints, long lessons and lessons with code never go.
- Other projects import a global lesson as a new lesson of their own and try it like one. A project that finds it hurts switches it off for itself; two such projects, or a negative combined score, retire it everywhere.
- `[harness] learn_from = "both" | "project" | "global"` picks which lessons apply (default `both`), and `openfarm run --learn-from ...` overrides it for one run.
- `openfarm harness status --global` lists the global lessons and their source projects; `openfarm harness demote <id> --global` retires one and `promote <id> --global` brings it back.
- Bench runs use a throwaway global library of their own, so they never read or change yours.
- One line in bold: sharing lessons between two real projects is **untested**.
