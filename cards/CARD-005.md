+++
id = "CARD-005"
title = "Adopt an existing git repository without touching its source"
state = "ready"
touches = ["openfarm_core/adopt.py"]
depends = ["CARD-004"]
category = "integration"
frontier_estimate = 2.00
new_symbols = true
unit = "."
type_changes = false
context_files = ["openfarm_core/git.py", "openfarm_core/bootstrap.py"]

[[tests]]
name = "test_adopt_writes_only_its_three_files"
file = "tests/test_adopt_project.py"
asserts = "on a committed python repo, adopt_project(root, state_dir=S, key_file=K) returns {\"ok\": True, \"language\": \"python\", \"written\": [\".openfarm/config.toml\", \".openfarm/PROJECT_SPEC.md\", \"cards/.gitkeep\"]} and the sha256 of every file that existed before is unchanged"

[[tests]]
name = "test_a_folder_that_is_not_a_repo_is_refused"
file = "tests/test_adopt_project.py"
asserts = "a dir with pyproject.toml and no .git raises Refused with .code == \"not-a-git-repo\""

[[tests]]
name = "test_uncommitted_changes_are_refused"
file = "tests/test_adopt_project.py"
asserts = "after Git(root).init() and an uncommitted file, Refused .code == \"dirty-worktree\""

[[tests]]
name = "test_a_second_adopt_is_refused"
file = "tests/test_adopt_project.py"
asserts = "calling adopt_project twice raises Refused .code == \"already-adopted\" the second time"

[[tests]]
name = "test_the_base_branch_comes_from_git"
file = "tests/test_adopt_project.py"
asserts = "a repo initialised with Git(root).init(branch=\"trunk\") gets [project] base_branch == \"trunk\""
+++

## Done

- `adopt_project(root: Path, *, language: str | None = None, state_dir: Path | None = None, key_file: Path | None = None) -> dict` in `openfarm_core/adopt.py`.
- Refuses with `Refused("not-a-git-repo")` when `root/.git` does not exist, `Refused("dirty-worktree")` when `Git(root).is_clean()` is False, and `Refused("already-adopted")` when `root/.openfarm/config.toml` exists, in that order.
- `language` defaults to `detect_language(root)`; the base branch is `Git(root).current_branch()`; `state_dir` defaults to `~/.openfarm/<root name>` and `key_file` to `~/.config/openfarm/<root name>.key`.
- Writes exactly `.openfarm/config.toml` (from `adopt_config_text`), `.openfarm/PROJECT_SPEC.md` (first line `# <root name>`), and an empty `cards/.gitkeep`, and returns `{"ok": True, "language": ..., "written": [those three paths, in that order]}`.
- Creates `state_dir` with mode `0o700`. Never commits, and never changes a file that existed before.
