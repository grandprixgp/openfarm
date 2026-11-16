+++
id = "CARD-002"
title = "openfarm --version prints the package version"
state = "ready"
touches = ["openfarm_core/cli.py"]
depends = []
category = "small-feature"
reference = "openfarm_core/cli.py"
frontier_estimate = 0.50
new_symbols = false
unit = "."
type_changes = false

[[tests]]
name = "test_version_flag_prints_the_package_version"
file = "tests/test_cli_version.py"
asserts = "main([\"--version\"]) raises SystemExit with code 0 and stdout == f\"openfarm {openfarm_core.__version__}\\n\""

[[tests]]
name = "test_version_flag_needs_no_repository"
file = "tests/test_cli_version.py"
asserts = "main([\"--version\"]) run from an empty temp directory (os.chdir) still exits 0 and prints the same line"
+++

## Done

- `openfarm --version` prints `openfarm <version>` where `<version>` is `openfarm_core.__version__`, then exits with code 0.
- It reads no config and needs no repository: it works from an empty directory.
- Add it to the top-level parser in `build_parser` (argparse `action="version"`); no other command changes.
