# Openfarm v0.4 Engineering Handoff

Concise entry point for engineers reviewing, testing, or extending Openfarm v0.4.

## What Changed in v0.4

1. **Expanded Card Diet & Authoring (`cards.py`, `eligibility.py`, `packs/rust.py`, `edits.py`)**:
   - Added `feature`, `refactor`, `integration`, and `port` alongside the v0.3 diet (`test-backfill`, `mechanical`, `pattern`, `pinned-bugfix`, `small-feature`).
   - Added optional card metadata fields (`unit`, `context_files`, `type_changes`) and fenced ` ```openfarm ` / ` ```toml ` metadata blocks (`[cards] fields_block = "toml"`).
   - Relaxed multi-file `new_symbols` restriction when `type_changes = true` or `[micromanager] scaffold_contracts = true`.
   - Upgraded `RustPack.signatures()` to extract full struct/enum/trait/type-alias/const/static/union/`mod`/`impl` signatures, added atomic multi-file `apply_edits`, and added checkpointed `plan.steps` execution in Phase B.
2. **6th Built-in Adapter & ACP Micromanager (`micromanager.py`, `adapters/acp.py`)**:
   - Added `[adapters] micromanager` (`off`, `claude-acp`, `scripted`, `cmd:<argv>`), alongside `telemetry` and `pack` `cmd:<argv>` support across all six adapters (`PROTOCOL_VERSION = 1`).
   - Implemented `ClaudeAcpAdapter` speaking Agent Client Protocol v1 (NDJSON JSON-RPC 2.0 over stdio, defaulting to `claude-agent-acp`).
   - Added five bounded supervisory ops (`shape_and_plan`, `scaffold_contract`, `coach`, `adjudicate_checker`, `triage_and_review`), `[micromanager]` budget/policy controls, `supervisor-reject` stop code, `openfarm supervise`, and `openfarm run --mm-script`.
3. **Security & Gate Hardening**:
   - ACP capability lock-down (`clientCapabilities: {}`, `-32601` for write/terminal RPCs, `session/request_permission` restricted to `"none"` or `"read-only"` inside worktree excluding `never_touch`/`forbidden_files`, isolated `$HOME`, key isolation, ANSI/OSC stripping, `todo!()`/`unimplemented!()` stub validation).
   - Phase A fail-before retry loop inside `test_writer_attempts` (with `scaffold_commit`).
   - Formatting before `full_diff` and `revert-check`; `revert-check` evaluated against `base + frozen-tests`.
   - Split stdout/stderr Cargo parser with signal detection; `--test <stem>` counter-test execution and bounce-back adjudication.
   - Safe git flag/config allowlist, `_safe_id` card-ID path sanitization, `[state] min_free_mb` disk guard, HTTP `429`/`5xx` retries, strict config table/key validation (`[cards]`, `[micromanager]`, `[state]`, new `[eligibility]`/`[gates]`/`[adapters]` keys), and real card IDs in `breaker.json`.

## Where to Start Reading

1. `openfarm_core/config.py` and `tests/test_project_config.py` — complete 12-table configuration contract (`KNOWN_TABLES`, `_TABLE_KEYS`).
2. `openfarm_core/adapters/acp.py` and `openfarm_core/micromanager.py` — ACP v1 protocol security boundaries, stub checks, budget caps, and the five supervisory ops.
3. `openfarm_core/flow.py` and `openfarm_core/cli.py` — Phase A/B/checker state machine, step checkpoints, `supervise` CLI, and stop-code transitions (`openfarm_core/errors.py`).
4. `openfarm_core/packs/rust.py` and `openfarm_core/gates/checks.py` — signature extraction, split diagnostics, `--test <stem>` counter-tests, and `revert-check`.
5. `tests/acp_stand_in.py` and `tests/test_acp_micromanager.py` — deterministic stdio NDJSON-RPC stand-in server used to verify ACP behavior offline.

## Security & Trust Rules to Preserve When Modifying

- Never add filesystem-write or shell/terminal capabilities to `ClaudeAcpAdapter.initialize`.
- Never relax `_is_path_allowed_readonly` in `openfarm_core/adapters/acp.py`: read-only micromanager access must remain bounded by `resolves_within` against the active card worktree and blocked from `never_touch` / `forbidden_files` (including `.openfarm/**`).
- Keep `[model] key_file` and host credentials strictly separated from `_sanitised_env` in the ACP adapter.
- Keep deterministic gates (`format`, `test`, `lint`, `revert-check`, `mutation`, `coverage`, `extra`) independent of micromanager overrides; `advisory` mode must never mutate repository files or block deterministic passes.

## Explicitly Untested Integrations

Do not assume live external coverage for items not exercised against live services/binaries here:
- **Live `claude-agent-acp` binary + real Anthropic auth**: verified only via `tests/acp_stand_in.py` over real stdio NDJSON-RPC pipes; live `claude-agent-acp` runs are **untested**.
- **Live hosted ZDR OpenAI-compatible endpoints**: verified only against local HTTP test servers and `ScriptedBackend`; live hosted endpoints are **untested**.
- **Live `cargo-mutants` runs**: parser and mutation gate logic tested with synthetic outputs; live `cargo-mutants` binary runs are **untested**.
- **TypeScript language pack**: unimplemented (`Invalid("no-typescript-pack")`).
- **Real external `cmd:` adapter binaries**: protocol verified via test subprocesses; external production binaries are **untested**.

## Suggested Next Checks for Live Bring-Up

1. Verify `claude-agent-acp --version` and run `openfarm supervise --prepare-queue` on a disposable target repo in `mode = "advisory"` first, inspecting `<state_dir>/cards/<id>/dossier.md` and micromanager transcripts.
2. Run a single `test-backfill` card against the intended hosted ZDR endpoint (`openfarm check <id>` then `openfarm run`) to validate live model formatting, token accounting, and `revert-check` end-to-end.
3. If `gates.mutation = "required"` or `"report"` is enabled on Rust crates, install and smoke-test `cargo-mutants` on the target repo's unit test suite.
