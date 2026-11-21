# From Empty Folder to Shipped App (Openfarm v0.20.0 Starter Guide)

This guide shows **both human owners and prospective frontier AI agents** how to go from an **empty folder** and a **plain-English idea** to a gate-verified, human-reviewed application using **Openfarm v0.20.0**.

## The seven steps, whichever door you use

Run bare `openfarm` in an empty folder and one screen walks you through the steps below. The agent skills
(`/openfarm-initialize`, Part 2) and the CLI go through the same steps and use the same words. The example
here is the web app this version was tried on: a page built around Wikimedia's image of the day.

| Step | On the screen | With the CLI or a skill |
|---|---|---|
| **Idea** | Type it: "User visits app, presented with image of the day, design is built around the image…" and press **Continue**. | `/openfarm-initialize iotd "<idea>"`, or `openfarm init iotd --idea "<idea>"` |
| **Questions** | The Architect asks a few questions, one at a time, with suggested answers ("Where does the image come from?"). **Answer**. | `/openfarm-grill-me` |
| **Spec** | Read the spec and its goals (1 Living Image, 2 Recursive Threads, 3 Realtime and polish). **Plan goal 1**. | the spec and goals are written by the skill; read them with `openfarm status` |
| **Plan** | Goal 1 as four tasks (fetch the feed, derive the theme, route and proxy, show the page), each checked before you see it. A repair is shown with its reason. **Build 4 tasks**. | `openfarm plan`, `openfarm card show W1-01`, `openfarm check W1-01` |
| **Build** | Each task's state in a sentence. A task that stopped says what kind of stop it is and offers the fitting action. **Accept W1-02** merges it locally. | `openfarm run --follow`, then `openfarm dossier`, `openfarm verdict W1-02 clean`, `openfarm land` |
| **Try it** | The app is started and screenshotted; the checks say whether the title, story and credit can be seen. **Fix this** if not. | `openfarm try` |
| **Accept** | The goal's summary. **Accept goal 1** moves on to goal 2's plan. | `openfarm land`, then plan the next goal |

In that run the first build produced a page where the text was in the page but hidden under the photo. "Try it"
is the step that catches this: its check fails with "can't be seen (it is covered by img#art)", and **Fix this**
turns that sentence into fix tasks under goal 1. The four finished tasks stay as they are.

Words: a **task** is a card, a **goal** is a chunk, a **plan** is a wave plan, and **accept** is what used to be
approve, land and merge. Commands, file names and settings keep the older words (`cards/CARD-001.md`,
`openfarm verdict`, `wave = 1`). Part 2 below is the agent-skill route in detail, with a calorie log as its
worked example. It uses the same words as this section, and the older ones only inside code.
>
> **For related docs:** See [README.md](../README.md) for the overview, [docs/USAGE-AND-FIRST-DEPLOYMENT.md](USAGE-AND-FIRST-DEPLOYMENT.md) for detailed configuration & operator runbooks on existing repositories (`tallybox`), and [docs/openfarm-internals.md](openfarm-internals.md) for module internals.

---

## Part 1 — For Human Owners: The Two-Step Workflow

You do **not** need to hand-write `.openfarm/config.toml`, memorize card schemas, or copy Rust boilerplate by hand. Openfarm v0.8.5 packages the CLI, slash commands (`/openfarm-grill-me` and `/openfarm-initialize`), frontier provider setup for Claude, Codex and Antigravity (`openfarm acp`), plugin rules, and frontier architect/operator skills into a **single repository or package target**.

### Your Two Prompts

| Step | What you type to your frontier agent | What happens behind the scenes |
| --- | --- | --- |
| **1. Install** | `"Read and install this skill <github_or_local_link>"` | The agent reads root [`SKILL.md`](../SKILL.md) (`openfarm-installer`), installs Openfarm (`uv`/`pipx`/`pip`, or zero-`pip` `python3 -m openfarm_core.cli install-skill`), deploys `openfarm-grill-me`, `openfarm-initialize`, `openfarm`, slash commands, and workspace plugin rules, and checks Frontier OAuth readiness (`openfarm acp status`). |
| **2. Initialize** | `"/openfarm-initialize calorie-log An app to track my calories"` | Inside your empty folder, the agent first runs `/openfarm-grill-me` (one question at a time, with a recommended answer) to lock down invariants and boundaries (pass `--no-grill` to skip), runs deterministic `openfarm init` (defaulting Frontier to Claude ACP via OAuth and Workers to ZDR API keys), executes the **Frontier Architect Pass**, decomposes **Wave 1–3 cards**, commits on `main`, and validates every card with `openfarm check` and `openfarm run --dry-run`. |

### Why This Saves Frontier Tokens & Prevents Context Rot

1. **Frontier spend happens only at high-leverage boundaries (~3–8k tokens up front):**
   - Designing the domain invariants (`src/invariants.rs`, sealed under `[policy] never_touch`).
   - Writing **one** small golden reference slice (`src/goals.rs`) so weaker models imitate an exact compilable pattern rather than inventing style from scratch.
   - Reviewing compact `openfarm dossier <CARD-ID>` reports and clean `git diff`s before running `openfarm verdict` and `git merge --no-ff`.
2. **Lesser models handle the bulk work inside deterministic gates:**
   - Phase A (`test_writer`) writes named integration tests that must **compile and fail** on the pre-implementation tree (`fail-before`).
   - Tests freeze by SHA-256 hash; Phase B (`implementer`) writes production code in isolated `git` worktrees across checkpointed steps (`plan.steps`).
   - No model ever reports its own pass/fail: Rust `cargo test --no-fail-fast`, `cargo clippy -D warnings`, `rustfmt --check`, `revert-check`, banned-pattern checks, `checker` counter-tests, and catch-up trial merges are all executed and parsed by deterministic Python code.
3. **Zero context rot:**
   - `openfarm run` and `openfarm supervise` execute model calls, builds, compiler errors, retries, and ACP `coach` guidance **outside your frontier chat context**. Your frontier session sees compact JSON summaries and final diffs—never pages of noisy Cargo compiler errors.
4. **Recursive Harness² Efficiency (`<state_dir>/harness/`):**
   - Inspired by `google-research/harness2` (`arXiv:2610.14224`), `HarnessLibrary` learns from retry recoveries, fails, and passes (`extract_contrast` + optional `reflect_harness`), injects fenced `harness adaptations` cues into subsequent cards in the same run and future launches, probes new revisions over `probe_window` outcomes, and auto-demotes regressions—while **keeping every verification gate immutable (`IMMUTABLE_GATE_KEYS`)**.

---

## Part 2 — Step-by-Step Guide (Owner & Agent Playbook)

This walkthrough uses the example idea `"An app to track my calories"` -> **`calorie-log`**.

The steps are the seven above, done by an agent through the CLI: the **Architect** (Claude, Codex or Antigravity,
signed in) plans and reviews, **builders** (a cheaper model behind an API key) write the code, each **task** is one
file under `cards/`, and the tasks of one **goal** are built together and then accepted.

### Step 1: Install Openfarm & Agent Skills (`openfarm install-skill`)

When you send `"Read and install this skill <github_or_local_link>"`, the agent executes [`SKILL.md`](../SKILL.md):

#### Option A — Zero-`pip` Pure Python 3.11+ (Works Everywhere)

If the Openfarm repository is cloned locally (for example `~/.local/share/openfarm` or a workspace path):

```bash
python3 --version   # requires >= 3.11
python3 -m openfarm_core.cli install-skill \
  --target all \
  --install-bin ~/.local/bin \
  --json
```

Ensure `~/.local/bin` is on your `PATH`, then verify:

```bash
openfarm --help
```

You should see all **22 subcommands**: `init`, `adopt`, `bench`, `install-skill`, `acp`, `tui`, `run`, `status`, `pause`, `resume`, `stop`, `release`, `done`, `dossier`, `check`, `verdict`, `escape`, `supervise`, `harness`, `board`, `swarm`, `drive`.

Sign in once with your Claude OAuth subscription for the Architect (it needs no API key; the builders use an API key):

```bash
openfarm acp status --json
openfarm acp login          # optional if already signed into Claude Code
```

Codex and Antigravity can be the Architect instead (`--provider codex|antigravity`). Read [Architect providers](USAGE-AND-FIRST-DEPLOYMENT.md#5-frontier-providers-claude-codex-and-antigravity) first: Antigravity needs bubblewrap (and Codex wants it), Codex should use `binary = "codex-exec"`, and you must check that your Antigravity plan's terms allow openfarm to start `agy` without a person typing before you use it on OAuth.

#### Option B — Package Install (`uv`, `pipx`, or `pip`)

```bash
uv tool install git+https://github.com/grandprixgp/openfarm   # or a local path
# or: pipx install git+https://github.com/grandprixgp/openfarm
# or: pip install --user git+https://github.com/grandprixgp/openfarm

openfarm install-skill --target all --json
```

`install-skill` supports `--target`:
- `all` (default): installs into Claude Code (`$CLAUDE_CONFIG_DIR`, default `~/.claude`: `skills/` + `commands/openfarm-initialize.md`), Antigravity (`~/.gemini/config/skills/` + `~/.gemini/config/plugins/openfarm/`), Codex (`$CODEX_HOME`, default `~/.codex`: `skills/` + `openfarm-AGENTS.md`; whether Codex reads these by itself is untested), and shared `.agents` (`~/.agents/skills/`).
- `claude`, `antigravity`, `codex`, `agents`, or `workspace` (`<dest>/.agents/skills/`, `<dest>/.codex/skills/` + `<dest>/.claude/commands/openfarm-initialize.md`).
- Supports `--dry-run` and `--dest PATH` to preview or target a custom directory.

---

### Step 2: Initialize an Empty Folder (`"/openfarm-initialize calorie-log ..."`)

Create and enter an empty directory, then run `/openfarm-initialize calorie-log An app to track my calories`.

#### Stage 1 — Environment Check & `openfarm init`

First, check available local tools and the Architect's sign-in once:

```bash
rustc --version && cargo --version && cargo clippy --version && rustfmt --version
openfarm acp status --json
command -v cargo-mutants || echo "mutation=off (no cargo-mutants found)"
```

Next, run deterministic bootstrap (`openfarm init`) rather than writing `.openfarm/config.toml` by hand:

```bash
mkdir -p calorie-log && cd calorie-log
openfarm init calorie-log \
  --dir . \
  --idea "An app to track my calories" \
  --json
```

Useful optional flags on `openfarm init`:
- `--endpoint https://<your-zdr-provider>/v1 --model <model-id>` — configure your Zero Data Retention (ZDR) OpenAI-compatible endpoint and model for the builders.
- `--profile auto|solo|advisory|active` — `auto` selects `active` (`claude-acp`, `auth = "oauth"`, `binary = "auto"`) as the default way to connect the Architect (`openfarm acp configure` adjusts settings later; `--profile solo` runs without an Architect).
- `--acp-provider claude|antigravity|codex` — which provider is the Architect (default `claude`).
- `--mutation auto|off|report|required` — `auto` selects `report` if `cargo-mutants` is on `PATH`, else `off`.
- `--state-dir PATH` / `--key-file PATH` — override external state/key paths (enforced strictly **outside** the repository, created with `0o700` permissions).
- `--force` — allow scaffolding into a non-empty directory (without `--force`, `openfarm init` refuses non-empty dirs with exit code `1`).

`openfarm init` creates and validates (via `load_config`):
- `.openfarm/config.toml` (validated strict TOML tables with `[swarm] enabled = false` default, optional `[tiers.*]`, `src/invariants.rs` protected in `never_touch`, and `.openfarm/**`, `.claude/**`, `**/Cargo.toml`, `Cargo.lock` in `forbidden_files`)
- `.openfarm/test-conventions.md`
- `.openfarm/PROJECT_SPEC.md`
- `AGENTS.md`
- `Cargo.toml` (`name = "calorie_log"`, edition `2021`, zero external dependencies) and `Cargo.lock` (`version = 3` so `--locked` cargo runs succeed cleanly without untracked lockfile diffs)
- `.gitignore` (`/target`)
- `src/invariants.rs`, `src/lib.rs`, `tests/baseline_tests.rs`
- `cards/.gitkeep`
- Git repository initialized on branch `main` (`Git.init(branch="main")`)

> **API Key Setup:** Place your ZDR provider key outside the repo at the printed `key_file` path (default `~/.config/openfarm/calorie-log.key`) with `chmod 600`. Keys are never stored inside the git tree or logged.

---

#### Stage 2 — The Architect's Pass (Seam + Thin Skeleton + Golden Reference Slice)

Now spend one focused Architect pass implementing:
1. **`src/invariants.rs`** (`never_touch` seam — builders can never view or modify this file): domain bounds (`MAX_KCAL_PER_DAY`), `DomainError`, validated `Kcal`, checked `MacroGrams::to_kcal()` (4/4/9 Atwater math), and `ensure_non_empty_label`.
2. **`src/goals.rs`** (**Golden Reference Slice**): one clean, idiomatic module (`DailyTarget` and `CalorieBudgetStatus`) demonstrating checked math, `Result<T, DomainError>`, doc comments, and zero `.unwrap()` / `.expect()` / `#[allow(...)]`.
3. **Thin sibling modules** (`src/entry.rs`, `src/logbook.rs`) + `pub` re-exports in `src/lib.rs` (required because `[eligibility] rust_tests_in_separate_files = true` puts every integration test in `tests/<subject>_tests.rs`).

Write these files cleanly:

**`src/invariants.rs`** (sealed in `never_touch`):

```rust
//! Domain invariants protected by `[policy] never_touch`.
//!
//! Openfarm keeps this file out of lesser-model briefs and edits.

pub const SCHEMA_VERSION: u32 = 1;
pub const MAX_KCAL_PER_DAY: u32 = 25_000;

/// Errors returned by validated calorie domain operations.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DomainError {
    EmptyLabel,
    Overflow,
    OutOfRange,
    DuplicateId,
}

/// Validated non-negative calorie count bounded by `MAX_KCAL_PER_DAY`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct Kcal(u32);

impl Kcal {
    /// Zero calories constant.
    pub const ZERO: Self = Self(0);

    /// Construct a bounded `Kcal` value.
    pub fn new(raw: u32) -> Result<Self, DomainError> {
        if raw > MAX_KCAL_PER_DAY {
            return Err(DomainError::OutOfRange);
        }
        Ok(Self(raw))
    }

    /// Return the underlying integer calorie count.
    pub fn value(self) -> u32 {
        self.0
    }

    /// Checked addition respecting `MAX_KCAL_PER_DAY`.
    pub fn checked_add(self, other: Self) -> Result<Self, DomainError> {
        let sum = self.0.checked_add(other.0).ok_or(DomainError::Overflow)?;
        Self::new(sum)
    }
}

/// Validated macronutrient breakdown in whole grams.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MacroGrams {
    pub protein: u32,
    pub carbs: u32,
    pub fat: u32,
}

impl MacroGrams {
    /// Compute energy via checked Atwater multipliers (4 kcal/g protein & carbs, 9 kcal/g fat).
    pub fn to_kcal(self) -> Result<Kcal, DomainError> {
        let p = self.protein.checked_mul(4).ok_or(DomainError::Overflow)?;
        let c = self.carbs.checked_mul(4).ok_or(DomainError::Overflow)?;
        let f = self.fat.checked_mul(9).ok_or(DomainError::Overflow)?;
        let total = p
            .checked_add(c)
            .and_then(|pc| pc.checked_add(f))
            .ok_or(DomainError::Overflow)?;
        Kcal::new(total)
    }
}

/// Normalize and require a non-empty trimmed label.
pub fn ensure_non_empty_label(label: &str) -> Result<String, DomainError> {
    let trimmed = label.trim();
    if trimmed.is_empty() {
        return Err(DomainError::EmptyLabel);
    }
    Ok(trimmed.to_string())
}
```

**`src/goals.rs`** (Golden Reference Slice for `pattern` / `small-feature` tasks):

```rust
//! Golden reference vertical slice: daily calorie targets and budget evaluation.

use crate::invariants::{DomainError, Kcal, MAX_KCAL_PER_DAY};

/// User's daily calorie and macronutrient target.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DailyTarget {
    pub kcal_limit: Kcal,
    pub protein_min_g: u32,
    pub carbs_target_g: u32,
    pub fat_limit_g: u32,
}

/// Comparison result for consumed energy against a `DailyTarget`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CalorieBudgetStatus {
    pub consumed: Kcal,
    pub limit: Kcal,
    pub remaining: Kcal,
    pub exceeded_by: Option<Kcal>,
}

impl DailyTarget {
    /// Create a validated daily target with a strictly positive calorie limit.
    pub fn new(
        kcal_limit: u32,
        protein_min_g: u32,
        carbs_target_g: u32,
        fat_limit_g: u32,
    ) -> Result<Self, DomainError> {
        if kcal_limit == 0 || kcal_limit > MAX_KCAL_PER_DAY {
            return Err(DomainError::OutOfRange);
        }
        Ok(Self {
            kcal_limit: Kcal::new(kcal_limit)?,
            protein_min_g,
            carbs_target_g,
            fat_limit_g,
        })
    }

    /// Evaluate how `consumed` compares against `self.kcal_limit`.
    pub fn evaluate(&self, consumed: Kcal) -> CalorieBudgetStatus {
        let limit_val = self.kcal_limit.value();
        let consumed_val = consumed.value();
        if consumed_val <= limit_val {
            let remaining = match Kcal::new(limit_val - consumed_val) {
                Ok(k) => k,
                Err(_) => Kcal::ZERO,
            };
            CalorieBudgetStatus {
                consumed,
                limit: self.kcal_limit,
                remaining,
                exceeded_by: None,
            }
        } else {
            let over = match Kcal::new(consumed_val - limit_val) {
                Ok(k) => k,
                Err(_) => self.kcal_limit,
            };
            CalorieBudgetStatus {
                consumed,
                limit: self.kcal_limit,
                remaining: Kcal::ZERO,
                exceeded_by: Some(over),
            }
        }
    }
}
```

> **Note:** Openfarm scans production diffs for `.unwrap()`, `.expect(...)`, `todo!()`, `dbg!()`, and `#[allow(...)]`. Using `Kcal::ZERO` and checked arithmetic keeps `src/` completely clean for both `clippy` and Openfarm's banned-pattern gate.

**`src/entry.rs`** (thin stub module for Goal 1 `CARD-001`):

```rust
//! Food entry records (implemented in Wave 1).
```

**`src/logbook.rs`** (thin stub module for tasks of goals 1–2):

```rust
//! Daily calorie logbook aggregation (implemented in Wave 1–2).
```

**`src/lib.rs`**:

```rust
pub mod entry;
pub mod goals;
pub mod invariants;
pub mod logbook;

pub use goals::{CalorieBudgetStatus, DailyTarget};
pub use invariants::{
    ensure_non_empty_label, DomainError, Kcal, MacroGrams, MAX_KCAL_PER_DAY, SCHEMA_VERSION,
};

pub fn project_name() -> &'static str {
    "calorie-log"
}
```

**`tests/baseline_tests.rs`**:

```rust
use calorie_log::{
    ensure_non_empty_label, project_name, DailyTarget, DomainError, Kcal, MacroGrams,
    SCHEMA_VERSION,
};

#[test]
fn baseline_invariants_and_schema_are_valid() {
    assert_eq!(SCHEMA_VERSION, 1);
    assert_eq!(project_name(), "calorie-log");
    assert_eq!(ensure_non_empty_label("  Oats  "), Ok("Oats".to_string()));
    assert_eq!(ensure_non_empty_label("   "), Err(DomainError::EmptyLabel));
}

#[test]
fn baseline_macros_compute_checked_kcal() {
    let macros = MacroGrams {
        protein: 20,
        carbs: 30,
        fat: 10,
    };
    let kcal = macros.to_kcal();
    assert_eq!(kcal, Kcal::new(290));
    assert_eq!(
        MacroGrams {
            protein: u32::MAX,
            carbs: 0,
            fat: 0,
        }
        .to_kcal(),
        Err(DomainError::Overflow)
    );
}

#[test]
fn baseline_golden_slice_evaluates_daily_target() {
    assert_eq!(DailyTarget::new(0, 100, 200, 60), Err(DomainError::OutOfRange));
    let target = DailyTarget::new(2_000, 140, 200, 65).ok();
    assert!(target.is_some());
    if let (Some(t), Ok(consumed), Ok(expected_rem)) = (target, Kcal::new(1_450), Kcal::new(550)) {
        let status = t.evaluate(consumed);
        assert_eq!(status.remaining, expected_rem);
        assert_eq!(status.exceeded_by, None);
    }
}
```

Verify every baseline gate passes with zero warnings before creating tasks:

```bash
cargo test
cargo clippy --all-targets -- -D warnings
rustfmt --edition 2021 --check src/*.rs tests/*.rs
```

---

#### Stage 3 — Decompose into Verified Tasks for Goals 1–3 (`cards/CARD-001.md`..`CARD-008.md`)

Key task hygiene rules enforced by `openfarm check`:
- Place one distinct `tests/<subject>_tests.rs` per task (`rust_tests_in_separate_files = true`) so branches merge cleanly without file collisions.
- Never reference `src/invariants.rs` in `touches`, `reference`, or `context_files`.
- Never use vague verbs/phrases (`"handle"`, `"properly"`, `"robust"`, `"decide how"`, `"design"`, `"choose an approach"`, `"figure out how"`) in `## Done`. State exact `pub` items, inputs, errors, and outputs.
- Disallow `new_symbols = true` on `pinned-bugfix` and `test-backfill` tasks; set `type_changes = true` when `new_symbols = true` spans multiple files in `touches`.
- Keep `[gates] mutation = "off"` (the default of `openfarm init` when `cargo-mutants` is absent) or omit blocking `test-backfill` tasks until `cargo-mutants` is installed.

**Goal 1 (`CARD-001`, `CARD-002` — Contrast Seed & Pipeline Proof):**

`cards/CARD-001.md` (`small-feature`):

```markdown
+++
id = "CARD-001"
title = "Implement validated FoodEntry and MealSlot"
state = "ready"
touches = ["src/entry.rs"]
depends = []
category = "small-feature"
reference = "src/goals.rs"
test_edits = ["tests/entry_tests.rs"]
frontier_estimate = 2.0
new_symbols = true
unit = "calorie_log"
type_changes = true

[[tests]]
name = "valid_food_entry_computes_atwater_kcal"
file = "tests/entry_tests.rs"
asserts = "FoodEntry::new(\"Oats\", MealSlot::Breakfast, MacroGrams { protein: 4, carbs: 27, fat: 5 }) returns Ok(e) with e.kcal().value() == 169."

[[tests]]
name = "food_entry_rejects_blank_name_and_macro_overflow"
file = "tests/entry_tests.rs"
asserts = "FoodEntry::new(\"   \", MealSlot::Snack, ...) returns Err(DomainError::EmptyLabel) and u32::MAX protein returns Err(DomainError::Overflow)."
+++

## Done

- Export `pub enum MealSlot { Breakfast, Lunch, Dinner, Snack }` and `pub struct FoodEntry` in `src/entry.rs` (`calorie_log::entry::{FoodEntry, MealSlot}`, since `src/lib.rs` already exports `pub mod entry;`).
- `pub fn FoodEntry::new(name: &str, meal: MealSlot, macros: MacroGrams) -> Result<Self, DomainError>` validates `name` via `ensure_non_empty_label` and computes energy via `macros.to_kcal()`.
- Export accessors `pub fn name(&self) -> &str`, `pub fn meal(&self) -> MealSlot`, `pub fn macros(&self) -> MacroGrams`, and `pub fn kcal(&self) -> Kcal`.
```

`cards/CARD-002.md` (`pattern`, depends on `CARD-001`, mirrors `src/goals.rs` inside `src/logbook.rs`):

```markdown
+++
id = "CARD-002"
title = "Implement DailyLog append and total_kcal"
state = "ready"
touches = ["src/logbook.rs"]
depends = ["CARD-001"]
category = "pattern"
reference = "src/goals.rs"
test_edits = ["tests/logbook_tests.rs"]
frontier_estimate = 2.0
new_symbols = true
unit = "calorie_log"
type_changes = true

[[tests]]
name = "daily_log_accumulates_entry_calories"
file = "tests/logbook_tests.rs"
asserts = "Adding 169 kcal and 400 kcal entries yields log.total_kcal().value() == 569 and log.len() == 2."

[[tests]]
name = "daily_log_rejects_sum_past_max_kcal_per_day"
file = "tests/logbook_tests.rs"
asserts = "Adding entries whose sum exceeds MAX_KCAL_PER_DAY returns Err(DomainError::OutOfRange) without mutating stored entries."
+++

## Done

- Implement `pub struct DailyLog` in `src/logbook.rs` (`calorie_log::logbook::DailyLog`) with `pub fn new() -> Self`, `pub fn add_entry(&mut self, entry: FoodEntry) -> Result<(), DomainError>`, `pub fn total_kcal(&self) -> Kcal`, `pub fn len(&self) -> usize`, and `pub fn is_empty(&self) -> bool`.
- Reject additions that would exceed `MAX_KCAL_PER_DAY` before pushing to `self`, returning `Err(DomainError::OutOfRange)`.
```

**Plans for Goals 2 and 3 (`CARD-003`..`CARD-008` — full TOML files following the same pattern):**

| Task | Category | Depends | Touches & Test File | Scope Summary |
| --- | --- | --- | --- | --- |
| `CARD-003` | `pinned-bugfix` (`new_symbols = false`) | `CARD-001` | `src/entry.rs`, `tests/entry_scale_tests.rs` | Pin decimal serving scaling / zero-macro rounding behavior on `FoodEntry` |
| `CARD-004` | `feature` | `CARD-002` | `src/logbook.rs`, `tests/meal_summary_tests.rs` | Add `pub fn kcal_for_meal(&self, meal: MealSlot) -> Kcal` and `pub fn macro_totals(&self) -> Result<MacroGrams, DomainError>` |
| `CARD-005` | `integration` (`type_changes = true`) | `CARD-002`, `CARD-004` | `src/logbook.rs`, `src/lib.rs`, `tests/progress_tests.rs` | Wire `DailyLog::evaluate_against(&self, target: &DailyTarget) -> CalorieBudgetStatus` |
| `CARD-006` | `port` (`reference = "src/goals.rs"`, `type_changes = true`) | `CARD-001`, `CARD-002` | `src/codec.rs`, `src/lib.rs`, `tests/codec_tests.rs` | Pure deterministic TSV serialization/parsing for `FoodEntry` rows |
| `CARD-007` | `refactor` | `CARD-004`, `CARD-005` | `src/logbook.rs`, `tests/logbook_refactor_tests.rs` | Deduplicate checked macro summation helper without changing public behavior |
| `CARD-008` | `test-backfill` (`new_symbols = false`) | `CARD-005`, `CARD-006` | `tests/boundary_backfill_tests.rs` | Backfill boundary & malformed-TSV tests (run only with `cargo-mutants` or `[gates] mutation = "off"`) |

**Negative Seam Control (`CARD-900` — verify `never_touch` refusal, then delete before committing `main`):**

If a task accidentally lists `src/invariants.rs` in `touches`, `openfarm check` refuses it before any LLM call runs:

```markdown
+++
id = "CARD-900"
title = "Seam violation control test"
state = "ready"
touches = ["src/invariants.rs"]
depends = []
category = "mechanical"
test_edits = []
frontier_estimate = 1.0
new_symbols = false
+++

## Done

- Modify `MAX_KCAL_PER_DAY` in `src/invariants.rs` (expected to be refused by `never_touch`).
```

---

#### Stage 4 — Commit Baseline on `main` & Run 100% Pre-Flight Checks

`FileCardSource` creates `openfarm-cards` from `main`, so **always commit your baseline architecture and tasks on `main` before checking or running tasks**:

```bash
git add .
git commit -m "chore: calorie-log architecture, golden slice, and wave cards"

for card_file in cards/CARD-*.md; do
  cid=$(basename "$card_file" .md)
  openfarm check "$cid" --json
done

openfarm run --dry-run --cards 2 --json
```

Expected:
- Every task (`CARD-001`..`CARD-008`) returns exit code `0` (`"eligible": true`).
- `CARD-900` (if tested before removal) returns exit code `1` with reason containing `never-touch`.

---

#### Stage 5 — Build the Goals Out of Context, Read the Task Reports & Accept Locally

Follow the installed `openfarm` skill (`wave_operator.md`) to execute tasks cleanly without filling the Architect's context:

```bash
# 1. Inspect queue & optionally prepare cards via ACP micromanager
openfarm status --json
openfarm supervise --prepare-queue --apply --json

# 2. Run Wave 1 (2 cards max) with strict spend budget
#    (Add --script /path/to/script.json for deterministic offline testing)
openfarm run --cards 2 --spend 5.00 --json

# 3. Inspect resulting local branches & compact dossiers
git branch --list 'openfarm/*'
openfarm dossier CARD-001
git diff main..openfarm/CARD-001-*

# 4. Record reviewer verdict & merge locally (Openfarm never pushes)
openfarm verdict CARD-001 clean --review-tokens 1100 --minutes 3 --note "Checked against PROJECT_SPEC"
git checkout main && git merge --no-ff openfarm/CARD-001-implement-validated-foodentry-and-mealslot
cargo test --quiet

# 5. Inspect Harness² experience library transferred into Wave 2
openfarm harness status --json
openfarm harness index
```

Repeat goal by goal (`--cards 3 --spend 10.00`) until all tasks of goals 1–3 are accepted onto `main`, then verify the completed app with:

```bash
cargo test
cargo clippy --all-targets -- -D warnings
rustfmt --edition 2021 --check src/*.rs tests/*.rs
```

---

#### Stage 6 — Operational Recovery Playbook

| Symptom / Stop Code | What happened | Deterministic recovery command |
| --- | --- | --- |
| `dirty-baseline` | Base branch failed a test before Phase A | Inspect `openfarm dossier <ID>`, fix test on `main`, clear stale cache if needed, re-run `--dry-run`. |
| `infra` | Disk `< min_free_mb`, network `429`/`5xx`, or ACP timeout (`HarnessLibrary` ignores `infra`) | Free space or fix endpoint/credentials -> `openfarm resume` -> `openfarm release <ID> --reason "infra retry"`. |
| `circuit_breaker` (`3` consecutive blocks) | Three tasks in a row hit the same block reason | Read `<state_dir>/breaker.json` -> `openfarm supervise --triage-blocked --json` -> fix task on `main` -> `openfarm release <ID> --reason "rescoped"` -> `openfarm resume --clear-breaker`. |
| Noisy or unhelpful Harness² cue | A promoted cue hurt efficiency (auto-demotes after `probe_window`, or intervene manually) | `openfarm harness show <ENTRY_ID>` -> `openfarm harness demote <ENTRY_ID_OR_REV> --reason "noisy cue"`. |

---

## Part 3 — Honest Testing & Untested Boundaries

Verified automatically in Openfarm's local test suite (including `tests/test_v05_skill_bootstrap.py`):
- PEP 621 offline wheel packaging (`pyproject.toml`, `dependencies = []`, bundling all 12 `openfarm_core/skill_assets/` files).
- Byte-for-byte parity between repo `skills/` and `openfarm_core/skill_assets/skills/`, YAML frontmatter, `$ARGUMENTS` slash command, and plugin JSON.
- `openfarm install-skill` (`--target all|claude|antigravity|codex|agents|workspace`, `--install-bin` executable `openfarm` launcher, `--dry-run`).
- `openfarm init calorie-log` (`Git.init`, strict 15-table `load_config`, `0o700` state/key dirs, non-empty refusal, path traversal & Rust keyword guards, compiling `cargo test` + `cargo clippy -- -D warnings` + `rustfmt --check`).
- The exact `calorie-log` domain skeleton (`invariants.rs` + `goals.rs` + `lib.rs` + `baseline_tests.rs`), `openfarm check CARD-001` (`exit 0`), negative seam `CARD-900` (`exit 1`), `openfarm run --dry-run`, and single-card `--script` execution to `in-review`.

What ran live on 17 Nov 2026 (real ruff, pytest, cargo-mutants, `tsc` and `prettier`; the checker with a live model; Claude through both routes; Codex through openfarm's bridge; `openfarm drive` with Codex and Claude) is listed in the [README](../README.md#whats-verified) with the numbers in the [build log](v0.8.1-build-log.md).

Explicitly **untested**:
- **Remote GitHub URL installs via `uv tool install` / `pipx install` / `pip install`** (tested via offline PEP 621 wheel build + local `openfarm install-skill`).
- **Hosted worker endpoints other than Command Code**.
- **Unattended multi-wave LLM execution of all 8 `calorie-log` cards end-to-end**.
- **Everything Antigravity since the first agent's runs**: the boundary around `agy`, the login copy, the prompt-size refusal, the driver.
- **TypeScript on a real npm-installed project with a live model writing counter-tests**.
- The rest of the list is in the [README](../README.md#not-yet-tested).
