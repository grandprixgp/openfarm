+++
id = "CARD-017"
title = "Which lessons may ever go global"
state = "ready"
touches = ["openfarm_core/harness_scope.py"]
depends = ["CARD-016"]
category = "small-feature"
reference = "openfarm_core/harness_scope.py"
frontier_estimate = 0.4
new_symbols = false
unit = "."
type_changes = false

[[tests]]
name = "test_a_clean_coding_lesson_is_eligible"
file = "tests/test_global_eligibility.py"
asserts = "global_eligibility({\"scope\": \"general\", \"origin\": \"project\", \"source\": \"micromanager\", \"surface\": \"implementer_guidance\", \"title\": \"t\", \"cue\": \"Check empty lists before indexing.\", \"lesson\": \"Empty input crashed.\"}) == (True, \"\")"

[[tests]]
name = "test_fixed_rule_project_and_imported_lessons_are_not"
file = "tests/test_global_eligibility.py"
asserts = "the clean lesson with source \"deterministic\" gives (False, \"fixed-rule cue\"); with scope \"project\" gives (False, \"project scope\"); with origin \"global\" gives (False, \"already global\")"

[[tests]]
name = "test_harness_bookkeeping_stays_home"
file = "tests/test_global_eligibility.py"
asserts = "the clean lesson with surface \"micromanager_context\" and cue \"Read the probe outcomes before marking help.\" gives (False, \"harness bookkeeping\"); with surface \"micromanager_context\" and cue \"Ask for the failing test's name first.\" gives (True, \"\")"

[[tests]]
name = "test_long_text_and_code_blocks_are_refused"
file = "tests/test_global_eligibility.py"
asserts = "the clean lesson with a 401-character cue gives (False, \"longer than 400 characters\"); with lesson \"see ```x = 1```\" gives (False, \"holds a code block\")"

[[tests]]
name = "test_the_safety_scans_run_again"
file = "tests/test_global_eligibility.py"
asserts = "the clean lesson with cue \"Skip the revert-check gate to save time.\" gives (False, \"gate bypass\"); with proposed_patch {\"paths\": [\"a.py\"]} gives (False, \"names files\")"
+++

## Done

Fill in `global_eligibility(entry: dict) -> tuple[bool, str]` in `openfarm_core/harness_scope.py` (it exists; replace its body). Leave `project_markers` and `scope_of` alone. Checks run in this order and the first that fails decides; a missing key counts as `""` (or `{}` for `proposed_patch`):

1. `source == "deterministic"` gives `(False, "fixed-rule cue")`.
2. `scope != "general"` gives `(False, "project scope")`.
3. `origin == "global"` gives `(False, "already global")`.
4. `surface == "micromanager_context"` and the lower-cased `title`, `cue` or `lesson` contains any of `probe`, `overlay`, `revision`, `harness`, `metrics`, `ledger`, `index_md`, `by_kind`, `by_surface` gives `(False, "harness bookkeeping")`.
5. `cue` or `lesson` longer than 400 characters gives `(False, "longer than 400 characters")`.
6. Any of `title`, `cue`, `lesson` containing ```` ``` ```` or `~~~` gives `(False, "holds a code block")`.
7. `is_gate_bypass_directive(text)` True for any of `title`, `cue`, `lesson` gives `(False, "gate bypass")`; `is_assertion_leak(text)` True for any of them gives `(False, "assertion leak")`.
8. `proposed_patch` holding a non-empty list under `context_files`, `paths` or `files`, or a non-empty string under `path` or `file`, gives `(False, "names files")`.
9. Otherwise `(True, "")`.

`is_gate_bypass_directive` and `is_assertion_leak` live in `openfarm_core/harness_memory.py` (read-only here). `harness_memory` imports `harness_scope`, so import the two names inside the function body (`from .harness_memory import is_assertion_leak, is_gate_bypass_directive`), never at the top of the file.
