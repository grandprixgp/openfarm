# Openfarm v0.8.1 Engineering Handoff

Concise entry point for engineers reviewing, testing, or extending Openfarm v0.8.1.

For greenfield empty-folder initialization (`"Read and install this skill <link>"` -> `/openfarm-grill-me` -> `"/openfarm-initialize calorie-log ..."`), see [docs/FROM-IDEA-TO-SHIPPED-APP.md](FROM-IDEA-TO-SHIPPED-APP.md). For step-by-step operator instructions, config table reference, provider setup, card authoring rules, and the executable `tallybox` first-deployment runbook on existing repos, see [docs/USAGE-AND-FIRST-DEPLOYMENT.md](USAGE-AND-FIRST-DEPLOYMENT.md). Module-level detail is in [docs/openfarm-internals.md](openfarm-internals.md).

## Current State (v0.8.1, 17 Nov 2026)

v0.8.1 was built in two rounds. The first agent added Codex and Antigravity as providers, `openfarm drive` and the TypeScript pack. Two reviews and live runs with the real tools then found gaps, and the fix work closed them. Since the first agent's last commit (`3129c0e`): 130 files changed, about 25,900 lines added and 2,900 removed, tests included. The history of both rounds is in [docs/v0.8.1-build-log.md](v0.8.1-build-log.md).

### Done

- **Providers.** Claude, Codex and Antigravity for the micromanager, swarm seats and an ACP worker backend. Codex and Antigravity children run inside a bubblewrap boundary under the default `[micromanager] confine = "auto"` (Codex through openfarm's bridge with tool policy `none` also has its tools turned off by flags, and runs on those flags alone when `bwrap` is missing). Logins are found through `CLAUDE_CONFIG_DIR` / `CODEX_HOME` and copied back only when checked. A binary that names another provider is refused.
- **Drive.** `openfarm drive` holds the coordinator and supervisor seats with a `claude`, `codex` or `agy` CLI. An aborted drive ends cleanly and frees the lock; there are no made-up answers or verdicts; the driver's spend counts in a managed run; each turn has a timeout and its own process group; a driven run's token split counts the driver.
- **Checker.** Both passes answer in a fixed form and are asked once more when unreadable. Counter-tests are written as files, run one at a time, and removed, with the worktree restored exactly. The dossier shows the verdict and every counter-test's outcome.
- **Packs.** Python uses ruff only where configured and pytest where configured. Rust mutation reads cargo-mutants' `outcomes.json` and reports timeouts and unviable mutants apart. Every row of a named test decides fail-before and pass-after. An infra run is never green. TypeScript runs `node --test` with a reporter that cannot read red as green, takes one tool set from the base checkout, and refuses other test runners.
- **Board, coordinators, flow, CLI.** Fixes to who may post and hint, stop requests, role models, the stall monitor, late joins, escalation, hand-back triage, the human run summary, token parts, TUI background commands and per-card supervisor sessions.
- **Tests.** 1411 offline tests on the final tree with no failures (89 skipped with no optional tools on `PATH`; 15 with `OPENFARM_E2E=1` and ruff, tsc, prettier and cargo-mutants installed). The skip count depends on what is installed.

### Tested live (17 Nov 2026)

Real ruff, pytest, cargo-mutants, tsc and prettier; the checker with a live model on 24 runs; Codex through the bridge inside the boundary (and one bench card supervised that way); a standalone `codex-acp` up to handshake and options; Claude through the bridge and the standalone `claude-agent-acp`; `openfarm drive` with real Codex and real Claude; the 12-task bench with Codex and with Claude, farm and solo, one repeat, before and after the test-writer fixes. Numbers are in the [README](../README.md#whats-verified), the [build log](v0.8.1-build-log.md) and the [bench report](bench-v0.8.1.md).

### Explicitly Untested

- Everything Antigravity since the first agent's runs: the boundary around `agy`, the login copy, the prompt-size refusal, the driver. Nobody may run `agy` unattended here.
- A turn through a standalone `codex-acp` server, and so per-card confined servers with it.
- Codex `read-only` inside the boundary on a system that allows a nested sandbox.
- In `drive` with a real CLI: Ctrl-C, taking over a stale lock, a turn timeout, a spend cap stopping a priced driver, two drives under one name.
- TypeScript on a real npm-installed project with a live model writing counter-tests (the TypeScript counter-test path ran with real node and scripted answers).
- Remote installs from a GitHub URL with `uv`, `pipx` or `pip`.
- Hosted worker endpoints other than Command Code.
- The 25-card Rust pilot: not started. No card set exists for it.
- `counter_tests = "reject"` end to end with a live implementer fixing the bounce.
- A Claude child on an API key (`auth = "api-key"`) inside the boundary (`confine = "bwrap"`).
- `openfarm run` refusing a run up front on a machine without bubblewrap, with the real Codex or Antigravity programs (it ran with stand-ins).

### Known Limits

- **The runner has no sandbox.** Tests, and the code they run, can do what your user can. A TypeScript test that writes through the linked `node_modules` into the base checkout is not undone.
- **Parallel swarm spend.** In a parallel swarm run, a card's `micromanager_spend` may include other cards' supervisor spend from the same moment (unproven, noted by a builder).
- **Parametrized tests and `known_failing`.** A card whose every pytest parameter row is on `known_failing` passes the eligibility rule's name check, because that rule compares exact names.
- **Claude under `confine = "auto"`** has no OS boundary from openfarm; Claude Code's own settings hold it. `confine = "bwrap"` puts it in the boundary.
- **Codex `read-only`** cannot run inside the boundary where Codex's own sandbox cannot start (Ubuntu's default AppArmor rule) and is refused there.
- **Standalone `codex-acp`** (0.16.0) completed no turn on a ChatGPT login; use `binary = "codex-exec"`.
- **Unpriced providers.** Codex and Antigravity report no cost. Without per-1k rates their spend reads zero and the spend caps cannot fire on it; the run's notes say so.
- **The boundary shares the network**, because the model call needs it. That includes this machine's loopback services.
- **What the boundary mounts.** A provider program installed outside a `bin` folder or a `node_modules` tree gets its whole folder mounted read-only. `confine_read_roots` are mounted as given, without the guards the install folders get, and the worker key file is not hidden inside them.
- **Hidden files and links.** A `never_touch` file reached through a symlinked folder in the worktree is not hidden from a confined child. A standalone server's hidden-file list is fixed when the server starts.
- **A standalone `codex-acp` with `read-only`** is started although its shell cannot work inside the boundary; only the bridge refuses up front.
- **Cached input tokens** are added to a server's input count unless its own total shows they are already in it; a server that includes them and reports no total would be counted high, which stops a cap early, never late.
- **Lock and seat takeover** trust the pid and hostname. Two processes with the same hostname in different pid namespaces (containers), or a state folder shared between machines with the same hostname, can see each other as gone.
- **Counter-tests on a dirty worktree.** The restore is exact for the clean worktree a card normally has; a difference after the restore is logged, not failed.
- **A provider CLI that updates itself during a run** can be missing for a moment (seen with Claude Code's npm auto-update, twice in one bench). Cards that start a child then stop as `infra` ("Failed to spawn"); nothing is retried. For a long run, point `[micromanager] binary` at a fixed copy or turn the CLI's auto-update off.
- **`openfarm swarm join` errors** carry no code in JSON, so a script cannot tell `seat-occupied` from `name-taken`.
- **No mutation tool for Python or TypeScript.**

### Where the Records Are

- **In this repository:** the [build log](v0.8.1-build-log.md) (both rounds, with trees and token shares), the [bench report](bench-v0.8.1.md), the [README](../README.md) live and untested lists, and the claim-to-test table in [internals](openfarm-internals.md#verification-status--claim-to-test-mapping).
- **Per run:** `<state_dir>/runs/<run_id>/run.json` (notes, token split, `tokens.with_driver` in a driven run), `<state_dir>/cards/<card>/<run>/` (briefs, responses, gate records including `gates/counter-test-N.json` and `gates/mutation/N/`, `dossier.md`, `verdict.json`), and the board under `runs/<run_id>/board/`.
- **Bench:** `bench-report.md`, `bench-report.json` and `records.jsonl` in the `--out` folder.
- The raw outputs of the 17 Nov live runs are kept outside the repository with the fix work's notes.

## Where to Start Reading

1. `openfarm_core/config.py`, `openfarm_core/config_template.py`, and `tests/test_project_config.py` — complete 15-table configuration contract (`KNOWN_TABLES`, `_TABLE_KEYS`, `TierConfig`, `SwarmConfig`, `RoleModel.effort`, `HarnessConfig`), including `[micromanager] confine` and the `[runner]` TypeScript keys.
2. `openfarm_core/adapters/acp.py`, `openfarm_core/confine.py`, `openfarm_core/logins.py`, the three bridges (`claude_native_acp.py`, `codex_native_acp.py`, `agy_native_acp.py`), `openfarm_core/acp_setup.py`, and `openfarm_core/micromanager.py` — ACP v1, how each provider's child is held, login copy and copy-back, `openfarm acp`, stub checks, budget caps, and the six supervisory ops (`tests/test_v081b_acp_*.py`, `tests/test_v081_lead_*.py`, `tests/test_multi_provider_acp.py`).
3. `openfarm_core/driver.py` — `openfarm drive` (`tests/test_v081b_drive_*.py`, `tests/test_v081c_drive2_*.py`).
4. `openfarm_core/roles/__init__.py`, `openfarm_core/flow.py` and `openfarm_core/dossier.py` — the checker's answer forms, counter-test runs and the worktree restore (`tests/test_v081b_checker_*.py`, `tests/test_v081c_glue_*.py`).
5. `openfarm_core/packs/` and `openfarm_core/gates/checks.py` — the Rust, Python and TypeScript packs, row rules, mutation, `revert-check` (`tests/test_v081b_packs_*.py`, `tests/test_v081b_ts_*.py`).
6. `openfarm_core/harness_memory.py` and `tests/test_harness_efficiency.py` — `HarnessLibrary`, `HarnessLedger`, contrast capture, anti-injection validation, probe-window rollback, and `openfarm harness` CLI.
7. `openfarm_core/board.py`, `swarm.py`, `coordinator.py`, `tui.py`, `bootstrap.py` and `cli.py` — the board, checked swarm actions, coordinators, `StallMonitor`, parallel cards, `openfarm init` / `install-skill`, and the 22-subcommand CLI.
8. `tests/acp_stand_in.py` and the other stand-in CLIs in the provider tests — how the suite speaks to providers without calling a model.

## Security & Trust Rules to Preserve When Modifying

- Never add filesystem-write or shell/terminal capabilities to `ClaudeAcpAdapter.initialize`.
- Never relax `_is_path_allowed_readonly` in `openfarm_core/adapters/acp.py`: read-only micromanager access must remain bounded by `resolves_within` against the session's own folder and blocked from `never_touch` / `forbidden_files` (including `.openfarm/**`).
- Never weaken `confine.decide`: a Codex or Antigravity child that needs the boundary is refused when it cannot have one, and `confine = "off"` must keep saying that the child can read whatever the user can.
- Keep every `.git`, every `never_touch` and `forbidden_files` match and the worker key file hidden inside the boundary.
- Keep `[model] key_file` and host credentials strictly separated from `_sanitised_env` in the ACP adapter, and copy a login back only through `logins.sync_back`.
- Never let a driver write an approval or a verdict it was not given; unanswered questions go the way `[swarm] on_external_timeout` says.
- Keep deterministic gates (`format`, `test`, `lint`, `revert-check`, `mutation`, `coverage`, `extra`, counter-test runs) independent of micromanager overrides; `advisory` mode must never mutate repository files or block deterministic passes.
- Never add deterministic gates, policy lists, touches, or budgets (`IMMUTABLE_GATE_KEYS`) to `EDITABLE_SURFACES`, never bypass `validate_harness_proposal` / `sanitize_lesson_text`, and never record `Infra` aborts as harness failures in `HarnessLibrary` / `HarnessLedger`.

## Suggested Next Checks for Live Bring-Up

1. `openfarm acp status` and `openfarm acp probe --provider <p>` for the provider you will use, and read the "Provider Children" and "Confinement" sections.
2. Run `openfarm supervise --prepare-queue` on a disposable target repo in `mode = "advisory"` first, inspecting `<state_dir>/cards/<id>/dossier.md` and micromanager transcripts.
3. Run a single card against the intended hosted endpoint (`openfarm check <id>` then `openfarm run --card <id>`) to validate live model formatting, token accounting, and `revert-check` end-to-end.
4. If `gates.mutation` is `"required"` or `"report"` on Rust crates, smoke-test `cargo-mutants` on the target repo's unit test suite.

## History: What Changed in v0.4

1. **Expanded Card Diet & Authoring (`cards.py`, `eligibility.py`, `packs/rust.py`, `edits.py`)**:
   - Added `feature`, `refactor`, `integration`, and `port` alongside the v0.3 diet (`test-backfill`, `mechanical`, `pattern`, `pinned-bugfix`, `small-feature`).
   - Added optional card metadata fields (`unit`, `context_files`, `type_changes`) and fenced ` ```openfarm ` / ` ```toml ` metadata blocks (`[cards] fields_block = "toml"`).
   - Relaxed multi-file `new_symbols` restriction when `type_changes = true` or `[micromanager] scaffold_contracts = true`.
   - Upgraded `RustPack.signatures()` to extract full struct/enum/trait/type-alias/const/static/union/`mod`/`impl` signatures, added atomic multi-file `apply_edits`, and added checkpointed `plan.steps` execution in Phase B.
2. **6th Built-in Adapter & ACP Micromanager (`micromanager.py`, `adapters/acp.py`)**:
   - Added `[adapters] micromanager` (`off`, `claude-acp`, `scripted`, `cmd:<argv>`), alongside `telemetry` and `pack` `cmd:<argv>` support across all six adapters (`PROTOCOL_VERSION = 1`).
   - Implemented `ClaudeAcpAdapter` speaking Agent Client Protocol v1 (NDJSON JSON-RPC 2.0 over stdio, then defaulting to `claude-agent-acp`).
   - Added six bounded supervisory ops (`shape_and_plan`, `scaffold_contract`, `coach`, `adjudicate_checker`, `triage_and_review`, `reflect_harness`), `[micromanager]` budget/policy controls, `supervisor-reject` stop code, `openfarm supervise`, and `openfarm run --mm-script`.
3. **Security & Gate Hardening**:
   - ACP capability lock-down (`clientCapabilities: {}`, `-32601` for write/terminal RPCs, `session/request_permission` restricted to `"none"` or `"read-only"` inside worktree excluding `never_touch`/`forbidden_files`, isolated `$HOME`, key isolation, ANSI/OSC stripping, `todo!()`/`unimplemented!()` stub validation).
   - Phase A fail-before retry loop inside `test_writer_attempts` (with `scaffold_commit`).
   - Formatting before `full_diff` and `revert-check`; `revert-check` evaluated against `base + frozen-tests`.
   - Split stdout/stderr Cargo parser with signal detection; `--test <stem>` counter-test execution and bounce-back adjudication.
   - Safe git flag/config allowlist, `_safe_id` card-ID path sanitization, `[state] min_free_mb` disk guard, HTTP `429`/`5xx` retries, strict config table/key validation, and real card IDs in `breaker.json`.
4. **Recursive Harness Efficiency (`harness_memory.py`, `[harness]`, `HarnessLibrary`, `HarnessLedger`)**:
   - Inspired by `google-research/harness2` (`arXiv:2610.14224`), stores overlays, `revisions/rev-XXXX.json`, `entries/<id>/EXPERIENCE.md` + `patch.json`, rebuilt `INDEX.md`, `ledger.jsonl`, and `metrics.json` outside the repo under `<state_dir>/harness/`.
   - Deterministic verification and policy/budget gates (`IMMUTABLE_GATE_KEYS`) remain strictly immutable; adaptations target only the 5 `EDITABLE_SURFACES`.
   - Dual capture via deterministic `extract_contrast` (even with micromanager `"off"`) and `reflect_harness`, sanitized against edit markers, path traversals, assertion leaks, and gate-bypass directives.
   - Intra-run transfer and cross-launch persistence feeding fenced `harness adaptations` in briefs and `micromanager.harness_context`.
   - Propose–probe–confirm/auto-demote rollback on efficiency regression, `Infra` isolation, card telemetry, dossier summary, and `openfarm harness` (`status`, `index`, `show`, `promote`, `demote`, `reset --yes`).
