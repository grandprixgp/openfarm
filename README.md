# openfarm

A plain-code orchestrator that runs a cheap, non-frontier coding model on narrowly
scoped cards and hands back a branch for a frontier lane to review.

Every gate is a command the harness runs and parses. No model ever reports its own
result. The core holds no project's assumptions: it is one config file per repository
and five adapters.

Built from `openfarm spec v0.3.md`, which is in this folder and is the authority on
what the tool is for. `docs/openfarm-internals.md` is about the code.

## Run it

Python 3.11 or later, standard library only. No install.

```
./openfarm --repo /path/to/repo check CARD-1 --json   # 0 eligible, 1 ineligible
./openfarm --repo /path/to/repo run --cards 3         # a night
./openfarm --repo /path/to/repo status --json
./openfarm --repo /path/to/repo dossier CARD-1
```

`openfarm --help` lists all ten commands. Exit codes are the same everywhere: `0`
done, `1` refused, `2` usage or config, `3` trouble.

## Configure it

One file, `.openfarm/config.toml`, on the repository's base branch. The spec's
Config section is the reference; `~/projects/openfarm-second/.openfarm/config.toml`
is a worked example for a real crate.

A repository supplies at most:

- a **card source** — the default is card files under `cards/`, with state kept on a
  branch of its own so concurrent cards can't conflict;
- a **runner** — the default runs `cargo` or `npm` directly;
- a **language pack** — Rust ships; TypeScript does not yet;
- a **model backend** — any OpenAI-compatible endpoint, with an allowlist and a key
  read from a file outside the repository;
- a **telemetry sink** — JSONL by default.

Anything odd about a repository goes through three hooks, never into the core:
`[[gates.extra]]`, `[runner.env]`, and `brief.test_conventions`.

## What it will not do

- **Merge, rebase or push.** The core has no code path that runs `git push`, and a
  test checks a bare remote stays empty after a full run.
- **Design or decompose work.** Cards come in fully specified and must pass
  eligibility before any model call.
- **Touch a seam.** Never-touch is file globs, checked against the card's touches and
  against every attempt's diff.
- **Run a model with tools or a shell.** Models return edits; the harness applies
  only those inside the allowed paths, and runs every command itself.
- **Let a model report a gate result.** Every gate is code.

## Test it

```
python3 -m unittest discover -s tests -t . -p 'test_*.py'            # fast
OPENFARM_E2E=1 python3 -m unittest discover -s tests -t . -p 'test_*.py'
```

The end-to-end suite builds a real toy repository with real git and a real cargo
crate, and plays each gaming trick against the gates: a test that asserts nothing, a
test that never compiled, a frozen test rewritten, an ignored test, a production
`.unwrap()`, a leftover stub, an edit outside touches, and more.

## Status

The core and adapters, with the Rust pack, running cards to `in-review` on a real toy
repository and on a real public crate (`dtolnay/anyhow`), whose branch passes
`clippy -D warnings` when checked by hand.

**Unproven:** no real model has run through it (every gate is proven against scripted
answers), mutation testing is not installed on this machine, cards run one at a time,
and spend caps need prices in the config to mean anything. `docs/openfarm-internals.md`
lists the rest, with the tests that back each claim.
