---
name: openfarm-initialize
description: >-
  Initialize a new software project from an empty directory and a plain-English
  idea (invoked via /openfarm-initialize <project_name> <project_idea> or when
  starting a fresh app with Openfarm v0.8.7). Integrates /openfarm-grill-me
  alignment, configures native Claude ACP via OAuth for Frontier + API key for
  Workers, scaffolds protected seams, a golden slice, and checked Wave cards.
---

# Openfarm Project Initialization (`/openfarm-initialize`)

Use this workflow to turn a plain-English project idea inside an empty directory into a validated, gate-enforced Openfarm repository ready for autonomous wave execution by lesser coding models (API key), native Claude ACP Frontier supervision (OAuth default), and Recursive Harness Memory.

> **Token-Efficiency Principle**: Spend ~3–8k frontier tokens **once** on design alignment, protected seam invariants, public API types, and a verified golden reference slice + pre-checked card queue. Do **not** burn frontier tokens implementing routine cards or iterating compiler errors—Openfarm runs lesser models, ACP coaching, and `HarnessLibrary` outside your context window.

## Workflow Steps

### 0. Phase 0 — Design Alignment (`/openfarm-grill-me`, Recommended)

Unless the user passed `--no-grill`, explicitly requested zero questions, or an agreed `.openfarm/GRILL_DECISIONS.md` already exists, invoke the `openfarm-grill-me` protocol first:
- Interview **one frontier question at a time** (recommended answer included, `"yes"` accepts) to lock down `src/invariants.rs` rules, golden slice, v1 non-goals, Frontier Claude ACP OAuth setup, and concrete `## Done` assertions.
- `--batch-grill` runs step 0 in batch mode; serial questions stay the default.
- Confirm shared understanding with the owner before modifying files.

### 1. Parse Arguments & Check Environment + Frontier OAuth

Extract `<project_name>` (normalized to ASCII slug, e.g. `calorie-log`) and `<project_idea>` (e.g. `"An app to track my calories"`).

Check toolchain, Worker key availability, and Frontier OAuth readiness once:
```bash
rustc --version && cargo --version && cargo clippy --version && rustfmt --version
openfarm acp status --provider all --json
command -v cargo-mutants || echo "no cargo-mutants (will default mutation=off)"
ls -la ~/.config/openfarm/ 2>/dev/null || true
```

- **Frontier Model (Claude / Antigravity / Codex ACP via OAuth, or Driver Mode):** Look at `oauth_ready` and `resolved_transport` in `openfarm acp status --json` (or `--provider claude|antigravity|codex|all`):
  - If `oauth_ready: true`, Frontier ACP supervision (`init --acp-provider claude|antigravity|codex`) and Driver mode (`openfarm drive --agent ...`) are ready with zero extra API keys.
  - If `oauth_ready: false`, guide the owner once to sign in via `openfarm acp login --provider <provider>` (or `claude auth login --claudeai` / `agy acp --login` / `codex login`), or pass `--profile solo` if intentionally disabling Frontier ACP supervision.
  - Claude is the default and the recommended provider. Codex and Antigravity children run inside a bubblewrap (`bwrap`) boundary; check the "Confinement" part of `openfarm acp status` says `bwrap` works before choosing Antigravity.
  - **Antigravity:** openfarm starts `agy` with no person typing. Ask the owner to check that their Antigravity plan's terms allow that before using it on OAuth; an API key (`auth = "api-key"` with `auth_file`) is the other route. Do not choose Antigravity OAuth by default. It is untested live since the first v0.8.1 build.
- **Worker Models (ZDR OpenAI-compatible API key):** Ask **at most once** if a ZDR endpoint URL / Worker API key file is not yet configured (or proceed immediately with placeholder settings if the user wants an offline dry-run or scripted run).

### Existing repository instead of an empty directory

When the directory already holds a git repository with code, `openfarm init` refuses it. Take this path instead:

1. Make sure the work tree is clean, then run `openfarm adopt --dir . --json` (auto-detects the language, or pass `--lang rust|python|typescript`; a `Cargo.toml` or Python manifest decides, so pass `--lang typescript` to adopt the TypeScript side of a repo that also has one; TypeScript tests must run with `node --test`, other test runners are refused). It writes `.openfarm/config.toml`, `.openfarm/PROJECT_SPEC.md` and `cards/.gitkeep`, commits nothing, and runs the suite once. If `known_failing_proposed` is not empty, show the owner those tests and copy them into `[policy] known_failing` only if they agree.
2. Grill as in step 0, reading the existing code for facts instead of asking.
3. Instead of step 3's new skeleton: propose `never_touch` from the code (the modules that hold the project's invariants), and pick one existing, well-tested module as the reference slice that `pattern` and `small-feature` cards name in `reference`. Write no new skeleton. Put both, and the module ownership map, in `.openfarm/PROJECT_SPEC.md`, and confirm `never_touch` with the owner.
4. Fill in `[model]` (endpoint, key file, models and any zero-retention header) as in step 1, then continue at step 4 (cards) and step 5 (commit and check) on the repository's own base branch rather than `main`.

### 2. Run Deterministic Bootstrap (`openfarm init`)

Run `openfarm init` in the target empty directory (never let a frontier model hand-write `.openfarm/config.toml` from memory):

```bash
openfarm init <project_name> \
  --dir . \
  --acp-provider claude \
  --idea "<project_idea>" \
  --json
```

By default `openfarm init` configures `[adapters] micromanager = "claude-acp"` (or `antigravity-acp` / `codex-acp` via `--acp-provider`), `[micromanager] mode = "active"`, `auth = "oauth"`, `binary = "auto"`, `tool_policy = "none"`, while configuring Workers under `[model]`. Add `--endpoint <URL> --model <MODEL_NAME>` if known, or pass `--profile advisory|solo` / adjust later via `openfarm acp configure --provider ...`. Confirm the JSON response reports `"config_valid": true`.

With `--acp-provider codex`, name openfarm's own Codex bridge right after init. `binary = "auto"` already picks it when the `codex` CLI is installed; naming it keeps a standalone `codex-acp` server (0.16.0 completed no turn on a ChatGPT login in testing) from ever being used:
```bash
openfarm acp configure --provider codex --binary codex-exec --tool-policy none --json
```

Verify zero-token ACP handshake works in the initialized repo when supervision is enabled (name the provider; probing Antigravity starts `agy`):
```bash
openfarm acp probe --provider <claude|codex|antigravity> --json
```

Remind the user (if `"key_file_present": false`) to place their Worker ZDR API key outside the repository at the printed `key_file` path with `chmod 600`.

### 3. Frontier Architect Pass (Seam + Thin Skeleton + Golden Reference Slice)

Read [architect_system.md](./prompts/architect_system.md), then design and write:
1. Complete `.openfarm/PROJECT_SPEC.md` (domain invariants, module ownership map, wave breakdown).
2. Expand `src/invariants.rs` (**protected by `never_touch`**) with exact types, arithmetic/bounds checks, and invariant validators that lesser models must never relax.
3. Add thin skeleton modules with **well-typed `pub` exports** (required because `rust_tests_in_separate_files = true` means `tests/*.rs` integration tests can only reach `pub` crate items) plus **one complete golden reference vertical slice** (`src/<ref_module>.rs`) that pattern cards can imitate cleanly.
4. Keep skeleton code small (~100–250 LOC total; leave ≥85% of application behavior for cards) with **zero** `.unwrap()`, `.expect()`, `todo!()`, or `#[allow(...)]` in `src/`.
5. Update `tests/baseline_tests.rs` so the baseline compiles and passes cleanly, then verify:
   ```bash
   cargo test
   cargo clippy --all-targets -- -D warnings
   rustfmt --edition 2021 --check src/**/*.rs tests/*.rs
   ```
   Every command above **must exit `0`**.

### 4. Card Decomposition Pass (Contrast Seed + Core + Polish Waves)

Read [card_decomposer.md](./prompts/card_decomposer.md), then author `cards/CARD-001.md`..`cards/CARD-NNN.md`:
- **Wave 1 (2 cards)**: High-contrast starter cards (`pinned-bugfix` or `small-feature`, plus `pattern` referencing your golden slice) that prove the pipeline end-to-end and seed `<state_dir>/harness/` contrast cues cleanly.
- **Wave 2 (`feature` / `pattern` / `integration` / `port`)**: Core application features with explicit `pub` function signatures, concrete assertions, separate `tests/<subject>_tests.rs` files per card, and `type_changes = true` where cross-file structs/enums evolve.
- **Wave 3 (`refactor` / `test-backfill`)**: Cleanup and edge-case coverage cards (set `[gates] mutation = "off"` or omit blocking `test-backfill` if `cargo-mutants` is absent).

### 5. Commit Baseline on `main` & Run 100% Pre-Flight Verification

> **Critical**: `FileCardSource` seeds the `openfarm-cards` branch from `main` on first prepare. Always commit the clean skeleton, config, conventions, and cards on `main` **before** running `openfarm check` or `openfarm run`.

```bash
git add .
git commit -m "chore: initial architecture, config, and wave cards"

for card_file in cards/CARD-*.md; do
  cid=$(basename "$card_file" .md)
  openfarm check "$cid" --json
done

openfarm run --dry-run --cards 3 --json
```

If any card fails `openfarm check`, fix its frontmatter or `Done` text immediately before finishing initialization.

### 6. Hand Off or Start Wave 1

Summarize the created files, `never_touch` seam (`src/invariants.rs`), verified card count, external `state_dir` / `key_file` paths, and ask the owner whether to launch Wave 1 immediately (via the [openfarm](../openfarm/SKILL.md) skill using `openfarm run` or `openfarm drive`) or stop after setup.
