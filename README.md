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
  <img src="https://img.shields.io/badge/version-0.5.1-16a34a?style=flat-square" alt="version 0.5.1">
  <img src="https://img.shields.io/badge/status-pre--release-d97706?style=flat-square" alt="status: pre-release">
  <img src="https://img.shields.io/badge/python-3.11%2B-7c3aed?style=flat-square" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/runtime%20deps-stdlib%20only-0284c7?style=flat-square" alt="runtime dependencies: standard library only">
  <img src="https://img.shields.io/badge/language%20pack-Rust-ea580c?style=flat-square" alt="language pack: Rust">
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
  <img src="docs/assets/openfarm-stats.svg" alt="400 tests passing in the full offline end-to-end suite, 15 CLI subcommands, 6 adapter interfaces, 0 runtime dependencies" width="100%">
</p>

> [!IMPORTANT]
> **Pre-release (v0.5.1).** The core is built and covered by a deterministic, offline test suite, but it has not yet been run end to end against live model endpoints. **Validation and efficacy results are still to come.** Nothing here is a benchmark or a measured cost saving; see [Not yet tested](#not-yet-tested) for exactly what is still open.

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
  <img src="docs/assets/openfarm-quickstart.svg" alt="Quickstart flow: Empty folder and a plain-English idea -> 1. Install (Read and install this skill) -> 2. Initialize (/openfarm-initialize name idea) -> Validated repo, golden slice and a checked card queue -> Farm works the cards, you review and merge." width="100%">
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

This deploys the `/openfarm-grill-me`, `/openfarm-initialize` and `openfarm` skills, slash commands and the Antigravity plugin, plus an optional `openfarm` launcher on your `PATH`.

Sign in once with your Claude OAuth subscription for the Frontier supervisor (optional if you already run Claude Code logged in, or pass `--profile solo` to disable ACP supervision):

```bash
openfarm acp status --json
openfarm acp login          # native Claude OAuth login (--claudeai default)
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

Then batch-grill the project:

```text
/openfarm-grill-me --batch
```

Check the first card:

```bash
openfarm check CARD-001
```

Python repositories use `unittest` unless they configure pytest, and formatting is checked only when `ruff` is installed.

## How it works

<p align="center">
  <img src="docs/assets/openfarm-lanes.svg" alt="Three lanes. Frontier: architect pass, decompose, review, merge locally. Orchestrator: claim and eligibility, run every gate, branch and dossier, with an optional micromanager. Farm: planner, test_writer, implementer and checker, which receive fenced briefs and return edit blocks only." width="100%">
</p>

| Lane | Who | Does | Never does |
| --- | --- | --- | --- |
| **Frontier** | Your frontier agent, or you | Architecture, seam contract, golden slice, card decomposition, review, verdict, merge | The compile and retry grind |
| **Orchestrator** | Plain Python | Claims cards, builds every brief, applies edits, runs every gate, enforces budgets, writes dossiers and telemetry | Merge, rebase or push (the core has no code path that runs `git push`) |
| **Farm** | Non-frontier models in four stateless roles | Splits a card into steps, writes the named tests, writes the code, tries to break it with counter-tests | Run commands, touch files outside the card, see never-touch files, report a gate result |

The **micromanager** is the built-in Frontier supervisor (defaulting in `openfarm init` to native Claude ACP via OAuth with `binary = "auto"`, `mode = "active"`; opt out with `--profile solo` or `mode = "off"`). It can shape cards, scaffold contracts, coach a stalled card, adjudicate counter-tests and pre-review under its own separate budget, gets no write or terminal capability, and never certifies a gate. Coding Workers remain configured separately via `[model] key_file`.

### Every card, every time

<p align="center">
  <img src="docs/assets/openfarm-pipeline.svg" alt="Eight-step per-card pipeline: 1 claim and eligibility, 2 isolate, 3 baseline green, 4 shape and plan, 5 phase A tests, 6 phase B code, 7 end gates, 8 catch-up and capture. Ends as a branch with a reviewer dossier, or a clean block with a note." width="100%">
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
  <img src="docs/assets/openfarm-adapters.svg" alt="openfarm core (Python 3.11+ stdlib, one config file) connected to six adapter interfaces: Card source (files: folder), Runner (direct cargo), Language pack (rust), Model backend (openai-compatible, scripted), Telemetry (jsonl), and Micromanager (claude-acp, off, scripted). Every interface also accepts cmd:<argv> over stdio JSON." width="100%">
</p>

Every adapter also accepts `cmd:<argv>`. Card state lives on its own `openfarm-cards` branch so parallel cards never conflict. TypeScript is not implemented yet. Git is the one hard dependency, and nothing in the core names a particular project.

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

One `openfarm` command with 15 subcommands, shared exit codes (`0` done, `1` refused or ineligible, `2` usage or config error, `3` trouble or infra) and `--json` output.

| Command | Purpose |
| --- | --- |
| `openfarm init NAME` | Scaffold a new repo with validated config (Frontier Claude ACP OAuth default + Worker API key), protected invariants, Rust starter files, `Cargo.lock` and git |
| `openfarm install-skill` | Deploy the bundled skills (`/openfarm-grill-me`, `/openfarm-initialize`, `openfarm`), slash commands, Antigravity plugin and optional launcher |
| `openfarm acp [ACTION]` | Check Frontier OAuth & ACP transport readiness (`status`), run interactive `claude auth login` (`login`), update `.openfarm/config.toml` (`configure`), or run a zero-token ACP handshake (`probe`) |
| `openfarm run` | Work Ready cards, bounded by `--cards`, `--spend`, `--for` or `--until`; `--dry-run` checks without model calls |
| `openfarm status` | Show farm, card and harness-memory status |
| `openfarm pause` / `resume` / `stop` | Operator controls, including clearing the circuit breaker or kill switch |
| `openfarm release CARD` | Release a card back to the queue |
| `openfarm dossier CARD` | Print a card's reviewer dossier as Markdown or JSON |
| `openfarm check CARD` | Check a card's eligibility, and optionally its baseline |
| `openfarm verdict CARD` | Record the review outcome (`clean`, `minor-fix`, `bounced`, `scrapped`) and its cost |
| `openfarm escape CARD` | Record a defect that got past review |
| `openfarm supervise` | Run bounded micromanager triage of blocked cards or queue preparation |
| `openfarm harness` | Inspect or manage harness memory (`status`, `index`, `show`, `promote`, `demote`, `reset --yes`) |

## Configuration

Each repository carries `.openfarm/config.toml` on its base branch, and `openfarm init` writes a validated one for you.

<details>
<summary><b>The 13 config tables, budgets and hooks</b></summary>

<br>

**Tables:** `[project]`, `[adapters]`, `[model]`, `[policy]`, `[eligibility]`, `[brief]`, `[runner]`, `[budget]`, `[gates]`, `[cards]`, `[micromanager]`, `[state]` and `[harness]`.

**Budgets** are absolute and enforced in code: implementer and test-writer attempts, model-driven builds, mutation builds, tokens and wall clock per card, spend per night, plus a kill switch and a circuit breaker. The micromanager has its own separate budget, so supervision spend stays visible in the payback numbers.

**Three hooks** keep a repository's quirks out of the core: `[[gates.extra]]` for named commands that must pass, `[runner.env]` for per-unit environment variables, and `[brief] test_conventions` for a test-writing guide.

Every key, gate, stop code and record format is documented in [`docs/openfarm-internals.md`](docs/openfarm-internals.md) (and the owner's local spec `openfarm spec v0.5.1.md` when present).

</details>

## What's verified

All checks below are deterministic and offline (see [`tests/test_v05_skill_bootstrap.py`](tests/test_v05_skill_bootstrap.py), [`tests/test_v051_acp_oauth_grill.py`](tests/test_v051_acp_oauth_grill.py) and the rest of [`tests/`](tests/)).

| Suite | Result |
| --- | --- |
| Fast suite | **400 tests ran**, 57 opt-in end-to-end tests skipped, **0 failures** |
| Full suite (`OPENFARM_E2E=1`, real `git` and `cargo`) | **400 tests passed**, 0 skipped, **0 failures** |

The full suite includes:

- an offline wheel build, with all 12 bundled skill asset files present and all 8 `skills/` files byte-identical;
- `install-skill` (deploying `openfarm-grill-me`, `openfarm-initialize`, `openfarm`, slash commands and plugin) and its launcher shim executing;
- `openfarm init` defaults (`profile = active`, `micromanager = claude-acp`, `auth = oauth`, `binary = auto`), `Cargo.lock`, containment and permissions;
- native `claude -p` ACP bridge (`claude_native_acp.py`) over stdio pipes with a mock `claude` CLI, OAuth env/credential isolation & refresh sync-back, `[micromanager] auth` validation, and `openfarm acp status|login|configure|probe`;
- the scaffolded project passing `cargo test --locked`, `cargo clippy --locked --all-targets -- -D warnings` and `rustfmt --edition 2021 --check`;
- a card that touches a never-touch path being rejected, and a scripted card running all the way to `in-review`.

Model calls in these tests use a scripted backend and local HTTP test servers, and the micromanager tests use deterministic stand-ins/mocks over real stdio pipes (alongside a local zero-token `openfarm acp status` and `openfarm acp probe` check against the installed `claude` CLI).

## Not yet tested

> [!WARNING]
> These have **not** been run yet:
>
> - Live remote GitHub URL installs via `uv`, `pipx` or `pip`.
> - Live paid Anthropic prompt turns through `claude-agent-acp` or `claude_native_acp.py` (only zero-token `openfarm acp status` + `initialize` handshake `probe` were run against the local `claude` binary).
> - Live hosted zero-retention OpenAI-compatible model endpoints doing card work. (v0.6 branch: two small cards on openfarm itself reached review and were merged with DeepSeek V4.1 Flash on Command Code as the worker and the Claude supervisor on, after three fixes to how openfarm reads the worker's edits. Two cards is not an efficacy result.)
> - Live `cargo-mutants` runs (the parser and gate logic are tested with synthetic output).
> - Live unattended multi-wave LLM execution of a generated app (for example all `calorie-log` cards) end to end.
> - External `cmd:` adapters against live repository tools, beyond subprocess test harnesses.
> - The TypeScript language pack, which is not implemented and refuses with `Invalid("no-typescript-pack")`.
> - The Python pack (v0.6 branch) against pytest: pytest is not installed here, so its output parsing is tested on copied `-v` and `--collect-only -q` shapes only. `unittest` parsing is tested against real interpreter output.
> - The Python pack's `ruff` lint and format path: `ruff` is not installed here, so only the no-ruff path (syntax check, formatting not checked) has run.
> - openfarm working on itself with a live worker model. Two cards have run on a throwaway clone of openfarm to `in-review` with a scripted model, from a pinned copy, with every gate on the real 446-test suite.
>
> Because none of the live runs have happened, there are no efficacy numbers yet: no acceptance rate, review time, or cost comparison against a frontier-only baseline.

## Roadmap

- [ ] **Live validation:** run everything in [Not yet tested](#not-yet-tested) against real endpoints, tools and installs.
- [ ] **Efficacy pilot:** an all-Rust pilot of 25 cards in two batches, with five frontier-baseline cards chosen in advance and all kept in the comparison, measuring per category whether a farm card pays for itself once review time is counted. Test-backfill cards wait for mutation tooling.
- [ ] **Calibrate the report-only gates:** measure the checker's counter-test false-reject rate and what mutation testing adds before letting either block more broadly.
- [ ] **Second repository:** run the core unchanged on a second, independent Rust repository before calling it done.
- [ ] **TypeScript pack and npm runner:** after the Rust pilot.

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
