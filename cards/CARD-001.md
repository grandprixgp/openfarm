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
name = "test_a_class_parametrised_id_with_spaces_is_listed"
file = "tests/test_python_pack_listing.py"
asserts = "parse_list(\"tests/test_x.py::TestK::test_p[1 2]\\n\") == {\"tests\": [{\"name\": \"test_p[1 2]\", \"binary\": \"tests/test_x.py::TestK\"}]}"
+++

## Done

- `PythonPack.parse_list` in pytest mode keeps every line that contains `::`, including ids whose parameter part holds spaces: `tests/test_x.py::test_p[a b]` returns name `test_p[a b]` and binary `tests/test_x.py`, and `tests/test_x.py::TestK::test_p[1 2]` returns name `test_p[1 2]` and binary `tests/test_x.py::TestK`.
- Lines without `::` (the blank line, `2 tests collected in 0.01s`) are still left out.
- unittest-mode listing is unchanged.
