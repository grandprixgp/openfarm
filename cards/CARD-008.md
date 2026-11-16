+++
id = "CARD-008"
title = "Load and validate a bench task"
state = "ready"
touches = ["openfarm_core/bench.py"]
depends = []
category = "feature"
reference = "openfarm_core/packs/python.py"
frontier_estimate = 1.00
new_symbols = true
unit = "."
type_changes = false

[[tests]]
name = "test_a_valid_task_loads"
file = "tests/test_bench_tasks.py"
asserts = "a task dir with task.toml (id=\"py-full\", language=\"python\", category=\"pinned-bugfix\", card=\"card.md\", hidden_tests=[\"hidden/test_full.py\"]) and those files loads into a BenchTask with those values and root == the dir"

[[tests]]
name = "test_a_missing_key_is_named"
file = "tests/test_bench_tasks.py"
asserts = "task.toml without \"category\" raises Invalid .code == \"bad-bench-task\" with \"category\" in the message"

[[tests]]
name = "test_a_missing_hidden_test_file_is_refused"
file = "tests/test_bench_tasks.py"
asserts = "hidden_tests naming a file that does not exist raises Invalid \"bad-bench-task\" naming that path"

[[tests]]
name = "test_an_unknown_language_is_refused"
file = "tests/test_bench_tasks.py"
asserts = "language=\"go\" raises Invalid \"bad-bench-task\""
+++

## Done

- In the skeleton module `openfarm_core/bench.py`, add a frozen dataclass `BenchTask(id: str, language: str, category: str, card: Path, hidden_tests: tuple[Path, ...], root: Path)` and `load_task(task_dir: Path) -> BenchTask`.
- `load_task` reads `task_dir / "task.toml"`. Required keys: `id`, `language` (`"rust"` or `"python"`), `category` (one of `small-feature`, `pinned-bugfix`, `pattern`, `refactor`), `card` (a relative path that exists), `hidden_tests` (a non-empty list of relative paths that exist).
- `card` and each `hidden_tests` entry become absolute paths under `task_dir`; a path that climbs out of `task_dir` raises.
- Every problem raises `openfarm_core.errors.Invalid("bad-bench-task", ...)` whose message names the key or path at fault.
