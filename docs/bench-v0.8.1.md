# Openfarm v0.8.1 Empirical Benchmark & Gate Calibration

Date: 17 and 18 Nov 2026

This page has three parts. Section 1 is the bench on the final v0.8.1 code. Section 2 is what the live runs on the fixed code measured: the checker with a planted-bug set, and single cards with Codex supervising and with `openfarm drive`. Section 3 keeps the first agent's 12-task bench as history: it ran before the fix work, and section 3.1 says what has changed since.

Cost is reported as token shares (worker, supervisor, coordinator), not dollars. On a subscription a supervisor's API list price says nothing about what it costs.

## 1. Bench on the Final Code

All 12 tasks, one repeat per arm, two frontier providers, two rounds. One repeat per task is a small sample: read the tables as "which cards passed and why the others did not", not as rates.

- **Workers in the farm arms:** DeepSeek V4.1 Flash on Command Code with `x-cmd-zdr: 1`.
- **Codex arms:** codex-cli 0.166.0 through openfarm's bridge (`binary = "codex-exec"`), effort medium, tool policy `none`, inside the boundary.
- **Claude arms:** Claude Code with Opus 5.5 through openfarm's bridge, effort medium, tool policy `none` (not confined, as designed under `confine = "auto"`).
- **farm** is the worker model with the frontier model supervising. **solo** is the frontier model doing every worker role itself through the same briefs and gates, with no supervisor.

### 1.1 Round 2: the Fixed Code (tree `04e58a3`, 18 Nov 2026)

| Arm | Passed | Tokens | Tokens per pass | Token share | Wall time |
| --- | --- | --- | --- | --- | --- |
| Codex farm | 12 of 12 | 818,547 | 68,212 | worker 28.5%, supervisor 71.5% | 22 min |
| Codex solo | 12 of 12 | 669,444 | 55,787 | all Codex | 9 min |
| Claude farm | 11 of 12 | 491,595 | 44,690 | worker 38.7%, supervisor 61.3% | 31 min |
| Claude solo | 12 of 12 | 383,779 | 31,982 | all Claude | 10 min |

| Task | Codex farm | Codex solo | Claude farm | Claude solo |
| --- | --- | --- | --- | --- |
| `py-bounded-full` | pass | pass | pass | pass |
| `py-inventory-remove` | pass | pass | pass | pass |
| `py-money-parse` | pass | pass | pass | pass |
| `py-settings-refactor` | pass | pass | pass | pass |
| `py-slug-ends` | pass | pass | pass | pass |
| `py-validate-port` | pass | pass | pass | pass |
| `rs-config-refactor` | pass | pass | pass | pass |
| `rs-duration-parse` | pass | pass | pass | pass |
| `rs-percent-zero` | pass | pass | pass | pass |
| `rs-ring-overwrite` | pass | pass | pass | pass |
| `rs-stack-pop-n` | pass | pass | pass | pass |
| `rs-triangle` | pass | pass | **tests-unprovable** | pass |

- **What the farm saves here.** With a frontier supervisor on every card, the farm arm used more tokens in total than the frontier model working alone, and passed no more cards. What it moved off the frontier model is smaller than the totals suggest: the Codex supervisor used 584,910 tokens against 669,444 for Codex alone (13% fewer), and the Claude supervisor 301,584 against 383,779 (21% fewer). On tasks this small the supervisor's fixed work per card (shape, scaffold, review brief) is most of the cost. Whether lighter supervision or larger cards change that has not been measured.
- **Six Claude tasks were run twice.** Claude Code updated itself on the test machine twice during the Claude pair, and the `claude` program is missing for a moment while it does. Five tasks (`py-bounded-full`, `rs-percent-zero`, `rs-ring-overwrite`, `rs-stack-pop-n`, `rs-triangle`) stopped as `infra` ("Failed to spawn claude") in both arms; they were run again with a fixed copy of the program, and the table shows those runs. `rs-config-refactor` in the farm arm first stopped as `tests-unprovable` with a supervisor turn that timed out during the second update; it was run again and passed, and the table shows the second run.
- **The one loss, and one more fix.** In `rs-triangle` (and in the first `rs-config-refactor` run) the Claude supervisor's scaffold was refused as "not stubs only", so the new symbol did not exist, the test could not build, and the card stopped. Both scaffolds were stubs: one kept its parameters used with `let _ = (a, b, c);` before `todo!()`, which a lint run with warnings as errors needs, and the other was anchored on the closing lines of the `impl` above it. The scaffold rule now takes both. In the first `rs-config-refactor` run the test writer also imported a function in `src/config.rs` from the crate root twice; the brief now names the crate's public modules, and the build-failure message carries the compiler's own label and hint.
- **Checked live after that fix** (tree `442c23d`, the six Rust tasks, Claude farm arm only): 6 of 6 passed, 270,365 tokens (worker 36.6%, supervisor 63.4%), and the supervisor's scaffold was applied on all four cards that had one. The Python tasks and the other three arms were not run again on that tree.

### 1.2 Round 1: Before the Test-Writer Fixes (tree `026274d`, 17 Nov 2026)

| Arm | Passed | Tokens | Tokens per pass | Token share | Wall time |
| --- | --- | --- | --- | --- | --- |
| Codex farm | 7 of 12 | 754,725 | 107,818 | worker 30.9%, supervisor 69.1% | 22 min |
| Codex solo | 12 of 12 | 747,530 | 62,294 | all Codex | 11 min |
| Claude farm | 8 of 12 | 573,685 | 71,711 | worker 44.7%, supervisor 55.3% | 26 min |
| Claude solo | 5 of 12 | 257,791 | 51,558 | all Claude | 7 min |

| Task | Codex farm | Codex solo | Claude farm | Claude solo |
| --- | --- | --- | --- | --- |
| `py-bounded-full` | pass | pass | pass | pass |
| `py-inventory-remove` | pass | pass | pass | **refused** |
| `py-money-parse` | **supervisor-reject** | pass | pass | pass |
| `py-settings-refactor` | pass | pass | pass | pass |
| `py-slug-ends` | pass | pass | pass | pass |
| `py-validate-port` | pass | pass | **supervisor-reject** | pass |
| `rs-config-refactor` | **tests-unprovable** | pass | **attempts** | **refused** |
| `rs-duration-parse` | **no-progress** | pass | **cannot-split** | **refused** |
| `rs-percent-zero` | pass | pass | pass | **refused** |
| `rs-ring-overwrite` | pass | pass | pass | **refused** |
| `rs-stack-pop-n` | **no-progress** | pass | **tests-unprovable** | **refused** |
| `rs-triangle` | **attempts** | pass | pass | **refused** |

Most of these losses had the same few causes, and three of the Rust tasks had been lost the same way in the first agent's bench (section 3):

- **The Rust test writer did not know the crate's name.** Shown only signatures, it pulled the source file in with `#[path = "../src/x.rs"] mod x;`. The copy's unused items then failed the lint gate, which the implementer may not fix, because tests are frozen (`no-progress`, `attempts`). Claude working alone silenced them with `#[allow(...)]` instead, which is a banned pattern (six `refused`).
- **A failed test build gave no reason.** The message said only "not reported by the test binary", so the next try was a guess. In `rs-stack-pop-n` a supervisor with no tools blamed a corrupted `Cargo.toml` that did not exist.
- **A stub the pack could not take ended the card.** `py-inventory-remove` (Claude solo) gave an indented method stub, which is appended at the top level; `rs-config-refactor` (Codex farm) declared a symbol twice.
- **Two supervisor rejections** (`py-money-parse`, `py-validate-port`) were the supervisor's own judgement on the workers' code, one of them over a 4,301-digit input.

The fixes between the rounds: the Rust brief names the library crate and forbids `#[path]`; the unprovable-tests message carries the compiler's errors; the stub note says where a stub lands and not to declare a shown symbol again; a refused stub is sent back to the test writer instead of ending the card.

## 2. Live Runs on the Fixed Code (17 Nov 2026)

Workers in every run below were DeepSeek V4.1 Flash on Command Code with `x-cmd-zdr: 1`.

### 2.1 Counter-Test Gate Calibration

The set-up is the 12 bench tasks twice: once with a planted bug that passes the card's named tests but breaks its Done text, and once clean. The test writer and implementer answer from each task's script; both checker passes go to the live worker model. Report mode (`counter_tests = "report"`). No supervisor or coordinator, so every token is a worker token.

| Run | Tree | Second pass readable | Planted bugs caught | Counter-tests on the bug set | Alarms on clean cards | Wrong counter-tests | Worker tokens (bug / clean set) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Before the checker fix | `6610f88` | No answer form; counter-tests read in 0 of 24 | 0 of 12 | 0 written | — | — | 128,928 / 166,834 |
| After the fix | `e9f3b15` | 23 of 24 first ask, 1 on the second | 12 of 12 | 19 written: 18 failed, 1 dropped, 0 passed | 3 of 12 (4 counter-tests) | 0 of 23 run | 119,499 / 128,141 |
| **Final code** | **`bb40935`** | **24 of 24 first ask** | **12 of 12** | **21 written: 21 failed, 0 dropped, 0 passed** | **2 of 12** | **0 of 23 run** | **129,092 / 125,255** |

- **Before the fix**, the checker's second pass often named the bug in prose, but the brief never said what form to answer in and the parser read only a JSON list or `COUNTER_TEST:` blocks. No counter-test was ever read, so the gate never ran.
- **The alarms on clean cards are real.** Each was checked against the card's Done text, and each is a gap in the bench's own reference solution that the hidden tests do not cover. On the final code: `py-money-parse` accepts `"12.34\n"` (the regex's `$` matches before a final newline) though "anything else raises MoneyError"; `rs-duration-parse` accepts a leading `+` (`u64::from_str` takes one) though the text says digits only. The run on `e9f3b15` also flagged `py-validate-port` (`"9" * 5000` raises Python's own `ValueError`, not `ValidationError`). Under `counter_tests = "reject"` these cards would have bounced to the implementer for a real reason.
- **First pass on the final code:** the new answer form was followed in 24 of 24, none asked again. Asked whether the tests would catch a bad implementation, it said "partly" 18 times and "no" 6 times, and every answer listed what the tests miss.
- **Not measured:** `counter_tests = "reject"` end to end with a live implementer fixing the bounce, and other checker models.

### 2.2 Single Cards on the Fixed Code

| Run | Tree | Task | Outcome | Hidden tests | Tokens | Worker / supervisor and review / coordinator |
| --- | --- | --- | --- | --- | --- | --- |
| `farm` arm, Codex supervising through openfarm's bridge (`binary = "codex-exec"`, tool policy `none`, flags + boundary) | `f444294` | `py-inventory-remove` | `in-review`, 79 s | **pass** | 63,735 | 20.0% / 80.0% / 0% |
| `openfarm drive --agent codex --seat both --cards 1 --wave --record-verdict` (flags + boundary) | `bb40935` | `py-inventory-remove` | `in-review`, 6 turns, wave verdict `minor-fix` by `driver/codex` | **pass** | 96,263 | 15.5% / 72.5% / 12.0% |
| `openfarm drive --agent claude` (Opus 5.5), same flags (not confined, as designed under `auto`) | `bb40935` | `py-inventory-remove` | `in-review`, 7 turns (one answer refused and handed to fallback), wave verdict `minor-fix` by `driver/claude` | **pass** | 68,443 | 23.0% / 70.5% / 6.4% |

The drive rows count the driver's turns. Codex reports no cost, so the drive's notes said the spend caps could not fire for the driver.

## 3. First Pass, Before the Fix Work (history)

Branch `v0.8` (`dd090d0` during the first agent's live QA), 17 Nov 2026.

### 3.1 What Has Changed Since

These results stand as "the card passed" or "the card did not pass" on the code of the time. Since then:

- **The checker's counter-tests were never read** in these runs (see 2.1), so the checker gate ran nothing. The "0 false rejects across 36 live card records" that used to stand in this page measured nothing and is withdrawn.
- **The Codex and Antigravity supervisors and drivers ran with no lock-down.** Their child could read any file the user could. That provider path is now confined (section 5 of the [usage guide](USAGE-AND-FIRST-DEPLOYMENT.md#5-frontier-providers-claude-codex-and-antigravity)).
- **The Python pack ran ruff whenever it was installed**, and the TypeScript, mutation and row rules have changed (see the [build log](v0.8.1-build-log.md#the-fix-work)).
- **Token figures** for Claude supervisors through the standalone `claude-agent-acp` were understated (cached input was not counted), and a driven run's own token split left the driver out. Both are fixed. The Codex numbers below are not affected by the first.
- The Antigravity rows here (`acp-agy`, `drive-agy`) are the only live Antigravity evidence. Every Antigravity change since is untested live.

### 3.2 Setup

All 12 shipped benchmark tasks in [`bench/`](../bench/) (`6` Python, `6` Rust; across `pinned-bugfix`, `small-feature`, `pattern`, and `refactor`) were run once per arm (`--repeats 1`, `24` runs, `2,272.4s` elapsed), each with its own throwaway global harness library (`OPENFARM_GLOBAL_HARNESS`):

- **`farm` arm (`farm-codex.toml`):** `model = "openai-compatible"` against Command Code with `headers = { "x-cmd-zdr" = "1" }` (`deepseek/deepseek-v4.1-flash`, `test_writer` temp `0.2`, `implementer`/`planner`/`checker` with `vary_seed = true`), supervised by **`codex-acp`** (`auth = "oauth"`, `mode = "active"`, `effort = "medium"`, `tool_policy = "none"`, `shape_cards = true`, `scaffold_contracts = true`, `coach_on_stall = true`, `adjudicate_counter_tests = true`, `pre_review = true`).
- **`solo` arm (`solo-codex.toml`):** `model = "codex-acp"` (`auth = "oauth"`, `effort = "medium"`, `isolate_home = true`), `micromanager = "off"` (Codex does every worker role itself through the same briefs, edit blocks, and gates).

Codex rather than Claude anchored both arms because the Claude account had reached its weekly quota during the first agent's runs. A single Antigravity farm card (`acp-agy`) ran apart because the Antigravity quota was nearly used up.

> [!NOTE]
> Sample size: only one of the planned three repeats ran, because the coordinator model's quota ran out. Every pass rate below is an `N = 1` observation per task and arm.

### 3.3 Headline Comparison (`12 Tasks x 1 Repeat`)

| Arm | Runs | Passed hidden tests | Pass rate | Total tokens | Tokens / pass | Worker share |
| --- | --- | --- | --- | --- | --- | --- |
| **`farm`** (`deepseek-v4.1-flash` + `codex-acp`) | 12 | 9 | **75.0%** | 968,891 | 107,655 | 28.6% (`277,545` worker / `691,346` supervisor) |
| **`solo`** (`codex-acp` only, no supervisor) | 12 | 9 | **75.0%** | 840,089 | 93,343 | 100.0% |

#### By Language

| Language | `farm` passed | `farm` tokens (worker / supervisor) | `solo` passed | `solo` tokens |
| --- | --- | --- | --- | --- |
| **Python (`6` tasks)** | **6 / 6** | 491,446 (`124,348` / `367,098`) | 5 / 6 | 393,411 |
| **Rust (`6` tasks)** | 3 / 6 | 477,445 (`153,197` / `324,248`) | **4 / 6** | 446,678 |

#### By Category

| Category | `farm` | `solo` |
| --- | --- | --- |
| `pinned-bugfix` (4 tasks) | 4/4 | 4/4 |
| `small-feature` (4 tasks) | **3/4** | 2/4 |
| `refactor` (2 tasks) | 1/2 | **2/2** |
| `pattern` (2 tasks) | 1/2 | 1/2 |

### 3.4 Per-Task Results (`bench-r1/records.jsonl`)

| Task | Lang | Category | `farm` Result | `farm` Tokens (W / S) | `farm` Time | `solo` Result | `solo` Tokens | `solo` Time |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `py-bounded-full` | python | `pinned-bugfix` | **pass** (`in-review`) | 62,054 (`12,740` / `49,314`) | 85.7s | **pass** (`in-review`) | 62,468 | 31.7s |
| `py-inventory-remove` | python | `small-feature` | **pass** (`in-review`) | 83,678 (`17,437` / `66,241`) | 111.9s | fail (`blocked` / `refused`) | 31,219 | 16.4s |
| `py-money-parse` | python | `small-feature` | **pass** (`in-review`) | 122,063 (`51,192` / `70,871`) | 306.4s | **pass** (`in-review`) | 79,034 | 57.0s |
| `py-settings-refactor` | python | `refactor` | **pass** (`in-review`) | 80,964 (`14,516` / `66,448`) | 103.1s | **pass** (`in-review`) | 79,897 | 51.4s |
| `py-slug-ends` | python | `pinned-bugfix` | **pass** (`in-review`) | 62,425 (`13,336` / `49,089`) | 102.9s | **pass** (`in-review`) | 61,076 | 33.2s |
| `py-validate-port` | python | `pattern` | **pass** (`in-review`) | 80,262 (`15,127` / `65,135`) | 109.2s | **pass** (`in-review`) | 79,717 | 48.2s |
| `rs-config-refactor` | rust | `refactor` | fail (`blocked` / `attempts`) | 68,633 (`20,579` / `48,054`) | 103.5s | **pass** (`in-review`) | 98,239 | 77.4s |
| `rs-duration-parse` | rust | `small-feature` | fail (`blocked` / `no-progress`) | 72,999 (`26,197` / `46,802`) | 159.3s | fail (`blocked` / `no-progress`) | 80,605 | 57.7s |
| `rs-percent-zero` | rust | `pinned-bugfix` | **pass** (`in-review`) | 68,573 (`19,637` / `48,936`) | 130.7s | **pass** (`in-review`) | 62,184 | 33.1s |
| `rs-ring-overwrite` | rust | `pinned-bugfix` | **pass** (`in-review`) | 71,434 (`21,018` / `50,416`) | 137.4s | **pass** (`in-review`) | 78,117 | 39.1s |
| `rs-stack-pop-n` | rust | `small-feature` | **pass** (`in-review`) | 87,536 (`20,673` / `66,863`) | 127.6s | **pass** (`in-review`) | 78,887 | 45.7s |
| `rs-triangle` | rust | `pattern` | fail (`blocked` / `no-progress`) | 108,270 (`45,093` / `63,177`) | 249.0s | fail (`blocked` / `handed-back`) | 48,646 | 51.3s |

### 3.5 Single-Task ACP & Driver Runs (first agent)

| Run | Mode / Provider | Task | Outcome | Hidden tests | Tokens | Worker share |
| --- | --- | --- | --- | --- | --- | --- |
| `acp-codex` | `farm` (`codex-acp` supervisor + DeepSeek) | `rs-percent-zero` | `in-review` | **pass** | 93,333 (`13,420` worker / `79,913` supervisor) | 14.4% |
| `acp-codex` | `solo` (`codex-acp` workers) | `rs-percent-zero` | `in-review` | **pass** | 61,586 | 100% |
| `acp-agy` | `farm` (`antigravity-acp` effort low + DeepSeek) | `rs-percent-zero` | `in-review` | **pass** | 34,796 (`18,052` worker / `16,744` supervisor) | 51.9% |
| `drive-codex` | `openfarm drive --agent codex --seat both --wave --record-verdict` | `py-inventory-remove` | `in-review`, wave verdict `clean` | **pass** | Worker `21,190`; driver `118,767` (6 turns) | 15.1% |
| `drive-agy` | `openfarm drive --agent antigravity --effort low --seat both` | `py-inventory-remove` | `in-review` | **pass** | Driver `95,885` (6 turns); worker not recorded | — |

The two drive rows are figured from the driver's own turn counts; the run's own split at the time left the driver out.
