# openfarm v0.8.7 Bench: 3 Repeats, Farm Against Solo

Date: 19 Nov 2026

This is the full 3-repeat bench the v0.8.6 roadmap asked for: all 12 tasks in [`bench/`](../bench/), 3 repeats per arm, Claude and Codex, farm and solo (144 runs total). It measures the v0.8.6 pre-review rule on **both** Claude and Codex, together with the v0.8.7 bench-card Done-text clarifications on `py-money-parse` and `py-validate-port`.

Cost is given as token counts and shares, never dollars.

## 1. Setup

- **Tree.** v0.8.7 branch (`8a183db` for Claude farm/solo and Codex farm; `ad43a67` for Codex solo after fixing the symlinked-`TMPDIR` worker `cwd` bug it surfaced).
- **Arms.** Same arms as [`docs/bench-v0.8.5.md`](bench-v0.8.5.md):
  - **farm:** DeepSeek V4.1 Flash workers on Command Code with `x-cmd-zdr: 1` (`effort = "high"`, `retry_effort = "max"`), with the frontier model supervising (effort medium, tool policy `none`).
  - **solo:** the frontier model doing every worker role itself through the same briefs and gates, with no supervisor.
- **Claude:** Opus 5.5 through openfarm's bridge (`DISABLE_AUTOUPDATER=1`).
- **Codex:** codex-cli through openfarm's bridge (`binary = "codex-exec"`), inside the bubblewrap boundary.
- **Runs.** 72 per provider, 144 in all. No run stopped as `infra` on the fixed tree.

## 2. Results

| Provider | Arm | Passed | Tokens | Tokens per run | Tokens per pass | Token share | Wall time |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Claude | farm | **35 of 36** | 920,054 | 25,557 | 26,287 | worker 65.3%, supervisor 34.7% | 53 min |
| Claude | solo | **34 of 36** | 511,165 | 14,199 | 15,034 | all Claude | 26 min |
| Codex | farm | **34 of 36** | 1,127,597 | 31,322 | 33,165 | worker 55.6%, supervisor 44.4% | 53 min |
| Codex | solo | **36 of 36** | 926,065 | 25,724 | 25,724 | all Codex | 30 min |

### 2.1 Against v0.8.5

| Provider | Arm | v0.8.5 passed | v0.8.7 passed | v0.8.5 tokens / pass | v0.8.7 tokens / pass | v0.8.5 frontier cut vs solo | v0.8.7 frontier cut vs solo |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Claude | farm | 34 of 36 | **35 of 36** | 29,914 | **26,287 (−12%)** | 33% fewer | **37.5% fewer** (319,232 vs 511,165) |
| Codex | farm | 27 of 36 | **34 of 36** | 60,873 | **33,165 (−46%)** | 32% fewer | **45.9% fewer** (501,119 vs 926,065) |

- **Pre-review rule validated on both providers.** Zero false pre-review bounces across all 72 farm runs (36 Claude, 36 Codex). Claude farm beat Claude solo on pass rate (35/36 vs 34/36) with zero `supervisor-reject` losses.
- **Codex farm jumped from 27/36 to 34/36.** Tokens per pass fell by 46% (60,873 → 33,165) and wall time fell by 58% (127 min → 53 min).
- **Frontier tokens moved off the frontier model.** The farm supervisor used **37.5% fewer** frontier tokens than Claude solo and **45.9% fewer** than Codex solo.
- **Solo is still cheaper in total tokens on these small cards.** Breaking down the v0.8.7 farm runs by role shows why: `test_writer` + `implementer` combined cost only ~5.1k–5.4k worker tokens/card, while the two-pass checker (`checker_pass1` + `checker_pass2`) spends ~10.7k–12.0k worker tokens/card and unconditional supervisor calls (`shape_and_scaffold` + `pre_review`) average 2.07–2.25 calls/card. That is the target for v0.8.8.

### 2.2 Per task

| Task | Claude farm | Claude solo | Codex farm | Codex solo |
| --- | --- | --- | --- | --- |
| `py-bounded-full` | 3 of 3 | 3 of 3 | 3 of 3 | 3 of 3 |
| `py-inventory-remove` | 3 of 3 | 2 of 3 | 3 of 3 | 3 of 3 |
| `py-money-parse` | 3 of 3 | 3 of 3 | 2 of 3 | 3 of 3 |
| `py-settings-refactor` | 3 of 3 | 2 of 3 | 3 of 3 | 3 of 3 |
| `py-slug-ends` | 3 of 3 | 3 of 3 | 3 of 3 | 3 of 3 |
| `py-validate-port` | 3 of 3 | 3 of 3 | 3 of 3 | 3 of 3 |
| `rs-config-refactor` | 3 of 3 | 3 of 3 | 3 of 3 | 3 of 3 |
| `rs-duration-parse` | 3 of 3 | 3 of 3 | 2 of 3 | 3 of 3 |
| `rs-percent-zero` | 3 of 3 | 3 of 3 | 3 of 3 | 3 of 3 |
| `rs-ring-overwrite` | 3 of 3 | 3 of 3 | 3 of 3 | 3 of 3 |
| `rs-stack-pop-n` | 3 of 3 | 3 of 3 | 3 of 3 | 3 of 3 |
| `rs-triangle` | 2 of 3 | 3 of 3 | 3 of 3 | 3 of 3 |

### 2.3 Every loss

| Run | Stop code | Cause |
| --- | --- | --- |
| Claude farm `rs-triangle` #3 | `no-progress` | Clippy flagged the frozen `tests/triangle.rs` (`assert_eq!(triangle.is_some(), true)`) once the implementation compiled; the implementer could not edit the frozen test. |
| Claude solo `py-inventory-remove` #3 | `refused` | Banned pattern `# noqa` (`suppression-noqa`) introduced in `tests/test_remove.py`. |
| Claude solo `py-settings-refactor` #3 | `in-review` | Reached `in-review` but failed the hidden acceptance tests. |
| Codex farm `py-money-parse` #2 | `supervisor-reject` | Backed by 1 failing counter-test quoting the Done line (`Anything else raises MoneyError...`). Passed hidden tests on branch. |
| Codex farm `rs-duration-parse` #1 | `supervisor-reject` | Backed by 2 failing counter-tests quoting the Done line (`Anything else is an Err(UnitError(..))...`). Passed hidden tests on branch. |

## 3. What This Says

- **The v0.8.6 pre-review rule holds on both Claude and Codex.** Across 72 live farm runs, there were zero false pre-review bounces (0 `supervisor-reject` losses on Claude; the 2 on Codex both quoted a Done clause and were backed by failing counter-tests). This clears the v0.8.6 roadmap gate for promoting `counter_tests = "reject"` to default in v0.9.
- **Total-token gap on small cards is now localized.** Writing tests and code is already ~5.1k–5.4k tokens/card; the fixed overhead is in the two-pass checker and unconditional supervisor calls, which are scheduled for v0.8.8.
