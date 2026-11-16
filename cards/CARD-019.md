+++
id = "CARD-019"
title = "A bench arm may choose which lessons apply"
state = "ready"
touches = ["openfarm_core/bench.py"]
depends = []
category = "small-feature"
reference = "openfarm_core/bench.py"
frontier_estimate = 0.4
new_symbols = false
unit = "."
type_changes = false

[[tests]]
name = "test_an_arm_may_set_learning_keys"
file = "tests/test_bench_arm_harness.py"
asserts = "an arm file holding '[harness]\\nlearn_from = \"global\"\\nshare = false\\n' loads with load_arm without raising"

[[tests]]
name = "test_other_harness_keys_are_refused"
file = "tests/test_bench_arm_harness.py"
asserts = "an arm file holding '[harness]\\nenabled = false\\n' makes load_arm raise Invalid whose message contains \"enabled\""

[[tests]]
name = "test_the_arm_replaces_or_adds_harness_lines"
file = "tests/test_bench_arm_harness.py"
asserts = "tomllib.loads(arm_config_text(base, '[harness]\\nlearn_from = \"project\"\\nglobal_dir = \"/tmp/g\"\\n'))[\"harness\"] has learn_from \"project\", global_dir \"/tmp/g\", share True and probe_window 3, where base is render_config(name=\"t\", language=\"python\", base_branch=\"main\", state_dir=Path(\"/tmp/s\"), key_file=Path(\"/tmp/k\"))"

[[tests]]
name = "test_an_arm_without_harness_leaves_it_alone"
file = "tests/test_bench_arm_harness.py"
asserts = "arm_config_text(base, '[model]\\nmax_tokens = 10\\n') has the same [harness] table as base"
+++

## Done

- `load_arm` also allows a `[harness]` table, holding only the keys `learn_from`, `share` and `global_dir`. Any other `[harness]` key raises `Invalid("bad-bench-arm", ...)` naming the keys, sorted, like the existing `[adapters]` check. The error for other tables lists `[harness]` among the allowed ones.
- `arm_config_text(base, arm)`: for each key the arm's `[harness]` sets, the line in the base's `[harness]` table that starts with that key followed by `=` is replaced with `key = <value>` (strings with `json.dumps`, booleans as `true`/`false`). A key the base's `[harness]` lacks is added as the last line of that table. Every other line of the base's `[harness]` stays as it was. An arm without `[harness]` changes nothing in it.
- The merged text still parses with `tomllib` (the function already checks this).
- `render_config` comes from `openfarm_core.config_template` (read-only here). Its `[harness]` table is the last one in the text and holds `learn_from` and `share` but not `global_dir`.
