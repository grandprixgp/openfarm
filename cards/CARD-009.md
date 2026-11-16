+++
id = "CARD-009"
title = "Score bench runs per arm and category"
state = "ready"
touches = ["openfarm_core/bench.py"]
depends = ["CARD-008"]
category = "feature"
frontier_estimate = 1.00
new_symbols = true
unit = "."
type_changes = false

[[tests]]
name = "test_pass_rate_and_cost_per_pass"
file = "tests/test_bench_score.py"
asserts = "records farm: (passed, 0.10), (failed, 0.05), (passed, 0.15) give arms[\"farm\"] == {\"runs\": 3, \"passed\": 2, \"pass_rate\": 0.6667, \"spend\": 0.3, \"cost_per_pass\": 0.15}"

[[tests]]
name = "test_no_pass_has_no_cost_per_pass"
file = "tests/test_bench_score.py"
asserts = "an arm with zero passed has \"cost_per_pass\": None"

[[tests]]
name = "test_categories_split_by_arm"
file = "tests/test_bench_score.py"
asserts = "categories[\"pinned-bugfix\"] == {\"farm\": 1.0, \"solo\": 0.5} for one farm pass and one solo pass plus one solo fail"

[[tests]]
name = "test_an_unknown_arm_is_refused"
file = "tests/test_bench_score.py"
asserts = "arm \"other\" raises Invalid .code == \"bad-bench-record\""

[[tests]]
name = "test_no_records_score_empty"
file = "tests/test_bench_score.py"
asserts = "score([]) == {\"arms\": {}, \"categories\": {}}"
+++

## Done

- `score(records: list[dict]) -> dict` in `openfarm_core/bench.py`. A record has `task`, `category`, `arm` (`"farm"` or `"solo"`), `passed` (bool), `spend` (float), `tokens` (int), `seconds` (float), `diff_lines` (int).
- Returns `{"arms": {arm: {"runs", "passed", "pass_rate", "spend", "cost_per_pass"}}, "categories": {category: {arm: pass_rate}}}`.
- `pass_rate` is `passed / runs` rounded to 4 places; `spend` is the sum rounded to 4 places; `cost_per_pass` is `round(spend / passed, 4)` or `None` when `passed == 0`.
- An arm other than `farm`/`solo`, or a missing key, raises `Invalid("bad-bench-record", ...)` naming it.
