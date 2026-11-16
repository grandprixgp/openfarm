+++
id = "CARD-001"
title = "List pytest ids that contain spaces"
state = "ready"
touches = ["openfarm_core/packs/python.py"]
depends = []
category = "pinned-bugfix"
frontier_estimate = 0.50
new_symbols = false
unit = "."
type_changes = false

[[tests]]
name = "test_a_parametrised_id_with_spaces_is_listed"
file = "tests/test_python_pack_listing.py"
asserts = "PythonPack(runner=\"pytest\", ruff=False).parse_list(\"tests/test_x.py::test_p[a b]\\n\") == {\"tests\": [{\"name\": \"test_p[a b]\", \"binary\": \"tests/test_x.py\"}]}"

[[tests]]
name = "test_the_collected_summary_line_is_not_a_test"
file = "tests/test_python_pack_listing.py"
asserts = "parse_list(\"tests/test_x.py::test_q\\n\\n2 tests collected in 0.01s\\n\") returns exactly [{\"name\": \"test_q\", \"binary\": \"tests/test_x.py\"}]"
+++

## Done

- `PythonPack.parse_list` in pytest mode keeps every line that contains `::`, including ids whose parameter part holds spaces: `tests/test_x.py::test_p[a b]` returns name `test_p[a b]` and binary `tests/test_x.py`.
- Lines without `::` (the blank line, `2 tests collected in 0.01s`) are still left out.
- unittest-mode listing is unchanged.
