# openfarm spec v0.8

Nov 17, 2026 · implemented on `v0.8` (Stages 1–10 + stdlib TUI/human CLI; live DeepSeek-only, effort, external seats, parallel cargo, 2-task bench & 2-project global exchange verified 17 Nov 2026)

v0.8 builds on v0.7. Until now openfarm ran a fixed team, one card at a time, and openfarm itself called the supervisor. v0.8 adds a shared board that every step of a run is posted to, a team whose members a coordinator chooses (which model tier, which effort), cards worked in parallel, and seats that an agent or person outside openfarm can hold. Everything in v0.7 that this file does not change still holds, and with `[swarm] enabled = false` (the default) a run behaves exactly as in v0.7.

The owner settled three things on 16 Nov 2026: the full scope (a coordinator that picks its own members, and parallel cards), effort as something the coordinator sets per member, and DeepSeek V4.1 Flash as the only member model for now. Every other choice here is my default and is listed under [Open for the owner](#open-for-the-owner).

Claims are tagged as before: **\[E\]** for well-documented behaviour, **\[J\]** for judgement until measured.

## Contents

- [Goals and non-goals](#goals-and-non-goals)
- [Words](#words)
- [The board](#the-board)
- [Members, tiers and effort](#members-tiers-and-effort)
- [Seats](#seats)
- [The coordinator](#the-coordinator)
- [Parallel cards](#parallel-cards)
- [Handing a card back](#handing-a-card-back)
- [Stalls](#stalls)
- [Commands](#commands)
- [Config](#config)
- [Reports](#reports)
- [What the gates still decide](#what-the-gates-still-decide)
- [Done](#done)
- [Open for the owner](#open-for-the-owner)

## Goals and non-goals

**Goals**

- A coordinator decides, per card, which tier and effort each role runs at, and raises them when a card struggles.
- Several cards run at the same time when they cannot collide.
- An agent or person outside openfarm can hold the coordinator's or the supervisor's seat. openfarm never starts or messages that agent: it writes to the board and waits, and the outside member reads and answers through the command line.
- A whole run can use DeepSeek alone: workers, supervisor and coordinator.
- A worker that finds the card itself is wrong can say so instead of spending its attempts.
- The gates decide what passes, whatever the coordinator or any member says.

**Non-goals**

- Several members racing on one card. One member per role per card.
- Members adding their own helpers, channels, or a board shared between machines.
- A tier with its own endpoint or key. Every tier uses `[model]`'s endpoint, key and headers, so the zero-retention header covers every call.
- A member editing a card or its frozen tests.
- openfarm merging or pushing anything.

## Words

- **Board:** the append-only list of numbered posts for one run.
- **Member:** one named participant in a run. Its kind is `api` (a model on the worker endpoint), `acp` (Claude over ACP, as today), `code` (plain code, such as the gates), `external` (outside openfarm) or `scripted` (tests).
- **Role:** what a member does: `coordinator`, `supervisor`, `planner`, `test-writer`, `implementer`, `checker`, `gates`, `monitor`.
- **Seat:** the coordinator's or the supervisor's place in a run. The config says who fills it.
- **Tier:** a named model from the config. **Effort:** how hard that model thinks, one of the tier's allowed values.

## The board

One board per run, at `<state_dir>/runs/<run_id>/board/`: `board.jsonl` (the posts) and `members.json` (the roster). Writes take a file lock, as the global library does. A post is never changed or removed.

A post:

| Field | Meaning |
| --- | --- |
| `seq` | Its number, from 1, with no gaps. |
| `at`, `from` | UTC time and the author's member name. |
| `to` | Member names, a role name (every member with that role), or `all`. `@name` in a model's text is read into `to`. |
| `kind` | `brief`, `result`, `gate`, `defect`, `question`, `answer`, `handback`, `action`, `done`, `blocked`, `stall`, `roster`, `note`. |
| `card`, `attempt` | The card and attempt it belongs to, when it has one. |
| `ref` | The `seq` it answers or cites. Posts cite earlier posts by number and never copy them. |
| `body` | Text, at most 8,000 characters. Anything longer goes in a file listed in `evidence`. |
| `evidence` | Paths under the run's state folder: a brief, a diff, a gate's output, a dossier. |
| `expects` | On a `question`: the op and the answer's shape, so an answer can be checked before it is accepted. |

Rules:

- **Who is told.** A problem goes to the member that can fix it. A gate failure is addressed to the member that wrote the code, a checker's counter-test to the implementer. The coordinator is addressed only by `handback`, `stall`, `done`, `blocked` and questions put to it. Every post addressed to a model member costs it a turn **\[J\]**.
- **Text from models is data.** A model's post body goes through the same cleaning as a lesson (edit markers, code fences, escape sequences, gate-bypass directives) before it is shown to another model. Posts from `code` members are trusted.
- **Reading.** Each member has a `last_read_seq`. A read returns posts after it that are addressed to the member, and moves it.
- **Waiting.** A member waits from the last `seq` it saw. A wait returns at once when a post with a higher `seq` already addresses the member, so nobody sleeps through a post made while they worked. Otherwise it returns when one arrives, when the run ends, or at its timeout.
- **Done is not a word in a post.** A card's `done` post is written by code after the gates pass. A model writing "done" changes nothing.

Every step the pipeline takes is posted: the claim, each brief (as evidence), each result, each gate with pass or fail, each supervisor question and answer, each coordinator action, and the final `done` or `blocked`. The board is the run's record, and the dossier links to it.

## Members, tiers and effort

The roster (`members.json`) holds, per member: `name`, `kind`, `role`, `card`, `tier`, `model`, `effort`, `state` (`active`, `waiting`, `removed`), `added_by`, `last_read_seq`, `wait_since`, token counts, and for an external member `owner_kind` (`human` or `agent`).

A tier comes from the config:

```toml
[tiers.dsf]
model = "deepseek/deepseek-v4.1-flash"
efforts = ["low", "medium", "high", "max"]
effort_param = "reasoning_effort"
```

- Effort is sent in the request body under `effort_param`. With `effort_param = ""` or no effort, nothing is sent.
- Checked on 16 Nov against Command Code with DeepSeek V4.1 Flash: `reasoning_effort` is accepted, the endpoint allows `off`, `low`, `medium`, `high`, `xhigh` and `max`, and any other value answers 400. Checked live on 17 Nov (`coordinator = "code"`, `supervisor = "off"`, `escalate_after = 99`): on `py-money-parse`, `low` (`7,778` tok) stopped `blocked` (`refused`), `high` (`11,722` tok) stopped `blocked` (`tests-unprovable`), and `max` (`52,020` tok, `281.15s`) reached `in-review` and passed `6/6` hidden tests; on `rs-percent-zero`, `low` (`10,699` tok), `high` (`18,100` tok), and `max` (`17,784` tok) all reached `in-review` and passed hidden tests.
- A member's tier and effort are always stated: by the coordinator, or else from `[swarm] default_tier` and each role's default effort. A member never copies them from whoever added it.
- An effort outside the tier's `efforts`, or a tier not in the config, is refused.
- `[model]`'s per-role tables gain `effort` (`implementer = { model = "...", effort = "medium" }`), which also works with the swarm off.
- Changing a waiting member's tier or effort takes effect on its next call. Workers are called fresh each attempt, so nothing is lost.

## Seats

```toml
[swarm]
coordinator = "code"     # "code" | "tier:<name>" | "external"
supervisor  = "config"   # "config" | "tier:<name>" | "external" | "off"
```

- **`code` coordinator:** plain code with a fixed policy (below). No model calls.
- **`tier:<name>`:** a model on the worker endpoint fills the seat, at `coordinator_effort` or `supervisor_effort`. For the supervisor this is a new backend that sends the supervisor's existing prompts to the worker endpoint and reads the same JSON answers. With it, a run needs no Claude at all.
- **`config` supervisor:** whatever `[micromanager]` says, as in v0.7.
- **`external`:** openfarm posts a `question` (for the supervisor: one per op, with the prompt as evidence and the answer's shape in `expects`) and waits for an `answer` that cites it. The outside member joins with `openfarm swarm join`, reads with `openfarm board read`, and answers with `openfarm board answer`. An answer that does not fit `expects` is refused with the reason and is not posted.
- **When an external member does not answer** within `[swarm] external_wait`: with `on_external_timeout = "fallback"` the supervisor's op is skipped the way a timed-out supervisor call is today, and the coordinator's decision goes to the `code` policy; with `"block"` the card blocks with stop code `seat-silent`. The wait does not count against the card's wall clock, the card's lease is renewed during it, and the kill switch and pause are still obeyed.

A seat is granted by the config, which no worker can edit. The board sits in the state folder, outside the repository. Anyone who can run openfarm as the owner can join a seat set to `external`; there is no separate key.

## The coordinator

The coordinator is woken by events and answers with actions. The same actions exist as commands for an external coordinator and as JSON for a model coordinator, and both go through one checked code path.

**Events** (each a post addressed to the coordinator):

| Event | When |
| --- | --- |
| `plan` | At the start, and whenever a slot frees with cards still ready. |
| `struggling` | A member has failed `escalate_after` attempts at its current effort. |
| `handback` | A worker handed its card back. |
| `stall` | The monitor found the run stuck. |
| `finished` | A card reached `done` or `blocked`. |

**Actions:**

| Action | Effect |
| --- | --- |
| `start` | Start a ready card with a team: tier and effort for `planner`, `test-writer`, `implementer` and `checker`. Roles left out take the defaults. |
| `set` | Change one member's tier or effort from its next call. |
| `hint` | Post a short note to a member; it is shown in that member's next brief as data. |
| `ask` | Put a question to the supervisor's seat. |
| `block` | Stop a card with a reason for the owner. |
| `remove` | Remove a member whose card is finished. |
| `wait` | Nothing to do until the next event. |

**Checks, by plain code, on every action:** the card is ready and not running; it does not collide with a running card (below); the tier exists and the effort is allowed; the slot, member and budget limits hold; the member exists and belongs to the card. A refused action is posted back with the reason. A model coordinator that gives two refused or unreadable answers in a row hands that decision to the `code` policy.

**The `code` policy:** start ready cards in ID order while slots are free and nothing collides; every role at its default effort; on `struggling`, raise that member's effort one step (`low` to `medium` to `high` to `max`), and at the top do nothing; on `handback`, ask the supervisor's seat when one is filled, otherwise block the card with the worker's reason; on `stall`, apply the timeout rule.

**A model coordinator** is given the ready cards (ID, title, category, touched paths, dependencies), the tiers and efforts, the roster, the free slots, and the posts addressed to it. It answers with a JSON list of actions. Its calls and tokens are capped by `coordinator_calls_per_run` and `coordinator_tokens_per_run`; past either, the `code` policy takes over.

## Parallel cards

`[budget] concurrent_cards` cards run at once, each in its own worktree and thread, with builds limited by `concurrent_builds`.

Two cards may run together only when neither depends on the other and their `touches` cannot name the same file (a glob counts as overlapping when one pattern could match a path the other could). Code enforces this whatever the coordinator asks.

Shared between cards, and so needing a lock or one writer: the night budget, the supervisor's budget, the harness library (its file lock is not safe between threads), the global library, telemetry, the card source's commits, git worktree add and remove on the repository, the baseline cache, the circuit breaker, the board, and the one Claude ACP process when the supervisor is `acp`.

Parallel runs must not change results: the same scripted cards give the same final states, gates and diffs whether run one at a time or three at once.

## Handing a card back

A worker may answer with a hand-back in place of edits:

```
<<<HANDBACK
The card's test expects sources == ["alpha"], but add() stores source objects.
>>>
```

- A card may list signs to watch for, shown in every brief for it:

  ```toml
  replan_if = ["a named test already passes on base", "the reference module has no such function"]
  ```

- Every brief says: after one failed fix of the same problem, or when something the card assumes is false, hand back instead of trying a third way.
- A hand-back is posted to the coordinator, does not count as an attempt, and is limited by `handbacks_per_card` (default 2). Past the limit it counts as a failed attempt.
- It ends one of three ways: a `hint` (the worker tries again with it), a `set` (the worker tries again at another tier or effort), or the card blocks with stop code `handed-back` and the worker's reason in the dossier.
- Nobody but the owner changes the card or its frozen tests.

In v0.7, two of six cards blocked on a frozen test that was wrong, after 9 and 5 attempts.

## Stalls

A `code` member named `monitor` checks the run every 30 seconds. When no card thread is working, every member is waiting, cards are unfinished, and that has lasted `stall_after`, it posts one `stall` to whoever owes the oldest unanswered question, and to the coordinator. It uses no model.

## Commands

```
openfarm board read   [--as NAME] [--since N] [--limit 20] [--seqs 4,6] [--status] [--run ID]
openfarm board post   --as NAME --to NAMES [--kind note] [--card ID] [--ref N] (--body TEXT | --file PATH)
openfarm board answer SEQ --as NAME --file ANSWER.json
openfarm board wait   --as NAME --since N [--timeout 30m]

openfarm swarm status | tiers
openfarm swarm join   --name NAME --seat coordinator|supervisor [--kind human|agent]
openfarm swarm start  CARD [--team role=tier:effort,...]
openfarm swarm set    MEMBER [--tier T] [--effort E]
openfarm swarm hint   MEMBER --body TEXT
openfarm swarm block  CARD --reason TEXT
openfarm swarm remove MEMBER
```

- With no `--run`, they use the run that holds the run lock.
- `board wait` prints the new posts and exits 0. It exits at once when posts are already waiting. On a timeout or when the run ends it prints `{"woke": false, "reason": ...}` and exits 0.
- `swarm start`, `set`, `hint`, `block` and `remove` are refused unless the caller holds the coordinator's seat (`--as`).
- An outside agent's loop: start `openfarm run` in the background; run `openfarm board wait` in the background; when it exits, read, answer or act, and wait again from the last `seq` it saw.

## Config

| Table | Key | Default | Meaning |
| --- | --- | --- | --- |
| `[swarm]` | `enabled` | `false` | Off: a run is v0.7's. |
| `[swarm]` | `coordinator` | `"code"` | Who fills the coordinator's seat. |
| `[swarm]` | `supervisor` | `"config"` | Who fills the supervisor's seat. |
| `[swarm]` | `coordinator_effort`, `supervisor_effort` | `"max"` | Effort for a seat filled by a tier. |
| `[swarm]` | `default_tier` | first tier | Tier for a member nobody chose one for. |
| `[swarm]` | `escalate_after` | `2` | Failed attempts at one effort before `struggling`. |
| `[swarm]` | `handbacks_per_card` | `2` | |
| `[swarm]` | `max_members` | `24` | Active members in a run. |
| `[swarm]` | `coordinator_calls_per_run`, `coordinator_tokens_per_run` | `60`, `600_000` | Limits on a model coordinator. |
| `[swarm]` | `external_wait` | `"30m"` | How long a question waits for an outside member. |
| `[swarm]` | `on_external_timeout` | `"fallback"` | Or `"block"`. |
| `[swarm]` | `stall_after` | `"5m"` | |
| `[tiers.<name>]` | `model`, `efforts`, `effort_param` | | A tier. With no `[tiers]` table, one tier `default` is made from `[model] implementer`. |
| `[model]` roles | `effort` | `""` | Default effort for that role. |
| card | `replan_if` | `[]` | Signs that mean hand back. |

New stop codes: `handed-back`, `seat-silent`. The config template writes `[swarm]` (off) and a commented `[tiers]` example.

## Reports

- The run summary's `tokens` gains `by_member` and `by_effort`.
- Each dossier gains a team table (member, role, tier, effort, attempts, tokens), every `set` with its reason, any hand-back, and the path of the board.
- Telemetry's card record gains `team` and `handbacks`.

## What the gates still decide

- A card reaches review only through every gate, as in v0.7. No action, answer or post can pass a red gate, skip one, or mark a card done.
- Limits on attempts, builds, tokens and wall clock per card are unchanged, and a coordinator cannot raise them.
- `never_touch`, `forbidden_files`, frozen tests and the card hash are checked as before.
- Every worker call still carries `[model] headers`, so `x-cmd-zdr: 1` is on every call from every tier and seat.

## Done

Offline, with scripted members, each as a test:

1. With the swarm off, every v0.7 test passes unchanged.
2. Effort reaches the request body under `effort_param`; a bad effort or tier is refused by the config and by `set`.
3. Board: numbering without gaps under 8 writers at once; a wait returns at once for a post made before it started; a model's post is cleaned before another model sees it.
4. A scripted card's whole run is on the board in order, and its dossier lists its team.
5. `code` coordinator: a member that fails twice at `medium` runs its third attempt at `high`.
6. A scripted model coordinator starts two cards with different efforts; a bad action is refused and posted back; two bad answers hand over to the `code` policy.
7. An external supervisor: a test thread answers each question through the `board answer` code path and the card reaches review; with no answer, `fallback` finishes the card and `block` gives `seat-silent`.
8. An external coordinator drives a two-card run to review through the `swarm` commands alone.
9. A supervisor on a tier (scripted backend) answers all six ops.
10. A hand-back blocks a card with `handed-back` without spending an attempt; with a hint the worker tries again; the third hand-back counts as an attempt.
11. Three scripted cards give the same final states, gates and diffs one at a time and three at once; two cards with overlapping `touches` never run together.
12. A stuck run gets exactly one `stall` post per stall.
13. No action passes a card whose gate is red.
14. The full suite passes with `OPENFARM_E2E=1`.

**Live verification (17 Nov 2026):** DeepSeek-only `tier:dsf` runs, `reasoning_effort` comparisons (`low`/`high`/`max`), an external coordinator+supervisor run passing hidden tests, parallel Rust `cargo` runs, a 2-task x 1-repeat `bench run` spot check (`farm` 1/2 vs `solo` 1/2), and a two-project global lesson exchange ran live against Command Code `deepseek/deepseek-v4.1-flash` (`x-cmd-zdr: 1`) and Claude ACP OAuth (full numbers and stop caveats in [`docs/v0.8-build-log.md`](docs/v0.8-build-log.md)). **Still untested:** the full 12-task x 3-repeat efficacy benchmark, `claude-agent-acp`, `cargo-mutants`, `pytest`/`ruff`, remote GitHub installs, and independent third-party repositories.

## Open for the owner

My defaults, to change at will:

1. A tier coordinator and supervisor run at `max` effort.
2. `escalate_after = 2`, one effort step at a time.
3. `external_wait = "30m"`, then fall back.
4. No join key for external seats.
5. One member per role per card (no racing).
6. The supervisor stays one seat for all six ops.
