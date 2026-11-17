---
name: openfarm
description: >-
  Operate an initialized Openfarm v0.8.1 project across card waves: manage
  multi-provider Frontier ACP OAuth setup (Claude, Antigravity agy, Codex via
  openfarm acp), run automated Driver mode (openfarm drive), clarify feature
  waves with openfarm-grill-me, prepare queues with openfarm supervise, execute
  lesser-model + ACP runs without bloating frontier context, inspect dossiers
  or openfarm tui, record verdicts, merge local branches, and curate
  <state_dir>/harness/ recursive memory.
---

# Openfarm Wave Operations Skill (`v0.8.1`)

Use this skill after `/openfarm-initialize` (or `openfarm adopt --lang rust|python|typescript` on an existing repository) has set up `.openfarm/config.toml`, committed the baseline on the base branch, and verified `cards/` via `openfarm check`.

See [prompts/wave_operator.md](./prompts/wave_operator.md) for the full operator protocol and [references/cheat_sheet.md](./references/cheat_sheet.md) for exact flags, exit codes, and stop codes.

## Core Execution Loop (Per Wave)

1. **Frontier OAuth Check & Queue Prep**:
   ```bash
   openfarm acp status --provider all --json
   openfarm status --json
   openfarm supervise --prepare-queue --apply --json
   ```
   (Use `/openfarm-grill-me` before decomposing a new feature wave, and `openfarm acp login --provider claude|antigravity|codex` or `openfarm acp configure --provider ...` if Frontier OAuth needs sign-in or mode tuning. Workers continue using the API key in `[model] key_file`.)
2. **Execute Bounded Wave (Out of Frontier Context — ACP Mode or Driver Mode)**:
   ```bash
   # ACP / configured-seat mode:
   openfarm run --cards 3 --spend 10.00 --json
   # Or automated external Driver mode (claude, antigravity/agy, or codex):
   openfarm drive --agent claude --seat both --cards 3 --spend 10.00 --wave --json
   ```
   Openfarm handles worktree isolation, Phase A fail-before tests, scaffold contracts, Phase B checkpoints, retries, ACP coaching / Driver board coordination, `revert-check`, checker counter-tests, and `HarnessLibrary` capture (`<state_dir>/harness/`) **without feeding build logs into your context window**. (`openfarm drive --wave` adds the driver's own first look at each `in-review` card, without reading `never_touch` files and without merging or pushing. With `--record-verdict` it writes that look to `verdict.json` marked `by: driver/<agent>`, only where no verdict exists yet, never `clean` on a cut diff, and records nothing when the driver gave no usable answer. It is a cheap model's opinion, not a review: your own or the frontier lane's verdict in step 4 still decides the merge.)
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
   openfarm harness status --global --json
   ```
   Lessons proven on 3 or more cards that name nothing of this project go to the global library (`~/.openfarm/global/harness`) unless `[harness] share = false`; other projects try them like their own. `[harness] learn_from` (`both`, `project` or `global`) or `openfarm run --learn-from` picks which lessons apply. Retire a bad global lesson with `openfarm harness demote <gid> --global`.
6. **Measure, When Asked**: `openfarm bench run --dry-run` checks the bench for free; `openfarm bench run --arm farm=<file> --arm solo=<file>` compares the farm with Claude working alone on 12 tasks with hidden tests (examples in `bench/arms/`). A live bench spends real model money, so run it only when the owner asks.
7. **Inspect Live Swarm Runs & TUI (v0.8)**: When `[swarm] enabled = true`, monitor the shared run board (`openfarm board read --as "*"`), drive external coordinator/supervisor seats manually (`openfarm swarm status|tiers|join|start|set|hint|block|remove`, `openfarm board answer`) or automatically (`openfarm drive --agent claude|antigravity|codex --seat both --run ID`), or inspect runs and seven live views with `openfarm tui [--run ID] [--view overview|cards|dossier|board|swarm|harness|ops] [--once]`.
8. **Compact Context Between Waves**: Keep only `main` commit state, remaining `cards/`, and `openfarm harness status` summary before starting the next wave.
