---
name: openfarm
description: >-
  Operate an initialized Openfarm v0.5.1 project across card waves: manage
  Frontier Claude ACP OAuth setup (openfarm acp), clarify new feature waves
  with openfarm-grill-me, prepare queues with openfarm supervise, execute
  lesser-model + ACP runs via openfarm run without bloating frontier context,
  inspect dossiers, record verdicts, merge local branches, and curate
  <state_dir>/harness/ recursive memory.
---

# Openfarm Wave Operations Skill (`v0.5.1`)

Use this skill after `/openfarm-initialize` has set up `.openfarm/config.toml`, committed the baseline on `main`, and verified `cards/` via `openfarm check`.

See [prompts/wave_operator.md](./prompts/wave_operator.md) for the full operator protocol and [references/cheat_sheet.md](./references/cheat_sheet.md) for exact flags, exit codes, and stop codes.

## Core Execution Loop (Per Wave)

1. **Frontier OAuth Check & Queue Prep**:
   ```bash
   openfarm acp status --json
   openfarm status --json
   openfarm supervise --prepare-queue --apply --json
   ```
   (Use `/openfarm-grill-me` before decomposing a new feature wave, and `openfarm acp login` or `openfarm acp configure` if Frontier Claude OAuth needs sign-in or mode tuning. Workers continue using the API key in `[model] key_file`.)
2. **Execute Bounded Wave (Out of Frontier Context)**:
   ```bash
   openfarm run --cards 3 --spend 10.00 --json
   ```
   Openfarm handles worktree isolation, Phase A fail-before tests, scaffold contracts, Phase B checkpoints, retries, ACP coaching, `revert-check`, checker counter-tests, and `HarnessLibrary` capture (`<state_dir>/harness/`) **without feeding build logs into your context window**.
3. **Review Compact Dossiers & Actual Git Diff**:
   ```bash
   git branch --list 'openfarm/*'
   openfarm dossier <CARD-ID>
   git diff main..openfarm/<CARD-ID>-*
   ```
4. **Record Verdict & Land Locally (Never Pushed Automatically)**:
   ```bash
   openfarm verdict <CARD-ID> clean --review-tokens 1200 --minutes 4 --note "Verified against PROJECT_SPEC"
   git checkout main && git merge --no-ff openfarm/<CARD-ID>-<slug>
   cargo test --quiet
   openfarm done <CARD-ID>   # cards that depend on it can now run
   ```
5. **Triage Blocks & Inspect Harness² Learning**:
   ```bash
   openfarm supervise --triage-blocked --json
   openfarm harness status --json
   openfarm harness index
   ```
6. **Compact Context Between Waves**: Keep only `main` commit state, remaining `cards/`, and `openfarm harness status` summary before starting the next wave.
