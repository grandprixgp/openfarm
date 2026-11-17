+++
id = "CARD-016"
title = "Check a lesson's general claim against the project's own names"
state = "ready"
touches = ["openfarm_core/harness_scope.py"]
depends = []
category = "small-feature"
reference = "openfarm_core/harness_scope.py"
frontier_estimate = 0.5
new_symbols = false
unit = "."
type_changes = false

[[tests]]
name = "test_markers_hold_name_paths_file_names_and_long_stems"
file = "tests/test_harness_scope.py"
asserts = "project_markers(root, \"Widget\", [\"src/pkg/harness_memory.py\", \"cli.py\"]) contains \"widget\", \"src/pkg/harness_memory.py\", \"harness_memory.py\", \"harness_memory\", \"cli.py\", and does not contain \"cli\""

[[tests]]
name = "test_markers_hold_distinctive_top_level_names"
file = "tests/test_harness_scope.py"
asserts = "with root/app.py holding \"def load_settings():\\n    pass\\nclass Run:\\n    pass\\ndef go():\\n    pass\\nclass SessionStore:\\n    pass\\n\", project_markers(root, \"x\", [\"app.py\"]) contains \"load_settings\" and \"sessionstore\" and does not contain \"run\" or \"go\""

[[tests]]
name = "test_a_missing_file_is_skipped"
file = "tests/test_harness_scope.py"
asserts = "project_markers(root, \"x\", [\"gone.py\"]) == {\"x\", \"gone.py\"} without raising"

[[tests]]
name = "test_a_lesson_naming_a_marker_is_project"
file = "tests/test_harness_scope.py"
asserts = "scope_of({\"cue\": \"Call load_settings() before the run.\"}, {\"load_settings\"}) == (\"project\", \"names 'load_settings'\")"

[[tests]]
name = "test_a_marker_inside_a_longer_word_does_not_count"
file = "tests/test_harness_scope.py"
asserts = "scope_of({\"cue\": \"Use reloaded values.\", \"scope_reason\": \"any project\"}, {\"load\"}) == (\"general\", \"any project\")"

[[tests]]
name = "test_card_ids_and_file_hints_are_project"
file = "tests/test_harness_scope.py"
asserts = "scope_of({\"lesson\": \"As in CARD-12, check twice.\"}, set()) == (\"project\", \"names a card\"); scope_of({\"cue\": \"x\", \"proposed_patch\": {\"context_files\": [\"a.py\"]}}, set()) == (\"project\", \"names files\")"

[[tests]]
name = "test_a_clean_lesson_is_general"
file = "tests/test_harness_scope.py"
asserts = "scope_of({\"title\": \"t\", \"cue\": \"Check empty lists first.\"}, {\"widget\"}) == (\"general\", \"names nothing of this project\")"
+++

## Done

Fill in `project_markers` and `scope_of` in `openfarm_core/harness_scope.py` (both exist; replace their bodies). Leave `global_eligibility` alone. Use only the standard library (`re`, `json`, `pathlib`).

`project_markers(repo_root: Path, project_name: str, tracked_paths: list[str]) -> set[str]` returns lower-cased strings:
- the stripped project name, when not empty;
- each tracked path as given, its file name (`Path(p).name`), and its stem (`Path(p).stem`) when the stem is 5 or more characters long;
- for each tracked path ending `.py` or `.rs` whose file `repo_root / path` exists, is under 1,000,000 bytes and decodes as UTF-8: every name defined at the start of a line by `def NAME`, `async def NAME`, `class NAME`, `fn NAME`, `pub fn NAME`, `struct NAME`, `pub struct NAME`, `enum NAME`, `pub enum NAME`, `trait NAME` or `pub trait NAME`, kept only when it is distinctive: it contains `_` and is 6 or more characters long, or it is 10 or more characters long, or it has 2 or more capital letters. A file that is missing or unreadable is skipped, never an error.

`scope_of(entry: dict, markers: set[str]) -> tuple[str, str]`:
- The text checked is `title`, `cue` and `lesson` (missing ones are `""`) joined with newlines, plus `json.dumps(proposed_patch)` when the entry has one, all lower-cased.
- When the text matches `card-[0-9]+` it returns `("project", "names a card")`.
- When `proposed_patch` holds a non-empty list under `context_files`, `paths` or `files`, or a non-empty string under `path` or `file`, it returns `("project", "names files")`.
- When a marker appears in the text with no letter, digit or `_` immediately before or after it, it returns `("project", "names '<marker>'")` for the first such marker in sorted order. An empty marker is ignored.
- Otherwise it returns `("general", reason)` where reason is the entry's `scope_reason` when that is a non-empty string, else `"names nothing of this project"`.
