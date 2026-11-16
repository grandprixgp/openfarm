+++
id = "CARD-014"
title = "Backfill tests for Python error signatures from pytest output"
state = "ready"
touches = ["tests/test_python_signature_pytest.py"]
depends = []
category = "test-backfill"
frontier_estimate = 0.25
new_symbols = false
unit = "."
type_changes = false

[[tests]]
name = "test_pytest_failed_lines_are_named"
file = "tests/test_python_signature_pytest.py"
asserts = "PythonPack(runner=\"pytest\", ruff=False).error_signature(\"FAILED tests/test_x.py::test_a - assert 1 == 2\\n\")[\"failed_tests\"] == [\"tests/test_x.py::test_a\"]"

[[tests]]
name = "test_pytest_exception_lines_are_codes"
file = "tests/test_python_signature_pytest.py"
asserts = "an \"E   KeyError: 'k'\" line gives codes == [\"KeyError\"]"

[[tests]]
name = "test_the_signature_ignores_durations"
file = "tests/test_python_signature_pytest.py"
asserts = "the same failure with \"in 0.05s\" and \"in 1.20s\" footers gives the same \"signature\""
+++

## Done

- Tests only, for the existing `PythonPack.error_signature` on pytest-shaped output: `FAILED <nodeid> - ...` lines appear in `failed_tests`, `E   <Name>Error:` lines appear in `codes`, and footer durations do not change `signature`.
- No production file changes.
