# openfarm internals

How the core is put together, for whoever works on it next. The specification is
`openfarm spec v0.3.md` in this folder; this page is about the code.

## What this is

`openfarm` runs a cheap model on narrowly scoped cards and hands back a branch for a
frontier lane to review. A plain-code orchestrator drives every step and runs every
gate itself, so no model ever reports its own result.

Run it from a repository root with no install:

```
./openfarm --repo /path/to/repo check CARD-1 --json
./openfarm --repo /path/to/repo run --cards 3
```

`openfarm` is a shim that puts its own folder on `sys.path` and calls
`openfarm_core.cli:main`. Python 3.11 or later, standard library only.

## The one rule the core keeps

**No project's assumptions in the core.** A repository is known to openfarm only
through `.openfarm/config.toml` and the adapters that file names. If a change needs a
project's name or path in `openfarm_core/`, it belongs in a config hook or an adapter
instead. The three hooks are `[[gates.extra]]`, `[runner.env]` and
`brief.test_conventions`.

## The modules

| Module | What it owns |
| --- | --- |
| `cli.py` | The ten commands, the shared exit codes, `--json`, and one `Context` per command |
| `config.py` | Reading and validating `.openfarm/config.toml`; unknown tables are an error |
| `cards.py` | The `Card` model, its canonical JSON, its hash |
| `protocol.py` | The adapter envelope, the five adapters' required ops, and the spec's fallbacks |
| `git.py` | **The only place the core runs git.** A subcommand allowlist that has no `push` |
| `globs.py` | Never-touch, forbidden and touch matching |
| `edits.py` | Applying edits; exactly-once search/replace, whole files, additions-only |
| `briefs.py` | Building briefs; card text fenced as data, never-touch files never loaded |
| `budget.py` | Model builds, fixed builds, mutation builds, tokens, spend, wall clock |
| `flow.py` | The per-card flow: nine steps, every gate, every stop code |
| `dossier.py` | The nine-section dossier, as Markdown and as JSON |
| `state.py` | The state folder layout, the run lock, pause, the kill switch, the breaker |
| `roles/` | The four model roles' calls, and parsing what they return |
| `gates/` | Eligibility, and the gate functions that don't need a worktree |
| `packs/` | The language pack interface, and the Rust pack |
| `adapters/` | Card source, runner, model backend, telemetry, and the `cmd:` transport |

## The per-card flow

`flow.CardRun.run()` is the whole card. Nine steps, and each gate is a command the
harness runs and parses:

1. **Claim and eligibility** — read, hash, and every eligibility rule, before any
   model call. A rejected card is *released*, because the card was wrong.
2. **Branch, worktree, target** — one of each per card, so a stale shared build can't
   pass.
3. **Baseline green** — the unit suite twice, with fail-fast off. Both must agree,
   and only then is the result cached.
4. **Planner** — off by default. It runs when the done text names two groups.
5. **Phase A, the test writer** — write the named tests, freeze their hashes, commit.
   The commit matters: an attempt starts from a clean tree.
6. **Named tests exist, and fail-before is a runtime failure** — a compile error
   proves nothing, so a card needs `new_symbols` and a stub to prove itself.
7. **Phase B, the implementer** — per attempt: diff within touches, banned patterns,
   frozen tests, suite, lint. Any failed gate goes back with only the diff and the
   failure output.
8. **The end gates** — pass-after twice, suite, formatting, workspace lint, extra
   gates, no test loss, revert check, mutation, checker pass 1 and 2.
9. **Catch-up** — a trial merge in a throwaway worktree, building the merged result,
   committing nothing.

## Rules worth knowing before you change something

- **Never judge a run by its exit code.** A `known_failing` test still makes cargo
  exit 101. The Rust pack parses per-test lines plus one footer per test binary, and
  a binary with no footer means the run died partway.
- **The model-build cap and the fixed-build cap are separate.** A healthy card needs
  more than ten builds if the gates count against it. Fixed gates sit outside.
- **A refused edit spends no attempt and no build.** If you add a refusal path, keep
  that property, or cards will die on a wrong search block.
- **`known_failing` matches exact full names**, never substrings, and the config
  holds it because `.openfarm/**` is forbidden to the farm.
- **The test writer writes the card's named test files**, even when `touches` doesn't
  list them. The implementer may not touch a test at all.
- **A card that names no tests is a `mechanical` card.** Eligibility allows it, the
  test writer is skipped, and the existing suite is the judge.
- **Text with newlines cannot go in a TOML basic string.** The card file writer uses
  a multi-line literal string, because TOML has no `\n` escape.

## Testing

```
python3 -m unittest discover -s tests -t . -p 'test_*.py'          # fast, no cargo
OPENFARM_E2E=1 python3 -m unittest discover -s tests -t . -p 'test_*.py'
```

The fast suite covers config, globs, the card model, the Rust pack's parsing against
captured cargo output, edits and stubs, eligibility, every gate on synthetic input,
the protocol and its fallbacks, and the no-push guarantee.

The end-to-end suite builds a real toy repository (`tests/toy.py`) with real git and a
real cargo crate, and drives cards through it with a scripted model backend. Each
planted card asserts one gaming trick is caught. Every toy is removed by its case's
`tearDown`, because each one carries a build target and `/tmp` on the development VM
is a small tmpfs with a per-user quota.

`tests/test_portability.py` points the core at a second, real public crate
(`dtolnay/anyhow`, cloned to `~/projects/openfarm-second`) to show nothing about the
core is toy-shaped. That repository's config and cards live in it, not here.

## What is not proven

- **No real model has run.** Every gate is proven against scripted answers. Whether
  a cheap model can clear the gates is the pilot's question.
- **Mutation is unverified end to end.** The gate and its parser are implemented and
  tested against a fake tool, but `cargo-mutants` is not installed on this machine and
  installing it is a system change. On Atlas it must also build through `tools/heavy`.
- **Concurrency is sequential.** `concurrent_cards` and `concurrent_builds` are in the
  config and the budget, but cards run one after another. The per-card state is
  already isolated enough for a pool; the loop in `cli._do_run` is what would change.
- **Leases are implemented and dormant.** The file card source keeps a lease; a card
  source without one falls back to its own stale list.
- **The two diagrams in the spec are missing** from the file, so the flow's step order
  was reconstructed from the prose in *Per-card flow*, *Roles*, *Gates* and the
  stop-code table.
- **The TypeScript pack does not exist.** It comes after the pilot, and `pack_for`
  refuses it plainly rather than pretending.

## Adding a language pack

Implement `packs/base.Pack`: the command shapes, `parse_list`, `parse_tests`,
`classify_paths`, `signatures`, `apply_edit`, `format`, `patterns`, `error_signature`
and, where the language has them, `append_stub` and mutation. Two properties matter
more than the rest: `parse_tests` must report a run that died partway, and
`apply_edit` must refuse a match that isn't unique. Register it in `pack_for`.

## Adding an adapter

An adapter is a small module or an external command speaking JSON on stdin and
stdout. `protocol.CmdAdapter` is the transport; `protocol.REQUIRED_OPS` says which ops
each kind must answer, and `protocol.OPTIONAL_OPS` which the spec gives a fallback
for. A project tool that doesn't speak the protocol is wrapped in a shim, and the
shim is kept where the farm can't edit it.
