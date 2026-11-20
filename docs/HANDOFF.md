# Openfarm v0.14.0 Engineering Handoff

Concise entry point for engineers reviewing, testing, or extending Openfarm v0.14.0.

For greenfield empty-folder initialization (`"Read and install this skill <link>"` -> `/openfarm-grill-me` -> `"/openfarm-initialize calorie-log ..."`), see [docs/FROM-IDEA-TO-SHIPPED-APP.md](FROM-IDEA-TO-SHIPPED-APP.md). For step-by-step operator instructions, config table reference, provider setup, card authoring rules, and the executable `tallybox` first-deployment runbook on existing repos, see [docs/USAGE-AND-FIRST-DEPLOYMENT.md](USAGE-AND-FIRST-DEPLOYMENT.md). Module-level detail is in [docs/openfarm-internals.md](openfarm-internals.md).

## Current State (v0.14.0, 20 Nov 2026)

`v0.10.0`–`v0.14.0` turn Openfarm into a multi-project autonomous software factory while keeping `openfarm_core/` **100% Python 3.11+ stdlib-only** (`dependencies = []`, optional `openfarm_tui/` Textual extra with automatic `curses` fallback) and strictly local-only merges (zero `git push` in core, zero `$` spend displayed in TUI or Web UI):

- **`v0.10.0`–`v0.10.2`:** Optional Textual workbench (`0 Projects`, `1 Studio`, `2 Review`, `3 Findings`, `4 Engine`), global Providers (`providers.toml` + `x-cmd-zdr`), global Projects Registry (`projects.toml` + workspace auto-discovery, non-destructive remove), In-TUI Card Editor & Split, deduplicated Findings Inbox + Wave Synthesis, Touches Conflict Matrix, and `openfarm doctor [--fix] [--probe]`.
- **`v0.11.0`:** Cards-as-the-DAG (`openfarm_core/dag.py`, `openfarm plan`), deterministic trial-rebase (`openfarm_core/factory.py`), and opt-in `[factory] auto_land` onto local `openfarm/integration` (`openfarm land --promote-integration`).
- **`v0.12.0`:** Architectural Invariants gate (`openfarm_core/invariants.py`, `[invariants]`), Runtime Acceptance CLI/HTTP smoke gate (`openfarm_core/acceptance.py`, `[acceptance]`), and Golden Replay Vault (`openfarm_core/replay.py`, `openfarm replay`).
- **`v0.13.0`:** Iterative **Idea → Conversational Frontier Architect Grill → Living Spec & Ordered Chunks (`.openfarm/CHUNKS.json`) → Split Chunk into Cards → Run → Review & Land → Iterate** flow with stack-authentic prompts, shared Live Architect Log (`#architect-panel`), Adaptive Model Router (`openfarm_core/routing.py`, `openfarm routing`), and Attempt Timeline / Phase-B resume inspection (`openfarm_core/timeline.py`, `openfarm timeline`).
- **`v0.14.0`:** Detached Factory Daemon (`openfarm_core/daemon.py`, `openfarm daemon {start,stop,status,logs}`), loopback-by-default stdlib Web Companion with SSE, per-session `X-Openfarm-Token` CSRF protection & built-in Tailscale HTTPS / IPv4 integration (`openfarm_core/web.py`, `openfarm web [--tailscale|--bind tailscale]`), and optional webhook/desktop notifications (`openfarm_core/notify.py`, `[notifications]`).

## Previous State (v0.9.2, 19 Nov 2026)

v0.9.2 makes the TUI **high-contrast by default** (`OPENFARM_TUI_THEME=high-contrast`, optional `soft`) and turns bare `openfarm` Automode into an **in-TUI execution pipeline** handling workspace init/adopt, Frontier OAuth login, masked Worker API key setup (`0600` atomic or reuse from `~/.config/openfarm/*.key`), interactive Grill + spec expansion, and native in-TUI card review ([docs/v0.9.2-build-log.md](v0.9.2-build-log.md)).

## Previous State (v0.9.1, 19 Nov 2026)

v0.9.1 resolves all findings from the Carbon (High) full-codebase audit of v0.9.0 and adds bare `openfarm` Automode TUI + command palette (`openfarm auto`), frontier card-complexity triage (`openfarm triage`), and mixture-of-frontiers review (`openfarm frontier-review`) ([docs/v0.9.1-build-log.md](v0.9.1-build-log.md)).

## Previous State (v0.9.0, 19 Nov 2026)

v0.9.0 promotes `[gates] counter_tests = "reject"` to default, adds named supervision profiles (`--profile lean | balanced | strict`), adds `openfarm review` and opt-in local-only `openfarm land`, adds the Go language pack (`openfarm_core/packs/go.py`), adds `vitest` and `jest` runner support to `TypeScriptPack`, and adds polyglot per-unit languages under `[units.<name>]` ([docs/v0.9.0-build-log.md](v0.9.0-build-log.md)). Builds on v0.8.8's small-card economics and retry effort ladder ([docs/bench-v0.8.8.md](bench-v0.8.8.md), [docs/v0.8.8-build-log.md](v0.8.8-build-log.md)).

### Done in v0.9.0

- **Defaults & named profiles.** `counter_tests = "reject"` is now the default; `--profile lean | balanced | strict` bundles `shape_when`, `pre_review_when`, `checker_mode`, `counter_tests`, `mutation`, and `confine`.
- **Wave review & local serial merge queue.** `openfarm review` (interactive + `--json` parity) and `openfarm land <CARD...>` (local-only trial-merge + test gate + `git merge --no-ff` + worktree cleanup + `done`; zero `git push`).
- **Go language pack.** `openfarm_core/packs/go.py`, `adopt --lang go` (`go.mod` detection), `go test -json`, `go vet`, `gofmt`, banned patterns, and `mutate.py --lang go`.
- **TypeScript `vitest` / `jest`.** `[runner] typescript` accepts `"node"`, `"vitest"`, or `"jest"`, with JSON output parsing and `adopt` auto-detection.
- **Polyglot per-unit languages.** `[units.<name>] language = "rust" | "python" | "typescript" | "go"` routed through `_configured_pack`.
- **Tests.** 1,611 offline tests with 0 failures (default and `WITH_TOOLS=1 OPENFARM_E2E=1`).
- **Untested in v0.9.0.** Live execution against real `go`, `vitest`, and `jest` binaries (none installed on this machine) and a live multi-language polyglot wave.

## Previous State (v0.8.7, 19 Nov 2026)

v0.8.7 scopes `cargo-mutants` to the card's own diff (`--in-diff`), validates the v0.8.6 pre-review rule across a full 3-repeat bench on both Claude and Codex ([docs/bench-v0.8.7.md](bench-v0.8.7.md)), aligns the two bench cards' Done text, and fixes TTY-hang and symlinked-`TMPDIR` `bwrap` bugs ([docs/v0.8.7-build-log.md](v0.8.7-build-log.md)).

### Done in v0.8.7

- **`cargo-mutants` scoped to the card's own diff (`--in-diff`).** `RustPack` writes `card.diff` into the mutation output folder (outside the worktree, mounted inside `bwrap`), keeps `test-backfill` mutating whole unit files, and handles zero-mutant diffs cleanly. Verified live on pilot card B4 (14 mutants → 1).
- **Symlinked `TMPDIR` fixed for `bwrap` children.** Resolved `isolated_home`, ACP worker empty `cwd`, and standalone mutation temp folders before `bwrap` mounts them.
- **Auth-status checks detached from the controlling TTY.** `claude auth status` and `codex login status` now run with `stdin=DEVNULL` and `start_new_session=True`.
- **Bench card wording aligned.** `py-money-parse` and `py-validate-port` state that inputs past Python's `int()` digit limit are out of scope.
- **Tests.** 1569 offline tests on the final tree with no failures (98 skipped default, 6 skipped with `WITH_TOOLS=1 OPENFARM_E2E=1`).

### Tested live in v0.8.7

- Full 12-task, 3-repeat bench on Claude and Codex, farm and solo (144 runs): Claude farm 35/36 vs solo 34/36 (37.5% fewer frontier tokens, 0 `supervisor-reject`); Codex farm 34/36 vs solo 36/36 (45.9% fewer frontier tokens, 0 false pre-review bounces).
- Scoped `cargo-mutants --in-diff` on pilot card B4 (`semver-r2`).

### Done in v0.8.6

- **The pre-review has a rule.** The supervisor bounces only for a missed or broken line of the Done text, and quotes it. Thin tests and unrequested robustness go in `findings`. On the five tasks the Codex farm arm had lost, it went from 6 of 15 to 12 of 15.
- **Tests are linted and formatted before they freeze**, and a lint finding goes back to the test writer.
- **The runner boundary** also hides the `CLAUDE_CONFIG_DIR` and `CODEX_HOME` login folders and unsets variables that look like secrets.
- **A reference that is also a touched file is sent once** in the first implementer brief.
- **Fixes to cards that stopped for no good reason:**
  - scaffold stubs inside an `impl`;
  - plan test names written the way the brief lists them;
  - delete-only edits;
  - Rust crate-root formatting;
  - a stale test-lint record.
- **ACP.** A refused effort setting is a note, and RPC errors keep the provider's reason.
- **Tests.** 1557 offline tests on the final tree with no failures. 96 are skipped with no optional tools on `PATH`, and 6 with `OPENFARM_E2E=1` and ruff, pytest, tsc, prettier and cargo-mutants installed.

### Tested live in v0.8.6

- The 12-task bench, 3 repeats, Claude and Codex, farm and solo (144 runs).
- The 25-card Rust pilot on dtolnay/semver, in three rounds (50 card runs), with five frontier baselines.
- `counter_tests = "reject"` end to end with a live implementer fixing the bounce (TypeScript, real npm project).
- `openfarm drive` on v0.8.5, with Ctrl-C, stale-lock takeover, turn timeout, spend cap and two drives under one name.
- `[runner] confine = "bwrap"` on a real Rust build.
- The mutation runner on more-itertools.
- Remote installs with uv, pipx and pip.
- `openfarm run` with no bubblewrap and the real Codex.
- A standalone `codex-acp` started per card inside bwrap.

### Untested in v0.8.6

- Everything Antigravity since the first agent's runs. Nobody may run `agy` unattended here.
- A completed turn through a standalone `codex-acp` server: on a ChatGPT login, codex-acp 0.16.0's bundled Codex core is refused by the backend.
- Codex `read-only` inside the boundary on a system that allows a nested sandbox.
- Hosted worker endpoints other than Command Code.
- A Claude child on an API key inside the boundary.
- `openfarm run` with no bubblewrap and the real Antigravity program.

### Known Limits found in v0.8.6

- **On small single-file cards the farm still spends more tokens in total** than the frontier model alone. It moves about a third of the frontier model's own tokens off it.
- **cargo-mutants measures the whole changed file**, so outside test-backfill its kill rate says little about the card. Keep `required` for test-backfill only.
- **The spend cap is checked after a driver turn**, so a drive can overshoot it by about one turn.

## v0.8.5 (18 Nov 2026)

v0.8.5 makes the supervisor cheaper in tokens, makes `openfarm drive` behave the same as ACP mode, and fixes the v0.8.1 known limits that code can fix. Over the 12 bench tasks with scripted workers, the supervisor gets 62% fewer prompt characters in 45% fewer calls. One live call through the lean prompts takes 58% (Claude) and 57% (Codex) fewer input tokens. The details are in [docs/v0.8.5-build-log.md](v0.8.5-build-log.md).

### Done in v0.8.5

- **Supervisor tokens.**
  - openfarm's bridges replace Claude's and Codex's built-in system prompts (`[micromanager] lean_prompt`).
  - Payloads are compact: no local paths, and passing gates are cut down.
  - One `shape_and_scaffold` call replaces two calls when a card needs both (`[micromanager] combine_calls`).
  - A clean first-try pass gets no reflection call (`[harness] reflect_on`).
- **Drive parity.**
  - With no flags, `drive` uses `[micromanager]`'s model, effort and wait.
  - A drive turn with only questions carries only the questions.
  - Drive sees the same compact payloads as every other backend.
- **Worker effort.** `[roles.<role>] retry_effort` sets the effort for every retry. In this repository's config the DSF roles run high on the first try and max on every retry.
- **Backlog.**
  - Mutation for Python and TypeScript, through openfarm's own runner.
  - An opt-in runner boundary (`[runner] confine = "bwrap"`).
  - Parametrized `known_failing` rows are refused.
  - Counter-test restore past 64 MB.
  - Lock takeover now checks the machine, boot and pid namespace.
  - Provider CLIs that are updating themselves get retried.
  - `never_touch` paths behind symlinked folders are hidden.
  - A read root holding home is refused.
  - Standalone `codex-acp` with `read-only` is refused where it can't run.
  - JSON error codes.
  - A test for parallel supervisor spend.
- **Tests.** 1524 offline tests on the final tree with no failures (93 skipped with no optional tools on `PATH`; 5 with `OPENFARM_E2E=1` and ruff, pytest, tsc, prettier and cargo-mutants installed).

### Tested live in v0.8.5

- One `shape_and_plan` call through the Claude and the Codex bridge, with the built-in system prompt and with the lean one.
- One merged `shape_and_scaffold` call through each bridge. Both answered `ready` with a correct stub.
- Command Code taking `reasoning_effort = "high"` and `"max"` for DeepSeek V4.1 Flash.

### Untested in v0.8.5

Every item here except Antigravity was run live in the v0.8.6 work (above).

## v0.8.1 (17 Nov 2026)

v0.8.1 was built in two rounds. The first agent added Codex and Antigravity as providers, `openfarm drive` and the TypeScript pack. Two reviews and live runs with the real tools then found gaps, and the fix work closed them. Since the first agent's last commit (`3129c0e`): 130 files changed, about 25,900 lines added and 2,900 removed, tests included. The history of both rounds is in [docs/v0.8.1-build-log.md](v0.8.1-build-log.md).

### Done in v0.8.1

- **Providers.** Claude, Codex and Antigravity for the micromanager, swarm seats and an ACP worker backend. Codex and Antigravity children run inside a bubblewrap boundary under the default `[micromanager] confine = "auto"` (Codex through openfarm's bridge with tool policy `none` also has its tools turned off by flags, and runs on those flags alone when `bwrap` is missing). Logins are found through `CLAUDE_CONFIG_DIR` / `CODEX_HOME` and copied back only when checked. A binary that names another provider is refused.
- **Drive.** `openfarm drive` holds the coordinator and supervisor seats with a `claude`, `codex` or `agy` CLI. An aborted drive ends cleanly and frees the lock; there are no made-up answers or verdicts; the driver's spend counts in a managed run; each turn has a timeout and its own process group; a driven run's token split counts the driver.
- **Checker.** Both passes answer in a fixed form and are asked once more when unreadable. Counter-tests are written as files, run one at a time, and removed, with the worktree restored exactly. The dossier shows the verdict and every counter-test's outcome.
- **Packs.** Python uses ruff only where configured and pytest where configured. Rust mutation reads cargo-mutants' `outcomes.json` and reports timeouts and unviable mutants apart. Every row of a named test decides fail-before and pass-after. An infra run is never green. TypeScript runs `node --test` with a reporter that cannot read red as green, takes one tool set from the base checkout, and refuses other test runners.
- **Board, coordinators, flow, CLI.** Fixes to who may post and hint, stop requests, role models, the stall monitor, late joins, escalation, hand-back triage, the human run summary, token parts, TUI background commands and per-card supervisor sessions.
- **Tests.** 1438 offline tests on the final tree with no failures (89 skipped with no optional tools on `PATH`; 15 with `OPENFARM_E2E=1` and ruff, tsc, prettier and cargo-mutants installed). The skip count depends on what is installed.

### Tested live (17 Nov 2026)

Real ruff, pytest, cargo-mutants, tsc and prettier; the checker with a live model on 24 runs; Codex through the bridge inside the boundary (and one bench card supervised that way); a standalone `codex-acp` up to handshake and options; Claude through the bridge and the standalone `claude-agent-acp`; `openfarm drive` with real Codex and real Claude; the 12-task bench with Codex and with Claude, farm and solo, one repeat, before and after the test-writer fixes. Numbers are in the [README](../README.md#whats-verified), the [build log](v0.8.1-build-log.md) and the [bench report](bench-v0.8.1.md).

### Explicitly Untested (at v0.8.1; see v0.8.6 above for the current list)

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

- **Claude under `confine = "auto"`** has no OS boundary from openfarm; Claude Code's own settings hold it. `confine = "bwrap"` puts it in the boundary.
- **Codex `read-only`** can't run inside the boundary where Codex's own sandbox can't start (Ubuntu's default AppArmor rule), so it is refused there, for the bridge and for a standalone `codex-acp` alike.
- **Standalone `codex-acp`** (0.16.0) completed no turn on a ChatGPT login; use `binary = "codex-exec"`.
- **Unpriced providers.** Codex and Antigravity report no cost. Without per-1k rates their spend reads zero and the spend caps can't fire on it; the run's notes say so.
- **The boundary shares the network**, because the model call needs it. That includes this machine's loopback services. The same holds for `[runner] confine = "bwrap"`.
- **What the boundary mounts.** A provider program installed outside a `bin` folder or a `node_modules` tree gets its whole folder mounted read-only. `confine_read_roots` are mounted as given, without the guards the install folders get, and the worker key file isn't hidden inside them. A read root that holds your home folder is refused.
- **A standalone server's hidden-file list** is fixed when the server starts.
- **Cached input tokens** are added to a server's input count unless its own total shows they are already in it. A server that includes them and reports no total would be counted high, which stops a cap early, never late.
- **The runner boundary is opt-in** (`[runner] confine = "off"` by default). Without it, tests and the code they run can do whatever your user can. With it, a tool that writes outside the worktree (a download cache, `~/.cargo/registry`) fails unless that folder is listed in `confine_write_roots`.
- **openfarm's mutation runner** makes a fixed set of small changes: operators, constants, returned values, and for TypeScript token swaps outside strings and comments. It is no full mutation framework. At most 40 mutants run per gate, spread evenly over the changed lines, and the record says when the cap was hit.
- **A merged `shape_and_scaffold` answer** that gives a contract the gates refuse is handled as a refused `scaffold_contract`. It isn't retried as a separate call; only an answer with no contract at all is.
- **Counter-tests on a dirty worktree.** The restore is exact for the clean worktree a card normally has. A difference after the restore is logged, not failed.

### Where the Records Are

- **In this repository:** the [v0.8.6 build log](v0.8.6-build-log.md), the [v0.8.5 bench report](bench-v0.8.5.md), the [pilot report](pilot-v0.8.6.md), the [v0.8.5 build log](v0.8.5-build-log.md), the [v0.8.1 build log](v0.8.1-build-log.md) (both rounds, with trees and token shares), the [bench report](bench-v0.8.1.md), the [README](../README.md) live and untested lists, and the claim-to-test table in [internals](openfarm-internals.md#verification-status--claim-to-test-mapping).
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
