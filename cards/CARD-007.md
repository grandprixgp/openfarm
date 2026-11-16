+++
id = "CARD-007"
title = "Propose known_failing from a red baseline"
state = "ready"
touches = ["openfarm_core/adopt.py"]
depends = ["CARD-005"]
category = "small-feature"
reference = "openfarm_core/packs/python.py"
frontier_estimate = 0.50
new_symbols = true
unit = "."
type_changes = false

[[tests]]
name = "test_failed_names_are_proposed_sorted"
file = "tests/test_adopt_known_failing.py"
asserts = "propose_known_failing({\"results\": [{\"name\": \"b\", \"status\": \"FAILED\"}, {\"name\": \"a\", \"status\": \"FAILED\"}, {\"name\": \"c\", \"status\": \"ok\"}, {\"name\": \"b\", \"status\": \"FAILED\"}], \"compile_errors\": []}) == [\"a\", \"b\"]"

[[tests]]
name = "test_ignored_tests_are_not_proposed"
file = "tests/test_adopt_known_failing.py"
asserts = "a result with status \"ignored\" is left out"

[[tests]]
name = "test_a_suite_that_does_not_compile_is_refused"
file = "tests/test_adopt_known_failing.py"
asserts = "non-empty \"compile_errors\" raises Refused .code == \"suite-does-not-compile\""
+++

## Done

- `propose_known_failing(parsed: dict) -> list[str]` in `openfarm_core/adopt.py`, taking a pack's `parse_tests` answer.
- Returns the sorted, de-duplicated names of results whose `status` is `"FAILED"`; `"ok"` and `"ignored"` are left out.
- Raises `Refused("suite-does-not-compile", ...)` when `parsed["compile_errors"]` is non-empty, naming the first binary in the message.
