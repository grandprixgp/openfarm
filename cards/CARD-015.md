+++
id = "CARD-015"
title = "Rules for promoting, vetoing and retiring global lessons"
state = "ready"
touches = ["openfarm_core/harness_global.py"]
depends = []
category = "small-feature"
reference = "openfarm_core/harness_global.py"
frontier_estimate = 0.25
new_symbols = false
unit = "."
type_changes = false

[[tests]]
name = "test_a_proven_general_lesson_is_promoted"
file = "tests/test_global_rules.py"
asserts = "should_promote({\"scope\": \"general\", \"status\": \"active\", \"stats\": {\"helped\": 3, \"hurt\": 2}}) is True"

[[tests]]
name = "test_unproven_or_project_lessons_are_not_promoted"
file = "tests/test_global_rules.py"
asserts = "should_promote returns False for helped 2 hurt 0; helped 3 hurt 3; scope \"project\"; status \"probing\"; a missing \"scope\"; and a missing \"stats\""

[[tests]]
name = "test_a_veto_needs_more_hurt_than_help"
file = "tests/test_global_rules.py"
asserts = "should_veto({\"helped\": 1, \"hurt\": 2}) is True; should_veto({\"helped\": 2, \"hurt\": 2}) is False; should_veto({}) is False"

[[tests]]
name = "test_two_vetoes_retire_a_lesson"
file = "tests/test_global_rules.py"
asserts = "retire_reason({\"projects\": {\"a\": {\"helped\": 5, \"hurt\": 0, \"vetoed\": True}, \"b\": {\"helped\": 5, \"hurt\": 0, \"vetoed\": True}}}) == \"vetoed by 2 projects\""

[[tests]]
name = "test_a_negative_combined_score_retires_a_lesson"
file = "tests/test_global_rules.py"
asserts = "retire_reason({\"projects\": {\"a\": {\"helped\": 1, \"hurt\": 0, \"vetoed\": False}, \"b\": {\"helped\": 0, \"hurt\": 2, \"vetoed\": True}}}) == \"combined score -1\""

[[tests]]
name = "test_a_lesson_in_good_standing_is_kept"
file = "tests/test_global_rules.py"
asserts = "retire_reason({\"projects\": {\"a\": {\"helped\": 2, \"hurt\": 1, \"vetoed\": True}}}) == \"\" and retire_reason({}) == \"\""
+++

## Done

Fill in the three rule functions near the top of `openfarm_core/harness_global.py`. They exist already and return `False`, `False` and `""`. Change only their bodies (and their docstrings, if you like); nothing else in the file.

- `should_promote(entry: dict) -> bool` is True exactly when `entry["scope"] == "general"`, `entry["status"] == "active"`, and, reading `entry["stats"]`, `helped >= 3` and `hurt < helped`. A missing key counts as `""` for `scope` and `status` and as `0` for a stat; a missing `stats` counts as `{}`.
- `should_veto(stats: dict) -> bool` is True exactly when `stats["hurt"] > stats["helped"]`, a missing stat counting as `0`.
- `retire_reason(global_entry: dict) -> str` reads `global_entry["projects"]`, a dict from project name to `{"helped": int, "hurt": int, "vetoed": bool}` (missing `projects` counts as `{}`, missing numbers as `0`, missing `vetoed` as False):
  - when 2 or more projects have `vetoed` True it returns `"vetoed by N projects"` with N that count;
  - otherwise, when the sum over all projects of `helped - hurt` is below 0, it returns `"combined score S"` with S that sum (for example `"combined score -1"`);
  - otherwise `""`.
