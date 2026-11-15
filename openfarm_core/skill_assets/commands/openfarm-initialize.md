---
description: Initialize a new Openfarm v0.5 project from an empty directory and plain-English idea
argument-hint: <project_name> <project_idea>
---

# `/openfarm-initialize`

Arguments received: `$ARGUMENTS`

Follow the installed `openfarm-initialize` skill workflow strictly:

1. Parse the first token of `$ARGUMENTS` as `<project_name>` and the remaining text as `<project_idea>`. If `$ARGUMENTS` is empty, ask the owner for `<project_name>` and a short description of what they want to build.
2. Check toolchain prerequisites (`python3 >= 3.11`, `git`, `rustc`, `cargo`, `clippy`, `rustfmt`, optional `claude-agent-acp` / `cargo-mutants`, and existing keys in `~/.config/openfarm/`).
3. Run `openfarm init <project_name> --dir . --idea "<project_idea>" --json` (never hand-author `.openfarm/config.toml` from memory).
4. Execute the **Frontier Architect Pass** (`openfarm-initialize/prompts/architect_system.md`): finalize `.openfarm/PROJECT_SPEC.md`, lock core domain invariants in `never_touch` (`src/invariants.rs`), build thin `pub`-exported skeleton modules plus **one green golden reference vertical slice**, and verify `cargo test`, `cargo clippy --all-targets -- -D warnings`, and `rustfmt --edition 2021 --check`.
5. Execute the **Card Decomposition Pass** (`openfarm-initialize/prompts/card_decomposer.md`): create dependency-ordered Wave 1–3 cards under `cards/CARD-001.md`..`CARD-NNN.md` with discrete `tests/<subject>_tests.rs` files.
6. Commit everything cleanly on `main`, run `openfarm check <CARD-ID> --json` on every card plus `openfarm run --dry-run --cards 3 --json` until all pass, then report readiness and ask whether to run Wave 1 via the `openfarm` wave operator skill.
