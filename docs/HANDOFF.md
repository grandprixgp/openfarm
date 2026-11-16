# Openfarm v0.7.0 Engineering Handoff

Concise entry point for engineers reviewing, testing, or extending Openfarm v0.7.0.

For greenfield empty-folder initialization (`"Read and install this skill <link>"` -> `/openfarm-grill-me` -> `"/openfarm-initialize calorie-log ..."`), see [docs/FROM-IDEA-TO-SHIPPED-APP.md](FROM-IDEA-TO-SHIPPED-APP.md). For step-by-step operator instructions, 13-table config reference, card authoring rules, and the executable `tallybox` first-deployment runbook on existing repos, see [docs/USAGE-AND-FIRST-DEPLOYMENT.md](USAGE-AND-FIRST-DEPLOYMENT.md).

## What Changed in v0.4

1. **Expanded Card Diet & Authoring (`cards.py`, `eligibility.py`, `packs/rust.py`, `edits.py`)**:
   - Added `feature`, `refactor`, `integration`, and `port` alongside the v0.3 diet (`test-backfill`, `mechanical`, `pattern`, `pinned-bugfix`, `small-feature`).
   - Added optional card metadata fields (`unit`, `context_files`, `type_changes`) and fenced ` ```openfarm ` / ` ```toml ` metadata blocks (`[cards] fields_block = "toml"`).
   - Relaxed multi-file `new_symbols` restriction when `type_changes = true` or `[micromanager] scaffold_contracts = true`.
   - Upgraded `RustPack.signatures()` to extract full struct/enum/trait/type-alias/const/static/union/`mod`/`impl` signatures, added atomic multi-file `apply_edits`, and added checkpointed `plan.steps` execution in Phase B.
2. **6th Built-in Adapter & ACP Micromanager (`micromanager.py`, `adapters/acp.py`)**:
   - Added `[adapters] micromanager` (`off`, `claude-acp`, `scripted`, `cmd:<argv>`), alongside `telemetry` and `pack` `cmd:<argv>` support across all six adapters (`PROTOCOL_VERSION = 1`).
   - Implemented `ClaudeAcpAdapter` speaking Agent Client Protocol v1 (NDJSON JSON-RPC 2.0 over stdio, defaulting to `claude-agent-acp`).
   - Added six bounded supervisory ops (`shape_and_plan`, `scaffold_contract`, `coach`, `adjudicate_checker`, `triage_and_review`, `reflect_harness`), `[micromanager]` budget/policy controls, `supervisor-reject` stop code, `openfarm supervise`, and `openfarm run --mm-script`.
3. **Security & Gate Hardening**:
   - ACP capability lock-down (`clientCapabilities: {}`, `-32601` for write/terminal RPCs, `session/request_permission` restricted to `"none"` or `"read-only"` inside worktree excluding `never_touch`/`forbidden_files`, isolated `$HOME`, key isolation, ANSI/OSC stripping, `todo!()`/`unimplemented!()` stub validation).
   - Phase A fail-before retry loop inside `test_writer_attempts` (with `scaffold_commit`).
   - Formatting before `full_diff` and `revert-check`; `revert-check` evaluated against `base + frozen-tests`.
   - Split stdout/stderr Cargo parser with signal detection; `--test <stem>` counter-test execution and bounce-back adjudication.
   - Safe git flag/config allowlist, `_safe_id` card-ID path sanitization, `[state] min_free_mb` disk guard, HTTP `429`/`5xx` retries, strict config table/key validation (`[cards]`, `[micromanager]`, `[state]`, `[harness]`, new `[eligibility]`/`[gates]`/`[adapters]` keys), and real card IDs in `breaker.json`.
4. **Recursive Harness Efficiency (`harness_memory.py`, `[harness]`, `HarnessLibrary`, `HarnessLedger`)**:
   - Inspired by `google-research/harness2` (`arXiv:2610.14224`), stores overlays, `revisions/rev-XXXX.json`, `entries/<id>/EXPERIENCE.md` + `patch.json`, rebuilt `INDEX.md`, `ledger.jsonl`, and `metrics.json` outside the repo under `<state_dir>/harness/`.
   - Deterministic verification and policy/budget gates (`IMMUTABLE_GATE_KEYS`) remain strictly immutable; adaptations target only the 5 `EDITABLE_SURFACES` (`test_writer_guidance`, `implementer_guidance`, `planner_guidance`, `context_hints`, `micromanager_context`).
   - Dual capture via deterministic `extract_contrast` (even with micromanager `"off"`) and `reflect_harness`, sanitized against edit markers, path traversals, assertion leaks, and gate-bypass directives (`validate_harness_proposal`, `sanitize_lesson_text`).
   - Intra-run transfer (Card N teaches Card N+1 + dynamic retry failure-class lookup) and cross-launch persistence feeding fenced `harness adaptations` in briefs and `micromanager.harness_context`.
   - Propose–probe–confirm/auto-demote rollback on efficiency regression, `Infra` isolation (machine/infra aborts never skew metrics or trigger rollbacks), card telemetry (`harness_revision`, `harness_cues_applied`, `harness_lessons_added`), dossier summary, and the 12th CLI subcommand `openfarm harness` (`status`, `index`, `show`, `promote`, `demote`, `reset --yes`).

## Where to Start Reading

1. `openfarm_core/config.py` and `tests/test_project_config.py` — complete 13-table configuration contract (`KNOWN_TABLES`, `_TABLE_KEYS`, `HarnessConfig`).
2. `openfarm_core/adapters/acp.py`, `openfarm_core/adapters/claude_native_acp.py`, `openfarm_core/acp_setup.py`, and `openfarm_core/micromanager.py` — ACP v1 protocol security boundaries, native `claude -p` OAuth bridge (`binary = "auto"`, `auth = "oauth"` default for Frontier while Workers use `[model] key_file`), `openfarm acp` CLI helpers, stub checks, budget caps, and the six supervisory ops (verified by `tests/test_v051_acp_oauth_grill.py`).
3. `openfarm_core/harness_memory.py` and `tests/test_harness_efficiency.py` — `HarnessLibrary`, `HarnessLedger`, contrast capture, anti-injection validation, probe-window rollback, and `openfarm harness` CLI.
4. `openfarm_core/flow.py`, `openfarm_core/dossier.py`, `openfarm_core/bootstrap.py`, and `openfarm_core/cli.py` — Phase A/B/checker state machine, step checkpoints, greenfield project bootstrap (`openfarm init`) & bundled skill installer (`openfarm install-skill`, `openfarm_core/skill_assets/` incl. `openfarm-grill-me`, `pyproject.toml`, `SKILL.md`, `skills/`, `Git.init`), 18-subcommand CLI (`PRE_CONFIG_COMMANDS` for `init`, `adopt`, `bench`, `install-skill`, `acp`, plus `supervise`, `harness`, `done`), and stop-code transitions (`openfarm_core/errors.py`), verified by `tests/test_v05_skill_bootstrap.py`.
5. `openfarm_core/packs/rust.py` and `openfarm_core/gates/checks.py` — signature extraction, split diagnostics, `--test <stem>` counter-tests, and `revert-check`.
6. `tests/acp_stand_in.py`, `tests/test_acp_micromanager.py`, `tests/test_core_io_v04.py`, `tests/test_pack_gates_v04.py`, and `tests/test_v04_e2e.py` — deterministic unit, I/O, gate, ACP stand-in, and end-to-end suites.

## Security & Trust Rules to Preserve When Modifying

- Never add filesystem-write or shell/terminal capabilities to `ClaudeAcpAdapter.initialize`.
- Never relax `_is_path_allowed_readonly` in `openfarm_core/adapters/acp.py`: read-only micromanager access must remain bounded by `resolves_within` against the active card worktree and blocked from `never_touch` / `forbidden_files` (including `.openfarm/**`).
- Keep `[model] key_file` and host credentials strictly separated from `_sanitised_env` in the ACP adapter.
- Keep deterministic gates (`format`, `test`, `lint`, `revert-check`, `mutation`, `coverage`, `extra`) independent of micromanager overrides; `advisory` mode must never mutate repository files or block deterministic passes.
- Never add deterministic gates, policy lists, touches, or budgets (`IMMUTABLE_GATE_KEYS`) to `EDITABLE_SURFACES`, never bypass `validate_harness_proposal` / `sanitize_lesson_text`, and never record `Infra` aborts as harness failures in `HarnessLibrary` / `HarnessLedger`.

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
