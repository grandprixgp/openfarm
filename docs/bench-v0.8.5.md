# openfarm v0.8.5 Bench: 3 Repeats, Farm Against Solo

Date: 18 Nov 2026

This is the full bench the v0.8.5 roadmap asked for: all 12 tasks in [`bench/`](../bench/), 3 repeats per arm, Claude and Codex, farm and solo. It measures v0.8.5's lighter supervision against the frontier model working alone. Section 3 is a follow-up on v0.8.6 for the Codex losses.

Cost is given as token counts and shares, never dollars.

## 1. Setup

- **Tree.** v0.8.5, `f25de74`.
- **Arms.** The v0.8.1 arms in [`bench/arms/`](../bench/arms/), plus the worker effort rule (`effort = "high"`, `retry_effort = "max"`).
  - **farm:** DeepSeek V4.1 Flash workers on Command Code with `x-cmd-zdr: 1`, with the frontier model supervising (effort medium, tool policy `none`).
  - **solo:** the frontier model doing every worker role itself through the same briefs and gates, with no supervisor.
- **Claude:** Opus 5.5 through openfarm's bridge.
- **Codex:** codex-cli 0.166.0 through openfarm's bridge (`binary = "codex-exec"`), inside the boundary.
- **Runs.** 72 per provider, 144 in all. Claude Code was kept from updating itself during the runs (`DISABLE_AUTOUPDATER=1`), so no run stopped as `infra`.

## 2. Results

| Provider | Arm | Passed | Tokens | Tokens per run | Tokens per pass | Token share | Wall time |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Claude | farm | **34 of 36** | 1,017,087 | 28,252 | 29,914 | worker 68.3%, supervisor 31.7% | 73 min |
| Claude | solo | **36 of 36** | 478,707 | 13,297 | 13,297 | all Claude | 27 min |
| Codex | farm | **27 of 36** | 1,643,589 | 45,655 | 60,873 | worker 61.1%, supervisor 38.9% | 127 min |
| Codex | solo | **36 of 36** | 934,587 | 25,960 | 25,960 | all Codex | 72 min |

### 2.1 Against v0.8.1

v0.8.1 ran one repeat, so compare tokens per run.

| Provider | Arm | v0.8.1 tokens per run | v0.8.5 tokens per run | v0.8.1 worker share | v0.8.5 worker share |
| --- | --- | --- | --- | --- | --- |
| Claude | farm | 40,966 | 28,252 (−31%) | 38.7% | **68.3%** |
| Claude | solo | 31,982 | 13,297 (−58%) | | |
| Codex | farm | 68,212 | 45,655 (−33%) | 28.5% | **61.1%** |
| Codex | solo | 55,787 | 25,960 (−53%) | | |

- **Supervisor tokens fell much more than worker tokens.** The farm's supervisor used 322,855 tokens with Claude (8,968 per run, against 25,132 on v0.8.1) and 639,874 with Codex (17,774 per run, against 48,743). That is about a third of v0.8.1's, which is what v0.8.5's lean prompts, compact payloads and combined calls were for. The worker became most of every farm run.
- **Frontier tokens moved off the frontier model.** Each farm supervisor used about a third fewer tokens than the same model working alone: Claude 322,855 against 478,707 (33% fewer, against 21% on v0.8.1), and Codex 639,874 against 934,587 (32% fewer, against 13%).
- **The solo arms got cheaper too.** They use the same shorter briefs. So in total tokens, solo is still the cheaper arm by about half. On tasks this small, the farm's fixed work per card (worker briefs, the checker's two passes) is most of what it spends.

### 2.2 Per task

| Task | Claude farm | Claude solo | Codex farm | Codex solo |
| --- | --- | --- | --- | --- |
| `py-bounded-full` | 3 of 3 | 3 of 3 | 2 of 3 | 3 of 3 |
| `py-inventory-remove` | 3 of 3 | 3 of 3 | 3 of 3 | 3 of 3 |
| `py-money-parse` | 3 of 3 | 3 of 3 | 1 of 3 | 3 of 3 |
| `py-settings-refactor` | 3 of 3 | 3 of 3 | 3 of 3 | 3 of 3 |
| `py-slug-ends` | 3 of 3 | 3 of 3 | 3 of 3 | 3 of 3 |
| `py-validate-port` | 2 of 3 | 3 of 3 | 2 of 3 | 3 of 3 |
| `rs-config-refactor` | 3 of 3 | 3 of 3 | 3 of 3 | 3 of 3 |
| `rs-duration-parse` | 3 of 3 | 3 of 3 | 3 of 3 | 3 of 3 |
| `rs-percent-zero` | 3 of 3 | 3 of 3 | 3 of 3 | 3 of 3 |
| `rs-ring-overwrite` | 3 of 3 | 3 of 3 | 1 of 3 | 3 of 3 |
| `rs-stack-pop-n` | 3 of 3 | 3 of 3 | 3 of 3 | 3 of 3 |
| `rs-triangle` | 2 of 3 | 3 of 3 | **0 of 3** | 3 of 3 |

### 2.3 Every farm loss

| Run | Stop code | Hidden tests on the branch | Cause |
| --- | --- | --- | --- |
| Claude `rs-triangle` #2 | `no-progress` | (did not finish) | The test writer's `assert_eq!(x, true)` failed clippy after the tests froze. Same cause as the pilot's card F1. Fixed on v0.8.6: tests are linted before they freeze. |
| Claude `py-validate-port` #3 | `supervisor-reject` | | A real gap: 700 digits pass the digit check and `int()` raises Python's own `ValueError`, not `ValidationError`. The bench's reference solution has the same gap (noted in [`bench-v0.8.1.md`](bench-v0.8.1.md#21-counter-test-gate-calibration)). |
| Codex, 9 runs | `supervisor-reject` | **pass, 9 of 9** | The supervisor bounced correct code: thin tests in 3 runs (`py-bounded-full` #1, `rs-ring-overwrite` #2 and #3), which the implementer cannot change; robustness the Done text does not ask for in 6 (`rs-triangle` ×3 on floating-point edge cases, `py-money-parse` ×2 and `py-validate-port` #2 on 4,300-digit inputs). |

## 3. Follow-up on v0.8.6: a Rule for the Pre-Review

Until v0.8.6 the pre-review gave the supervisor only the answer schema, with no rule for when to bounce. The tree for this follow-up is `2354994`. Its rule says:

> Bounce only when the diff misses or breaks a line of the card's Done text, shown by a failing counter-test or by the code itself, and quote that line in `missed_clause`. The tests are frozen and the implementer cannot change them, so thin tests are never a reason to bounce. A concern the Done text does not state goes in `findings` with verdict `approve`.

I re-ran the Codex farm arm on the five tasks it had lost, 3 repeats each:

| Task | v0.8.5 | With the rule |
| --- | --- | --- |
| `py-bounded-full` | 2 of 3 | **3 of 3** |
| `py-money-parse` | 1 of 3 | 1 of 3 |
| `py-validate-port` | 2 of 3 | 2 of 3 |
| `rs-ring-overwrite` | 1 of 3 | **3 of 3** |
| `rs-triangle` | 0 of 3 | **3 of 3** |
| All five | 6 of 15 | **12 of 15**, 706,660 tokens, worker 64.9% |

- **What the remaining three bounces were.** Each now quotes a Done line ("one or more digits", "or raise `ValidationError`") and points at a real gap against it: an input past Python's 4,300-digit `int()` limit raises `ValueError`. The hidden tests do not cover that input, and the bench's own reference solutions share the gap. By the letter of each card these are true rejects, of a kind the cards never meant to ask for. The fix belongs in those two cards' Done text, not in openfarm.
- **Not measured:** the rule with Claude supervising, and a full 3-repeat bench on v0.8.6.

## 4. What This Says

- **v0.8.5's lighter supervision did what it set out to do.** Supervisor tokens per run fell to about a third of v0.8.1's on both providers. The worker's share of a farm run rose from 29–39% to 61–68%. The farm moved a third of the frontier model's tokens off it.
- **On cards this small the farm is still not cheaper in total.** Solo spent about half the farm's tokens, and the frontier model alone passed 72 of 72.
- **Most farm losses were the supervisor's judgement, not the workers' code.** 11 of the 13 losses had code that passed the hidden tests. With the v0.8.6 rule, the Codex losses fell from 9 to 3, and the three left are true by the card's text.
- **Caveats.** Three repeats per task is still a small sample. The 12 tasks are small, single-file cards.
