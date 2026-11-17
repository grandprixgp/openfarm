+++
id = "CARD-018"
title = "openfarm harness --global shows and manages the global library"
state = "ready"
touches = ["openfarm_core/cli.py"]
depends = []
category = "small-feature"
reference = "openfarm_core/cli.py"
frontier_estimate = 0.5
new_symbols = false
unit = "."
context_files = ["openfarm_core/harness_global.py"]
type_changes = false

[[tests]]
name = "test_global_status_lists_the_library"
file = "tests/test_harness_global_cli.py"
asserts = "after GlobalLibrary(<tmp>/global).add(<lesson>, \"alpha\"), main([\"--repo\", repo, \"--json\", \"harness\", \"status\", \"--global\"]) returns 0 and prints JSON with \"active\" == 1, \"dir\" == str(<tmp>/global) and entries[0][\"sources\"] == [\"alpha\"]"

[[tests]]
name = "test_global_demote_retires_and_promote_restores"
file = "tests/test_harness_global_cli.py"
asserts = "\"harness\", \"demote\", <gid>, \"--global\", \"--reason\", \"noisy\" returns 0 and the entry's \"status\" becomes \"retired\" with \"retire_reason\" \"noisy\"; then \"harness\", \"promote\", <gid>, \"--global\" returns 0 and \"status\" is \"active\" again"

[[tests]]
name = "test_an_unknown_global_id_is_refused"
file = "tests/test_harness_global_cli.py"
asserts = "\"harness\", \"demote\", \"g-0000000000\", \"--global\" returns the refused exit code and prints \"kind\" == \"refused\" with \"code\" == \"unknown-global-entry\""

[[tests]]
name = "test_other_actions_do_not_take_global"
file = "tests/test_harness_global_cli.py"
asserts = "\"harness\", \"reset\", \"--yes\", \"--global\" returns a non-zero exit code and leaves the project's harness unchanged"
+++

## Done

- The `harness` subcommand gains a `--global` flag (`store_true`). Without it nothing changes.
- With `--global`, `cmd_harness` opens `GlobalLibrary(global_dir_for(ctx.cfg.harness))` (both from `openfarm_core.harness_global`, read-only here) and:
  - `status` emits `GlobalLibrary.status()`;
  - `promote <id>` emits `GlobalLibrary.restore(id, reason)` (an id is `g-` and 10 hex digits);
  - `demote <id>` emits `GlobalLibrary.retire(id, reason)`;
  - a missing `<id>` raises `UsageError` like the project versions;
  - an `Invalid` from the library emits `{"error": exc.message, "kind": "refused", "code": exc.code}` and returns `EXIT_REFUSED`;
  - any other action raises `UsageError("harness <action> does not take --global")`.
- `reason` is the existing `--reason` value (default `manual`).
- Tests write `.openfarm/config.toml` with `render_config(name="demo", language="python", base_branch="main", state_dir=<tmp>/"state", key_file=<tmp>/"k")` from `openfarm_core.config_template`, followed by the line `global_dir = "<tmp>/global"` (`[harness]` is the last table), and call `openfarm_core.cli.main` with `--repo` and `--json`, reading stdout with `contextlib.redirect_stdout`.
