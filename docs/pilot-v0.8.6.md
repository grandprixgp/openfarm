# openfarm Efficacy Pilot: 25 Rust Cards (v0.8.5 to v0.8.6)

Date: 18 Nov 2026

This is the efficacy pilot the roadmap asked for: 25 cards on one real Rust crate, frontier baselines for five of them, `counter_tests = "reject"` measured on a second round, and what cargo-mutants paid back. The plan, card list and baselines were fixed before the first farm card ran. Every openfarm defect it found is fixed on v0.8.6 with a test that fails on the old code.

Cost is given as token counts and shares, never dollars.

## 1. Setup

- **Repository.** `dtolnay/semver` at upstream `280ebcb`, cloned locally with the remote removed. Nothing was pushed anywhere.
- **My three setup commits.**
  1. Drop criterion and the bench, so the crate builds with no downloads.
  2. Add five reference methods that the pattern cards copy, with tests.
  3. Plant five bugs for the bug-fix cards. Each is a cargo-mutants mutant that the real suite misses, so the suite stays green with all five in.
- **Workers.** DeepSeek V4.1 Flash on Command Code with `x-cmd-zdr: 1`, effort `high` on the first try and `max` on retries.
- **Supervisor.** Claude (Opus 5.5) through openfarm's bridge, effort medium.
- **Gates.**
  - `counter_tests = "report"` in round 1.
  - `mutation = "report"`, with `mutation_required_for = ["test-backfill"]`.
  - The planner was on only for M3.
- **Cards.** 25 in five categories:

  | Category | Cards |
  | --- | --- |
  | pattern | P1–P7 |
  | pinned-bugfix | B1–B5 |
  | mechanical | M1–M3 |
  | small-feature | F1–F3 |
  | test-backfill | T1–T7 |

- **Hidden acceptance tests.** I wrote hidden tests for every non-backfill card. Each one passes on my reference solution and fails on the base. They stand in for "no escapes after merge".
  - For a test-backfill card, "hidden pass" means its tests kill every targeted cargo-mutants survivor.
  - For M1 and M2 it is a structural check plus the suite.
- **Frontier baselines.** One card per category was fixed in advance: P3, B2, M2, F1 and T3. For each, `claude -p` (Opus 5.5, effort medium, cargo, Read and Edit tools only) did the card alone in its own worktree, from the same base commit as the farm card. Its work was never merged.

## 2. Results

| Card | Round 1 (v0.8.5) | Tokens (worker share) | Re-run on v0.8.6 | Tokens | Round 2, `reject` (v0.8.6) | Tokens |
| --- | --- | --- | --- | --- | --- | --- |
| P1 | in-review | 63,503 (65%) | | | in-review | 55,087 (60%) |
| P2 | in-review | 63,224 (64%) | | | in-review | 58,479 (62%) |
| P3 | in-review | 60,853 (61%) | | | in-review | 55,636 (59%) |
| P4 | in-review | 61,090 (62%) | | | in-review | 52,803 (59%) |
| P5 | blocked `tests-unprovable` | 59,307 (58%) | in-review | 100,308 (63%) | in-review | 73,535 (59%) |
| P6 | in-review | 62,096 (63%) | | | blocked `supervisor-reject` | 112,168 (69%) |
| P7 | in-review | 66,039 (64%) | | | in-review | 51,860 (57%) |
| B1 | in-review | 42,127 (80%) | | | blocked `supervisor-reject` | 139,603 (85%) |
| B2 | in-review | 25,517 (66%) | | | in-review | 20,933 (62%) |
| B3 | in-review | 26,706 (68%) | | | in-review | 26,403 (67%) |
| B4 | in-review | 40,031 (77%) | | | blocked `supervisor-reject` | 77,528 (70%) |
| B5 | in-review | 21,362 (60%) | | | in-review | 21,898 (61%) |
| M1 | in-review | 20,141 (64%) | | | in-review | 19,995 (65%) |
| M2 | blocked `supervisor-reject` | 108,470 (75%) | in-review | 39,317 (73%) | in-review | 31,114 (70%) |
| M3 | blocked `cannot-split` | 21,275 (10%) | blocked `supervisor-reject` | 101,107 (70%) | in-review | 56,784 (54%) |
| F1 | blocked `no-progress` | 113,811 (74%) | in-review | 84,060 (68%) | in-review | 62,111 (61%) |
| F2 | in-review | 72,525 (66%) | | | in-review | 113,684 (68%) |
| F3 | in-review | 71,235 (63%) | | | in-review | 159,783 (75%) |
| T1 | in-review | 20,333 (60%) | | | | |
| T2 | blocked `supervisor-reject` | 32,337 (45%) | in-review | 24,113 (60%) | | |
| T3 | in-review | 22,760 (63%) | | | | |
| T4 | in-review | 31,994 (68%) | | | | |
| T5 | in-review | 20,927 (57%) | | | | |
| T6 | in-review | 30,386 (68%) | | | | |
| T7 | in-review | 21,417 (58%) | | | | |

- **Round 1:** 20 of 25 in-review, 1,179,466 tokens, 65.4% of them worker tokens.
- **Re-runs:** 4 of 5 in-review, 348,905 tokens, 67.5% worker.
- **Round 2:** 15 of 18 in-review, 1,189,404 tokens, 67.9% worker.
- **No escapes.** Every card that reached in-review, in every round, passed its hidden tests, the full suite and clippy with `-D warnings`.
- **My review.**
  - In round 1, 2 cards were clean and 18 were minor-fix. Every minor fix was formatting: each Rust test file froze without its final newline, and some crate-root code was left unformatted.
  - Both formatting causes are fixed on v0.8.6. On the re-runs, three of the four in-review cards were format-clean. F1b, which ran before the crate-root fix, was the exception.

### 2.1 Why the five round 1 cards stopped

Three were openfarm defects, all fixed on v0.8.6. Two were my own card defects.

| Card | Cause | Fixed in |
| --- | --- | --- |
| P5 | The supervisor's scaffold put a stub inside an `impl` block above its closing brace, which the scaffold check refused. | `f1c4b6f` |
| M3 | The planner wrote a step's test as `name in file`, the way the brief lists it, and only the bare name was accepted. On the re-run it copied the whole brief line (`name in file — asserts`). | `0737cb6`, `b9a8273` |
| F1 | The test writer wrote `assert_eq!(x, true)`. That fails clippy `bool_assert_comparison`, and once the tests are frozen nobody may fix it, so every implementer attempt failed the same way. The live Claude bench lost `rs-triangle` #2 the same way. Tests are now linted, and formatted, before they freeze, and a finding goes back to the test writer. | `3f3ffbf`, `41d3b9e`, `2f392da` |
| M2 | My card defect: it asked for `digits` to be private to the new module while `display.rs` still calls it. The supervisor bounced it correctly, and the frontier baseline hit the same contradiction. | Card fixed (M2b, M2c) |
| T2 | My card defect: the Done text promised an "exactly as wide" case that the card's own asserts left out. The supervisor bounced it correctly. Its tests were otherwise good: they killed 37 of 37 targeted mutants, against 34 of 37 at base. | Card fixed (T2b) |

The re-runs found two more openfarm defects:

- **M2b:** a search/replace block that only deletes code was refused as malformed. The trailing-junk trimmer took the block's own `>>> REPLACE` line for a stray marker. Fixed in `89e4aa8`.
- **F1b:** the base-formatting check copied a file out on its own and ran `rustfmt --check` on the copy. A crate root with `mod x;` always failed that, so it was never formatted. Fixed in `4eba2d3`.

M3c, on the plan fix, made the planner run. Its code added `Op::symbol` but did not switch `Display for Comparator` to use it, which the card asked for. A counter-test caught that (`display_uses_op_symbol`) and the supervisor bounced it: a true reject. My hidden test for M3 checked only `symbol()`, so the scorer missed it. In round 2, M3 did the whole card.

## 3. Frontier Baselines and Payback

Both columns count cached input, as openfarm's Claude bridge does. The column without cache reads is there because a cache read costs a provider far less than a fresh token.

| Card | Frontier alone: tokens (without cache reads), turns, time | Farm: frontier (supervisor) tokens | Farm: worker tokens | Frontier tokens moved off |
| --- | --- | --- | --- | --- |
| P3 | 103,588 (23,083), 6 turns, 17 s, all gates and hidden pass | 23,155 | 37,698 | 78% |
| B2 | 108,538 (9,815), 6 turns, 18 s, pass | 8,466 | 17,051 | 92% |
| M2 | 213,538 (16,189), 11 turns, 53 s, pass | 26,813 (blocked: card defect), then 10,584 (pass) | 81,657, then 28,733 | 87%, then 95% |
| F1 | 133,502 (12,024), 7 turns, 31 s, pass | 29,004 (blocked: openfarm defect), then 26,483 (pass) | 84,807, then 57,577 | 78%, then 80% |
| T3 | 61,478 (7,734), 4 turns, 14 s, pass | 8,368 | 14,392 | 86% |

- **Read the last column carefully.** Against the baseline's total, the farm moved 78–95% of the frontier tokens off the frontier model on every baseline card. Against the baseline's tokens without cache reads, the farm's supervisor used more on P3, M2's first run, F1 and T3, and less on B2 and M2c. In this pilot most of a frontier agent's tokens were cheap cache reads of its own context.
- **Wall time** was 5 to 12 times the frontier model alone (103–662 s against 14–53 s), because of the gates and cargo builds.
- **Not counted:** my own frontier tokens spent writing the cards and reviewing the results. I wrote one card set for all three rounds, so this is a fixed cost. The `--review-tokens` I recorded (300–700 per card) are estimates.
- **The keep bar** was positive payback, at least 60% clean-or-minor at review, and no escapes. On this pilot every category meets it:

  | Category | Clean-or-minor at review |
  | --- | --- |
  | pattern | 6 of 7, and 7 of 7 with the re-run |
  | pinned-bugfix | 5 of 5 |
  | mechanical | 1 of 3 in round 1, 3 of 3 in round 2 |
  | small-feature | 2 of 3, and 3 of 3 with the re-run |
  | test-backfill | 6 of 7, and 7 of 7 with the corrected card |

  Payback is positive on total tokens. On tokens without cache reads it is positive for B2 and M2c only.

## 4. `counter_tests = "reject"`

Round 2 ran batch 1 again on v0.8.6 in a second clone from the same base, with `counter_tests = "reject"`. The corrected M2 card was used.

- **Counter-tests across both rounds:** 4 written, 3 failed, 1 dropped because it did not compile, 0 passed.
  - All 3 failures were real alarms: M2's two caught my contradictory card, and M3c's caught `Display` not using `symbol()`.
  - None was wrong, so in this pilot **counter-tests had no false rejects.**
- **Round 2 blocked three correct cards** (B1, B4, P6). Each passes its hidden tests, the suite and clippy. All three bounces came from the supervisor's pre-review, not from a counter-test:
  - B1 and P6 were bounced for thin tests, which the implementer cannot change.
  - B4 was blocked because the mutation report was below 80% (report mode, not a gate).
  - P6 also over a stale `test-lint` fail record on file, which a retry had already fixed.
  - The same pattern lost 9 of 36 Codex farm cards in the bench ([`bench-v0.8.5.md`](bench-v0.8.5.md)).
  - Fixed on v0.8.6: the pre-review now has a rule (bounce only on a missed line of the Done text, shown by a failing counter-test or by the code, and quote it; thin tests and unrequested robustness go in `findings` with `approve`) in `2354994`. Test-lint failures stay in the attempt log, not the gate list, in `2f392da`.
- **A live implementer fixing a counter-test bounce:** no counter-test failed in round 2, so the pilot itself never exercised that path. It was run separately on the TypeScript project; see the [v0.8.6 build log](v0.8.6-build-log.md).
- **Recommendation:** `reject` is safe to offer for counter-tests. It is not yet safe as a default: the pre-review rule that removes the false rejects is checked on 15 bench runs with Codex only (bench report section 3), not on a pilot round.

## 5. Mutation Payback

- **Cost.** cargo-mutants made 1,522 of the 1,895 cargo builds in round 1 (80%), and 1,170 of 1,465 in round 2.
- **Test-backfill, where mutation is the proof (`required`).** All 7 cards met the 80% bar.
  - My own targeted cargo-mutants run, on just the code each card covers, agrees: every card's tests killed every targeted survivor.
  - The base had survivors for T1 (1 of 2), T2 (3 of 37), T3 (1 of 1), T4 (1 of 1) and T5 (1 of 1). T6 and T7 had none, so those tests add coverage without new kills.
  - Here the gate paid for itself: it is the only proof a backfill card has.
- **Other categories (report).**
  - The kill rate measures the whole changed file, the card's own lines and every line already there. So it says little about the card: B1 reported 84% because of 16 survivors that were already in `eval.rs`.
  - It stopped nothing, and it misled the supervisor once. B4's report-only "75% below the required 80%" became part of a round 2 block.
- **Recommendation:**
  - Keep `required` for test-backfill.
  - Leave the other Rust categories on `report`, or off, until cargo-mutants can be scoped to the card's own lines, the way openfarm's own Python and TypeScript runner is.

## 6. Caveats

- **One crate, and a small one.** semver has about 2,000 lines of library code, so its cards are small.
- **One worker model, one supervisor model, one run per card per round.** Read the rates as what happened, not as estimates.
- **The hidden tests are mine.** They missed one requirement (M3's Display change), which a counter-test caught.
- **My scorer had a bug, found during the pilot.** Its T2 pattern also matched `VersionReq`. Results are corrected in this page.
- **The shared-target trap.** Five old baseline worktrees briefly scored red because a shared cargo target folder held another branch's library. They pass on their own targets. Every farm score was taken in a freshly made worktree, where the trap does not bite.
