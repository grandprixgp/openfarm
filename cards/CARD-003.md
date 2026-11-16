+++
id = "CARD-003"
title = "Detect an existing repository's language"
state = "ready"
touches = ["openfarm_core/adopt.py"]
depends = []
category = "feature"
reference = "openfarm_core/packs/python.py"
frontier_estimate = 1.00
new_symbols = true
unit = "."
type_changes = false

[[tests]]
name = "test_a_cargo_manifest_means_rust"
file = "tests/test_adopt_detect.py"
asserts = "a temp dir holding only Cargo.toml: detect_language(root) == \"rust\""

[[tests]]
name = "test_a_pyproject_or_setup_means_python"
file = "tests/test_adopt_detect.py"
asserts = "pyproject.toml alone returns \"python\"; setup.py alone returns \"python\""

[[tests]]
name = "test_both_manifests_are_ambiguous"
file = "tests/test_adopt_detect.py"
asserts = "Cargo.toml plus pyproject.toml raises Refused with .code == \"ambiguous-language\""

[[tests]]
name = "test_no_manifest_is_unsupported"
file = "tests/test_adopt_detect.py"
asserts = "an empty dir raises Refused with .code == \"unsupported-language\"; with package.json the message contains \"TypeScript\""
+++

## Done

- In the skeleton module `openfarm_core/adopt.py`, add `detect_language(root: Path) -> str`.
- Returns `"rust"` when `root / "Cargo.toml"` is a file and neither `pyproject.toml` nor `setup.py` is.
- Returns `"python"` when `pyproject.toml` or `setup.py` is a file and `Cargo.toml` is not.
- Raises `openfarm_core.errors.Refused("ambiguous-language", ...)` when both kinds are present; the message says to pass `--lang rust` or `--lang python`.
- Raises `Refused("unsupported-language", ...)` when none is present; when `package.json` exists the message contains `TypeScript has no pack yet`.
- Only the top level of `root` is looked at; nothing is written.
