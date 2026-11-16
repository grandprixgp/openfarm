# openfarm spec v0.7

Nov 16, 2026 · draft

v0.7 builds on v0.6. Until now, what a project's runs learned stayed with that project. v0.7 lets a lesson that names nothing of its project become global across every openfarm project on the machine, and each project chooses whether it applies its own lessons, global ones, or both. Everything in v0.6 that this file does not change still holds. The decisions behind it are in `.openfarm/GRILL_DECISIONS.md` (8 decisions, confirmed 16 Nov 2026).

Claims are tagged as before: **\[E\]** for well-documented behaviour, **\[J\]** for judgement until measured.

## Contents

- [Goals and non-goals](#goals-and-non-goals)
- [Two libraries](#two-libraries)
- [Scope at capture](#scope-at-capture)
- [Promotion and eligibility](#promotion-and-eligibility)
- [Applying lessons](#applying-lessons)
- [Vetoes and retirement](#vetoes-and-retirement)
- [Commands](#commands)
- [Bench](#bench)
- [Reports count tokens](#reports-count-tokens)
- [Config changes](#config-changes)
- [Build order and waves](#build-order-and-waves)
- [Honest status](#honest-status)

## Goals and non-goals

**Goals**

- A lesson proven in one project helps the next project without being learned again.
- A lesson tied to one project (its files, functions, cards or name) never leaves it.
- Each project decides which lessons apply to it and whether its own may travel.
- A lesson that hurts elsewhere is switched off there, and retired everywhere once it clearly does harm.

**Non-goals**

- Sharing between machines or people. The global library is a local folder; nothing is uploaded.
- Changing a lesson's text on the way. A global lesson is the source lesson's travelling fields, unchanged.
- Rewriting the 34 lessons openfarm's own library already held: they stay `project`, so the global library starts empty.

## Two libraries

Each project keeps its own library at `<state_dir>/harness`, as before. The global library sits at `[harness] global_dir`, by default `~/.openfarm/global/harness` (or `OPENFARM_GLOBAL_HARNESS`), and is refused inside the repository. It holds `entries/g-<hash>.json`, an append-only `ledger.jsonl` and a lock file, and nothing is created until the first write. A lesson's global id is a hash of its surface, role, language, kind, failure class and cue, so the same lesson promoted by two projects is one global lesson with two sources.

A global entry keeps only the lesson's travelling fields (title, surface, role, language, kind, failure class, tags, cue, lesson, patch, confidence). Evidence and card IDs stay home. It records its source projects and, per project, `applied`, `helped`, `hurt`, `neutral` and `vetoed`.

## Scope at capture

Every new lesson gets `scope = "project" | "general"` with a reason. The supervisor's reflection claims one, told that bookkeeping notes are always `project` and to choose `project` when in doubt. The fixed-rule extractor's lessons are `general` but are never eligible to travel.

Plain code checks every `general` claim (`harness_scope.scope_of`) against the project's markers (`project_markers`): its name, its tracked paths, their file names and stems of 5 or more characters, and distinctive definition names from its Python and Rust files (a name with an underscore and 6 or more characters, 10 or more characters, or two capitals). A lesson naming a card, a file in its patch, or any marker is `project`, whatever it claimed. When the markers cannot be listed, every lesson is `project` **\[J\]**.

## Promotion and eligibility

After each card, a project with `share = true` promotes each lesson that passes both checks:

- **Proof** (`should_promote`): `general`, `active`, helped on 3 or more cards, and fewer hurts than helps.
- **Eligibility** (`global_eligibility`): not a fixed-rule cue, not `project`, not itself imported, not a supervisor note on harness bookkeeping, no field over 400 characters, no code block, no gate-bypass directive, no assertion leak, no files named in its patch.

A refused lesson records why (`global_refused`). The owner can force a lesson global with `openfarm harness promote <id> --global`, which skips the proof and the lesson's scope claim but still runs the marker check and eligibility.

## Applying lessons

At the start of a run, each active global lesson the project doesn't already hold is imported as a local candidate `exp-<gid>` with `origin = "global"`. It enters the project's own probe cycle and is scored by the project's own card outcomes, like a lesson the project learned. `[harness] learn_from` decides what applies:

- `both` (the default): own and global lessons. On equal fit an own lesson ranks first, and `max_cues_per_brief` caps both together.
- `project`: own lessons only. Nothing is imported.
- `global`: imported lessons only.

`openfarm run --learn-from` overrides it for one run.

## Vetoes and retirement

After each card, a project reports its scores for every linked lesson. If an imported lesson has hurt more than it helped there, the project switches it off for itself (`demote_reason = "global_veto"`) and records a veto. A global lesson is retired everywhere once 2 or more projects veto it, or when help minus hurt summed over all projects goes below zero **\[J\]**. At their next run, projects drop a retired lesson (`demote_reason = "global_retired"`), even with `learn_from = "project"`.

## Commands

- `openfarm harness status --global`: the global lessons, their status, sources and reporting projects.
- `openfarm harness promote <id> --global`: with a global id, restore a retired lesson; with a project lesson's id, force it global (checks above).
- `openfarm harness demote <id> --global`: retire a global lesson, by its global id or by the id of the project lesson linked to it.
- `openfarm run --learn-from both|project|global`.

## Bench

A bench run gives each task run a throwaway global library inside the run folder, so the bench never reads or changes the owner's. An arm file may hold a `[harness]` table with only `learn_from`, `share` and `global_dir`, so arms can compare learning settings.

## Reports count tokens

Supervisor cost was reported in API list-price dollars, which on a subscription say nothing about real cost. The run summary (`tokens`), each dossier ("Tokens: N tokens: X% worker, Y% supervisor") and the bench report (tokens, tokens per pass, worker share) now give the token split instead. Dollars stay in `run.json` only for the spend limits, and a zero spend limit now means no limit.

## Config changes

| Table | Key | Default | Meaning |
| --- | --- | --- | --- |
| `[harness]` | `global_dir` | `""` | The global library; empty means `~/.openfarm/global/harness`. Must sit outside the repository. |
| `[harness]` | `learn_from` | `"both"` | Which lessons apply: `both`, `project` or `global`. |
| `[harness]` | `share` | `true` | Whether this project's proven lessons may go global. |
| `[budget]`, `[micromanager]` | `spend_per_night`, `frontier_cost_ratio` | unchanged | `0` now means no limit. |

## Build order and waves

- **Frontier by hand (branch `v0.7`):** the config keys, the global store, scope and origin on every lesson, the exchange in `harness_memory.py`, the flow wiring, `run --learn-from`, bench isolation, the config template, and zero meaning no spend limit.
- **Wave 1, live:** CARD-016 (scope check, merged), CARD-018 (`harness --global`, merged by hand after its frozen test proved wrong), CARD-020 (README section, merged with the fix its pre-review asked for). CARD-015 and CARD-019 blocked.
- **Wave 2, live:** CARD-015 (the rules) and CARD-017 (eligibility) merged clean; CARD-019 (bench arm `[harness]`) blocked again on an unprovable frozen test, so I wrote it by hand.
- **After:** the unpatched end-to-end test, token-share reports, forced promotion by a project lesson's id, and the release (version 0.7.0, `base_branch` back to `master`).

## Honest status

Tested here, offline: 599 unit and end-to-end cases, including the real-cargo suite (`OPENFARM_E2E=1`). `tests/test_global_end_to_end.py` runs four throwaway projects through the real rules with nothing patched: a lesson proven in one reaches another's briefs, that project's failures veto it, a second veto retires it, a later project never imports it, a combined negative score retires it, two helps are not enough, `learn_from` and `share` are obeyed, and a lesson naming the source project's function never leaves it. Each rule was broken in turn to confirm a test fails.

Live, 16 Nov: the six cards ran on openfarm itself with DeepSeek V4.1 Flash on Command Code (`x-cmd-zdr: 1`) and Claude supervising. Of the 8 card runs, 65% of the 1.3 million tokens went to the workers and 35% to Claude. Every run synced with and reported to the real global library, which stayed empty because nothing was yet eligible to promote.

One Rust bench dry run stopped on an infrastructure error once during a full suite run and passed on three reruns; the cause is not known.

**Untested:** sharing lessons between two real projects; a forced promotion on a real library; whether the global rules' thresholds (3 helps, 2 vetoes) are right, which needs runs across projects to judge.
