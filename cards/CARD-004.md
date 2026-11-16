+++
id = "CARD-004"
title = "Render an adopt config for an existing repository"
state = "ready"
touches = ["openfarm_core/adopt.py"]
depends = ["CARD-003"]
category = "feature"
frontier_estimate = 1.50
new_symbols = true
unit = "."
type_changes = false
context_files = ["openfarm_core/bootstrap.py"]

[[tests]]
name = "test_python_config_loads"
file = "tests/test_adopt_config.py"
asserts = "adopt_config_text(root, \"python\", base_branch=\"trunk\", state_dir=S, key_file=K) written to root/.openfarm/config.toml makes load_config(root).languages == [\"python\"] and .base_branch == \"trunk\""

[[tests]]
name = "test_rust_config_forbids_manifests"
file = "tests/test_adopt_config.py"
asserts = "for \"rust\" tomllib.loads(text)[\"policy\"][\"forbidden_files\"] == [\"**/Cargo.toml\", \"Cargo.lock\", \".openfarm/**\", \".claude/**\"]"

[[tests]]
name = "test_python_config_forbids_manifests"
file = "tests/test_adopt_config.py"
asserts = "for \"python\" forbidden_files == [\"pyproject.toml\", \"setup.py\", \".openfarm/**\", \".claude/**\"] and never_touch == []"

[[tests]]
name = "test_state_inside_the_repo_is_refused"
file = "tests/test_adopt_config.py"
asserts = "state_dir=root / \"state\" raises Refused with .code == \"state-inside-repo\""
+++

## Done

- `adopt_config_text(root: Path, language: str, *, base_branch: str, state_dir: Path, key_file: Path) -> str` in `openfarm_core/adopt.py`.
- The text parses with `tomllib` and `openfarm_core.config.load_config(root)` accepts it once written to `root/.openfarm/config.toml`.
- `[adapters] languages` is `[language]`; `[policy] never_touch` is `[]` (the owner fills it after the grill).
- `forbidden_files` is `["**/Cargo.toml", "Cargo.lock", ".openfarm/**", ".claude/**"]` for rust and `["pyproject.toml", "setup.py", ".openfarm/**", ".claude/**"]` for python.
- For python, `[gates] mutation` is `"off"` and `[runner] python` is `""`; for rust, `[runner] cargo_flags` is `["--locked"]` when `root/Cargo.lock` exists and `[]` otherwise.
- `state_dir` or `key_file` inside `root` raises `Refused("state-inside-repo", ...)`.
- Language other than `rust`/`python` raises `Refused("unsupported-language", ...)`.
