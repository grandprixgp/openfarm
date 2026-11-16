+++
id = "CARD-010"
title = "Render the bench report as Markdown"
state = "ready"
touches = ["openfarm_core/bench.py"]
depends = ["CARD-009"]
category = "small-feature"
reference = "openfarm_core/dossier.py"
frontier_estimate = 0.75
new_symbols = true
unit = "."
type_changes = false

[[tests]]
name = "test_the_arm_table"
file = "tests/test_bench_report.py"
asserts = "render_report(score(...)) contains the line \"| farm | 3 | 2 | 66.7% | $0.30 | $0.15 |\""

[[tests]]
name = "test_no_cost_per_pass_is_a_dash"
file = "tests/test_bench_report.py"
asserts = "an arm with cost_per_pass None renders \"\u2014\" in the last column"

[[tests]]
name = "test_category_rows_are_sorted"
file = "tests/test_bench_report.py"
asserts = "category rows appear in sorted category order, each as \"| <category> | <farm %> | <solo %> |\" with \"\u2014\" for a missing arm"
+++

## Done

- `render_report(summary: dict) -> str` in `openfarm_core/bench.py`, taking `score()`'s answer.
- Starts with `# openfarm bench report`, then the table header `| Arm | Runs | Passed | Pass rate | Spend | Cost per pass |`, one row per arm sorted by name.
- Pass rate prints as a percentage with one decimal (`0.6667` -> `66.7%`); money prints as `$` plus two decimals; `None` prints `—`.
- Then `## By category` and `| Category | farm | solo |`, one row per category sorted by name, `—` where an arm has no runs.
