# openfarm v0.8.8 Bench: Small-Card Economics & Effort Ladder

Date: 19 Nov 2026

This bench measures v0.8.8's small-card economics (`shape_when = "new-symbols"`, `pre_review_when = "on-flag"`, `checker.effort = "medium"`) and the default `medium` → `high` → `max` retry effort ladder across all 12 tasks in [`bench/`](../bench/), 3 repeats each for Claude and Codex farm arms (72 farm runs), compared against the v0.8.7 farm and solo baselines ([`docs/bench-v0.8.7.md`](bench-v0.8.7.md)).

Cost is given as token counts and shares (plus fresh vs. cached input), never dollars.

## 1. Setup

- **Tree.** v0.8.8 (`ba5a7e2`).
- **Farm arms:** DeepSeek V4.1 Flash workers on Command Code with `x-cmd-zdr: 1`, `effort_ladder = ["medium", "high", "max"]`, `checker.effort = "medium"`, `[micromanager] shape_when = "new-symbols"`, `pre_review_when = "on-flag"`.
- **Solo baselines:** v0.8.7 solo runs from the same day (`Claude solo` 34/36, 511,165 tokens; `Codex solo` 36/36, 926,065 tokens).

## 2. Results

| Provider | Arm | Passed | Tokens | Tokens / run | Tokens / pass | Worker / supervisor | Fresh in / Cached in | Wall time | Frontier cut vs solo |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Claude | farm (v0.8.8) | **34 of 36** (34/34 non-infra) | 586,212 | 16,284 | 17,242 | 80.7% / 19.3% | 260,886 / 18,304 | 36.4 min | **77.9% fewer** (113,015 vs 511,165) |
| Codex | farm (v0.8.8) | **35 of 36** | **883,051** | **24,529** | **25,230** | 67.9% / 32.1% | 459,531 / 28,416 | 48.6 min | **69.4% fewer** (283,513 vs 926,065) |

### 2.1 Against v0.8.7

| Provider | v0.8.7 farm passed | v0.8.8 farm passed | v0.8.7 tokens / run | v0.8.8 tokens / run | v0.8.7 frontier cut | v0.8.8 frontier cut |
| --- | --- | --- | --- | --- | --- | --- |
| Claude | 35 of 36 | 34 of 36 (2 transient infra) | 25,557 | **16,284 (−36%)** | 37.5% fewer | **77.9% fewer** |
| Codex | 34 of 36 | **35 of 36** | 31,322 | **24,529 (−22%)** | 45.9% fewer | **69.4% fewer** |

- **First time the farm beat solo on total tokens on small cards.** Codex farm (883,051 total tokens, 25,230/pass) used **4.6% fewer total tokens** than Codex solo (926,065 total tokens, 25,724/pass) while passing 35 of 36 and cutting frontier tokens by **69.4%**.
- **Claude farm cut total tokens by 36% and frontier tokens by 65% vs v0.8.7** (113,015 vs 319,232), pushing worker share to **80.7%** and passing 34 of 34 non-infra runs (100%). The 2 stops (`py-validate-port #3`, `rs-config-refactor #1`) were transient `infra` (`claude-agent-acp` fallback when `claude` briefly missed PATH).
- **Codex farm had only 1 loss** (`rs-duration-parse #1`, `supervisor-reject`).

### 2.2 Per task (v0.8.8 farm)

| Task | Claude farm | Codex farm |
| --- | --- | --- |
| `py-bounded-full` | 3 of 3 | 3 of 3 |
| `py-inventory-remove` | 3 of 3 | 3 of 3 |
| `py-money-parse` | 3 of 3 | 3 of 3 |
| `py-settings-refactor` | 3 of 3 | 3 of 3 |
| `py-slug-ends` | 3 of 3 | 3 of 3 |
| `py-validate-port` | 2 of 3 (1 infra) | 3 of 3 |
| `rs-config-refactor` | 2 of 3 (1 infra) | 3 of 3 |
| `rs-duration-parse` | 3 of 3 | 2 of 3 |
| `rs-percent-zero` | 3 of 3 | 3 of 3 |
| `rs-ring-overwrite` | 3 of 3 | 3 of 3 |
| `rs-stack-pop-n` | 3 of 3 | 3 of 3 |
| `rs-triangle` | 3 of 3 | 3 of 3 |
