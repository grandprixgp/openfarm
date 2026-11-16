+++
id = "CARD-011"
title = "openfarm bench list shows the task set"
state = "ready"
touches = ["openfarm_core/cli.py"]
depends = ["CARD-006", "CARD-008"]
category = "small-feature"
reference = "openfarm_core/cli.py"
frontier_estimate = 0.75
new_symbols = false
unit = "."
context_files = ["openfarm_core/bench.py"]
type_changes = false

[[tests]]
name = "test_bench_list_prints_tasks_sorted"
file = "tests/test_bench_cli.py"
asserts = "with two valid task dirs under <dir>, main([\"--json\", \"bench\", \"list\", \"--dir\", str(dir)]) returns 0 and prints {\"tasks\": [{\"id\", \"language\", \"category\"}...]} sorted by id"

[[tests]]
name = "test_a_bad_task_fails_the_listing"
file = "tests/test_bench_cli.py"
asserts = "a task.toml missing \"id\" makes the command exit non-zero with \"kind\" == \"invalid\" and the path in \"error\""
+++

## Done

- New subcommand `openfarm bench list [--dir DIR]` (`--dir` defaults to `bench`) that calls `load_task` on every direct subfolder holding a `task.toml`.
- `--json` prints `{"tasks": [{"id": ..., "language": ..., "category": ...}]}` sorted by `id`; without `--json` it prints one `id  language  category` line per task.
- `load_task(task_dir: Path) -> BenchTask` lives in `openfarm_core/bench.py` (read-only here); `BenchTask` has `id`, `language` and `category` attributes. Import it, don't search for it.
- An `Invalid` from `load_task` prints `{"error": <message>, "kind": "invalid"}` and exits with the code other invalid answers use.
