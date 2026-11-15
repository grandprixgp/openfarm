# Openfarm v0.4 Usage, Operations Guide & First-Deployment Runbook

> **Audience**: Human operators and autonomous frontier agents bringing up **Openfarm v0.4** on a Rust repository.  
> **Local-only guarantee**: Openfarm has **no `git push` code path**. Every passing card ends on a local branch (`openfarm/<card-id>-<slug>`) for human/frontier review and local landing.

---

## Table of Contents

- [Part 1 — Usage & Operations Guide](#part-1--usage--operations-guide)
  - [1. Mental Model & Architecture](#1-mental-model--architecture)
  - [2. Annotated Configuration (`.openfarm/config.toml`)](#2-annotated-configuration-openfarmconfigtoml)
  - [3. Authoring Cards That Pass Eligibility](#3-authoring-cards-that-pass-eligibility)
  - [4. Operations & Runbook Controls (All 12 CLI Subcommands)](#4-operations--runbook-controls-all-12-cli-subcommands)
- [Part 2 — Contrast-Rich First-Target App (`tallybox`)](#part-2--contrast-rich-first-target-app-tallybox)
  - [Crate Layout & Protected Seam](#crate-layout--protected-seam)
  - [Why `tallybox` Produces High Contrast for Weak Models & Harness²](#why-tallybox-produces-high-contrast-for-weak-models--harness²)
  - [The 7-Card Suite (`CARD-001`..`CARD-006` + `CARD-900`)](#the-7-card-suite-card-001card-006--card-900)
- [Part 3 — Executable First-Deployment Plan (Phases 0–6)](#part-3--executable-first-deployment-plan-phases-06)
  - [Phase 0 — Toolchain, Python, Model Endpoint & Micromanager Readiness](#phase-0--toolchain-python-model-endpoint--micromanager-readiness)
  - [Phase 1 — Create & Verify the `tallybox` Git Repository](#phase-1--create--verify-the-tallybox-git-repository)
  - [Phase 2 — Install External Secrets/State & `.openfarm/config.toml`](#phase-2--install-external-secretsstate--openfarmconfigtoml)
  - [Phase 3 — Author Cards, Commit Setup, & Run Pre-Flight Gates](#phase-3--author-cards-commit-setup--run-pre-flight-gates)
  - [Phase 4 — Stage 1 Contrast Capture (`CARD-001` & `CARD-002`)](#phase-4--stage-1-contrast-capture-card-001--card-002)
  - [Phase 5 — Stage 2 Recursive Transfer & Probe Evaluation (`CARD-003`..`CARD-006`)](#phase-5--stage-2-recursive-transfer--probe-evaluation-card-003card-006)
  - [Phase 6 — Review Dossiers, Record Verdicts, Curate Harness & Clean Up](#phase-6--review-dossiers-record-verdicts-curate-harness--clean-up)

---

## Part 1 — Usage & Operations Guide

### 1. Mental Model & Architecture

Openfarm is a **deterministic Python 3.11+ (stdlib-only) orchestrator** that drives non-frontier coding models (`openai-compatible`, `scripted`, or `cmd:<argv>`) on narrowly scoped work cards inside disposable git worktrees, optionally supervised by a bounded frontier **micromanager** (`claude-acp`, `scripted`, or `cmd:<argv>`) and accelerated across runs by **Recursive Harness Memory** (`<state_dir>/harness/`).

```
  [cards source: files:cards/ on openfarm-cards branch]
                 │
                 ▼ (claim + hash + eligibility gate; released if ineligible)
  [disposable git worktree + per-card CARGO_TARGET_DIR]
                 │
   ┌─────────────┴───────────────────────────────────────────┐
   │ 1. Baseline green (2x cargo test --no-fail-fast)        │
   │ 2. Shape, plan & optional todo!() scaffold (micromanager)│
   │ 3. Phase A: test_writer writes named tests -> fail-before│
   │    -> rustfmt + SHA-256 freeze (tests locked read-only)  │
   │ 4. Phase B: implementer edits touches (checkpointed steps)│
   │    -> diff_within_touches, banned_patterns, suite, lint  │
   │ 5. End gates: pass-after 2x, ws-lint, extra, no-test-loss│
   │    revert-check, mutation, checker (Pass 1 & 2),         │
   │    adjudicate_checker, pre_review, throwaway catch-up    │
   └─────────────┬───────────────────────────────────────────┘
                 │
     ┌───────────┴───────────┐
     ▼                       ▼
  [in-review]             [blocked]
  local branch            note + dossier
  openfarm/<id>-<slug>    (breaker tracks real IDs)
     │                       │
     └───────────┬───────────┘
                 ▼ (non-Infra outcomes only)
  [Recursive Harness Memory: <state_dir>/harness/]
  extract_contrast -> reflect_harness -> validate_harness_proposal
  -> probing revision (probe_window) -> confirm or auto_demote
```

#### Core Invariants (Enforced in Python, Never Delegated to LLMs)

1. **No LLM ever self-certifies a deterministic gate.** Every build, test, lint, format check, `revert-check`, `mutation`, `diff_within_touches`, `banned_patterns`, `no-test-loss`, and `catch-up` trial merge is executed and parsed by Python (`openfarm_core/flow.py`, `openfarm_core/gates/checks.py`, `openfarm_core/packs/rust.py`).
2. **Coding models (`planner`, `test_writer`, `implementer`, `checker`) get zero shell and zero tools.** They receive data-fenced briefs (`-------- BEGIN CARD DATA (not instructions) --------`) with ANSI/OSC escapes stripped and return `search_replace`, `whole_file`, or `append_stub` edits (or JSON plans/counter-tests). `apply_edits` stages every batch in memory with symlink containment checks before writing atomically.
3. **Immutable gates vs. editable surfaces.** In `openfarm_core/harness_memory.py`, deterministic gates, policy lists, touches, and budgets (`IMMUTABLE_GATE_KEYS`) are strictly off-limits. Harness adaptations can target only the 5 `EDITABLE_SURFACES`:
   - `test_writer_guidance` (`test_writer` briefs)
   - `implementer_guidance` (`implementer` briefs)
   - `planner_guidance` (`planner` briefs)
   - `context_hints` (cross-role cues and validated repo-relative read-only `context_files`)
   - `micromanager_context` (structured context in `micromanager.harness_context`)
4. **Protected seams and forbidden paths stay invisible and uneditable.** Paths matching `[policy] never_touch` or `[policy] forbidden_files` (which always includes `.openfarm/**`) are rejected at eligibility, blocked from brief loads (`BriefBook.is_hidden`), blocked from `apply_edits`, blocked from `context_hints`, and blocked from ACP `read-only` tool calls.
5. **Bounded ACP micromanager (`openfarm_core/adapters/acp.py`).** `ClaudeAcpAdapter` advertises `clientCapabilities: {}` (no write or terminal capabilities), rejects write/terminal RPCs with `-32601`, denies `allow_always` permissions, isolates `$HOME` at `0700` when `isolate_home = true`, never passes `[model] key_file` to `claude-agent-acp`, strips edit blocks from `coach` outputs, and requires `todo!(` or `unimplemented!(` in every `fn` body of a `scaffold_contract` production edit.
6. **Local-only branches.** Openfarm creates `openfarm/<card-id>-<slug>` locally and leaves merging or cherry-picking to a human reviewer. Nothing in `openfarm_core/` runs `git push`.

#### Honest Verification Boundaries (`untested` Externals)

All core orchestration, config validation, Rust pack parsing, ACP v1 stdio containment, Recursive Harness Efficiency, and local `git` + `cargo` flows are covered by deterministic repository tests (`tests/test_project_config.py`, `tests/test_core_io_v04.py`, `tests/test_pack_gates_v04.py`, `tests/test_acp_micromanager.py`, `tests/test_harness_efficiency.py`, `tests/test_v04_e2e.py`). The following external integrations are explicitly **`untested`** in offline repository tests:

- **Live `claude-agent-acp` with real Anthropic credentials**: **`untested`** (verified via deterministic `tests/acp_stand_in.py` over stdio NDJSON-RPC pipes).
- **Live hosted ZDR OpenAI-compatible endpoints**: **`untested`** (verified via local HTTP test servers and `ScriptedBackend`).
- **Live `cargo-mutants` binary runs**: **`untested`** (`cargo-mutants` output parsing and gate rules are tested with synthetic JSON outputs).
- **External production `cmd:` adapter binaries**: **`untested`** beyond subprocess protocol test harnesses.
- **TypeScript language pack**: **unimplemented**; selecting `languages = ["typescript"]` without a custom `pack = "cmd:<argv>"` raises `Invalid("no-typescript-pack")`.

---

### 2. Annotated Configuration (`.openfarm/config.toml`)

`openfarm_core/config.py` validates `.openfarm/config.toml` against **13 strict top-level tables** (`KNOWN_TABLES`). Any unknown table or unknown key on a table, role sub-table, or `[[gates.extra]]` item fails closed with `ConfigError` (CLI exit code `2`).

#### All 13 Tables & Exact Keys

| Table | Exact Validated Keys | Key Validation Rules & Traps |
| --- | --- | --- |
| `[project]` | `name`, `lane`, `base_branch`, `state_dir` | `name` and `state_dir` are required. `state_dir` is checked by `_outside_repo`—pointing it inside the repo raises `ConfigError`. Keep it off unencrypted cloud-synced folders because prompts contain repo text. |
| `[adapters]` | `cards`, `runner`, `languages`, `model`, `micromanager`, `telemetry`, `pack` | Defaults: `cards = "files:cards/"`, `runner = "direct"`, `languages = ["rust"]`, `model = "openai-compatible"`, `micromanager = "off"`, `telemetry = "jsonl"`, `pack = ""`. `micromanager` accepts `"off"`, `"claude-acp"`, `"scripted"`, or `"cmd:<argv>"`. `telemetry` accepts `"jsonl"` or `"cmd:<argv>"`. `pack` accepts `""` or `"cmd:<argv>"`. |
| `[model]` | `endpoint`, `allowed_endpoints`, `key_file`, `test_writer`, `implementer`, `checker`, `planner`, `cost_per_1k_in`, `cost_per_1k_out` | `endpoint` must be in `allowed_endpoints` (redirects off allowlist are refused). `key_file` must sit outside the repo (`_outside_repo`). Set non-zero `cost_per_1k_in` / `cost_per_1k_out` so spend caps can trigger. Role sub-tables accept `model`, `temperature`, `vary_seed` (or a bare string model name). Omitted role models inherit `implementer.model`; `checker` defaults to `temperature = 0.35`, `vary_seed = true` (others `0.2`, `false`). |
| `[policy]` | `never_touch`, `forbidden_files`, `known_failing`, `flagged_patterns`, `categories` | `categories` is required and must be non-empty. `.openfarm/**` and `.claude/**` are automatically appended to `forbidden_files` in code. `known_failing` matches exact full test names only (never substrings). |
| `[eligibility]` | `max_files`, `max_units`, `require_reference`, `refuse_touches_prefixes`, `rust_tests_in_separate_files`, `allow_type_changes`, `max_context_files` | Defaults: `max_files = 6`, `max_units = 1` (or `len(languages)` if > 1), `require_reference = ["pattern", "small-feature"]`, `refuse_touches_prefixes = []`, `rust_tests_in_separate_files = true`, `allow_type_changes = true`, `max_context_files = 8`. |
| `[brief]` | `test_conventions` | Optional repo-relative path to a test conventions guide (`.openfarm/test-conventions.md` is permitted even though `.openfarm/**` is in `forbidden_files`). Escapes (`../`) or absolute paths raise `ConfigError` at load time, while `never_touch`, `.openfarm/config.toml`, and other `forbidden_files` paths are hidden by `BriefBook` at runtime. |
| `[runner]` | `cargo_flags`, `env` (`[runner.env]`) | `cargo_flags = ["--locked"]` automatically drops `--locked` (with a dossier note) if `Cargo.lock` is absent. `[runner.env]` maps `<unit> = { VAR = "val" }`. |
| `[budget]` | `implementer_attempts`, `test_writer_attempts`, `model_build_calls_per_card`, `mutation_build_calls_per_card`, `tokens_per_card`, `wall_clock_per_card`, `frontier_cost_ratio`, `spend_per_night`, `concurrent_builds`, `concurrent_cards`, `lease_ttl`, `kill_switch` | Defaults: `3`, `2`, `10`, `40`, `400_000`, `"45m"`, `0.0` (`None` when 0), `20.0`, `2`, `3`, `"20m"`, `<state_dir>/STOP`. Durations accept `"45m"`, `"2h"`, `"30s"`, `"1d"`, or seconds. Note: `concurrent_cards` currently executes sequentially in `cli.py` (noted in `run.json`). |
| `[gates]` | `revert_check`, `mutation`, `mutation_required_for`, `mutation_min_kill_rate`, `counter_tests`, `coverage`, `workspace_lint`, `baseline_cache`, `catch_up`, `findings_per_card`, `circuit_breaker`, `planner`, `extra` (`[[gates.extra]]`) | `revert_check`: `"required" \| "off"`. `mutation`: `"off" \| "report" \| "required"` (default `"report"`). `mutation_required_for`: default `["test-backfill"]`. `counter_tests`: `"report" \| "reject"`. `coverage`: `"off" \| "report" \| "required"` (not yet implemented). `catch_up`: `"off" \| "check"`. `planner`: `"off" \| "auto" \| "on"` (default `"auto"`). Each `[[gates.extra]]` requires `name` and `run`, plus optional `via` (`"runner" \| "local"`), `paths`, `on_fail` (`"ineligible" \| "block"`). |
| `[cards]` | `fields_block`, `required_fields` | `fields_block`: `""` or `"toml"` (enables extracting ` ```openfarm ` / ` ```toml ` fenced metadata blocks). `required_fields`: list of field names. |
| `[micromanager]` | `mode`, `binary`, `args`, `model`, `effort`, `tool_policy`, `timeout_s`, `timeout`, `max_calls_per_card`, `tokens_per_card`, `spend_per_night`, `cost_per_1k_in`, `cost_per_1k_out`, `auth_file`, `isolate_home`, `shape_cards`, `scaffold_contracts`, `coach_on_stall`, `adjudicate_counter_tests`, `pre_review` | `mode`: `"off" \| "advisory" \| "active"` (default `"active"` when table exists, but disabled unless `[adapters] micromanager != "off"`). **`timeout_s` and `timeout` are mutually exclusive** (`ConfigError` if both set; must be `> 0`). `tool_policy`: `"none" \| "read-only"`. `auth_file` must be outside the repo. `MicromanagerBudget` tracks `spend_per_night` across the entire `openfarm run` and `max_calls_per_card` / `tokens_per_card` per card. |
| `[state]` | `min_free_mb` | Default `256`. `openfarm run` refuses/aborts with `Infra` (`disk-full`) if free disk space on `state_dir` falls below this floor. |
| `[harness]` | `enabled`, `dir`, `capture_on`, `max_cues_per_brief`, `max_active_adaptations`, `min_confidence`, `probe_window`, `auto_demote`, `allow_micromanager_reflection`, `editable_surfaces` | Optional table (defaults active). `dir = ""` defaults to `<state_dir>/harness`; non-empty `dir` must pass `_outside_repo`. `capture_on` subset of `["retry_recovery", "fail", "counter_fail", "pass"]`. `0.0 <= min_confidence <= 1.0` (default `0.55`). `probe_window >= 1` (default `3`). `editable_surfaces` subset of the 5 `EDITABLE_SURFACES`. |

#### Three Ready-to-Adapt Config Profiles

1. **Solo / Micromanager Off** (deterministic contrast capture still works via `extract_contrast`!):
   ```toml
   [adapters]
   micromanager = "off"

   [micromanager]
   mode = "off"

   [harness]
   enabled = true
   allow_micromanager_reflection = false
   ```

2. **Advisory Supervision** (micromanager shapes, coaches, and writes `reviewer_brief`, but `scaffold_contract` is skipped and `abort` / `dismiss_false_reject` / `bounce` / `block` are suppressed):
   ```toml
   [adapters]
   micromanager = "claude-acp"

   [micromanager]
   mode = "advisory"
   tool_policy = "none"
   isolate_home = true
   ```

3. **Active Supervision + Recursive Harness** (full v0.4 workflow with `scaffold_contracts`, `adjudicate_checker`, `pre_review`, and `reflect_harness`; see the complete `.openfarm/config.toml` in [Phase 2](#phase-2--install-external-secretsstate--openfarmconfigtoml)).

---

### 3. Authoring Cards That Pass Eligibility

With `[adapters] cards = "files:cards/"`, each card is a Markdown file `cards/<CARD-ID>.md` with a TOML header fenced by `+++` lines on their own lines, followed by a `## Done` Markdown body.

#### Header Format A: Classic `+++` TOML Frontmatter

```markdown
+++
id = "CARD-001"
title = "Fix sliding window boundary expiry"
state = "ready"
touches = ["src/window.rs"]
depends = []
category = "pinned-bugfix"
reference = ""
test_edits = ["tests/window_tests.rs"]
frontier_estimate = 2.50
new_symbols = false
unit = "tallybox"
type_changes = false
context_files = ["src/lib.rs"]

[[tests]]
name = "boundary_expiry_evicts_at_exact_window"
file = "tests/window_tests.rs"
asserts = "SlidingWindow::new(100, 500) evicts at ts_ms = 200 after record(100, 300) and returns Ok(400)."
+++

## Done

- Update `SlidingWindow::record` so `ts_ms.saturating_sub(front.ts_ms) >= self.window_ms` evicts expired entries cleanly.
```

#### Header Format B: Fenced Metadata Block (`[cards] fields_block = "toml"`)

When `[cards] fields_block = "toml"` is set, `extract_fenced_fields` extracts the first ` ```openfarm ` or ` ```toml ` block from the card body and merges it with the `+++` header (**`+++` header keys take precedence over fenced block keys**; invalid TOML inside the block fails with `Invalid("bad-fields-block")`).

````markdown
+++
id = "CARD-006"
title = "Wire policy verdict and seam frame"
state = "ready"
touches = ["src/lib.rs"]
depends = []
+++

```openfarm
category = "integration"
reference = ""
test_edits = ["tests/integration_wire_tests.rs"]
frontier_estimate = 3.50
new_symbols = true
context_files = ["src/policies/strict_cap.rs", "src/window.rs", "src/ledger.rs"]

[[tests]]
name = "reserve_with_policy_records_window_ledger_and_seam_frame"
file = "tests/integration_wire_tests.rs"
asserts = "reserve_with_policy returns Ok(IntegrationOutcome { ... }) with SeamFrame version 1."
```

## Done

- Implement `pub fn reserve_with_policy(...) -> Result<IntegrationOutcome, IntegrationError>` in `src/lib.rs`.
````

#### All 9 Categories & Field Rules

| Category | Typical Use | Reference Required by Default? | `new_symbols = true` Allowed? |
| --- | --- | --- | --- |
| `test-backfill` | Add tests to working code (`touches` = test file(s) only; `fail-before` & `implementer` skipped; **requires `mutation` unless `mutation = "off"` or removed from `mutation_required_for`**) | No | **No** |
| `mechanical` | Compiler-guided type/rename/split edits (may omit `[[tests]]`) | No | Yes |
| `pattern` | New sibling module/handler imitating a reference file | **Yes** | Yes |
| `pinned-bugfix` | Bug with concrete failing reproduction test (`fail-before` must fail at runtime on base) | No | **No** |
| `small-feature` | Bounded single-unit feature mirroring a sibling | **Yes** | Yes |
| `feature` | Broader single-unit feature (supports `type_changes`, `context_files`, `plan.steps`) | No | Yes |
| `refactor` | Structural cleanup inside a unit; non-empty `[[tests]]` still checked by `fail-before` | No | Yes |
| `integration` | Wiring existing unit modules together (often uses `context_files`) | No | Yes |
| `port` | Porting a pattern/behavior from a reference or `context_files` | No | Yes |

**Critical Rust & multi-file field rules:**
- `unit`: Optional explicit crate name; if set, verified against `[package].name` in the unit's `Cargo.toml` (`unit-mismatch` if different).
- `context_files`: Read-only files outside `touches` shown in `test_writer` (signatures) and `implementer` (full text), bounded by `[eligibility] max_context_files` (default `8`) and checked against `never_touch`, `forbidden_files`, escapes, and symlinks.
- `type_changes`: When `true` (and `[eligibility] allow_type_changes = true`), `test_writer` receives full base production files in `touches` as a read-only base type contract instead of signatures only.
- `new_symbols`: Disallowed on `pinned-bugfix` and `test-backfill`. If `new_symbols = true` and `len(touches) > 1`, eligibility **requires** either `type_changes = true` OR active micromanager `scaffold_contracts = true` (`new-symbols-touches` otherwise).
- `depends` (or `deps` in `FileCardSource`): Card IDs that must be in `"done"` state before claiming (`deps` rule otherwise).
- **Explicit `tests` naming & `pub` reachability**: When `[eligibility] rust_tests_in_separate_files = true` (default), every `tests[].file` must live under `tests/` (`rust-tests-in-separate-files` otherwise). Because Cargo integration tests under `tests/` are separate crates, every named test can reach only `pub` items exported by your crate! Prefer one `tests/<subject>_tests.rs` file per card/subject so cards do not contend on a single test file.

#### Common Eligibility Rejections & Fixes

| Rejection Rule | Why It Fired | Clean Fix |
| --- | --- | --- |
| `seam-prefix` | `touches` entry starts with `[eligibility] refuse_touches_prefixes` (e.g. `seam:`) | Remove seam target; handle outside the farm |
| `never-touch` / `forbidden-file` / `reference-never-touch` | `touches`, `reference`, `tests[].file`, `test_edits`, or `context_files` (or symlink target) matches `never_touch` or `forbidden_files` | Move target off protected file or drop card |
| `category` | Missing, unknown, or not listed in `[policy] categories` | Use one of the 9 categories enabled in config |
| `no-touches` / `max-files` / `max-units` / `unit-mismatch` | Empty `touches`, `> max_files`, spans `> max_units` crates, or `unit` != `Cargo.toml` `[package].name` | Scope card to 1 crate and `<= max_files` paths |
| `reference-required` | `pattern` or `small-feature` missing `reference` | Add a sibling repo-relative path in `reference` |
| `named-tests` / `known-failing` / `duplicate-tests` | Non-`mechanical` card has no `[[tests]]`, names a `known_failing` test, or duplicates a test name | Add unique `[[tests]]` entries not in `known_failing` |
| `vague-done` / `design-left` / `done-text-concrete` | Done text uses `"handle"`, `"properly"`, `"robust"`, `"decide how"`, `"design"`, `"choose an approach"`, `"figure out how"`, or has no concrete value/code span/assertion | Replace vague verbs with exact inputs, error variants, and return values |
| `new-symbols` / `new-symbols-touches` | `new_symbols = true` on `pinned-bugfix`/`test-backfill`, or `>1` `touches` without `type_changes` or `scaffold_contracts` | Split card, set `type_changes = true`, or enable `scaffold_contracts` |
| `rust-tests-in-separate-files` | `tests[].file` is outside `tests/` (e.g. `src/lib.rs`) | Move test file to `tests/<subject>_tests.rs` |
| `bad-fields-block` | Malformed TOML inside ` ```openfarm ` / ` ```toml ` block | Fix TOML syntax in the fenced block |

---

### 4. Operations & Runbook Controls (All 12 CLI Subcommands)

Invoke via `./openfarm` (when copied/installed) or `python3 -m openfarm_core.cli` from the Openfarm checkout (`PYTHONPATH=/path/to/openfarm`).

- **Global flags** (accepted before or after the subcommand): `--repo PATH` (default `.`), `--config PATH`, `--json`.
- **Shared exit codes**:
  - `0`: success / eligible
  - `1`: refused / ineligible (never retried automatically)
  - `2`: usage or `ConfigError`
  - `3`: trouble (`Infra`, `Invalid`, or adapter failure)

#### All 12 Subcommands

| Stage | Command & Exact Flags | Purpose |
| --- | --- | --- |
| Pre-flight | `openfarm check <CARD> [--baseline]` | Check card eligibility (exit `0` eligible, `1` ineligible). `--baseline` also runs base suite 2x + confirms named tests compile & fail on base. |
| Pre-flight | `openfarm supervise (--triage-blocked \| --prepare-queue) [--card ID] [--apply] [--mm-script PATH]` | Run micromanager `prepare_queue` on `ready`/`backlog` cards (or `triage_blocked` on `blocked` cards). Writes `<state_dir>/supervision/<stamp>-supervise.json`. `--apply` files findings or releases cards. |
| Pre-flight | `openfarm run --dry-run [--cards N] [--card ID]` | Validate eligibility and baseline health across the next `N` ready cards without claiming cards or calling models. |
| Running | `openfarm run [--cards N] [--spend AMOUNT] [--for DUR \| --until HH:MM] [--card ID] [--script PATH] [--mm-script PATH] [--run-id ID]` | Claim and drive cards. `--spend` lowers (never raises) `spend_per_night`; `--spend 0` is a usage error (`2`). `--for` takes durations like `90m`/`2h`; `--until` takes UTC `HH:MM` or ISO timestamp. |
| Monitoring | `openfarm status [--card ID]` | Read-only status: active `run.lock` holder, `paused`, `kill_switch`, `breaker`, `spend_cap`, `last_run`, `harness` summary, and optional card run history. |
| Emergency | `openfarm pause [--reason TEXT]` | Create `<state_dir>/paused`; stops new card claims while letting any in-flight attempt finish. |
| Emergency | `openfarm stop [--reason TEXT]` | Create `kill_switch` (`<state_dir>/STOP`); stops the run and blocks in-flight cards with `killed`. |
| Recovery | `openfarm resume [--clear-breaker] [--clear-kill]` | Remove `paused`; refuses with exit `1` if breaker is tripped or `STOP` exists unless `--clear-breaker` / `--clear-kill` is passed. |
| Recovery | `openfarm release <CARD> --reason TEXT` | Release a stuck/blocked card via card source first, then archive prior card artifacts under `<state_dir>/cards/<CARD>/partial/`. Refuses (`1`) if a live unexpired lease holds it. |
| Review | `openfarm dossier <CARD> [--run RUN] [--attempt N] [--format md\|json]` | Print the 9-section dossier (`dossier.md` / `dossier.json`) including `1_risk`, `2_gates`, `4_red_green`, `6_checker`, `9_diffs` (test diff first, production diff second), and `harness` summary. |
| Review | `openfarm verdict <CARD> (clean\|minor-fix\|bounced\|scrapped) --review-tokens N [--minutes M] [--note TEXT]` | Record reviewer verdict in `<state_dir>/cards/<CARD>/<latest_run>/verdict.json` for payback accounting. |
| Review | `openfarm escape <CARD> --note TEXT` | Append a post-merge defect record to `<state_dir>/cards/<CARD>/<latest_run>/escapes.jsonl`. |
| Harness² | `openfarm harness [status\|index\|show\|promote\|demote\|reset] [TARGET] [--reason TEXT] [--yes]` | Inspect or curate `<state_dir>/harness/` (`status` default; `index` prints `INDEX.md`; `show <id>` shows entry + `EXPERIENCE.md`; `promote <id>` validates & activates; `demote <id\|rev-XXXX>` rolls back; `reset --yes` resets to `rev-0000`). |

#### Stop Codes Reference (`openfarm_core/errors.py`)

| Stop Code | Trigger | Final Card State | Feeds Harness Memory? |
| --- | --- | --- | --- |
| `ineligible` | Eligibility gate fails, `shape_and_plan` rejects without supervisor block, or `[[gates.extra]]` with `on_fail = "ineligible"` fails | `released` | **No** (isolated) |
| `dirty-baseline` | Base suite has non-`known_failing` failure or 2 baseline runs disagree | `blocked` | **No** (isolated) |
| `tests-unprovable` | Phase A fails to produce compiling fail-before tests within `test_writer_attempts`, or `test-backfill` misses `mutation_min_kill_rate` | `blocked` | Yes |
| `attempts` | `implementer_attempts` exhausted | `blocked` | Yes |
| `no-progress` | Named passing test count fails to rise over 2 attempts, diff hash repeats, or normalized error signature repeats | `blocked` | Yes |
| `build-cap` | Model-driven build calls exceed `model_build_calls_per_card` | `blocked` | Yes |
| `mutation-cap` | Blocking `mutation` hits `mutation_build_calls_per_card` or cannot run (`cargo-mutants` missing) | `blocked` | Yes |
| `budget` | `tokens_per_card`, `wall_clock_per_card`, `spend_per_night`, or `frontier_cost_ratio` exceeded | `blocked` | Yes |
| `cannot-split` | Planner returns `CANNOT_SPLIT` or a `plan.steps` checkpoint exhausts attempts | `blocked` | Yes |
| `checker-reject` | Failing counter-test rejects under `counter_tests = "reject"` after retries | `blocked` | Yes |
| `supervisor-reject` | Active micromanager blocks/aborts (`shape_and_plan`, `coach`, or `pre_review`) | `blocked` | Yes |
| `extra-gate` | `[[gates.extra]]` with `on_fail = "block"` fails | `blocked` | Yes |
| `card-changed` | Card hash changed mid-run (`card_hash_at_claim != current`) | `blocked` | **No** (isolated) |
| `stale-conflict` | Throwaway catch-up merge of `base_branch` conflicts or breaks suite | `blocked` | **No** (isolated) |
| `refused` | Tool/sandbox/path-escape/git-allowlist/micromanager-budget/claim refusal | `released` / `blocked` | Only if model calls ran |
| `infra` | Runner timeout/`SIGKILL`, ACP transport failure, HTTP outage after retries, or disk `< min_free_mb` | `blocked` (pauses queue) | **No** (isolated) |
| `lease-expired` | Card lease heartbeat missed past `lease_ttl` | `blocked` | **No** (isolated) |
| `killed` / `released` / `paused` | Operator `stop`, `release`, or `pause` | `blocked` / `released` | **No** (isolated) |

#### State Directory Layout (`<state_dir>/` & `<state_dir>/harness/`)

```text
<state_dir>/
  config.lock.toml
  run.lock
  paused
  STOP
  breaker.json
  baseline-cache/<commit[:12]>/<unit>/<known_failing_hash[:12]>-<env_hash>.json
  cards-source/                     # checkout of openfarm-cards branch (for files: source)
  worktrees/<card-id>-<run-id>/
  runs/<run-id>/
    run.json
    telemetry/attempts.jsonl
    telemetry/cards.jsonl
  cards/<card-id>/<run-id>/
    card.json
    card-record.json
    frozen/tests.sha256
    briefs/<attempt>-<role>.md
    responses/<attempt>-<role>.json
    edits/<attempt>.diff
    gates/<attempt>/<gate>.json
    micromanager/<seq>-<op>.json
    target/                         # isolated CARGO_TARGET_DIR per card
    dossier.md
    dossier.json
    verdict.json
    escapes.jsonl
  supervision/
    <stamp>-supervise.json
    <stamp>/micromanager/
  harness/                          # Recursive Harness Memory (outside repo)
    active_overlay.json             # revision, parent_revision, state ("confirmed"|"probing"), surfaces
    INDEX.md                        # rebuilt Markdown table of entries & efficiency metrics
    ledger.jsonl                    # append-only events (card_outcome, lesson_added, revision_*, demotion, reset)
    metrics.json                    # overall, by_revision, by_surface, by_kind, recent_window
    revisions/rev-0000.json ...     # immutable per-revision snapshots
    entries/<id>/
      EXPERIENCE.md                 # human-readable cue, lesson, evidence, stats
      patch.json                    # validated entry metadata + applied/helped/hurt/neutral counts
```

---

## Part 2 — Contrast-Rich First-Target App (`tallybox`)

### Crate Layout & Protected Seam

**`tallybox`** is a zero-dependency Rust (`edition = "2021"`) crate implementing a deterministic sliding-window rate limiter and millicredit reservation ledger:

```text
tallybox/
  Cargo.toml                        # zero [dependencies]
  Cargo.lock                        # pinned for --locked
  .openfarm/
    config.toml
    test-conventions.md
  src/
    lib.rs                          # crate exports + build_seam_frame helper
    ledger.rs                       # ReservationLedger, ReserveReceipt, ReservationKind, LedgerError
    window.rs                       # SlidingWindow, WindowEntry, WindowError (VecDeque)
    policies/
      mod.rs                        # exports strict_cap & burst_cap
      strict_cap.rs                 # StrictCapPolicy + PolicyVerdict (pattern reference)
      burst_cap.rs                  # placeholder module for CARD-002
    seam.rs                         # PROTECTED wire codec (never_touch = ["src/seam.rs"])
  tests/
    baseline_tests.rs               # 4 green baseline integration tests
  cards/
    CARD-001.md .. CARD-006.md       # 6 eligible exercise cards across the diet
    CARD-900.md                     # intentional negative seam control (rejected by check)
```

`src/seam.rs` (`SeamFrame`, `SeamCodecError`) is listed in `[policy] never_touch = ["src/seam.rs"]`. Openfarm never loads `src/seam.rs` into any brief (not even signatures), rejects any card whose fields reference `src/seam.rs`, and rejects any attempt diff that touches it.

### Why `tallybox` Produces High Contrast for Weak Models & Harness²

Weak coding models routinely stumble on exact Rust invariants that deterministic gates catch immediately, generating rich retry-recovery contrast for `<state_dir>/harness/`:

1. **Checked integer arithmetic (`u64::MAX` overflow/underflow)**: Weak models write `front.ts_ms + self.window_ms` or `self.available + amount`, panicking or wrapping on boundary tests instead of using `checked_add`, `checked_sub`, and `saturating_sub`.
2. **Exact sliding-window boundary expiry (`>=` vs `<`)**: Off-by-one errors leave an entry alive at `ts_ms == entry.ts_ms + window_ms`.
3. **Borrow-checker scopes around `VecDeque`**: Iterating `self.entries` while calling `self.entries.pop_front()` triggers `E0502` / `E0499`, producing `borrow-checker` contrast lessons.
4. **Banned `.unwrap()` / `todo!()` / `#[allow(...)]` traps**: `RustPack` (`banned-patterns`) and `cargo clippy --all-targets -- -D warnings` reject `.unwrap()` / `.expect()` / `#[allow(...)]` in `src/*.rs`, producing `lint-clippy` lessons.
5. **Multi-file `type_changes` contracts**: `CARD-003` spans `src/lib.rs` and `src/ledger.rs` with `SweepReport` and `plan.steps` checkpoints, exercising `missing-symbol` and `type-mismatch` recovery.
6. **Adversarial counter-tests**: Checker Pass 2 targets `0`-limit windows, empty ledgers, and `u64::MAX` inputs.

### The 7-Card Suite (`CARD-001`..`CARD-006` + `CARD-900`)

| Card | Category | Key Flags / Fields | Purpose & Contrast Role |
| --- | --- | --- | --- |
| `CARD-001` | `pinned-bugfix` | `touches = ["src/window.rs", "src/ledger.rs"]` | Fix `u64` overflow + `<` vs `>=` boundary expiry in `SlidingWindow::record`, and over-refund / overflow bugs in `ReservationLedger::refund` (Stage 1 high-contrast retry target). |
| `CARD-002` | `pattern` | `reference = "src/policies/strict_cap.rs"`, `new_symbols = true` | Add `BurstCapPolicy` + `BurstCapError` in `src/policies/burst_cap.rs` mirroring `strict_cap.rs` without `.unwrap()` (Stage 1 pattern imitation + `lint-clippy` contrast). |
| `CARD-003` | `feature` | `type_changes = true`, `new_symbols = true`, 2 test files | Add `SweepReport` & `ReservationLedger::sweep_expired` across `src/ledger.rs` and `src/lib.rs` with `plan.steps` checkpoints (Stage 2 transfer). |
| `CARD-004` | `test-backfill` | `touches = ["tests/backfill_edge_tests.rs"]` | Backfill zero-window, empty-window, and overflow edge tests. Note: requires `cargo-mutants` unless `mutation = "off"` or `mutation_required_for = []`. |
| `CARD-005` | `refactor` | `touches = ["src/window.rs"]` | Deduplicate `prune_expired` across `record`, `used_in_window`, and `remaining_at` so stale queries evict expired entries without changing public signatures. |
| `CARD-006` | `integration` | Fenced ` ```openfarm ` block with `context_files` | Wire `StrictCapPolicy`, `SlidingWindow`, `ReservationLedger`, and `build_seam_frame` in `src/lib.rs` without reading protected `src/seam.rs`. |
| `CARD-900` | `pinned-bugfix` | `touches = ["src/seam.rs"]` | **Negative control**: `openfarm check CARD-900` **must** exit `1` (`never-touch`). |

---

## Part 3 — Executable First-Deployment Plan (Phases 0–6)

Every phase below is self-contained for either a human operator or an autonomous frontier agent. Run commands from the directory noted in each phase, and **do not proceed past a Verification Gate if it fails**.

> **Path convention**: Replace `/path/to/openfarm` with your local clone of the Openfarm repository, and `/path/to/tallybox` with the target workspace directory (for example `~/work/tallybox`).

---

### Phase 0 — Toolchain, Python, Model Endpoint & Micromanager Readiness

#### 1. Verify Prerequisites

```bash
python3 --version            # requires >= 3.11 (for tomllib)
git --version                # git >= 2.30 recommended
rustc --version
cargo --version
cargo clippy --version
rustfmt --version
```

Optional checks:

```bash
# Optional mutation testing (required for blocking test-backfill cards unless disabled)
command -v cargo-mutants && cargo mutants --version || echo "cargo-mutants not installed (untested locally)"

# Optional Claude ACP micromanager (required only if [adapters] micromanager = "claude-acp")
command -v claude-agent-acp && claude-agent-acp --version || echo "claude-agent-acp not installed"
```

#### 2. Verification Gate & Branching Decision

| Check | Expected | On Failure |
| --- | --- | --- |
| `python3 --version` | `Python 3.11.x` or newer | Install Python 3.11+; Openfarm has zero third-party Python dependencies. |
| `rustc`, `cargo`, `clippy`, `rustfmt` | All exit `0` | Install via `rustup component add clippy rustfmt`. |
| Model API endpoint + key | HTTPS ZDR OpenAI-compatible URL + API key ready | For an **offline smoke test without spending tokens**, use `openfarm check`, `openfarm run --dry-run`, or `openfarm run --script <path> [--mm-script <path>]` (see `tests/acp_stand_in.py` & `ScriptedBackend`). Live hosted endpoints and live `claude-agent-acp` runs are **`untested`** in offline repo tests. |
| `cargo-mutants` missing | Not installed | Either install `cargo-mutants`, or in Phase 2 set `[gates] mutation = "off"` (or `mutation_required_for = []`) before running `CARD-004`, otherwise `CARD-004` (`test-backfill`) stops with `mutation-cap`. |
| `claude-agent-acp` missing | Not installed | Switch Phase 2 `[adapters] micromanager = "off"` and `[micromanager] mode = "off"` (Solo profile). |

---

### Phase 1 — Create & Verify the `tallybox` Git Repository

#### 1. Create Directory Structure

```bash
export TARGET_REPO="$HOME/work/tallybox"
mkdir -p "$TARGET_REPO"/{.openfarm,cards,src/policies,tests}
cd "$TARGET_REPO"
```

#### 2. Write Cargo & Rust Baseline Payloads

Create `Cargo.toml`:

```bash
cat <<'EOF' > "$TARGET_REPO/Cargo.toml"
[package]
name = "tallybox"
version = "0.1.0"
edition = "2021"
EOF
```

Create `.gitignore`:

```bash
cat <<'EOF' > "$TARGET_REPO/.gitignore"
/target
EOF
```

Create `src/seam.rs` (protected wire codec):

```bash
cat <<'EOF' > "$TARGET_REPO/src/seam.rs"
//! Stable wire codec for external snapshot exchange.
//! Protected by `[policy] never_touch` in `.openfarm/config.toml`.

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SeamFrame {
    pub version: u16,
    pub account_id: String,
    pub used_millicredits: u64,
    pub reserved_millicredits: u64,
    pub remaining_millicredits: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SeamCodecError {
    EmptyAccount,
    UnsupportedVersion(u16),
    MalformedFrame,
}

impl SeamFrame {
    pub fn encode_line(&self) -> Result<String, SeamCodecError> {
        if self.account_id.is_empty() || self.account_id.contains('|') {
            return Err(SeamCodecError::EmptyAccount);
        }
        if self.version != 1 {
            return Err(SeamCodecError::UnsupportedVersion(self.version));
        }
        Ok(format!(
            "v1|{}|{}|{}|{}",
            self.account_id,
            self.used_millicredits,
            self.reserved_millicredits,
            self.remaining_millicredits
        ))
    }

    pub fn decode_line(line: &str) -> Result<Self, SeamCodecError> {
        let parts: Vec<&str> = line.split('|').collect();
        if parts.len() != 5 || parts[0] != "v1" {
            return Err(SeamCodecError::MalformedFrame);
        }
        let account_id = parts[1].to_string();
        if account_id.is_empty() {
            return Err(SeamCodecError::EmptyAccount);
        }
        let used = parts[2]
            .parse::<u64>()
            .map_err(|_| SeamCodecError::MalformedFrame)?;
        let reserved = parts[3]
            .parse::<u64>()
            .map_err(|_| SeamCodecError::MalformedFrame)?;
        let remaining = parts[4]
            .parse::<u64>()
            .map_err(|_| SeamCodecError::MalformedFrame)?;
        Ok(Self {
            version: 1,
            account_id,
            used_millicredits: used,
            reserved_millicredits: reserved,
            remaining_millicredits: remaining,
        })
    }
}
EOF
```

Create `src/window.rs`:

```bash
cat <<'EOF' > "$TARGET_REPO/src/window.rs"
//! Deterministic sliding-window consumption tracker.

use std::collections::VecDeque;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WindowEntry {
    pub ts_ms: u64,
    pub millicredits: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WindowError {
    ZeroWindow,
    Overflow,
}

#[derive(Debug, Clone)]
pub struct SlidingWindow {
    window_ms: u64,
    capacity_millicredits: u64,
    entries: VecDeque<WindowEntry>,
}

impl SlidingWindow {
    pub fn new(window_ms: u64, capacity_millicredits: u64) -> Result<Self, WindowError> {
        if window_ms == 0 {
            return Err(WindowError::ZeroWindow);
        }
        Ok(Self {
            window_ms,
            capacity_millicredits,
            entries: VecDeque::new(),
        })
    }

    pub fn record(&mut self, ts_ms: u64, millicredits: u64) -> Result<u64, WindowError> {
        while let Some(front) = self.entries.front() {
            if front.ts_ms + self.window_ms < ts_ms {
                self.entries.pop_front();
            } else {
                break;
            }
        }
        let used = self
            .entries
            .iter()
            .fold(0u64, |acc, item| acc.saturating_add(item.millicredits));
        let next_used = used
            .checked_add(millicredits)
            .ok_or(WindowError::Overflow)?;
        if next_used > self.capacity_millicredits {
            return Err(WindowError::Overflow);
        }
        self.entries.push_back(WindowEntry {
            ts_ms,
            millicredits,
        });
        Ok(next_used)
    }

    pub fn used_in_window(&mut self, _now_ms: u64) -> u64 {
        self.entries
            .iter()
            .fold(0u64, |acc, item| acc.saturating_add(item.millicredits))
    }

    pub fn remaining_at(&mut self, now_ms: u64) -> u64 {
        let used = self.used_in_window(now_ms);
        self.capacity_millicredits.saturating_sub(used)
    }

    pub fn active_entries(&self) -> usize {
        self.entries.len()
    }
}
EOF
```

Create `src/ledger.rs`:

```bash
cat <<'EOF' > "$TARGET_REPO/src/ledger.rs"
//! Millicredit reservation ledger.

use std::collections::BTreeMap;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReservationKind {
    Standard,
    Priority,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReserveReceipt {
    pub id: u64,
    pub account_id: String,
    pub amount_millicredits: u64,
    pub remaining_millicredits: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Reservation {
    pub id: u64,
    pub account_id: String,
    pub amount_millicredits: u64,
    pub expires_at_ms: u64,
    pub kind: ReservationKind,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LedgerError {
    InvalidAmount,
    InsufficientBalance,
    UnknownReservation(u64),
    Overflow,
}

#[derive(Debug, Clone)]
pub struct ReservationLedger {
    available: u64,
    reserved: u64,
    next_id: u64,
    active: BTreeMap<u64, Reservation>,
}

impl ReservationLedger {
    pub fn new(total_millicredits: u64) -> Self {
        Self {
            available: total_millicredits,
            reserved: 0,
            next_id: 1,
            active: BTreeMap::new(),
        }
    }

    pub fn available(&self) -> u64 {
        self.available
    }

    pub fn reserved(&self) -> u64 {
        self.reserved
    }

    pub fn active_len(&self) -> usize {
        self.active.len()
    }

    pub fn reserve(
        &mut self,
        account_id: &str,
        amount_millicredits: u64,
        expires_at_ms: u64,
        kind: ReservationKind,
    ) -> Result<ReserveReceipt, LedgerError> {
        if amount_millicredits == 0 || account_id.is_empty() {
            return Err(LedgerError::InvalidAmount);
        }
        if self.available < amount_millicredits {
            return Err(LedgerError::InsufficientBalance);
        }
        let next_reserved = self
            .reserved
            .checked_add(amount_millicredits)
            .ok_or(LedgerError::Overflow)?;
        let id = self.next_id;
        self.next_id = self.next_id.checked_add(1).ok_or(LedgerError::Overflow)?;
        self.available -= amount_millicredits;
        self.reserved = next_reserved;
        self.active.insert(
            id,
            Reservation {
                id,
                account_id: account_id.to_string(),
                amount_millicredits,
                expires_at_ms,
                kind,
            },
        );
        Ok(ReserveReceipt {
            id,
            account_id: account_id.to_string(),
            amount_millicredits,
            remaining_millicredits: self.available,
        })
    }

    pub fn commit(&mut self, id: u64) -> Result<u64, LedgerError> {
        let entry = self
            .active
            .remove(&id)
            .ok_or(LedgerError::UnknownReservation(id))?;
        self.reserved = self
            .reserved
            .checked_sub(entry.amount_millicredits)
            .ok_or(LedgerError::Overflow)?;
        Ok(entry.amount_millicredits)
    }

    pub fn refund(&mut self, id: u64, amount_millicredits: u64) -> Result<u64, LedgerError> {
        if amount_millicredits == 0 {
            return Err(LedgerError::InvalidAmount);
        }
        let _entry = self
            .active
            .get(&id)
            .ok_or(LedgerError::UnknownReservation(id))?;
        self.reserved = self.reserved.saturating_sub(amount_millicredits);
        self.available = self.available.saturating_add(amount_millicredits);
        Ok(self.available)
    }
}
EOF
```

Create `src/policies/mod.rs`, `src/policies/strict_cap.rs`, and `src/policies/burst_cap.rs`:

```bash
cat <<'EOF' > "$TARGET_REPO/src/policies/mod.rs"
pub mod burst_cap;
pub mod strict_cap;
EOF

cat <<'EOF' > "$TARGET_REPO/src/policies/strict_cap.rs"
//! Strict sliding-cap admission policy.

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PolicyVerdict {
    Allow { remaining_millicredits: u64 },
    Deny { deficit_millicredits: u64 },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StrictCapPolicy {
    pub cap_millicredits: u64,
}

impl StrictCapPolicy {
    pub fn new(cap_millicredits: u64) -> Self {
        Self { cap_millicredits }
    }

    pub fn evaluate(&self, used_millicredits: u64, requested_millicredits: u64) -> PolicyVerdict {
        let Some(projected) = used_millicredits.checked_add(requested_millicredits) else {
            return PolicyVerdict::Deny {
                deficit_millicredits: requested_millicredits,
            };
        };
        if projected <= self.cap_millicredits {
            PolicyVerdict::Allow {
                remaining_millicredits: self.cap_millicredits.saturating_sub(projected),
            }
        } else {
            PolicyVerdict::Deny {
                deficit_millicredits: projected.saturating_sub(self.cap_millicredits),
            }
        }
    }
}
EOF

cat <<'EOF' > "$TARGET_REPO/src/policies/burst_cap.rs"
//! Bounded burst admission policy (placeholder for pattern card).
EOF
```

Create `src/lib.rs` and `tests/baseline_tests.rs`:

```bash
cat <<'EOF' > "$TARGET_REPO/src/lib.rs"
//! `tallybox`: deterministic sliding-window rate limiter and millicredit reservation ledger.

pub mod ledger;
pub mod policies;
pub mod seam;
pub mod window;

pub use ledger::{LedgerError, Reservation, ReservationKind, ReservationLedger, ReserveReceipt};
pub use policies::strict_cap::{PolicyVerdict, StrictCapPolicy};
pub use seam::{SeamCodecError, SeamFrame};
pub use window::{SlidingWindow, WindowEntry, WindowError};

pub fn build_seam_frame(
    account_id: &str,
    used_millicredits: u64,
    ledger: &ReservationLedger,
) -> SeamFrame {
    SeamFrame {
        version: 1,
        account_id: account_id.to_string(),
        used_millicredits,
        reserved_millicredits: ledger.reserved(),
        remaining_millicredits: ledger.available(),
    }
}
EOF

cat <<'EOF' > "$TARGET_REPO/tests/baseline_tests.rs"
use tallybox::{
    build_seam_frame, LedgerError, PolicyVerdict, ReservationKind, ReservationLedger, SeamFrame,
    SlidingWindow, StrictCapPolicy,
};

#[test]
fn baseline_strict_cap_allows_and_denies() {
    let policy = StrictCapPolicy::new(1_000);
    assert_eq!(
        policy.evaluate(400, 500),
        PolicyVerdict::Allow {
            remaining_millicredits: 100
        }
    );
    assert_eq!(
        policy.evaluate(800, 300),
        PolicyVerdict::Deny {
            deficit_millicredits: 100
        }
    );
}

#[test]
fn baseline_window_records_within_capacity() {
    let mut win = SlidingWindow::new(1_000, 500).expect("valid window");
    assert_eq!(win.record(100, 200), Ok(200));
    assert_eq!(win.record(200, 300), Ok(500));
    assert_eq!(win.active_entries(), 2);
}

#[test]
fn baseline_ledger_reserve_commit_and_exact_refund() {
    let mut ledger = ReservationLedger::new(1_000);
    let receipt = ledger
        .reserve("acct-a", 400, 5_000, ReservationKind::Standard)
        .expect("reserve succeeds");
    assert_eq!(receipt.remaining_millicredits, 600);
    assert_eq!(ledger.reserved(), 400);
    assert_eq!(ledger.commit(receipt.id), Ok(400));
    assert_eq!(ledger.reserved(), 0);

    let r2 = ledger
        .reserve("acct-a", 250, 5_000, ReservationKind::Priority)
        .expect("second reserve succeeds");
    assert_eq!(ledger.refund(r2.id, 250), Ok(600));
    assert_eq!(ledger.reserved(), 0);
    assert_eq!(
        ledger.refund(999, 10),
        Err(LedgerError::UnknownReservation(999))
    );
}

#[test]
fn baseline_seam_roundtrip() {
    let ledger = ReservationLedger::new(900);
    let frame = build_seam_frame("acct-a", 100, &ledger);
    let encoded = frame.encode_line().expect("encode works");
    let decoded = SeamFrame::decode_line(&encoded).expect("decode works");
    assert_eq!(decoded, frame);
}
EOF
```

#### 3. Initialize Git, Generate `Cargo.lock`, & Verify Baseline

```bash
cd "$TARGET_REPO"
git init -b main
cargo test
cargo clippy --all-targets -- -D warnings
rustfmt --edition 2021 --check src/*.rs src/policies/*.rs tests/*.rs
```

#### 4. Verification Gate

- `cargo test` passes (`4 passed; 0 failed` in `tests/baseline_tests.rs`) and generates `Cargo.lock`.
- `cargo clippy --all-targets -- -D warnings` exits `0`.
- `rustfmt --edition 2021 --check ...` exits `0`.
- **On failure**: Fix formatting (`rustfmt --edition 2021 src/*.rs src/policies/*.rs tests/*.rs`) or compiler error before continuing; baseline red tests cause `dirty-baseline` in Openfarm.

---

### Phase 2 — Install External Secrets/State & `.openfarm/config.toml`

#### 1. Create External State and Secret Directories

Keep secrets and state strictly outside `$TARGET_REPO` and out of unencrypted cloud sync folders:

```bash
export OPENFARM_HOME="/path/to/openfarm"
export OPENFARM_STATE="$HOME/.openfarm/tallybox"
export OPENFARM_SECRETS="$HOME/.config/openfarm"

mkdir -p "$OPENFARM_STATE" "$OPENFARM_SECRETS"
chmod 700 "$OPENFARM_STATE" "$OPENFARM_SECRETS"

# Write your ZDR OpenAI-compatible provider key (single line, no quotes)
printf '%s\n' '<YOUR_ZDR_MODEL_API_KEY>' > "$OPENFARM_SECRETS/tallybox.key"
chmod 600 "$OPENFARM_SECRETS/tallybox.key"

# Optional: if using an explicit Anthropic API key file for claude-agent-acp
# printf '%s\n' '<YOUR_ANTHROPIC_API_KEY>' > "$OPENFARM_SECRETS/claude.key"
# chmod 600 "$OPENFARM_SECRETS/claude.key"
```

#### 2. Write `.openfarm/test-conventions.md` and `.openfarm/config.toml`

```bash
cat <<'EOF' > "$TARGET_REPO/.openfarm/test-conventions.md"
# Test Conventions for tallybox

- Place integration tests in `tests/<subject>_tests.rs`.
- Reach crate behavior only through `pub` items exported by `tallybox`.
- Assert exact variants and values (`assert_eq!`, `matches!`), never bare `is_err()`.
- Never use `#[ignore]`, `#[allow(...)]`, or `dbg!()`.
EOF
```

Write `.openfarm/config.toml` (replace `<YOUR_ZDR_ENDPOINT>`, `<MODEL_NAME>`, and paths as needed; if `claude-agent-acp` is not installed, change `micromanager = "off"` and `mode = "off"`):

```bash
cat <<EOF > "$TARGET_REPO/.openfarm/config.toml"
[project]
name = "tallybox"
lane = "farm"
base_branch = "main"
state_dir = "$OPENFARM_STATE"

[adapters]
cards = "files:cards/"
runner = "direct"
languages = ["rust"]
model = "openai-compatible"
micromanager = "claude-acp"
telemetry = "jsonl"
pack = ""

[model]
endpoint = "https://<YOUR_ZDR_ENDPOINT>/v1"
allowed_endpoints = ["https://<YOUR_ZDR_ENDPOINT>/v1"]
key_file = "$OPENFARM_SECRETS/tallybox.key"
cost_per_1k_in = 0.0005
cost_per_1k_out = 0.0015
test_writer = { model = "<MODEL_NAME>", temperature = 0.2 }
implementer = { model = "<MODEL_NAME>", temperature = 0.3, vary_seed = true }
checker     = { model = "<MODEL_NAME>", temperature = 0.35, vary_seed = true }
planner     = { model = "<MODEL_NAME>", temperature = 0.2 }

[policy]
never_touch = ["src/seam.rs"]
forbidden_files = ["**/Cargo.toml", "Cargo.lock", ".openfarm/**"]
known_failing = []
flagged_patterns = ["std::env::set_var", "sleep("]
categories = [
  "test-backfill",
  "mechanical",
  "pattern",
  "pinned-bugfix",
  "small-feature",
  "feature",
  "refactor",
  "integration",
  "port",
]

[eligibility]
max_files = 6
max_units = 1
require_reference = ["pattern", "small-feature"]
refuse_touches_prefixes = ["seam:"]
rust_tests_in_separate_files = true
allow_type_changes = true
max_context_files = 8

[brief]
test_conventions = ".openfarm/test-conventions.md"

[runner]
cargo_flags = ["--locked"]

[runner.env]

[budget]
implementer_attempts = 3
test_writer_attempts = 2
model_build_calls_per_card = 10
mutation_build_calls_per_card = 40
tokens_per_card = 400_000
wall_clock_per_card = "45m"
frontier_cost_ratio = 0.30
spend_per_night = 20.00
concurrent_builds = 2
concurrent_cards = 3
lease_ttl = "20m"
kill_switch = "$OPENFARM_STATE/STOP"

[gates]
revert_check = "required"
mutation = "report"
mutation_required_for = ["test-backfill"]
mutation_min_kill_rate = 0.8
counter_tests = "report"
coverage = "off"
workspace_lint = true
baseline_cache = true
catch_up = "check"
findings_per_card = 2
circuit_breaker = 3
planner = "auto"

[cards]
fields_block = "toml"
required_fields = []

[micromanager]
mode = "active"
binary = "claude-agent-acp"
args = []
model = ""
effort = "medium"
tool_policy = "none"
timeout_s = 180.0
max_calls_per_card = 4
tokens_per_card = 120_000
spend_per_night = 10.0
cost_per_1k_in = 0.003
cost_per_1k_out = 0.015
isolate_home = true
shape_cards = true
scaffold_contracts = true
coach_on_stall = true
adjudicate_counter_tests = true
pre_review = true

[state]
min_free_mb = 256

[harness]
enabled = true
dir = ""
capture_on = ["retry_recovery", "fail", "counter_fail", "pass"]
max_cues_per_brief = 4
max_active_adaptations = 8
min_confidence = 0.55
probe_window = 3
auto_demote = true
allow_micromanager_reflection = true
editable_surfaces = [
  "test_writer_guidance",
  "implementer_guidance",
  "planner_guidance",
  "context_hints",
  "micromanager_context",
]
EOF
```

#### 3. Validate Config & Initial Harness State

```bash
PYTHONPATH="$OPENFARM_HOME" python3 -m openfarm_core.cli --repo "$TARGET_REPO" --json status
PYTHONPATH="$OPENFARM_HOME" python3 -m openfarm_core.cli --repo "$TARGET_REPO" --json harness status
```

#### 4. Verification Gate

- Both commands exit `0` (`harness status` reports `"revision": 0`, `"state": "confirmed"`, `"total_entries": 0`).
- **On exit code `2` (`ConfigError`)**: Check unknown keys, ensure `state_dir` and `key_file` resolve outside `$TARGET_REPO`, and ensure `timeout` and `timeout_s` are not both specified.

---

### Phase 3 — Author Cards, Commit Setup, & Run Pre-Flight Gates

#### 1. Write `CARD-001`..`CARD-006` and `CARD-900`

```bash
cat <<'EOF' > "$TARGET_REPO/cards/CARD-001.md"
+++
id = "CARD-001"
title = "Fix sliding window boundary expiry and ledger refund checks"
state = "ready"
touches = ["src/window.rs", "src/ledger.rs"]
depends = []
category = "pinned-bugfix"
reference = ""
test_edits = ["tests/window_refund_boundary_tests.rs"]
frontier_estimate = 2.50
new_symbols = false

[[tests]]
name = "boundary_expiry_evicts_at_exact_window_and_near_u64_max"
file = "tests/window_refund_boundary_tests.rs"
asserts = "SlidingWindow::new(100, 500) after record(u64::MAX - 150, 300) evicts at ts_ms = u64::MAX - 50 without overflow and returns Ok(400) for record(u64::MAX - 50, 400)."

[[tests]]
name = "refund_rejects_over_refund_and_overflow_without_mutating_state"
file = "tests/window_refund_boundary_tests.rs"
asserts = "ReservationLedger::refund rejects amount > reserved with Err(LedgerError::InvalidAmount), rejects u64::MAX available overflow with Err(LedgerError::Overflow), and removes depleted reservations on full refund."
+++

## Done

- In `src/window.rs`, `SlidingWindow::record` must expire any entry where `ts_ms.saturating_sub(front.ts_ms) >= self.window_ms` so exact boundary timestamps (`front.ts_ms + window_ms == ts_ms`) expire without `u64` addition overflow near `u64::MAX`.
- In `src/ledger.rs`, `ReservationLedger::refund` must check that `amount_millicredits <= entry.amount_millicredits` (returning `Err(LedgerError::InvalidAmount)` otherwise), use `checked_add` for `self.available` (returning `Err(LedgerError::Overflow)` without partial state mutation), subtract from `entry.amount_millicredits`, and remove the reservation when its remaining amount reaches `0`.
EOF

cat <<'EOF' > "$TARGET_REPO/cards/CARD-002.md"
+++
id = "CARD-002"
title = "Add bounded burst cap policy mirroring strict cap"
state = "ready"
touches = ["src/policies/burst_cap.rs"]
depends = []
category = "pattern"
reference = "src/policies/strict_cap.rs"
test_edits = ["tests/burst_cap_tests.rs"]
frontier_estimate = 2.00
new_symbols = true

[[tests]]
name = "burst_cap_allows_within_base_plus_burst_and_reports_remaining"
file = "tests/burst_cap_tests.rs"
asserts = "BurstCapPolicy::new(1000, 250).evaluate(900, 300) == Ok(PolicyVerdict::Allow { remaining_millicredits: 50 })."

[[tests]]
name = "burst_cap_denies_over_limit_and_checked_overflow_without_unwrap"
file = "tests/burst_cap_tests.rs"
asserts = "BurstCapPolicy::new(1000, 250).evaluate(1200, 100) == Ok(PolicyVerdict::Deny { deficit_millicredits: 50 }) and evaluate(u64::MAX, 1) returns Err(BurstCapError::Overflow)."
+++

## Done

Mirror `src/policies/strict_cap.rs` in `src/policies/burst_cap.rs` (exported as `tallybox::policies::burst_cap`):
- Define `pub enum BurstCapError { Overflow }` and `pub struct BurstCapPolicy { pub base_cap_millicredits: u64, pub burst_millicredits: u64 }` deriving `Debug, Clone, Copy, PartialEq, Eq`.
- Implement `BurstCapPolicy::new(base_cap_millicredits: u64, burst_millicredits: u64) -> Self`.
- Implement `BurstCapPolicy::evaluate(&self, used_millicredits: u64, requested_millicredits: u64) -> Result<PolicyVerdict, BurstCapError>` using `checked_add` for both `effective_cap = base_cap + burst` and `projected = used + requested`, returning `Err(BurstCapError::Overflow)` on overflow, `Ok(PolicyVerdict::Allow { remaining_millicredits })` when `projected <= effective_cap`, and `Ok(PolicyVerdict::Deny { deficit_millicredits })` when `projected > effective_cap`.
EOF

cat <<'EOF' > "$TARGET_REPO/cards/CARD-003.md"
+++
id = "CARD-003"
title = "Add reservation expiry sweep and SweepReport contract"
state = "ready"
touches = ["src/lib.rs", "src/ledger.rs"]
depends = []
category = "feature"
reference = ""
test_edits = ["tests/ledger_sweep_tests.rs", "tests/lib_sweep_tests.rs"]
frontier_estimate = 4.00
new_symbols = true
type_changes = true

[[tests]]
name = "sweep_expired_reclaims_due_reservations_and_keeps_future_ones"
file = "tests/ledger_sweep_tests.rs"
asserts = "ReservationLedger::sweep_expired(500) returns Ok(SweepReport { expired_count: 2, reclaimed_millicredits: 350, available_millicredits: 850 }) when two reservations expire at <= 500 and one expires at 501."

[[tests]]
name = "crate_reexports_sweep_report_and_handles_empty_ledger"
file = "tests/lib_sweep_tests.rs"
asserts = "tallybox::sweep_ledger(&mut ledger, 100) returns Ok(SweepReport { expired_count: 0, reclaimed_millicredits: 0, available_millicredits: 1000 }) on a fresh ledger."
+++

## Done

1. In `src/ledger.rs`, add `pub struct SweepReport { pub expired_count: usize, pub reclaimed_millicredits: u64, pub available_millicredits: u64 }` (deriving `Debug, Clone, PartialEq, Eq`) and `pub fn sweep_expired(&mut self, now_ms: u64) -> Result<SweepReport, LedgerError>` on `ReservationLedger`, expiring every active reservation with `expires_at_ms <= now_ms`, returning reclaimed credits to `available` with `checked_add` / `checked_sub` (returning `Err(LedgerError::Overflow)` on overflow without partial mutation).
2. In `src/lib.rs`, re-export `SweepReport` at crate root and add `pub fn sweep_ledger(ledger: &mut ReservationLedger, now_ms: u64) -> Result<SweepReport, LedgerError>`.
EOF

cat <<'EOF' > "$TARGET_REPO/cards/CARD-004.md"
+++
id = "CARD-004"
title = "Backfill edge case tests for zero limits and empty windows"
state = "ready"
touches = ["tests/backfill_edge_tests.rs"]
depends = []
category = "test-backfill"
reference = ""
test_edits = ["tests/backfill_edge_tests.rs"]
frontier_estimate = 1.50
new_symbols = false

[[tests]]
name = "empty_window_and_zero_window_edges"
file = "tests/backfill_edge_tests.rs"
asserts = "SlidingWindow::new(0, 100) returns Err(WindowError::ZeroWindow) and a fresh SlidingWindow::new(100, 0) reports used_in_window(50) == 0 and remaining_at(50) == 0."

[[tests]]
name = "checked_arithmetic_and_zero_amount_ledger_edges"
file = "tests/backfill_edge_tests.rs"
asserts = "StrictCapPolicy::new(100).evaluate(u64::MAX, 1) == PolicyVerdict::Deny { deficit_millicredits: 1 } and ReservationLedger::new(100).reserve(\"acct\", 0, 10, ReservationKind::Standard) == Err(LedgerError::InvalidAmount)."
+++

## Done

Backfill standalone edge-case tests in `tests/backfill_edge_tests.rs` covering existing behavior:
- `SlidingWindow::new(0, 100)` returns `Err(WindowError::ZeroWindow)`.
- Fresh `SlidingWindow::new(100, 0)` reports `used_in_window(50) == 0` and `remaining_at(50) == 0`.
- `StrictCapPolicy::new(100).evaluate(u64::MAX, 1)` returns `PolicyVerdict::Deny { deficit_millicredits: 1 }`.
- `ReservationLedger::new(100).reserve("acct", 0, 10, ReservationKind::Standard)` returns `Err(LedgerError::InvalidAmount)`.
EOF

cat <<'EOF' > "$TARGET_REPO/cards/CARD-005.md"
+++
id = "CARD-005"
title = "Deduplicate window pruning across query methods"
state = "ready"
touches = ["src/window.rs"]
depends = []
category = "refactor"
reference = ""
test_edits = ["tests/window_prune_consistency_tests.rs"]
frontier_estimate = 2.00
new_symbols = false

[[tests]]
name = "used_in_window_and_remaining_at_prune_expired_entries"
file = "tests/window_prune_consistency_tests.rs"
asserts = "After record(100, 300) on SlidingWindow::new(100, 500), used_in_window(250) == 0, remaining_at(250) == 500, and active_entries() == 0."
+++

## Done

Refactor `src/window.rs` by extracting a single private `fn prune_expired(&mut self, now_ms: u64)` helper on `SlidingWindow` and calling it from `record`, `used_in_window`, and `remaining_at` so all window query and mutation methods evict expired entries consistently without changing public signatures:
- After `record(100, 300)` on `SlidingWindow::new(100, 500)`, calling `used_in_window(250)` returns `0`, `remaining_at(250)` returns `500`, and `active_entries()` returns `0`.
EOF

cat <<'EOF' > "$TARGET_REPO/cards/CARD-006.md"
+++
id = "CARD-006"
title = "Wire policy verdict, ledger reservation, and seam frame end-to-end"
state = "ready"
touches = ["src/lib.rs"]
depends = []
category = "integration"
reference = ""
test_edits = ["tests/integration_wire_tests.rs"]
frontier_estimate = 3.50
new_symbols = true

[[tests]]
name = "reserve_with_policy_records_window_ledger_and_seam_frame"
file = "tests/integration_wire_tests.rs"
asserts = "tallybox::reserve_with_policy(&StrictCapPolicy::new(1000), &mut win, &mut ledger, \"acct-1\", 300, 100, 500) returns Ok(IntegrationOutcome { receipt: ..., frame: SeamFrame { version: 1, account_id: \"acct-1\", used_millicredits: 300, reserved_millicredits: 300, remaining_millicredits: 700 } })."

[[tests]]
name = "reserve_with_policy_denies_without_mutating_ledger_or_window"
file = "tests/integration_wire_tests.rs"
asserts = "When StrictCapPolicy denies a request, reserve_with_policy returns Err(IntegrationError::PolicyDenied { deficit_millicredits: 200 }) and leaves ledger.available() and win.active_entries() unchanged."
+++

```openfarm
context_files = ["src/policies/strict_cap.rs", "src/window.rs", "src/ledger.rs"]
```

## Done

In `src/lib.rs`, wire `StrictCapPolicy`, `SlidingWindow`, `ReservationLedger`, and `build_seam_frame` together:
- Define `pub enum IntegrationError { PolicyDenied { deficit_millicredits: u64 }, Window(WindowError), Ledger(LedgerError) }` and `pub struct IntegrationOutcome { pub receipt: ReserveReceipt, pub frame: SeamFrame }` (deriving `Debug, Clone, PartialEq, Eq`).
- Implement `pub fn reserve_with_policy(policy: &StrictCapPolicy, window: &mut SlidingWindow, ledger: &mut ReservationLedger, account_id: &str, amount_millicredits: u64, now_ms: u64, expires_at_ms: u64) -> Result<IntegrationOutcome, IntegrationError>`.
- First query `let used = window.used_in_window(now_ms);` and `policy.evaluate(used, amount_millicredits)`; if `PolicyVerdict::Deny { deficit_millicredits }`, return `Err(IntegrationError::PolicyDenied { deficit_millicredits })` without mutating `window` or `ledger`.
- Otherwise reserve in `ledger` first (`ReservationKind::Standard`), then record in `window`; if `window.record(now_ms, amount_millicredits)` fails, refund the reservation cleanly and return `Err(IntegrationError::Window(err))`.
- On success, construct `frame` via `build_seam_frame(account_id, new_used, ledger)` and return `Ok(IntegrationOutcome { receipt, frame })`.
EOF

cat <<'EOF' > "$TARGET_REPO/cards/CARD-900.md"
+++
id = "CARD-900"
title = "Negative control card attempting to edit protected seam codec"
state = "ready"
touches = ["src/seam.rs"]
depends = []
category = "pinned-bugfix"
reference = ""
test_edits = ["tests/seam_negative_tests.rs"]
frontier_estimate = 1.00
new_symbols = false

[[tests]]
name = "seam_version_two_check"
file = "tests/seam_negative_tests.rs"
asserts = "SeamFrame::encode_line accepts version 2."
+++

## Done

Change `src/seam.rs` so `SeamFrame::encode_line` returns `Ok(...)` for `version == 2`.
EOF
```

#### 2. Commit Baseline Repository on `main` & Run Eligibility Checks

> **Important**: `FileCardSource` seeds `openfarm-cards` from `main` the first time it prepares. Always commit `.openfarm/`, `src/`, `tests/`, `Cargo.*`, and `cards/` on `main` before running `openfarm check` or `openfarm run`.

```bash
cd "$TARGET_REPO"
git add .
git commit -m "Initial tallybox crate, config, and 7-card suite"

for cid in CARD-001 CARD-002 CARD-003 CARD-004 CARD-005 CARD-006; do
  PYTHONPATH="$OPENFARM_HOME" python3 -m openfarm_core.cli --repo "$TARGET_REPO" --json check "$cid"
done

# Negative control MUST return exit code 1
set +e
PYTHONPATH="$OPENFARM_HOME" python3 -m openfarm_core.cli --repo "$TARGET_REPO" --json check CARD-900
echo "CARD-900 exit=$?"
set -e

# Baseline + dry-run across the 6 eligible cards (no model calls, no claims)
PYTHONPATH="$OPENFARM_HOME" python3 -m openfarm_core.cli --repo "$TARGET_REPO" --json check CARD-001 --baseline
PYTHONPATH="$OPENFARM_HOME" python3 -m openfarm_core.cli --repo "$TARGET_REPO" --json run --dry-run --cards 6
```

#### 3. Verification Gate

- `CARD-001`..`CARD-006`: `openfarm check` exits `0` (`"eligible": true`).
- `CARD-900`: `openfarm check` exits `1` (`"eligible": false`, rule `"never-touch"`).
- `openfarm check CARD-001 --baseline` and `openfarm run --dry-run --cards 6` exit `0` with `"baseline": {"ok": true, ...}` on all 6 cards.
- **On failure**: If `openfarm-cards` was created before you edited a card on `main`, either update the card on `openfarm-cards` (via `<state_dir>/cards-source`) or delete and re-seed before retrying.

---

### Phase 4 — Stage 1 Contrast Capture (`CARD-001` & `CARD-002`)

Run the first two contrast-rich cards (`pinned-bugfix` and `pattern`) so Openfarm captures retry recoveries, failures, or reflection lessons into `<state_dir>/harness/`.

> **Offline / scripted check option**: If running without live credentials, pass `--script <model_script.json> --mm-script <mm_script.json>` to exercise the exact same harness capture pipeline deterministically.

#### 1. Execute `CARD-001` and `CARD-002`

```bash
PYTHONPATH="$OPENFARM_HOME" python3 -m openfarm_core.cli --repo "$TARGET_REPO" --json run --card CARD-001
PYTHONPATH="$OPENFARM_HOME" python3 -m openfarm_core.cli --repo "$TARGET_REPO" --json run --card CARD-002
```

#### 2. Inspect `<state_dir>/harness/` Capture

```bash
PYTHONPATH="$OPENFARM_HOME" python3 -m openfarm_core.cli --repo "$TARGET_REPO" --json harness status
PYTHONPATH="$OPENFARM_HOME" python3 -m openfarm_core.cli --repo "$TARGET_REPO" --json harness index
cat "$OPENFARM_STATE/harness/INDEX.md"
cat "$OPENFARM_STATE/harness/metrics.json"
cat "$OPENFARM_STATE/harness/active_overlay.json"
ls -la "$OPENFARM_STATE/harness/entries/"
```

To inspect one captured experience card:

```bash
# Replace <ENTRY_ID> with an ID shown in INDEX.md / entries/
PYTHONPATH="$OPENFARM_HOME" python3 -m openfarm_core.cli --repo "$TARGET_REPO" --json harness show <ENTRY_ID>
cat "$OPENFARM_STATE/harness/entries/<ENTRY_ID>/EXPERIENCE.md"
```

#### 3. Verification Gate & Branching Decision

- `harness status` exits `0` and `metrics.json` reflects `cards_total >= 2` (unless both cards stopped on `Infra`, which is isolated and leaves `cards_total` unchanged).
- Whenever a retry/failure or `reflect_harness` proposal passes `validate_harness_proposal`:
  - `active_overlay.json` advances to `revision >= 1` (`revisions/rev-0001.json` exists) with `"state": "probing"` and `"probe_remaining": 3`.
  - `entries/<id>/EXPERIENCE.md` and `patch.json` contain sanitized cues (zero edit-block headers, zero assertion leaks, zero `IMMUTABLE_GATE_KEYS`).
- **If a card stopped with `infra`**: Inspect the error (`openfarm status`), fix the endpoint/ACP/disk issue, run `openfarm resume`, release the card (`openfarm release <CARD> --reason "infra retry"`), and re-run.
- **If both cards passed cleanly on attempt 1 with `reflect_harness` yielding 0 lessons**: That is valid (`revision: 0`, `"state": "confirmed"`). You can manually test curation with `openfarm harness status` or proceed to Phase 5 (`CARD-003`..`CARD-006`).

---

### Phase 5 — Stage 2 Recursive Transfer & Probe Evaluation (`CARD-003`..`CARD-006`)

Now run pre-flight queue preparation (`openfarm supervise`) and batch execution across `CARD-003`..`CARD-006` so active/probing cues transfer across cards and the probe window resolves (`confirmed` or `auto_demote` rollback).

> **Note on `CARD-004` (`test-backfill`)**: If `cargo-mutants` is **not** installed, `CARD-004` will block with `mutation-cap` when `[gates] mutation = "report"` and `mutation_required_for = ["test-backfill"]`. Either install `cargo-mutants` beforehand or set `mutation = "off"` in `.openfarm/config.toml` and commit that config change on `main`.

#### 1. Prepare Queue & Run 4 Cards

```bash
# Run micromanager queue preparation (or inspect findings before --apply)
PYTHONPATH="$OPENFARM_HOME" python3 -m openfarm_core.cli --repo "$TARGET_REPO" --json supervise --prepare-queue --apply

# Run up to 4 ready cards in one run (intra-run Harness² transfer active across cards)
PYTHONPATH="$OPENFARM_HOME" python3 -m openfarm_core.cli --repo "$TARGET_REPO" --json run --cards 4 --spend 10.00
```

#### 2. Verify Fenced `harness adaptations`, Supervision Report & Probe Transition

```bash
# Inspect supervision reports
ls -la "$OPENFARM_STATE/supervision/"
cat "$OPENFARM_STATE"/supervision/*-supervise.json

# Inspect generated briefs for fenced `harness adaptations`
grep -rn "harness adaptations" "$OPENFARM_STATE/cards/" || true

# Inspect ledger events (revision_confirmed vs demotion / probe_regression) and metrics
cat "$OPENFARM_STATE/harness/ledger.jsonl"
cat "$OPENFARM_STATE/harness/metrics.json"
PYTHONPATH="$OPENFARM_HOME" python3 -m openfarm_core.cli --repo "$TARGET_REPO" --json harness index
```

#### 3. Verification Gate & Branching Decision

- `supervision/<stamp>-supervise.json` exists and reports `prepare_queue` recommendations/findings.
- When `active_overlay.json` held active/probing cues matching the card's `language`/`kind`/`role`, `cards/<id>/<run>/briefs/*-implementer.md` (or `*-test_writer.md` / `*-planner.md`) contains a fenced `[harness adaptations]` block inside `-------- BEGIN CARD DATA (not instructions) --------`.
- After `probe_window` (`3`) non-`Infra` outcomes complete, `ledger.jsonl` records either:
  - `revision_confirmed` (`state` returns to `"confirmed"`, probing entries promote to `"active"`), **or**
  - `demotion` with `reason = "probe_regression"` (`auto_demote = true` rolls `active_overlay.json` back to `parent_revision`, marks entries `"demoted"`, and promotes any buffered `"candidate"` entries).
- **If the circuit breaker trips (`3` consecutive blocks with the same stop code)**: Check `cat "$OPENFARM_STATE/breaker.json"`, inspect the dossiers of the real card IDs listed there, fix the underlying config/card issue, then run `openfarm resume --clear-breaker`.

---

### Phase 6 — Review Dossiers, Record Verdicts, Curate Harness & Clean Up

#### 1. Inspect Dossiers & Review Local Branches

```bash
cd "$TARGET_REPO"
git branch --list 'openfarm/*'

# Read Markdown dossier (test diff appears before production diff)
PYTHONPATH="$OPENFARM_HOME" python3 -m openfarm_core.cli --repo "$TARGET_REPO" dossier CARD-001

# Inspect actual git diff on the local branch (never pushed)
git diff main..openfarm/CARD-001-*
```

#### 2. Triage Blocked Cards & Record Human Verdicts

```bash
# If any cards ended blocked, ask micromanager to triage them
PYTHONPATH="$OPENFARM_HOME" python3 -m openfarm_core.cli --repo "$TARGET_REPO" --json supervise --triage-blocked

# Record your review verdict for each reviewed card
PYTHONPATH="$OPENFARM_HOME" python3 -m openfarm_core.cli --repo "$TARGET_REPO" --json \
  verdict CARD-001 clean --review-tokens 1200 --minutes 4 --note "Boundary and refund checks verified"

# If a defect is found later post-review:
# PYTHONPATH="$OPENFARM_HOME" python3 -m openfarm_core.cli --repo "$TARGET_REPO" --json \
#   escape CARD-001 --note "Describe escaped edge case"
```

#### 3. Curate Recursive Harness Memory & Verify Clean Local State

```bash
# Inspect index and promote or demote specific entries/revisions if desired
PYTHONPATH="$OPENFARM_HOME" python3 -m openfarm_core.cli --repo "$TARGET_REPO" --json harness index
# PYTHONPATH="$OPENFARM_HOME" python3 -m openfarm_core.cli --repo "$TARGET_REPO" --json harness demote <ENTRY_ID_OR_rev-0001> --reason "noisy cue"
# PYTHONPATH="$OPENFARM_HOME" python3 -m openfarm_core.cli --repo "$TARGET_REPO" --json harness promote <ENTRY_ID> --reason "helpful cue"

# To wipe active adaptations back to rev-0000 (--yes required):
# PYTHONPATH="$OPENFARM_HOME" python3 -m openfarm_core.cli --repo "$TARGET_REPO" --json harness reset --yes

# Land approved branches locally after review (Openfarm never merges or pushes):
# git checkout main && git merge --no-ff openfarm/CARD-001-<slug>
```

#### 4. Final Verification Gate

- Every approved branch (`openfarm/<card-id>-<slug>`) has a recorded `verdict.json` (`openfarm verdict ... --review-tokens N`).
- `git status` on `main` is clean and no automated `git push` was executed.
- `openfarm status` reports no active `run.lock`, no tripped `breaker`, and no lingering `STOP` kill switch.
