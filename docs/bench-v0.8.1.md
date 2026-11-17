# Openfarm v0.8.1 Empirical Benchmark & Gate Calibration

Date: 17 Nov 2026
Branch: `v0.8` (`dd090d0` during live QA)

## 1. Benchmark Setup

All 12 shipped benchmark tasks in [`bench/`](../bench/) (`6` Python, `6` Rust; across `pinned-bugfix`, `small-feature`, `pattern`, and `refactor`) were executed once per arm (`--repeats 1`, `24` runs total, `2,272.4s` elapsed) using isolated per-run temporary global harness libraries (`OPENFARM_GLOBAL_HARNESS`) inside disposable workspaces under `/var/tmp/openfarm-v081-live/bench-r1`:

- **`farm` arm (`farm-codex.toml`):** `model = "openai-compatible"` against `https://api.commandcode.ai/provider/v1` with `headers = { "x-cmd-zdr" = "1" }` (`deepseek/deepseek-v4.1-flash`, `test_writer` temp `0.2`, `implementer`/`planner`/`checker` with `vary_seed = true`), supervised by **`codex-acp`** (`auth = "oauth"`, `mode = "active"`, `effort = "medium"`, `tool_policy = "none"`, `shape_cards = true`, `scaffold_contracts = true`, `coach_on_stall = true`, `adjudicate_counter_tests = true`, `pre_review = true`, list-price tracking `cost_per_1k_in = 0.003`, `cost_per_1k_out = 0.015`).
- **`solo` arm (`solo-codex.toml`):** `model = "codex-acp"` (`auth = "oauth"`, `effort = "medium"`, `isolate_home = true`), `micromanager = "off"` (Codex executes every worker role directly through the same briefs, edit blocks, and deterministic gates).

Why Codex rather than Claude for the 12-task run: `openfarm acp status` and `openfarm acp probe` verified local Claude OAuth readiness, while a live `DriverAgent(agent="claude")` turn encountered Anthropic's weekly account quota cap (`resets Nov 18, 10pm`), so Codex CLI OAuth (`codex-native-acp`) anchored both Frontier supervision (`farm`) and unassisted baseline (`solo`). Single-task `antigravity-acp` verification (`farm-agy`, `effort = "low"`, `max_calls_per_card = 2`) was also measured separately on `rs-percent-zero` (`34,796` tokens, passed hidden tests) due to shared Antigravity Gemini quota (`<= 2%` remaining).

> [!NOTE]
> Sample size caveat: Because Pass 1 across all 12 tasks took `37.9` minutes and coordinator model quota reached `0%`, Pass 2 and Pass 3 (`3` total repeats) were not run. All pass rates below are exact `N = 1` per task/arm observations without extrapolation.

---

## 2. Headline Comparison (`12 Tasks x 1 Repeat`)

| Arm | Runs | Passed Hidden Tests | Pass Rate | Total Tokens | Tokens / Pass | Worker Share | API List Spend* | Cost / Pass* |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **`farm`** (`deepseek-v4.1-flash` + `codex-acp`) | 12 | 9 | **75.0%** | 968,891 | 107,655 | 28.6% (`277,545` worker / `691,346` sup) | \$2.4305 | \$0.2701 |
| **`solo`** (`codex-acp` only, no supervisor) | 12 | 9 | **75.0%** | 840,089 | 93,343 | 100.0% | \$0.0000 | \$0.0000 |

\*`spend` reflects configured API list rates (`cost_per_1k_in` / `cost_per_1k_out`); Codex CLI runs over an interactive OAuth subscription (`$0.00` vendor charge reported by `codex exec`), while `farm` configured nominal `$0.003/$0.015` proxy rates on the supervisor for accounting plus real Command Code worker rates.

### By Language

| Language | `farm` Passed | `farm` Pass Rate | `farm` Tokens (Worker / Sup) | `solo` Passed | `solo` Pass Rate | `solo` Tokens |
| --- | --- | --- | --- | --- | --- | --- |
| **Python (`6` tasks)** | **6 / 6** | **100.0%** | 491,446 (`124,348` / `367,098`) | 5 / 6 | 83.3% | 393,411 |
| **Rust (`6` tasks)** | 3 / 6 | 50.0% | 477,445 (`153,197` / `324,248`) | **4 / 6** | **66.7%** | 446,678 |

### By Category

| Category | `farm` | `solo` |
| --- | --- | --- |
| `pinned-bugfix` (4 tasks) | 100.0% (`4/4`) | 100.0% (`4/4`) |
| `small-feature` (4 tasks) | **75.0%** (`3/4`) | 50.0% (`2/4`) |
| `refactor` (2 tasks) | 50.0% (`1/2`) | **100.0%** (`2/2`) |
| `pattern` (2 tasks) | 50.0% (`1/2`) | 50.0% (`1/2`) |

---

## 3. Per-Task Results (`bench-r1/records.jsonl`)

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

Additional single-task ACP & Driver runs on 17 Nov 2026:

| Run | Mode / Provider | Task | Outcome | Hidden Tests | Total Tokens | Time |
| --- | --- | --- | --- | --- | --- | --- |
| `acp-codex` | `farm` (`codex-acp` sup + DeepSeek) | `rs-percent-zero` | `in-review` | **pass** | 93,333 (`13,420` w / `79,913` sup) | 114.2s |
| `acp-codex` | `solo` (`codex-acp` workers) | `rs-percent-zero` | `in-review` | **pass** | 61,586 (`61,586` w / `0` sup) | 33.4s |
| `acp-agy` | `farm` (`antigravity-acp` low + DeepSeek) | `rs-percent-zero` | `in-review` | **pass** | 34,796 (`18,052` w / `16,744` sup) | 131.1s |
| `drive-codex` | `openfarm drive --agent codex --seat both --wave --record-verdict` | `py-inventory-remove` | `in-review` (`verdict: clean`, `fallback: false`) | **pass** | Worker `21,190` (`6,740` in / `14,450` out); Driver `118,767` (`6` turns, `2` actions, `4` answers) | — |
| `drive-agy` | `openfarm drive --agent antigravity --effort low --seat both` | `py-inventory-remove` | `in-review` (`4` actions, `3` answers, `0` fallback) | **pass** | Driver `95,885` (`90,748` in / `5,137` out, `6` turns) | — |

---

## 4. Counter-Test Gate Calibration (`counter_tests = "reject"`)

Two calibration controls (`ct-calibration`, `farm-codex` with `[gates] counter_tests = "reject"` and `[micromanager] adjudicate_counter_tests = true`) plus all `36` live `card-record.json` artifacts from Stage 4 runs were inspected:

| Run | Mode | `counter_tests.written` | `dropped_uncompiled` | `passed` | `failed` | `false_rejects_dismissed` | Final State | Hidden Tests |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `ct-calibration/py-inventory-remove` | `reject` + `adjudicate` | 0 | 0 | 0 | 0 | 0 | `in-review` | **pass** (`5,810`/`12,712` worker, `65,472`/`471` mm) |
| `ct-calibration/rs-stack-pop-n` | `reject` + `adjudicate` | 0 | 0 | 0 | 0 | 0 | `in-review` | **pass** (`5,458`/`12,485` worker, `64,799`/`476` mm) |
| Aggregate across all `36` live Stage 4 cards | mix (`report` + `reject`) | 0 | 0 | 0 | 0 | 0 | — | — |

**Observation from `checker-pass2.json`:** Both DeepSeek V4.1 Flash (`farm` checker) and Codex (`solo` checker) produced structured traceability tables and 3–4 traced edge cases, and explicitly wrote `"None"` under `## Counter-tests expected to fail` when the passing implementation matched all card Done clauses. Across these 36 clean/borderline runs, switching `counter_tests` from `"report"` to `"reject"` introduced a **0.0% (`0/36`) false-reject rate** and required **0** checker-adjudication calls.

**Calibration limit (what remains untested):** Because none of the live passing implementations left an obvious specification gap that prompted `checker_pass2` to emit failing counter-tests, the **true-bug catch rate** and **adjudicator dismissal rate under noisy checker counter-tests** remain empirically uncalibrated against intentionally subtle faulty implementations. Keep `counter_tests = "report"` as the repository default until calibrated against a labeled subtle-bug suite or `cargo-mutants`.
