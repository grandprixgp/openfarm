# Openfarm v0.5.1 Internals

Technical reference for `openfarm_core`, packaging/bootstrap (`pyproject.toml`, `SKILL.md`, `skills/`, `bootstrap.py`, `acp_setup.py`, `skill_assets/`), native Claude ACP OAuth integration (`adapters/acp.py`, `adapters/claude_native_acp.py`), its six protocol-versioned adapters, configuration schema, execution flow, security boundaries, and gate hardening.

## Module Layout (`openfarm_core/` & Packaging)

- `config.py`: Strict TOML loader (`KNOWN_TABLES`, `_TABLE_KEYS`, `Config`, `HarnessConfig`) with fail-closed rejection (`ConfigError`) on unknown tables, unknown keys, conflicting keys (e.g. `timeout` vs `timeout_s`), invalid bounds/choices (including `[harness]` `min_confidence`, `probe_window`, `capture_on`, `editable_surfaces`), invalid policy/globs, or paths escaping the repo root.
- `cards.py`: Card data models (`Card`, `TestRef`), `canonical_json`, `hash_value`, `CARD_FIELDS` and `OPTIONAL_CARD_FIELDS` (`unit`, `context_files`, `type_changes`), and fenced TOML block extractor (`extract_fenced_fields` supporting ` ```openfarm ` and ` ```toml `). Markdown/`+++` TOML parsing (`parse_card_file`) lives in `adapters/files.py`.
- `errors.py`: `StopCode` enum (17 per-card stop codes including `supervisor-reject`, plus `killed`, `released`, `paused`), `RELEASING_CODES`, and typed exception hierarchy (`AdapterError`, `Refused`, `Infra`, `Invalid`, `Unsupported`, `ConfigError`, `UsageError`, `GateFailure`).
- `protocol.py`: `PROTOCOL_VERSION = 1`, `REQUIRED_OPS` / `OPTIONAL_OPS` for all 6 adapters (`cards`, `runner`, `pack`, `model`, `telemetry`, `micromanager`), and JSON-over-stdio `CmdAdapter` wrappers (`CmdCardSource`, `CmdModelBackend`, `CmdTelemetry`, `CmdPack`). `CmdRunner` lives in `adapters/direct.py` and `CmdMicromanager` lives in `micromanager.py`.
- `globs.py`: Repo-relative glob normalization, `resolves_within` path-traversal guard, and matchers for `never_touch` / `forbidden_files`.
- `git.py`: Git worktree lifecycle (`worktree_add`, `worktree_remove`, baseline reset), `Git.init` (`git init -q -b <branch>`), commit helpers, safe flag/config allowlisting (rejecting arbitrary `-c` / unsafe options), and repo lock coordination.
- `state.py`: On-disk state under `state_dir` (`cards/<safe_id>/<run_id>/`, `runs/<run_id>/`, `checks/<safe_id>/`, `worktrees/`, `baseline-cache/`, `breaker.json`, `config.lock.toml`, `run.lock`, `paused`, `STOP`, and `harness/` via `StateDir.harness(tag="")`), card ID path sanitization (`_safe_id` preventing path traversal), atomic writes, and disk-space floor enforcement (`[state] min_free_mb`).
- `budget.py`: Per-card and nightly token/spend/build/wall-clock accounting (`Budget`, `NightBudget` with separate `micromanager_spend`), frontier cost ratio checks, and lease TTL checks.
- `edits.py`: Structured `search_replace`, `whole_file`, and `append_stub` edit parser (`parse_edits`) and atomic multi-file `apply_edits` (rolling back all files in a batch if any edit fails or touches a forbidden path).
- `briefs.py`: Prompt assembly for `test_writer`, `implementer`, `checker`, and `planner`, incorporating `reference`, `context_files`, rich Rust `signatures()`, `flagged_patterns`, sanitized failure logs, and optional fenced `harness adaptations` (`fence(harness_guidance, "harness adaptations")`).
- `roles/__init__.py`: Role dispatch (`Roles`), plan validation (`parse_plan`, `wants_planner`), counter-test/restatement parsing, and brief forwarding (including `harness_guidance`) for `test_writer`, `implementer`, `checker`, and `planner`.
- `harness_memory.py`: Recursive Harness Efficiency subsystem (`EDITABLE_SURFACES`, `IMMUTABLE_GATE_KEYS`, `sanitize_lesson_text`, `is_gate_bypass_directive`, `validate_harness_proposal`, `_classify_failure`, `extract_contrast`, `HarnessLedger`, `HarnessLibrary`, `GuidanceSelection`).
- `micromanager.py`: Supervisory orchestration (`Micromanager`, `NullMicromanager`, `ScriptedMicromanager`, `CmdMicromanager`, `ClaudeAcpMicromanager`, `open_micromanager`) implementing 6 ops: `shape_and_plan`, `scaffold_contract`, `coach`, `adjudicate_checker`, `triage_and_review` (`pre_review`, `triage_blocked`, `prepare_queue`), and `reflect_harness`, plus `self.harness_context` forwarding. Enforces `MicromanagerBudget` (`max_calls_per_card`, `tokens_per_card`, `spend_per_night`), strips/blocks edit blocks outside contracts (`_strip_edit_blocks`), validates production `todo!()`/`unimplemented!()` stubs (`_valid_production_scaffold`), and sanitizes paths (`_validate_safe_path`).
- `adapters/`:
  - `files.py`: `FileCardSource` (`parse_card_file`) reading `+++` TOML/Markdown cards and applying safe card/checkbox updates.
  - `direct.py`: `DirectRunner` & `CmdRunner` executing build/test subprocesses with safe env scoping (`[runner.env]`) and timeout/signal reporting.
  - `openai_compat.py`: `OpenAICompatibleBackend` & `ScriptedBackend` with endpoint allowlist validation (`allowed_endpoints`), key-file loading, retry handling for HTTP `429` and `5xx` responses, and token/cost accounting.
  - `jsonl.py`: `JsonlTelemetry` writing structured JSONL telemetry events (`CARD_FIELDS` includes `harness_revision`, `harness_cues_applied`, `harness_lessons_added`).
  - `acp.py`: `ClaudeAcpAdapter` speaking Agent Client Protocol v1 (NDJSON JSON-RPC 2.0 over stdio), `resolve_acp_command` (`auto` -> `claude-agent-acp` if present else built-in `claude_native_acp.py`), OAuth isolation (`auth_mode = "oauth"` strips `ANTHROPIC_API_KEY`, preserves `CLAUDE_CODE_OAUTH_TOKEN`, copies `.credentials.json` + slim `.claude.json`, syncs refreshed `.credentials.json` atomically on `close()`), automatic `authMethods` OAuth `authenticate` handshake, `Refused("acp-auth-required")` mapping, strict capability denial, and read-only path mediation.
  - `claude_native_acp.py`: Pure-stdlib `NativeClaudeAcpServer` ACP v1 bridge wrapping native `claude -p --output-format json` (without `--bare` so OAuth credentials work) with tool policies (`--tools ""` vs `--tools Read,Glob,Grep`), usage/cost extraction, subprocess group cancellation, and OAuth refusal classification.
- `packs/`:
  - `base.py`: `Pack` abstract contract (`commands`, `parse_list`, `parse_tests`, `parse_mutation`, `error_signature`, `classify_paths`, `signatures`, `edit_formats`, `apply_edit`, `append_stub`, `format`, `format_check`, `patterns`, `mutation`), `UnsupportedPack`, and `pack_for` factory. TypeScript raises `Invalid("no-typescript-pack")`.
  - `base.py` (v0.6) also holds the language-neutral `apply_text_edit` and `added_lines_from_diff`; `pack_for` builds `PythonPack` for `python`/`py`, passing `[runner] python`.
  - `python.py` (v0.6): `PythonPack` for Python 3.11+ repositories. `unittest` unless the repository configures pytest (`detect_runner`); parses `unittest -v` and `pytest -v` per-test lines and footers (no footer is infra, an import failure is a compile error, `NO TESTS RAN` is a finished empty run); lists tests without running them; an `ast`-only syntax check as lint (plus `ruff check` and `ruff format` only when `ruff` is installed, otherwise a dossier note); body-free `ast` signatures; `raise NotImplementedError` stubs; Python banned patterns (bare `except:`, swallowed exceptions, `# noqa`, `# type: ignore`, debugger calls, skips, process, network and `eval`/`exec`, production stubs). Mutation is refused.
  - `rust.py`: `RustPack` implementing rich `signatures()` (structs, enums, traits, type aliases, consts, statics, unions, `mod`, `impl` blocks), `cargo fmt`, split stdout/stderr Cargo error & crash signal parsing, `--test <stem>` execution for counter-tests, and `cargo-mutants` output parsing.
- `gates/`:
  - `eligibility.py`: `check_eligibility` enforcing 9 `KNOWN_CATEGORIES`, `max_files`, `max_units`, `require_reference`, `context_files` bounds (`max_context_files`), `type_changes` (`allow_type_changes`), multi-file `new_symbols` rules (permitted when `type_changes = true` or `scaffold_contracts = true`), `never_touch`/`forbidden_files` intersections, and card hash checks.
  - `checks.py`: Deterministic gate helpers including `revert-check` against `base + frozen-tests` (after formatting), mutation kill-rate enforcement (`mutation_required_for`), counter-test bounce-back handling, `workspace_lint`, `baseline_cache`, `catch_up`, `extra` custom gates, and circuit breaker tracking keyed by real card IDs in `breaker.json`.
- `selfhost.py` (v0.6): `unpinned_reason` refuses a real run when the running `openfarm_core` sits inside the repository or its `state_dir` (a card could edit its own judge); `orchestrator_identity` gives the version, path and commit printed as `Judged by ...` at the top of every dossier. Git goes through `git.py`.
- `adopt.py`, `bench.py` (v0.6): skeletons that the v0.6 cards fill (`openfarm adopt`, `openfarm bench`).
- `dossier.py`: Structured per-card artifact and Markdown/JSON dossier writer (including micromanager calls, scaffolds, coaching hints, verdicts, and optional `harness` summary).
- `flow.py`: End-to-end card scheduler, worktree manager, Phase A fail-before loop, `scaffold_commit`, checkpointed Phase B step execution, checker/adjudicator loop, pre-review, `HarnessLibrary` cue selection (with dynamic retry failure-class lookup) and non-`Infra` outcome recording, and dossier finalization.
- `acp_setup.py`: Native Claude ACP setup helper (`inspect_acp_status` with PII redaction, `run_acp_login`, `configure_acp_repo` with pre-write `Config` validation, and `probe_acp` zero-token handshake) powering `openfarm acp`.
- `bootstrap.py`: Greenfield initializer (`initialize_project`, `validate_project_name`) enforcing Rust 2021 keyword checks, non-empty directory refusal, external `0o700` `state_dir`/`secrets_dir` creation, validated 13-table `.openfarm/config.toml` generation (defaulting Frontier to `claude-acp` + `auth = "oauth"` + `binary = "auto"` and Workers to ZDR API `key_file`), `Cargo.lock` (`version = 3`), `never_touch` `src/invariants.rs` seam scaffolding, and `Git.init`, plus `install_skill_bundle` deploying `openfarm_core/skill_assets/` (`/openfarm-grill-me`, `/openfarm-initialize`, `openfarm`, slash commands, Antigravity plugin; mirrored at repo-root `skills/` alongside root `SKILL.md` and PEP 621 `pyproject.toml`).
- `cli.py`: 15-subcommand CLI with `PRE_CONFIG_COMMANDS` pre-`Context` dispatch for `init`, `install-skill`, and `acp`, alongside `run`, `status`, `pause`, `resume`, `stop`, `release`, `dossier`, `check`, `verdict`, `escape`, `supervise`, `harness`.

## Configuration Schema (13 Tables)

Exact table/key validation lives in `openfarm_core/config.py` and is verified by `tests/test_project_config.py`:

- `[project]`: `name`, `lane`, `base_branch`, `state_dir`
- `[adapters]`: `cards`, `runner`, `languages`, `model`, `micromanager`, `telemetry`, `pack`
- `[model]`: `endpoint`, `allowed_endpoints`, `key_file`, `headers` (v0.6: extra request headers, never `Authorization`, `Content-Type`, `Content-Length` or `Host`), `max_tokens` (v0.6, default 4096; an answer with `finish_reason` `length` is refused as `answer-cut-off` by `roles.parse_answer_edits`), `test_writer`, `implementer`, `checker`, `planner`, `cost_per_1k_in`, `cost_per_1k_out` (`RoleModel`: `model`, `temperature`, `vary_seed`; `checker` defaults `temperature=0.35`, `vary_seed=True`)
- `[policy]`: `never_touch`, `forbidden_files` (always includes `.openfarm/**` and `.claude/**`), `known_failing`, `flagged_patterns`, `categories`
- `[eligibility]`: `max_files` (`6`), `max_units` (`1` or `len(languages)`), `require_reference` (`["pattern", "small-feature"]`), `refuse_touches_prefixes`, `rust_tests_in_separate_files` (`True`), `python_tests_in_test_files` (`True`: a Python card's named tests live in `tests/**/test_*.py`), `allow_type_changes` (`True`), `max_context_files` (`8`)
- `[brief]`: `test_conventions`
- `[runner]`: `env` (`[runner.env]` per-unit env), `cargo_flags`, `python` (the interpreter the Python pack runs; empty means openfarm's own)
- `[budget]`: `implementer_attempts` (`3`), `test_writer_attempts` (`2`), `model_build_calls_per_card` (`10`), `mutation_build_calls_per_card` (`40`), `tokens_per_card` (`400_000`), `wall_clock_per_card` (`"45m"`), `frontier_cost_ratio`, `spend_per_night` (`20.0`), `concurrent_builds` (`2`), `concurrent_cards` (`3`), `lease_ttl` (`"20m"`), `kill_switch` (`<state_dir>/STOP`)
- `[gates]`: `revert_check` (`"required"` | `"off"`), `mutation` (`"off"` | `"report"` | `"required"`, default `"report"`), `mutation_required_for` (`["test-backfill"]`), `mutation_min_kill_rate` (`0.8`), `counter_tests` (`"report"` | `"reject"`), `coverage` (`"off"` | `"report"` | `"required"`), `workspace_lint` (`True`), `baseline_cache` (`True`), `catch_up` (`"off"` | `"check"`), `findings_per_card` (`2`), `circuit_breaker` (`3`), `extra` (`name`, `run`, `via`, `paths`, `on_fail`), `planner` (`"off"` | `"auto"` | `"on"`, default `"auto"`)
- `[cards]`: `fields_block` (`""` | `"toml"`), `required_fields` (`[]`)
- `[micromanager]`: `mode` (`"off"` | `"advisory"` | `"active"`, default `"active"`), `binary` (`"auto"` -> `claude-agent-acp` or built-in `claude_native_acp.py`), `args`, `model`, `effort` (`"low"` | `"medium"` | `"high"`), `tool_policy` (`"none"` | `"read-only"`), `timeout_s` / `timeout` (`180.0`, mutually exclusive), `max_calls_per_card` (`4`), `tokens_per_card` (`120_000`), `spend_per_night` (`10.0`), `cost_per_1k_in`, `cost_per_1k_out`, `auth` (`"oauth"` | `"api-key"` | `"auto"`, default `"oauth"` unless `auth_file` is set), `auth_file` (disallowed when `auth = "oauth"`), `isolate_home` (`True`), `shape_cards` (`True`), `scaffold_contracts` (`True`), `coach_on_stall` (`True`), `adjudicate_counter_tests` (`True`), `pre_review` (`True`)
- `[state]`: `min_free_mb` (`256`)
- `[harness]`: `enabled` (`True`), `dir` (`""`), `capture_on` (`("retry_recovery", "fail", "counter_fail", "pass")`), `max_cues_per_brief` (`4`), `max_active_adaptations` (`8`), `min_confidence` (`0.55`), `probe_window` (`3`), `auto_demote` (`True`), `allow_micromanager_reflection` (`True`), `editable_surfaces` (`EDITABLE_SURFACES`)

## Recursive Harness Efficiency Architecture

Inspired by `google-research/harness2` (`arXiv:2610.14224`), `openfarm_core/harness_memory.py` keeps all learned state outside the git repository under `<state_dir>/harness/` (`StateDir.harness()`, or `[harness] dir`):

1. **Invariants & Editable Surfaces**:
   - `IMMUTABLE_GATE_KEYS` blocks any lesson or `proposed_patch` from touching deterministic verification gates, policy lists, touches, or budgets (`cargo_test`, `clippy`, `mutants`, `diff_check`, `revert_check`, `counter_tests`, `forbidden_files`, `never_touch`, `touches`, `eligibility`, `budget`, etc.).
   - `EDITABLE_SURFACES` permits adaptations solely on: `test_writer_guidance`, `implementer_guidance`, `planner_guidance`, `context_hints`, and `micromanager_context`.
2. **State Layout (`<state_dir>/harness/`)**:
   - `active_overlay.json`: active `revision`, `parent_revision`, `next_revision`, `state` (`"confirmed"` | `"probing"`), `probe_remaining`, `probe_baseline`, `probe_outcomes`, `active_entry_ids`, and `surfaces`.
   - `revisions/rev-XXXX.json`: immutable revision snapshots (`rev-0000.json`, `rev-0001.json`, ...).
   - `entries/<id>/EXPERIENCE.md` + `patch.json`: atomic per-entry Markdown card and JSON metadata (`stats`: `applied`, `helped`, `hurt`, `neutral`).
   - `INDEX.md`, `ledger.jsonl`, and `metrics.json`: rebuilt summary index, append-only event log (`card_outcome`, `lesson_added`, `revision_promoted`, `revision_confirmed`, `demotion`, `reset`), and computed efficiency metrics (`first_pass_rate`, `retry_recovery_rate`, `pass_rate`, `avg_attempts`, `avg_tokens_per_pass`, `avg_spend_per_pass`, `counter_fail_rate`, `harness_efficiency`).
3. **Dual Capture, Anti-Injection & Feedback Loops**:
   - `extract_contrast` extracts failure classes (`_classify_failure`) and scrubbed `evidence_pair` deltas (`_scrub_test_leaks` prevents assertion answer leaks), while `_deterministic_extract` captures fallback lessons even when `micromanager` is `"off"`.
   - When enabled, `Micromanager.reflect_harness` proposes lessons validated by `validate_harness_proposal` and `sanitize_lesson_text` (stripping edit markers, code fences, ANSI/OSC escapes, path traversals, and gate-bypass directives).
   - `openfarm run` shares one `HarnessLibrary` across cards so Card N updates Card N+1 immediately, retries dynamically re-query `select_guidance` with the latest `failure_class`, and persisted overlays reload across launches into fenced `harness adaptations` and `micromanager.harness_context`.
4. **Probe-Window Auto-Demotion & `Infra` Isolation**:
   - New revisions probe for `probe_window` non-`Infra` card outcomes and automatically roll back to `parent_revision` (`demote_reason = "probe_regression"`) when `pass_rate` or `harness_efficiency` regresses beyond `0.05` (with `auto_demote = true`).
   - `Infra` aborts never call `HarnessLibrary.record_card_outcome` or skew `metrics.json`.
   - `openfarm harness` exposes `status`, `index`, `show`, `promote`, `demote`, and `reset --yes`.

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

## Verification Status & Claim-to-Test Mapping

| Subsystem / Claim | Test Coverage |
| --- | --- |
| 13-table config validation (`[harness]`, `[micromanager]`, `[cards]`, `[state]`) | `tests/test_project_config.py`, `tests/test_config.py`, `tests/test_harness_efficiency.py` |
| Protocol v1 (`cmd:` wrappers), `StopCode`, `StateDir`, `DirectRunner`, `OpenAICompatibleBackend`, `JsonlTelemetry` | `tests/test_protocol.py`, `tests/test_core_io_v04.py` |
| `RustPack` signatures/split diagnostics, atomic `apply_edits`, briefs, eligibility, `revert-check`/gates | `tests/test_pack_gates_v04.py`, `tests/test_edits.py`, `tests/test_eligibility.py`, `tests/test_gates.py`, `tests/test_rust_pack.py` |
| ACP JSON-RPC security isolation (`clientCapabilities: {}`, `tool_policy`, isolated `$HOME`), 6 micromanager ops | `tests/test_acp_micromanager.py`, `tests/acp_stand_in.py` |
| Recursive Harness Efficiency (`HarnessLibrary`, `HarnessLedger`, sanitization, `reflect_harness`, probe rollback, `Infra` isolation, `openfarm harness`) | `tests/test_harness_efficiency.py` |
| Packaging (`pyproject.toml`, `skill_assets/`), repo `skills/` parity, `openfarm install-skill`, `openfarm init` (`Git.init`, strict 13-table config, Rust compile/clippy/fmt, `calorie-log` check/E2E) | `tests/test_v05_skill_bootstrap.py` |
| Native Claude ACP OAuth (`claude_native_acp.py`, `acp_setup.py`, `openfarm acp`, `[micromanager] auth`, OAuth env/creds sync-back) & `/openfarm-grill-me` skill integration | `tests/test_v051_acp_oauth_grill.py` |
| End-to-end orchestration, multi-step checkpoints, Phase A retries, counter-test adjudication, `openfarm supervise`, regressions | `tests/test_v04_e2e.py`, `tests/test_e2e.py`, `tests/test_regressions.py`, `tests/test_flow_edges.py` |

Explicitly **untested** against live external targets on this machine:
- Live remote GitHub URL installs via `uv`/`pipx`/`pip` and live multi-wave LLM execution of `calorie-log` (**untested**).
- Live paid Anthropic prompt turns through `claude-agent-acp` or `claude_native_acp.py` (only zero-token `openfarm acp status` and `initialize` `probe` ran against the local `claude` CLI; prompt turns used `tests/acp_stand_in.py` and mock `claude` scripts — live prompt turns are **untested**).
- Live `claude-agent-acp` binary with real Anthropic credentials (**untested**).
- Live hosted ZDR OpenAI-compatible endpoints (**untested**).
- Live `cargo-mutants` binary runs (**untested**).
- TypeScript language pack (explicitly unimplemented; raises `Invalid("no-typescript-pack")`).
- External `cmd:` adapter binaries outside subprocess test harnesses (**untested**).
