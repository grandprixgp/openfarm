# Openfarm (v0.4)

Openfarm is a standalone, fail-closed pipeline that turns eligible work cards in a git repository into verified branches ready for human review. It isolates a lesser code-writing model in a disposable git worktree, enforces strict write boundaries and deterministic verification gates, and pairs the runner with a bounded supervisory micromanager (`claude-acp` over Agent Client Protocol v1, or `off` / `scripted` / `cmd:<argv>`).

## Core Workflow

1. **Eligibility & shaping**: Cards are validated against policy, file/unit bounds, and category rules. Before briefs or supervision run, `HarnessLibrary.select_guidance(...)` injects matching active/probing cues into `micromanager.harness_context` and fenced `harness adaptations`. When enabled, the micromanager shapes ambiguous cards, validates `context_files`, and proposes step plans (`plan.steps`) or `todo!()` / `unimplemented!()` type-contract scaffolds (`scaffold_contracts`).
2. **Test-first Phase A (and fail-before loop)**: Within `test_writer_attempts`, a `test_writer` role writes or updates tests inside `test_edits` against frozen `touches` (and optional `scaffold_commit`). Tests must fail on the pre-implementation tree before implementation begins.
3. **Frozen-test Phase B (checkpointed)**: `tests` and `test_edits` are frozen read-only while an `implementer` role edits `touches` (optionally step-by-step across `plan.steps` checkpoints, with `coach` assistance if stalled and dynamic failure-class cue lookup on retries). Every edit batch runs through the language pack formatter, unit tests, lints, `revert-check` against `base + frozen-tests`, and optional `mutation` / `coverage` / `extra` gates.
4. **Adversarial & supervisory review**: Independent `checker` runs generate counter-tests (`--test <stem>`) and structured findings (`findings_per_card`). When counter-tests fail or review finishes, `adjudicate_checker` and `triage_and_review` classify bounce-backs, stale conflicts, or `pre_review` status (`supervisor-reject` if unshippable).
5. **Dossier, harness capture & human landing**: Every card writes a self-contained dossier (card, plan, diffs, gate logs, checker findings, micromanager transcripts/verdicts, harness efficiency summary, and cost breakdown) under `state_dir`. Non-`Infra` card outcomes feed back into `<state_dir>/harness/` via deterministic contrast extraction (`extract_contrast`) and optional `reflect_harness` proposals. Branches are never pushed or auto-merged without human review.

## Six Adapters

Openfarm dispatches six protocol-versioned (`PROTOCOL_VERSION = 1`) adapter interfaces configured in `.openfarm/config.toml`:

| Adapter | Config key | Built-in options | External option |
| --- | --- | --- | --- |
| Card source | `[adapters] cards` | `files:<dir>` (default `files:cards/`) | `cmd:<argv>` |
| Build/test runner | `[adapters] runner` | `direct` | `cmd:<argv>` |
| Language pack | `[adapters] languages` / `pack` | `rust` (`typescript` unimplemented: `Invalid("no-typescript-pack")`) | `cmd:<argv>` |
| Model backend | `[adapters] model` | `openai-compatible`, `scripted` | `cmd:<argv>` |
| Telemetry sink | `[adapters] telemetry` | `jsonl` | `cmd:<argv>` |
| Micromanager | `[adapters] micromanager` | `off`, `claude-acp`, `scripted` | `cmd:<argv>` |

External `cmd:<argv>` adapters communicate via newline-delimited JSON over stdio (`op`, `protocol_version`, `ok`, `error`).

## Card Diet & Categories

Eligible cards declare one of nine categories under `[policy] categories`:
- **Original lesser-model diet**: `test-backfill`, `mechanical`, `pattern`, `pinned-bugfix`, `small-feature`
- **Expanded v0.4 diet**: `feature`, `refactor`, `integration`, `port`

Core card metadata fields (`category`, `reference`, `tests`, `test_edits`, `frontier_estimate`, `new_symbols`) may be written as `Key: value` lines or inside fenced ` ```openfarm ` / ` ```toml ` blocks (`[cards] fields_block = "toml"`). Optional v0.4 fields:
- `unit`: explicit package/crate unit when not auto-derived
- `context_files`: read-only files outside `touches` shown to the model (bounded by `[eligibility] max_context_files`)
- `type_changes`: boolean enabling cross-file type edits/signatures (gated by `[eligibility] allow_type_changes`)

Multi-file `new_symbols` declarations are allowed when `type_changes = true` or `[micromanager] scaffold_contracts = true`.

## CLI Commands (12 Subcommands)

Run `python3 -m openfarm_core.cli --help` (or `openfarm` when installed):

| Command | Purpose |
| --- | --- |
| `openfarm run [--script <path>] [--mm-script <path>] [--dry-run]` | Run the card queue once (or with scripted model/micromanager harnesses) |
| `openfarm status` | Show card states, queue counts, spend, circuit-breaker status, and harness status |
| `openfarm pause` / `openfarm resume` | Pause or resume queue scheduling without touching active leases |
| `openfarm stop` | Trigger the emergency `kill_switch` (`<state_dir>/STOP`) |
| `openfarm release <card_id>` | Release a blocked/paused card back to eligible state |
| `openfarm dossier <card_id>` | Print or inspect the card dossier and artifacts |
| `openfarm check <card_id>` | Run eligibility checks on a single card without running models |
| `openfarm verdict <card_id> <approve\|reject>` | Record human verdict on a `ready` card |
| `openfarm escape <card_id>` | Mark an escaped defect for post-mortem tracking |
| `openfarm supervise (--triage-blocked \| --prepare-queue \| --card <id>) [--apply] [--mm-script <path>]` | Run micromanager triage, queue preparation, or single-card shaping (`--apply` writes card updates or release decisions) |
| `openfarm harness [status\|index\|show\|promote\|demote\|reset] [target] [--reason <text>] [--yes]` | Inspect `<state_dir>/harness/` metrics/entries, manually promote/demote entries or revisions, or reset (`--yes` required) |

Exit codes: `0` success/eligible, `1` refused/ineligible, `2` usage or configuration error, `3` infrastructure/trouble error.

## Configuration (`.openfarm/config.toml`)

Thirteen strict TOML tables are recognized (unknown tables or keys fail closed with `ConfigError`):
`[project]`, `[adapters]`, `[model]`, `[policy]`, `[eligibility]`, `[brief]`, `[runner]`, `[budget]`, `[gates]`, `[cards]`, `[micromanager]`, `[state]`, `[harness]`.

Key v0.4 settings include:
- `[adapters]`: `micromanager`, `telemetry`, `pack`
- `[eligibility]`: `allow_type_changes` (default `true`), `max_context_files` (default `8`)
- `[gates]`: `planner` (`"off"` | `"auto"` | `"on"`, default `"auto"`), `mutation_required_for` (default `["test-backfill"]`)
- `[cards]`: `fields_block` (`""` | `"toml"`), `required_fields`
- `[state]`: `min_free_mb` (default `256`)
- `[micromanager]`: `mode` (`"off"` | `"advisory"` | `"active"`), `binary` (`"claude-agent-acp"`), `args`, `model`, `effort`, `tool_policy` (`"none"` | `"read-only"`), `timeout_s`/`timeout`, `max_calls_per_card`, `tokens_per_card`, `spend_per_night`, `cost_per_1k_in`, `cost_per_1k_out`, `auth_file`, `isolate_home`, `shape_cards`, `scaffold_contracts`, `coach_on_stall`, `adjudicate_counter_tests`, `pre_review`
- `[harness]`: `enabled` (default `true`), `dir` (`""` defaults to `<state_dir>/harness`), `capture_on` (`["retry_recovery", "fail", "counter_fail", "pass"]`), `max_cues_per_brief` (`4`), `max_active_adaptations` (`8`), `min_confidence` (`0.55`), `probe_window` (`3`), `auto_demote` (`true`), `allow_micromanager_reflection` (`true`), `editable_surfaces` (`["test_writer_guidance", "implementer_guidance", "planner_guidance", "context_hints", "micromanager_context"]`)

## Recursive Harness Efficiency

Inspired by `google-research/harness2` (`arXiv:2610.14224`), `openfarm_core/harness_memory.py` (`HarnessLibrary` and `HarnessLedger`) learns bounded execution and supervision cues outside the repository under `<state_dir>/harness/` (`active_overlay.json`, `revisions/rev-XXXX.json`, `entries/<id>/EXPERIENCE.md` + `patch.json`, `INDEX.md`, `ledger.jsonl`, `metrics.json`):

- **Immutable gates invariant**: Deterministic verification and safety gates (`cargo test`, `clippy`, `mutants`, `diff_check`, `forbidden_files`, `touches`, `never_touch`, `eligibility`, `budget`, `revert_check`) are **immutable** and can never appear in `EDITABLE_SURFACES` (`test_writer_guidance`, `implementer_guidance`, `planner_guidance`, `context_hints`, `micromanager_context`).
- **Dual capture & sanitization**: `extract_contrast` captures contrast pairs deterministically on retry recoveries and failures even when `micromanager` is `"off"`, scrubbing test assertion leaks; when enabled, the 6th micromanager op `reflect_harness` proposes sanitized lessons filtered through `validate_harness_proposal` and `sanitize_lesson_text`.
- **Intra-run & cross-launch loops**: One `HarnessLibrary` spans each `openfarm run` so Card N's lessons feed Card N+1 immediately (alongside dynamic failure-class cue lookup on retries) and persist across subsequent launches via fenced `harness adaptations` in role briefs and `micromanager.harness_context`.
- **Probe-window auto-demotion & `Infra` isolation**: New overlay revisions enter `"probing"` for `probe_window` non-`Infra` outcomes and automatically demote and roll back to `parent_revision` if `pass_rate` or `harness_efficiency` regresses beyond tolerance (`auto_demote = true`). Infrastructure aborts (`Infra`) never record failed card outcomes or trigger false rollbacks.

## Security & Trust Boundaries

- **Fail-closed ACP client (`openfarm_core/adapters/acp.py`)**: Advertises `clientCapabilities: {}`; rejects `fs/write_text_file` and `terminal/*` RPC calls (`-32601`); `session/request_permission` denies all writes/executions and restricts read-only tools strictly inside the card worktree excluding `never_touch` and `forbidden_files`.
- **Credential & environment isolation**: `isolate_home = true` runs the ACP subprocess under an isolated `$HOME` with optional `[micromanager] auth_file`. The OpenAI-compatible `[model] key_file` is never passed to the ACP subprocess.
- **Prompt & output hygiene**: Untrusted repo files and test output are fenced and stripped of ANSI/OSC control sequences (`strip_control_sequences`). Production `scaffold_contract` `fn` stubs require `todo!()` or `unimplemented!()`. Harness adaptation proposals are stripped of edit-block markers (`<<< SEARCH`, `>>> REPLACE`, `=== search_replace`) and rejected on path escapes or gate-bypass directives. The micromanager and harness memory never waive deterministic gates.

## Verification & Untested Boundaries

The core Python implementation and deterministic test suites verify:
- `tests/test_project_config.py` and `tests/test_config.py`: strict 13-table config validation (`[harness]`, `[micromanager]`, `[cards]`, `[state]`, etc.).
- `tests/test_protocol.py` and `tests/test_core_io_v04.py`: `PROTOCOL_VERSION = 1` dispatch across all 6 adapters, `StopCode`, path/glob guards, fenced card fields, `StateDir`, `DirectRunner`, `OpenAICompatibleBackend` retries, and `JsonlTelemetry` (`CARD_FIELDS`).
- `tests/test_pack_gates_v04.py`: `RustPack` signature extraction, split Cargo diagnostics, atomic `apply_edits`, fenced brief construction, and eligibility/gate checks.
- `tests/test_acp_micromanager.py` and `tests/acp_stand_in.py`: ACP JSON-RPC security containment, capability denial, isolated `$HOME`, and all micromanager operations.
- `tests/test_harness_efficiency.py`: `[harness]` config contracts, `StateDir.harness`, `extract_contrast`, `HarnessLedger` metrics, `sanitize_lesson_text` / `validate_harness_proposal`, `reflect_harness`, intra-run transfer, cross-launch persistence, probe-window confirm/rollback, `Infra` isolation, and `openfarm harness` CLI.
- `tests/test_v04_e2e.py`: end-to-end ACP stand-in orchestration, multi-step checkpoints, Phase A retry, `revert-check`, counter-test adjudication, and `openfarm supervise`.

The following integrations are **untested** against live external binaries/services in this repository:
- **Live `claude-agent-acp` binary with real Anthropic credentials**: verified only against `tests/acp_stand_in.py` over real stdio NDJSON-RPC pipes; live `claude-agent-acp` execution is **untested**.
- **Live hosted ZDR OpenAI-compatible model endpoints**: verified against local HTTP test servers and `ScriptedBackend`; live hosted endpoints are **untested**.
- **Live `cargo-mutants` binary runs**: output parsing and mutation gates are tested with synthetic outputs; live `cargo-mutants` execution is **untested**.
- **TypeScript language pack**: explicitly unimplemented (`Invalid("no-typescript-pack")`).
- **External `cmd:` adapters in production**: verified via subprocess test harnesses; real external `cmd:` binaries beyond test harnesses are **untested**.

See [docs/USAGE-AND-FIRST-DEPLOYMENT.md](docs/USAGE-AND-FIRST-DEPLOYMENT.md) for the user-friendly usage & operations guide and executable first-deployment runbook (`tallybox`), [docs/openfarm-internals.md](file:///docs/openfarm-internals.md) for module architecture, and [docs/HANDOFF.md](file:///docs/HANDOFF.md) for engineering handoff notes.
