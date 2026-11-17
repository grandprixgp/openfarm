<p align="center">
  <img src="docs/assets/openfarm-banner.svg" alt="openfarm: Frontier thinks. The farm grinds. Code decides." width="100%">
</p>

<h3 align="center">Spend frontier tokens on thinking, not on compile errors.</h3>

<p align="center">
  openfarm gives your best model the work that needs judgement: the architecture, the card design and the final review.<br>
  A cheaper model takes the grind: writing the tests, writing the code, and retrying against the compiler and the tests within fixed budgets.<br>
  The gates are run by plain code, the grind never touches your frontier context, and every card comes back as a reviewable branch or an honest "blocked".
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-0.8.1-16a34a?style=flat-square" alt="version 0.8.1">
  <img src="https://img.shields.io/badge/status-pre--release-d97706?style=flat-square" alt="status: pre-release">
  <img src="https://img.shields.io/badge/python-3.11%2B-7c3aed?style=flat-square" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/runtime%20deps-stdlib%20only-0284c7?style=flat-square" alt="runtime dependencies: standard library only">
  <img src="https://img.shields.io/badge/language%20packs-Rust%20%7C%20Python%20%7C%20TypeScript-ea580c?style=flat-square" alt="language packs: Rust, Python and TypeScript">
  <img src="https://img.shields.io/badge/UI-stdlib%20TUI%20%2B%20CLI-0ea5e9?style=flat-square" alt="UI: stdlib TUI + CLI">
</p>

<p align="center">
  <a href="#quickstart"><b>Quickstart</b></a> &nbsp;&bull;&nbsp;
  <a href="#how-it-works"><b>How it works</b></a> &nbsp;&bull;&nbsp;
  <a href="#commands"><b>Commands</b></a> &nbsp;&bull;&nbsp;
  <a href="#whats-verified"><b>What's verified</b></a> &nbsp;&bull;&nbsp;
  <a href="#roadmap"><b>Roadmap</b></a> &nbsp;&bull;&nbsp;
  <a href="docs/openfarm-internals.md"><b>Internals &amp; spec</b></a>
</p>

<p align="center">
  <img src="docs/assets/openfarm-stats.svg" alt="704 tests passing in the full offline end-to-end suite, 21 CLI subcommands, 6 adapter interfaces, 0 runtime dependencies" width="100%">
</p>

> [!IMPORTANT]
> **Pre-release (v0.8.0).** The core is covered by a 704-test deterministic suite (with v0.8 adding shared message boards, named `[tiers.*]` / `effort`, parallel non-colliding cards, worker hand-back, coordinator/supervisor seats, `StallMonitor`, humanized CLI formatting, and the split-pane stdlib TUI; when `[swarm] enabled = false` runs behave identically to v0.7). Previously, v0.6 and v0.7 were partly built by openfarm itself: 19 cards on its own repository, worked live by a cheap worker model with Claude supervising, reviewed and merged. On 17 Nov 2026, live v0.8 QA against Command Code (`deepseek/deepseek-v4.1-flash`, `x-cmd-zdr: 1`) and Claude ACP OAuth exercised `tier:dsf` all-seats runs, `reasoning_effort` comparisons (`low`/`high`/`max`), an external coordinator+supervisor run, concurrent Rust `cargo` builds, a 2-project global lesson exchange, and a 2-task x 1-repeat `openfarm bench` spot check (`farm` 1/2 vs `solo` 1/2). That validates the pipeline end to end, not statistical efficacy — the full 12-task x 3-repeat benchmark and 25-card pilot remain **untested**; see [Not yet tested](#not-yet-tested).

---

## Why openfarm

Frontier models are brilliant at architecture and review, and wasteful when you leave them grinding through compile, fix, retry. Every failed build they read is context they carry forward, and a long session full of stale attempts makes their later work worse (context rot). openfarm moves the grind somewhere cheaper and keeps the frontier conversation clean.

<table>
  <tr>
    <td width="33%" valign="top">
      <h3>Think once</h3>
      Your frontier model writes a protected invariants file, one clean golden reference slice and a queue of small, precisely specified cards. Then it steps back until there is something worth reviewing. The design target is roughly 3k to 8k frontier tokens for all of that, review included.
    </td>
    <td width="33%" valign="top">
      <h3>Grind out of sight</h3>
      A non-frontier model works each card, and the farm is built to run unattended, overnight if you like. Compiler loops, retries, revert checks and counter-tests all happen inside openfarm, against fixed budgets, and none of that noise ever reaches your frontier context.
    </td>
    <td width="33%" valign="top">
      <h3>Trust code, not claims</h3>
      No model reports its own result. The orchestrator runs every gate itself, models get no shell and no tools, and edits only land inside the files a card allows. You review a compact dossier and a diff, not a transcript.
    </td>
  </tr>
</table>

The bar openfarm sets itself is economic, not just "cheap tokens": a merged card should cost less in total, **including the frontier's time to specify and review it**, than having a frontier subagent write it. That is the hypothesis the [planned pilot](#roadmap) will test.

## Quickstart

openfarm is installed and driven through your frontier coding agent (Claude Code, Antigravity, or any agent that reads `.agents/skills`). Two sentences take you from an empty folder to a checked queue of cards.

<p align="center">
  <img src="docs/assets/openfarm-quickstart.svg" alt="Quickstart flow: New or existing repo (Rust, Python, or TypeScript via init or adopt) -> 1. Install (Read and install this skill) -> 2. Initialize (/openfarm-initialize or openfarm adopt) -> Checked queue (invariants, golden slice and baseline-checked cards) -> Swarm + TUI (parallel gated branches, inspect in openfarm tui or drive, merge locally)." width="100%">
</p>

### 1. Install

Ask your agent:

```text
Read and install this skill https://github.com/grandprixgp/openfarm
```

The agent reads the root [`SKILL.md`](SKILL.md) and installs openfarm with a package installer (`uv tool install`, `pipx install` or `pip install --user`), or straight from a clone with no `pip` at all:

```bash
git clone https://github.com/grandprixgp/openfarm
cd openfarm
python3 -m openfarm_core.cli install-skill --target all --install-bin ~/.local/bin --json
```

This deploys `/openfarm-grill-me`, `/openfarm-initialize` and `openfarm` across Claude Code (`~/.claude`), Antigravity (`~/.gemini/config`), Codex (`~/.codex/skills` + `~/.codex/openfarm-AGENTS.md`), and `.agents` / workspace directories (`--target codex|claude|antigravity|agents|workspace|all`), plus an optional `openfarm` launcher on your `PATH`.

Check Frontier OAuth readiness for Claude, Antigravity (`agy`), or Codex (`codex`) — or pick your ACP provider on `openfarm init --acp-provider claude|antigravity|codex`, or run `openfarm drive` in headless external Driver mode (passing `--profile solo` disables ACP supervision):

```bash
openfarm acp status --provider all --json
openfarm acp login --provider claude      # or --provider antigravity | codex
```

### 2. Grill (Optional) & Initialize

In an empty folder, ask your agent:

```text
/openfarm-initialize calorie-log An app to track my calories
```

By default `/openfarm-initialize` starts with `/openfarm-grill-me` (one frontier question at a time, with a recommended answer) to lock down invariants, golden slice boundaries, and Frontier OAuth + Worker API setup before touching code (pass `--no-grill` or run `/openfarm-grill-me` separately beforehand).

| Stage | Lane | What happens |
| --- | --- | --- |
| **Grill** | You + Frontier | One-question-at-a-time decision tree settles invariants, public crate surface, dependencies, and exact `Done` boundaries into `.openfarm/GRILL_DECISIONS.md` |
| **Scaffold** | Code | `openfarm init` writes a validated `.openfarm/config.toml` (Frontier Claude ACP OAuth default + Workers ZDR API key), protected `src/invariants.rs`, starter Rust files, `Cargo.lock` and git, putting state outside the repo with `0700` permissions |
| **Architect** | Frontier | Expands the invariants, exports clean `pub` types and builds one complete, green golden slice that passes `cargo test`, `clippy -D warnings` and `rustfmt --check` |
| **Decompose** | Frontier | Writes dependency-ordered Wave 1 to 3 cards under `cards/`, commits them on `main`, and checks every one with `openfarm check` and `openfarm run --dry-run` |
| **Run** | Farm | `openfarm supervise --prepare-queue --apply` then `openfarm run --cards N` work the queue out of frontier context |
| **Review** | You | Read each dossier and `git diff main..<branch>`, record `openfarm verdict`, and merge locally with `git merge --no-ff`. openfarm never merges or pushes |

The full walkthrough of this example is in [`docs/FROM-IDEA-TO-SHIPPED-APP.md`](docs/FROM-IDEA-TO-SHIPPED-APP.md).

> [!NOTE]
> **Requirements:** Python 3.11+ and git. The built-in Rust pack also needs `cargo`, `clippy` and `rustfmt`, and mutation testing needs `cargo-mutants`. Frontier supervision plugs in by default via **native Claude ACP over OAuth** (`openfarm acp status` / `openfarm acp login`, supporting `claude` CLI directly or external `claude-agent-acp`), while **coding Workers still authenticate via API key** against a zero-retention OpenAI-compatible endpoint enforced by `allowed_endpoints`.

## Existing projects and Python

For a repository that already exists, adopt it first:

```bash
openfarm adopt --dir .
```

Adopt writes `.openfarm/config.toml`, `.openfarm/PROJECT_SPEC.md` and `cards/.gitkeep` and commits nothing. Commit them, and your cards, before `openfarm check` or `openfarm run`: openfarm reads cards from the committed base branch.

Adopt also runs your suite once (`--no-suite` skips it). A suite that does not build is refused before anything is written. Tests that fail are listed in a comment above `[policy] known_failing`; openfarm refuses a red base, so copy them in only if you want to work with them failing.

Then batch-grill the project:

```text
/openfarm-grill-me --batch
```

Check the first card:

```bash
openfarm check CARD-001
```

Python repositories use `unittest` unless they configure pytest, and formatting is checked only when `ruff` is installed.

## Global learning

- Every project learns lessons from its own cards, as before. Each lesson is marked `project` or `general`. Claude claims one, and plain code keeps it `project` if it names one of the project's files, functions, cards or its name.
- A general lesson that has helped on 3 or more cards, with fewer hurts than helps, goes to a global library at `~/.openfarm/global/harness`, unless the project sets `share = false`. `[harness] global_dir` moves the library. Notes about openfarm's own bookkeeping, fixed-rule hints, long lessons and lessons with code never go.
- Other projects import a global lesson as a new lesson of their own and try it like one. A project that finds it hurts switches it off for itself. Two such projects, or a negative combined score, retire it everywhere.
- `[harness] learn_from = "both" | "project" | "global"` picks which lessons apply. The default is `both`, and `openfarm run --learn-from ...` overrides it for one run.
- `openfarm harness status --global` lists the global lessons and their source projects. `openfarm harness demote <id> --global` retires one, and `openfarm harness promote <id> --global` brings it back.
- Bench runs use a throwaway global library of their own, so they never read or change yours.
- Sharing lessons across a live two-project exchange ran on 17 Nov 2026 (`proj-alpha` promoted `g-52723ab8fa` via `harness promote --global`, and `proj-beta` imported and applied it in a live `tier:dsf` run to pass hidden tests); multi-card organic accumulation across two long-lived external repositories remains **untested**.

## Efficacy bench

`openfarm bench` measures whether the farm pays off, with your own models. It ships 12 tasks in `bench/` (6 Rust, 6 Python, across small features, pinned bug fixes, patterns and refactors), each with a starting project, a card, and hidden tests that the models never see.

```bash
openfarm bench list
openfarm bench run --dry-run                      # scripted answers, costs nothing
openfarm bench run --arm farm=farm.toml --arm solo=solo.toml
```

Each run starts from a fresh copy of the task's project and runs its card. Then it copies the hidden tests onto the result and runs the whole suite. There are two arms:

- **farm**: your worker model, with Claude supervising if you like.
- **solo**: Claude doing every worker role itself, through the same briefs and gates.

An arm file sets only the model and supervisor tables; copy one from [`bench/arms/`](bench/arms/). Each task runs 3 times per arm by default. The report (`bench-report.md` and `.json`) gives pass rate, tokens, tokens per pass, the worker's share of those tokens and per-category pass rates for each arm, and stays on your machine. The workers and Claude still see each card and starting project, as in any run.

## Swarm, board, parallel cards, and TUI (v0.8)

<p align="center">
  <img src="docs/assets/openfarm-swarm.svg" alt="v0.8 swarm architecture: Coordinator seat (code, tier:<name>, external), Supervisor seat (config, tier:<name>, external, off), and code StallMonitor connected to an append-only file-locked board.jsonl while non-colliding cards run in parallel worktrees bounded by globs_overlap and BuildLimiter and inspected live via the 7-view stdlib TUI and humanized CLI." width="100%">
</p>

Enable `[swarm]` (`enabled = true` in `.openfarm/config.toml`; off by default so v0.7 queues behave identically) to run cards through a shared message board:

- **Tiers & effort (`[tiers.<name>]`, `RoleModel.effort`)**: Define model tiers and effort ladders (`off`..`max`) injected cleanly via `effort_param` (`reasoning_effort` by default) on OpenAI-compatible payloads, while keeping `[model]` endpoint allowlists and `headers` (e.g. `x-cmd-zdr: 1`) invariant across every call.
- **Shared board & checked actions (`openfarm board`, `openfarm swarm`)**: Every claim, brief, worker result, gate check, hand-back (`<<<HANDBACK ... >>>` when `replan_if` fires), and `answer` (validated against structured `expects`) lands on `<state_dir>/runs/<run_id>/board/`. Non-code members can never post `done`/`gate` or bypass deterministic gates.
- **Coordinators & seats**: Plug in `"code"` (default deterministic escalation), `"tier:<name>"` (model coordinator with call/token caps and 2-bad-answer fallback), or `"external"` (`openfarm swarm join|start|set|hint|block|remove`, timed by `external_wait` -> `fallback` or `seat-silent`) for `[swarm] coordinator` and `supervisor` (`config`, `external`, `off`, or `tier:<name>`).
- **Parallel non-colliding cards & `StallMonitor`**: Exact glob overlap (`globs_overlap`) + dependency/ID checks schedule independent cards concurrently (`concurrent_cards`, bounded by `[budget] concurrent_builds`), while `StallMonitor` (`role = "monitor"`, `stall_after`) posts a single `stall` alert when all active members pause on an unanswered question.
- **Pure-stdlib TUI (`openfarm tui`) & human CLI**: Inspect 7 views (`overview`, `cards`, `dossier`, `board`, `swarm`, `harness`, `ops`) in a responsive split-pane `curses` UI (`NO_COLOR` fallback, `/` live search, centered modals, real-PTY tested) or one-shot `--once` / `--json` snapshots, view evidence safely confined inside `state_dir`, and run CLI commands with aligned TTY/`NO_COLOR` tables, token bars, forgiving bare `board`/`swarm`/`bench`, and contextual hints (`--json` parity preserved). Live smoke instructions and 17 Nov 2026 results are in [`docs/v0.8-smoke.md`](docs/v0.8-smoke.md).

## How it works

<p align="center">
  <img src="docs/assets/openfarm-lanes.svg" alt="Three lanes: Frontier (grill and architect pass, decompose or adopt, inspect via openfarm tui and external swarm/board commands, review and merge locally), Orchestrator (parallel non-colliding cards, append-only board, every gate, BuildLimiter, StallMonitor, configurable seats), and Farm (stateless planner, test_writer, implementer and checker roles per tier and effort receiving fenced briefs plus cues and returning edit blocks or <<<HANDBACK)." width="100%">
</p>

| Lane | Who | Does | Never does |
| --- | --- | --- | --- |
| **Frontier** | Your frontier agent, or you | Architecture, seam contract, golden slice, card decomposition, review, verdict, merge | The compile and retry grind |
| **Orchestrator** | Plain Python | Claims cards, builds every brief, applies edits, runs every gate, enforces budgets, writes dossiers and telemetry | Merge, rebase or push (the core has no code path that runs `git push`) |
| **Farm** | Non-frontier models in four stateless roles | Splits a card into steps, writes the named tests, writes the code, tries to break it with counter-tests | Run commands, touch files outside the card, see never-touch files, report a gate result |

The **micromanager** is the built-in Frontier supervisor (defaulting in `openfarm init` to native Claude ACP via OAuth with `binary = "auto"`, `mode = "active"`; opt out with `--profile solo` or `mode = "off"`). It can shape cards, scaffold contracts, coach a stalled card, adjudicate counter-tests and pre-review under its own separate budget, gets no write or terminal capability, and never certifies a gate. Coding Workers remain configured separately via `[model] key_file`.

### Every card, every time

<p align="center">
  <img src="docs/assets/openfarm-pipeline.svg" alt="Eight-step per-card pipeline posted to board.jsonl: 1 claim and eligibility, 2 isolate (parallel when touches are disjoint), 3 baseline green, 4 shape and plan, 5 phase A tests, 6 phase B code (escalates effort or hands back early), 7 end gates, 8 catch-up and project plus global harness capture. Ends as a branch with a reviewer dossier, or a handed-back / clean block." width="100%">
</p>

<details>
<summary><b>The eight steps in detail</b></summary>

1. **Claim and eligibility:** the card is hashed and checked against every eligibility rule before any model call. An ineligible card is released, not blocked.
2. **Isolate:** one branch, one worktree and one build target per card.
3. **Baseline green:** the suite runs twice on the base; a red test or a disagreement stops the card.
4. **Shape and plan:** an optional planner or micromanager splits the card into checkpointed steps.
5. **Phase A, tests:** the test writer writes the named tests. They must compile and *fail* on the base, then they are frozen by hash.
6. **Phase B, code:** the implementer edits only the files the card lists. Every attempt runs the diff, banned-pattern, frozen-test, suite and lint gates; on failure it gets only the diff and the exact failure output.
7. **End gates:** pass-after twice, full suite, format and workspace lint, no test loss, revert check, mutation (report-only by default), two checker passes, optional pre-review.
8. **Catch-up and capture:** a throwaway trial merge with the current base, then lessons from the outcome are recorded for the next card.

</details>

### What makes a good card

Cards declare a category, and each repository enables the ones it trusts:

`test-backfill` &nbsp; `pattern` &nbsp; `pinned-bugfix` &nbsp; `mechanical` &nbsp; `small-feature` &nbsp; `feature` &nbsp; `refactor` &nbsp; `integration` &nbsp; `port`

A card must name every test and what it asserts with concrete inputs and outputs, stay within one unit and a handful of files, and leave no design decisions. Words like "handle", "properly" or "robust" without named cases are rejected. Concurrency primitives, `unsafe`, cross-crate public APIs and anything on the `never_touch` list are never eligible.

### A farm that learns, safely

openfarm keeps a small, bounded memory of what helped weaker models recover from failures, inspired by Harness² ([Xu et al., 2026, arXiv:2610.14224](https://arxiv.org/abs/2610.14224)). After each judged card it extracts short cues, such as a recovery pattern for a borrow-checker error, and feeds them into later briefs in the same run and in later runs.

> [!TIP]
> New cues go through a **propose, probe, confirm** loop: they are tried over a window of cards and rolled back automatically if pass rate or efficiency regresses. Cues can only change five guidance surfaces. Every gate, budget and policy list is immutable, and any cue that tries to bypass a gate, leak test values or escape a path is rejected. Outages and operator stops are kept out of the metrics. Manage it all with `openfarm harness`.

## Architecture

The core reads one config file per repository and talks to the outside world only through six adapter interfaces. Each has built-in implementations, and any of them can be swapped for an external command that speaks a small JSON protocol (`{"openfarm": 1, ...}`) over stdin and stdout.

<p align="center">
  <img src="docs/assets/openfarm-adapters.svg" alt="openfarm core (Python 3.11+ stdlib, one config file enforcing gates, budgets, swarm board and TUI) connected to six adapter interfaces: Card source (files: folder on openfarm-cards), Runner (direct cargo, python, node, BuildLimiter), Language pack (rust, python, typescript), Model backend (openai-compatible, claude|antigravity|codex-acp, scripted), Telemetry (jsonl plus run board and dossiers), and Micromanager &amp; seats (claude|antigravity|codex-acp, openfarm drive, tier:<name>, external, off). Every interface also accepts cmd:<argv> over stdio JSON." width="100%">
</p>

Every adapter also accepts `cmd:<argv>`. Card state lives on its own `openfarm-cards` branch so parallel cards never conflict. TypeScript projects run via native Node (`node --test --test-reporter=tap`) and optional `npm` / `tsc` / `prettier`. Git is the one hard dependency, and nothing in the core names a particular project.

### Safety boundaries

| Boundary | How it holds |
| --- | --- |
| **Never-touch paths** | File globs, with symlinks resolved, checked on the card at eligibility and on the diff at every attempt. Never loaded into any brief, not even as signatures |
| **No push** | The core has no code path that runs `git push`, and its git wrapper blocks dangerous global flags and config overrides |
| **Untrusted text is fenced** | Card text, diffs, test output and repository files are wrapped as data in every brief, with terminal control sequences stripped |
| **Endpoint allowlist** | The model backend refuses any endpoint or redirect not on `allowed_endpoints`, and reads its key from a file outside the repo that is never logged |
| **State stays out of the repo** | Prompts contain repository text, so the state folder lives outside the repo and should stay out of unencrypted synced folders |
| **Config is farm-proof** | `.openfarm/**` and `.claude/**` are always forbidden to the farm, and unknown config tables or keys are rejected so a typo can never silently disable a gate |

## Commands

One `openfarm` command with 22 subcommands (`6` pre-config helpers + `16` repository commands), shared exit codes (`0` done, `1` refused or ineligible, `2` usage or config error, `3` trouble or infra) and `--json` output.

| Command | Purpose |
| --- | --- |
| `openfarm init NAME` | Scaffold a new repo with validated config (`--acp-provider claude\|antigravity\|codex` OAuth default + Worker API key), protected invariants, Rust starter files, `Cargo.lock` and git |
| `openfarm adopt` | Adopt an existing Rust, Python, or TypeScript repository with baseline verification (`--lang rust\|python\|typescript`) |
| `openfarm bench` | List or run the 12 efficacy bench tasks (`list`, `run --dry-run` or live arms across [`bench/arms/`](bench/arms/)) |
| `openfarm install-skill` | Deploy the bundled skills (`/openfarm-grill-me`, `/openfarm-initialize`, `openfarm`), slash commands, Antigravity plugin, Codex skills/AGENTS, and optional launcher (`--target claude\|antigravity\|codex\|agents\|workspace\|all`) |
| `openfarm acp [ACTION]` | Check Frontier OAuth & ACP readiness across `--provider claude\|antigravity\|codex\|all` (`status`), log in (`login`), update `.openfarm/config.toml` (`configure`), or run a zero-token ACP handshake (`probe`) |
| `openfarm tui` | Launch the 7-view split-pane stdlib TUI (`--run`, `--refresh`, `--once`, `--view`, `--json`; `NO_COLOR`, `/` search, works pre-config too) |
| `openfarm run` | Work Ready cards, bounded by `--cards`, `--spend`, `--for` or `--until`; `--dry-run` checks without model calls |
| `openfarm status` | Show farm, card and harness-memory status |
| `openfarm pause` / `resume` / `stop` | Operator controls, including clearing the circuit breaker or kill switch |
| `openfarm release CARD` / `done CARD` | Release a card back to the queue, or mark a locally merged card done |
| `openfarm dossier CARD` | Print a card's reviewer dossier as Markdown or JSON |
| `openfarm check CARD` | Check a card's eligibility, and optionally its baseline |
| `openfarm verdict CARD` | Record the review outcome (`clean`, `minor-fix`, `bounced`, `scrapped`) and its cost |
| `openfarm escape CARD` | Record a defect that got past review |
| `openfarm supervise` | Run bounded micromanager triage of blocked cards or queue preparation |
| `openfarm harness` | Inspect or manage harness memory (`status`, `index`, `show`, `promote`, `demote`, `reset --yes`, `--global`) |
| `openfarm board` | Inspect or interact with a live run's board (`read`, `post`, `answer`, `wait`) |
| `openfarm swarm` | Inspect tiers/roster or act as external coordinator (`status`, `tiers`, `join`, `start`, `set`, `hint`, `block`, `remove`) |
| `openfarm drive` | Drive external `coordinator`/`supervisor` seats headlessly via read-only `claude`, `agy`, or `codex` CLIs (`--agent`, `--seat`, `--wave`, `--record-verdict`) |

## Configuration

Each repository carries `.openfarm/config.toml` on its base branch, and `openfarm init` / `openfarm adopt` writes a validated one for you.

<details>
<summary><b>The 15 config tables, budgets and hooks</b></summary>

<br>

**Tables:** `[project]`, `[adapters]`, `[model]`, `[policy]`, `[eligibility]`, `[brief]`, `[runner]`, `[budget]`, `[gates]`, `[cards]`, `[micromanager]`, `[state]`, `[harness]`, `[tiers.*]` and `[swarm]`.

**Budgets** are absolute and enforced in code: implementer and test-writer attempts, model-driven builds, mutation builds, tokens and wall clock per card, spend per night, per-run coordinator call/token caps, concurrent builds/cards limits, plus a kill switch and a circuit breaker. The micromanager has its own separate budget, so supervision spend stays visible in the payback numbers.

**Three hooks** keep a repository's quirks out of the core: `[[gates.extra]]` for named commands that must pass, `[runner.env]` for per-unit environment variables, and `[brief] test_conventions` for a test-writing guide.

Every key, gate, stop code (`19` per-card stop codes including `handed-back` and `seat-silent`) and record format is documented in [`docs/openfarm-internals.md`](docs/openfarm-internals.md) (and the owner's local spec `openfarm spec v0.8.md` when present).

</details>

## What's verified

All checks below are deterministic and offline (see [`tests/test_v05_skill_bootstrap.py`](tests/test_v05_skill_bootstrap.py), [`tests/test_v051_acp_oauth_grill.py`](tests/test_v051_acp_oauth_grill.py) and the rest of [`tests/`](tests/)).

| Suite | Result |
| --- | --- |
| Fast suite | **643 tests ran** (704 discovered, 61 opt-in end-to-end tests skipped), **0 failures** |
| Full suite (`OPENFARM_E2E=1`, real `git` and `cargo`) | **704 tests passed**, 0 skipped, **0 failures** |

The full suite includes:

- an offline wheel build, with all 12 bundled skill asset files present and all 8 `skills/` files byte-identical;
- `install-skill` (deploying `openfarm-grill-me`, `openfarm-initialize`, `openfarm`, slash commands and plugin) and its launcher shim executing;
- `openfarm init` defaults (`profile = active`, `micromanager = claude-acp`, `auth = oauth`, `binary = auto`), `Cargo.lock`, containment and permissions;
- native `claude -p` ACP bridge (`claude_native_acp.py`) over stdio pipes with a mock `claude` CLI, OAuth env/credential isolation & refresh sync-back, `[micromanager] auth` validation, and `openfarm acp status|login|configure|probe`;
- the scaffolded project passing `cargo test --locked`, `cargo clippy --locked --all-targets -- -D warnings` and `rustfmt --edition 2021 --check`;
- a card that touches a never-touch path being rejected, and a scripted card running all the way to `in-review`;
- `openfarm adopt` on Python and Rust repositories, including a red suite proposed in a comment and a crate that does not build refused before anything is written;
- every one of the 12 bench tasks: its scripted answer passes its hidden tests through a real `openfarm run`, and its untouched starting project fails them;
- parallel non-colliding Rust `cargo` card execution (`ParallelRustCargoE2ETests`: serial `1/1` vs parallel `3/2` identical `in-review` outcomes under real `cargo test`/`clippy`/`rustfmt`), prettified PTY-tested `curses` TUI (`xterm-256color` & `NO_COLOR=1`), and human CLI formatting + `--json` parity (`tests/test_cli_human.py`).

Model calls in these offline tests use a scripted backend and local HTTP test servers, and the micromanager tests use deterministic stand-ins/mocks over real stdio pipes (alongside a local zero-token `openfarm acp status` and `openfarm acp probe` check against the installed `claude` CLI). Live 17 Nov 2026 QA runs against Command Code (`deepseek/deepseek-v4.1-flash`, `x-cmd-zdr: 1`) and Claude ACP OAuth are recorded in [`docs/v0.8-build-log.md`](docs/v0.8-build-log.md).

## Not yet tested

> [!WARNING]
> **Verified live in v0.8.1 (17 Nov 2026; exact numbers in [`docs/v0.8.1-build-log.md`](docs/v0.8.1-build-log.md) and [`docs/bench-v0.8.1.md`](docs/bench-v0.8.1.md), building on the v0.8 spot checks in [`docs/v0.8-build-log.md`](docs/v0.8-build-log.md)):**
>
> - **Multi-provider ACP & Driver mode (`claude`, `antigravity`, `codex`)**: Zero-token `openfarm acp status|probe --provider all` passed across all 3 providers; live `rs-percent-zero` passed hidden tests under `farm-codex` (`codex-acp` + DeepSeek), `solo-codex`, and `farm-agy` (`antigravity-acp`); headless `openfarm drive` passed hidden tests on `py-inventory-remove` via both `--agent codex` (`--wave --record-verdict` `clean`) and `--agent antigravity --effort low` (while `claude` properly surfaced an account quota message).
> - **TypeScript pack & external `cmd:` adapters**: `openfarm adopt` detected `typescript` and `openfarm run --card TS-1` reached `in-review` passing hidden `node --test --test-reporter=tap` assertions; executable protocol v1 `cmd_runner.py` + `cmd_telemetry.py` ran `py-validate-port` to `in-review` passing hidden `unittest` tests.
> - **Second independent Rust repository & multi-wave greenfield app**: Copied `anyhow` (`MECH-1`) reached `in-review` passing `cargo clippy` and `test_context`; initialized `calorie-log` (`openfarm init --acp-provider codex`) merged Wave 1 (`CARD-001`) -> `done` -> unlocked Wave 2 (`CARD-002`) -> `done`, leaving `main` green on `cargo test --locked` and `cargo clippy`.
> - **12-task `openfarm bench` (`repeats = 1`, `24` runs) & counter-test calibration**: `farm` (`deepseek/deepseek-v4.1-flash` `x-cmd-zdr: 1` + `codex-acp` sup) scored `9/12` (`75.0%`: `6/6` Python, `3/6` Rust; `968,891` tok, `28.6%` worker share) vs `solo` (`codex-acp` all roles) `9/12` (`75.0%`: `5/6` Python, `4/6` Rust; `840,089` tok). Two live `counter_tests = "reject"` calibration controls passed hidden tests with `0` false rejects across `36` inspected live card records.
>
> **Still untested:**
>
> - Live remote GitHub URL installs via `uv`, `pipx` or `pip` (local clone / wheel `install-skill --target all` and `codex` are tested).
> - Live multi-turn Claude (`claude`) driver/bench execution when Anthropic weekly quota resets, and standalone `claude-agent-acp` / `codex-acp` npm binaries (`claude_native_acp.py`, `codex_native_acp.py`, and native `agy acp` ran live).
> - Hosted ZDR OpenAI-compatible worker endpoints other than Command Code (`x-cmd-zdr: 1` with DeepSeek V4.1 Flash).
> - Repeats 2 and 3 of the 12-task live benchmark (`12 tasks x 3 repeats`), the 25-card Rust efficacy pilot, and counter-test true-bug catch rate against intentionally buggy/borderline implementations.
> - Live `cargo-mutants`, `pytest`, `ruff`, `tsc`, and `prettier` binaries (`pytest`, `ruff`, `cargo-mutants`, `tsc`, and `prettier` are not installed on this test host, so syntax/node/unittest fallback paths ran live).

## Roadmap

- [x] **Multi-provider ACP & Driver mode:** `claude`, `antigravity` (`agy`), and `codex` ACP adapters (`claude`/`agy`/`codex-acp`), headless `openfarm drive`, and Codex skill installation (`install-skill --target codex`).
- [x] **Second repository & live multi-wave greenfield smoke:** `anyhow` copy (`MECH-1`), two-wave `calorie-log` (`CARD-001` -> `CARD-002`), live TypeScript `node --test`, and live `cmd:` runner/telemetry adapters.
- [x] **TypeScript pack and Node runner:** `openfarm adopt --lang typescript`, `node --test --test-reporter=tap` / `npm`, optional `tsc` / `prettier` integration, and live card pass.
- [ ] **Run the full 3-repeat bench live:** extend v0.8.1's 12-task `N = 1` bench ([`docs/bench-v0.8.1.md`](docs/bench-v0.8.1.md)) to `3` repeats across arms including Claude once quota resets.
- [ ] **Efficacy pilot & adversarial gate calibration:** complete the 25-card all-Rust pilot with frontier baseline cards and measure counter-test catch rates plus `cargo-mutants` payback before promoting `reject` / `required` gates.

## Contributing

openfarm is pre-release and its design is still being validated, so issues are especially welcome for:

- results from running it against real endpoints or repositories, failures included;
- adapters for other card sources, runners, model backends or languages;
- gaps between this README, the spec and the code.

When contributing code, keep the core to the Python standard library (3.11+), keep project-specific behaviour in config and adapters rather than the core, and add tests that run offline. The opt-in end-to-end tests run with `OPENFARM_E2E=1` and need `git` and a Rust toolchain.

<!-- TODO(maintainer): add the exact command to run the test suite. -->

## Licence

<!-- TODO(maintainer): choose a licence, add a LICENSE file, and replace this section. -->

Licence to be decided. Until a licence is added, no rights to use, copy or modify this code are granted.

---

<p align="center">
  <sub>Built by <a href="https://github.com/grandprixgp">@grandprixgp</a>. Frontier thinks. The farm grinds. Code decides.</sub>
</p>
