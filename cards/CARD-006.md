+++
id = "CARD-006"
title = "openfarm adopt command, and init points at it"
state = "ready"
touches = ["openfarm_core/cli.py", "openfarm_core/bootstrap.py"]
depends = ["CARD-002", "CARD-005"]
category = "small-feature"
reference = "openfarm_core/cli.py"
frontier_estimate = 1.00
new_symbols = false
unit = "."
type_changes = false

[[tests]]
name = "test_adopt_command_prints_json"
file = "tests/test_adopt_cli.py"
asserts = "main([\"--json\", \"adopt\", \"--dir\", str(root), \"--state-dir\", S, \"--key-file\", K]) returns 0 and stdout parses as JSON with \"language\" == \"python\""

[[tests]]
name = "test_adopt_command_reports_a_refusal"
file = "tests/test_adopt_cli.py"
asserts = "on a dir without .git, main([... \"adopt\" ...]) returns a non-zero code and the JSON has \"kind\" == \"refused\""

[[tests]]
name = "test_init_on_a_full_folder_names_adopt"
file = "tests/test_adopt_cli.py"
asserts = "initialize_project(\"x\", directory=<dir holding a file>) raises Refused .code == \"dir-not-empty\" with \"openfarm adopt\" in the message"
+++

## Done

- New subcommand `openfarm adopt [--dir DIR] [--lang rust|python] [--state-dir PATH] [--key-file PATH]` calling `openfarm_core.adopt.adopt_project`; `--dir` defaults to the current directory.
- With `--json` it prints the returned dict as one JSON object and exits 0; a `Refused` prints `{"error": <message>, "kind": "refused"}` and exits with the same code other refusals use.
- `initialize_project` on a non-empty directory keeps `Refused("dir-not-empty")` and its message now ends with `; to add openfarm to an existing project, run: openfarm adopt --dir <dir>`.
