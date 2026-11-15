# Openfarm v0.4 Internals

Technical reference for `openfarm_core`, its six protocol-versioned adapters, configuration schema, execution flow, security boundaries, and gate hardening.

## Module Layout (`openfarm_core/`)

- `config.py`: Strict TOML loader (`KNOWN_TABLES`, `_TABLE_KEYS`) with fail-closed rejection (`ConfigError`) on unknown tables, unknown keys, conflicting keys (e.g. `timeout` vs `timeout_s`), invalid policy/globs, or paths escaping the repo root.
- `cards.py`: Card parser (`parse_card`), `Card` / `TestRef` models, `CARD_FIELDS` and `OPTIONAL_CARD_FIELDS` (`unit`, `context_files`, `type_changes`), and fenced TOML block extractor (`extract_fenced_fields` supporting ` ```openfarm ` and ` ```toml `).
- `errors.py`: `StopCode` enum (17 per-card stop codes including `supervisor-reject`, plus `killed`, `released`, `paused`), `RELEASING_CODES`, and typed exception hierarchy (`AdapterError`, `Refused`, `Infra`, `Invalid`, `Unsupported`, `ConfigError`, `UsageError`, `GateFailure`).
- `protocol.py`: `PROTOCOL_VERSION = 1`, `REQUIRED_OPS` / `OPTIONAL_OPS` for all 6 adapters (`cards`, `runner`, `pack`, `model`, `telemetry`, `micromanager`), and JSON-over-stdio `CmdAdapter` wrappers (`CmdCardSource`, `CmdModelBackend`, `CmdTelemetry`, `CmdPack`). `CmdRunner` lives in `adapters/direct.py` and `CmdMicromanager` lives in `micromanager.py`.
- `globs.py`: Repo-relative glob normalization, `resolves_within` path-traversal guard, and matchers for `never_touch` / `forbidden_files`.
- `git.py`: Git worktree lifecycle (`worktree_add`, `worktree_remove`, baseline reset), commit helpers, safe flag/config allowlisting (rejecting arbitrary `-c` / unsafe options), and repo lock coordination.
- `state.py`: On-disk state under `state_dir` (`cards/<safe_id>/`, `leases/`, `breaker.json`, `STOP`), card ID path sanitization (`_safe_id` preventing path traversal), atomic writes, and disk-space floor enforcement (`[state] min_free_mb`).
- `budget.py`: Per-card and nightly token/spend/build/wall-clock accounting (`BudgetTracker`), frontier cost ratio checks, and lease TTL checks.
- `edits.py`: Structured search/replace/create/delete edit parser and atomic multi-file `apply_edits` (rolling back all files in a batch if any edit fails or touches a forbidden path).
- `briefs.py`: Prompt assembly for `test_writer`, `implementer`, `checker`, and `planner`, incorporating `reference`, `context_files`, rich Rust `signatures()`, `flagged_patterns`, and sanitized failure logs.
- `roles/__init__.py`: Role dispatch and response parsing for `test_writer`, `implementer`, `checker`, and `planner` (`plan.steps` multi-step output).
- `micromanager.py`: Supervisory orchestration (`Micromanager`, `NullMicromanager`, `ScriptedMicromanager`, `CmdMicromanager`, `ClaudeAcpMicromanager`, `open_micromanager`) implementing 5 ops: `shape_and_plan`, `scaffold_contract`, `coach`, `adjudicate_checker`, `triage_and_review` (`pre_review`, `triage_blocked`, `prepare_queue`). Enforces `MicromanagerBudget` (`max_calls_per_card`, `tokens_per_card`, `spend_per_night`), strips/blocks edit blocks outside contracts (`_strip_edit_blocks`), validates production `todo!()`/`unimplemented!()` stubs (`_valid_production_scaffold`), and sanitizes paths (`_validate_safe_path`).
- `adapters/`:
  - `files.py`: `FileCardSource` reading markdown cards and applying safe card/checkbox updates.
  - `direct.py`: `DirectRunner` & `CmdRunner` executing build/test subprocesses with safe env scoping (`[runner.env]`) and timeout/signal reporting.
  - `openai_compat.py`: `OpenAICompatibleBackend` & `ScriptedBackend` with endpoint allowlist validation (`allowed_endpoints`), key-file loading, retry handling for HTTP `429` and `5xx` responses, and token/cost accounting.
  - `jsonl.py`: `JsonlTelemetry` writing structured JSONL telemetry events.
  - `acp.py`: `ClaudeAcpAdapter` speaking Agent Client Protocol v1 (NDJSON JSON-RPC 2.0 over stdio) with strict capability denial and read-only path mediation.
- `packs/`:
  - `base.py`: `LanguagePack` abstract contract (`unit_for`, `signatures`, `format`, `check`, `test`, `lint`, `mutate`, `coverage`, `parse_counter_tests`, etc.) and `pack_for` factory. TypeScript raises `Invalid("no-typescript-pack")`.
  - `rust.py`: `RustPack` implementing rich `signatures()` (structs, enums, traits, type aliases, consts, statics, unions, `mod`, `impl` blocks), `cargo fmt`, split stdout/stderr Cargo error & crash signal parsing, `--test <stem>` execution for counter-tests, and `cargo-mutants` output parsing.
- `gates/`:
  - `eligibility.py`: `check_eligibility` enforcing 9 `KNOWN_CATEGORIES`, `max_files`, `max_units`, `require_reference`, `context_files` bounds (`max_context_files`), `type_changes` (`allow_type_changes`), multi-file `new_symbols` rules (permitted when `type_changes = true` or `scaffold_contracts = true`), `never_touch`/`forbidden_files` intersections, and card hash checks.
  - `checks.py`: Deterministic gate helpers including `revert-check` against `base + frozen-tests` (after formatting), mutation kill-rate enforcement (`mutation_required_for`), counter-test bounce-back handling, `workspace_lint`, `baseline_cache`, `catch_up`, `extra` custom gates, and circuit breaker tracking keyed by real card IDs in `breaker.json`.
- `dossier.py`: Structured per-card artifact and Markdown/JSON dossier writer (including micromanager calls, scaffolds, coaching hints, and verdicts).
- `flow.py`: End-to-end card scheduler, worktree manager, Phase A fail-before loop, `scaffold_commit`, checkpointed Phase B step execution, checker/adjudicator loop, pre-review, and dossier finalization.
- `cli.py`: 11-subcommand CLI (`run`, `status`, `pause`, `resume`, `stop`, `release`, `dossier`, `check`, `verdict`, `escape`, `supervise`).

## Configuration Schema (12 Tables)

Exact table/key validation lives in `openfarm_core/config.py` and is verified by `tests/test_project_config.py`:

- `[project]`: `name`, `lane`, `base_branch`, `state_dir`
- `[adapters]`: `cards`, `runner`, `languages`, `model`, `micromanager`, `telemetry`, `pack`
- `[model]`: `endpoint`, `allowed_endpoints`, `key_file`, `test_writer`, `implementer`, `checker`, `planner`, `cost_per_1k_in`, `cost_per_1k_out` (`RoleModel`: `model`, `temperature`, `vary_seed`; `checker` defaults `temperature=0.35`, `vary_seed=True`)
- `[policy]`: `never_touch`, `forbidden_files` (always includes `.openfarm/**`), `known_failing`, `flagged_patterns`, `categories`
- `[eligibility]`: `max_files` (`6`), `max_units` (`1` or `len(languages)`), `require_reference` (`["pattern", "small-feature"]`), `refuse_touches_prefixes`, `rust_tests_in_separate_files` (`True`), `allow_type_changes` (`True`), `max_context_files` (`8`)
- `[brief]`: `test_conventions`
- `[runner]`: `env` (`[runner.env]` per-unit env), `cargo_flags`
- `[budget]`: `implementer_attempts` (`3`), `test_writer_attempts` (`2`), `model_build_calls_per_card` (`10`), `mutation_build_calls_per_card` (`40`), `tokens_per_card` (`400_000`), `wall_clock_per_card` (`"45m"`), `frontier_cost_ratio`, `spend_per_night` (`20.0`), `concurrent_builds` (`2`), `concurrent_cards` (`3`), `lease_ttl` (`"20m"`), `kill_switch` (`<state_dir>/STOP`)
- `[gates]`: `revert_check` (`"required"` | `"off"`), `mutation` (`"off"` | `"report"` | `"required"`, default `"report"`), `mutation_required_for` (`["test-backfill"]`), `mutation_min_kill_rate` (`0.8`), `counter_tests` (`"report"` | `"reject"`), `coverage` (`"off"` | `"report"` | `"required"`), `workspace_lint` (`True`), `baseline_cache` (`True`), `catch_up` (`"off"` | `"check"`), `findings_per_card` (`2`), `circuit_breaker` (`3`), `extra` (`name`, `run`, `via`, `paths`, `on_fail`), `planner` (`"off"` | `"auto"` | `"on"`, default `"auto"`)
- `[cards]`: `fields_block` (`""` | `"toml"`), `required_fields` (`[]`)
- `[micromanager]`: `mode` (`"off"` | `"advisory"` | `"active"`, default `"active"`), `binary` (`"claude-agent-acp"`), `args`, `model`, `effort` (`"low"` | `"medium"` | `"high"`), `tool_policy` (`"none"` | `"read-only"`), `timeout_s` / `timeout` (`180.0`, mutually exclusive), `max_calls_per_card` (`4`), `tokens_per_card` (`120_000`), `spend_per_night` (`10.0`), `cost_per_1k_in`, `cost_per_1k_out`, `auth_file`, `isolate_home` (`True`), `shape_cards` (`True`), `scaffold_contracts` (`True`), `coach_on_stall` (`True`), `adjudicate_counter_tests` (`True`), `pre_review` (`True`)
- `[state]`: `min_free_mb` (`256`)

## Key v0.4 Pipeline & Gate Hardening

1. **ACP Micromanager & Security Isolation (`adapters/acp.py`, `micromanager.py`)**:
   - `initialize` sends `clientCapabilities: {}` so write (`fs/write_text_file`) and `terminal/*` capabilities are never advertised, and incoming write/terminal JSON-RPC requests return `-32601`.
   - `session/request_permission` denies write/execute tools and enforces `[micromanager] tool_policy` (`"none"` denies all tools; `"read-only"` permits read-only inspections strictly inside the worktree via `resolves_within`, excluding `never_touch` and `forbidden_files`).
   - `_sanitised_env` strips sensitive host env vars and, when `isolate_home = true`, points `$HOME` to a scratch directory populated only from `[micromanager] auth_file` when provided. `[model] key_file` is never passed to ACP.
   - `strip_control_sequences` removes ANSI/OSC escape sequences and fences untrusted test/repo excerpts in prompts.
   - `scaffold_contract` may emit type stubs only; any production `fn` body must be `todo!()` or `unimplemented!()`.
   - Modes: `"off"` disables all calls; `"advisory"` logs decisions/recommendations without blocking or writing scaffolds/plans; `"active"` applies shaping/plans/scaffolds and can reject via `supervisor-reject` (`StopCode.SUPERVISOR_REJECT`).
2. **Phase A Fail-Before Retry Loop & Scaffolds**:
   - `test_writer` runs inside `test_writer_attempts` until generated tests compile against optional `scaffold_commit` (when `scaffold_contracts = true`) and fail for the right reason on the unimplemented codebase before entering Phase B.
3. **Checkpointed Phase B Execution**:
   - When `planner` (`"auto"` / `"on"`) or `shape_and_plan` emits `plan.steps`, Phase B commits intermediate checkpoints per step via atomic `apply_edits` while keeping `tests` and `test_edits` frozen.
4. **Gate Hardening**:
   - Formatting runs before `full_diff` capture and before `revert-check`.
   - `revert-check` compares cleanly against `base + frozen-tests` so frozen test changes do not pass falsely.
   - Cargo parser consumes split stdout/stderr streams and surfaces crash signals (`SIGSEGV`, `SIGABRT`, etc.).
   - Counter-tests run with `--test <stem>`, supporting bounce-back (`adjudicate_checker` or Phase A/B feedback) when counter-tests fail.
   - `breaker.json` keys circuit-breaker strikes by real sanitized card IDs (`_safe_id`).
   - `[state] min_free_mb` aborts before spawning worktrees on low disk space (`infra`).
   - `OpenAICompatibleBackend` retries transient HTTP `429` and `5xx` errors with backoff while enforcing `allowed_endpoints`.

## Verification Status

Verified locally on deterministic suites (config schema, protocol v1 dispatch, ACP security & micromanager orchestration via `tests/acp_stand_in.py`, core git/state/edits/retry I/O, and Rust pack/gate logic). Explicitly **untested** against live external targets on this machine:
- Live `claude-agent-acp` binary with real Anthropic credentials (**untested**).
- Live hosted ZDR OpenAI-compatible endpoints (**untested**).
- Live `cargo-mutants` binary runs (**untested**).
- TypeScript language pack (explicitly unimplemented; raises `Invalid("no-typescript-pack")`).
- External `cmd:` adapter binaries outside subprocess test harnesses (**untested**).
