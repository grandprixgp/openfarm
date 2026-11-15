# Frontier Card Decomposer Prompt (Openfarm v0.5)

You are decomposing `.openfarm/PROJECT_SPEC.md` into deterministic Openfarm v0.5 work cards under `cards/CARD-001.md`..`cards/CARD-NNN.md`.

## Card File Format

Every card file starts with a `+++` TOML header (delimiters alone on their own lines) and a `## Done` Markdown body. You may optionally keep extended v0.4/v0.5 fields in a fenced ```` ```openfarm ```` or ```` ```toml ```` block when `[cards] fields_block = "toml"` is enabled (`+++` keys win on conflict).

```markdown
+++
id = "CARD-001"
title = "Short imperative title"
state = "ready"
touches = ["src/entry.rs"]
depends = []
category = "small-feature"
reference = "src/goals.rs"
test_edits = ["tests/entry_tests.rs"]
frontier_estimate = 2.00
new_symbols = true
unit = "<crate_name>"
type_changes = false
context_files = ["src/invariants.rs", "src/lib.rs"]

[[tests]]
name = "valid_food_entry_stores_checked_macros"
file = "tests/entry_tests.rs"
asserts = "FoodEntry::new(\"Oats\", 4, 27, 5) returns Ok(entry) with total_kcal() == 169."
+++

## Done

- Export `pub struct FoodEntry` and `pub fn FoodEntry::new(...) -> Result<FoodEntry, DomainError>` from `src/entry.rs` and `pub use entry::{...};` in `src/lib.rs`.
- Compute calories via `checked_mul` and `checked_add` (`4 * protein + 4 * carbs + 9 * fat`), returning `Err(DomainError::Overflow)` on `u32::MAX` inputs.
```

> Note: Never list `src/invariants.rs` (or any `never_touch` file) in `touches`, `reference`, `context_files`, or `test_edits`. Pass invariant guarantees via `src/lib.rs` exports or explicit specifications in `## Done`.

## Mandatory Eligibility Checklist (`openfarm check`)

1. **Category must match `[policy] categories`**:
   - Original diet: `test-backfill`, `mechanical`, `pattern`, `pinned-bugfix`, `small-feature`
   - Expanded diet: `feature`, `refactor`, `integration`, `port`
2. **`reference` is required** when category is `pattern` or `small-feature` (and must NOT be in `never_touch` or `forbidden_files`).
3. **`new_symbols` constraints**:
   - **Disallowed** on `pinned-bugfix` and `test-backfill`.
   - If `new_symbols = true` and `len(touches) > 1`, eligibility requires **`type_changes = true`** OR active micromanager `scaffold_contracts = true`.
4. **Separate integration test files**:
   - Under `rust_tests_in_separate_files = true`, every `tests[].file` MUST be `tests/<subject>_tests.rs` (one distinct file per card prevents merge conflicts when landing branches).
   - Never target `Cargo.toml`, `Cargo.lock`, `.openfarm/**`, or `.claude/**`.
5. **Banned vague phrases in `## Done`**:
   - Eligibility **rejects** `"handle"`, `"properly"`, `"robust"`, `"decide how"`, `"design"`, `"choose an approach"`, `"figure out how"`.
   - Every `## Done` bullet MUST name exact `pub` item names, input values, error variants, and expected outputs, plus at least one concrete value/code span or assertion.
6. **File/unit bounds**:
   - Keep `len(touches) <= 4` (well below `max_files = 6`) and within `max_units = 1`.
7. **Wave Ordering for Maximum Harness² Efficiency**:
   - **Wave 1 (`CARD-001`, `CARD-002`)**: Independent foundational cards (`small-feature`/`pattern`/`pinned-bugfix`) targeting checked math, `clippy` cleanliness, and sibling replication.
   - **Wave 2 (`CARD-003`..`CARD-006`)**: Dependent core features (`feature`, `integration` with `depends = ["CARD-001", "CARD-002"]`, `context_files`, and `type_changes = true` where shared structs expand).
   - **Wave 3 (`CARD-007`+)**: `refactor` and `test-backfill` (only emit `test-backfill` if `cargo-mutants` is enabled or `[gates] mutation = "off"`).
