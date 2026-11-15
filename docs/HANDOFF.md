# openfarm handoff

15 Nov 2026. For a reviewer, and for whoever takes it to the next step.

**Where:** `/home/prix/projects/openfarm` (its own git repo, branch `main`, at `HEAD` with a clean tree). It was built from `openfarm spec v0.3.md`, which sits in that folder and is **gitignored** — it is the user's document. The code and `docs/openfarm-internals.md` are written to stand without it.

**What it is:** the spec's build steps 1 and 2. A plain-code orchestrator (Python 3.11+, standard library only) that runs a cheap model on narrowly scoped cards and hands back a branch for a frontier lane. One config file per repository, five adapters, every gate a command the harness runs and parses. It has been proven on a generated toy repo and on a real public crate. **No real model has run through it** — every gate is proven against scripted answers.

---

## 1. What is finished and proven

238 tests. The fast suite is 0.4 s and skips the 47 cargo-backed cases
(`python3 -m unittest discover -s tests -t . -p 'test_*.py'`); with `OPENFARM_E2E=1`
it runs all 238 in about 55 s. Both modes are green.

| Claim | Where it is proven |
| --- | --- |
| A card runs to `in-review` on a real git repo, real cargo crate | `tests/test_e2e.py` |
| A card runs to `in-review` on **`dtolnay/anyhow`**, and the branch is exactly the intended diff | `tests/test_portability.py` |
| The branch openfarm produced passes `clippy -D warnings` (checked by hand, not in the suite) | `MECH-1` on the second repo |
| Ten gaming tricks, each asserting one trap is caught (`GamingTrickTests`) | `tests/test_e2e.py` |
| No code path pushes: allowlist + AST scan + a bare remote that stays empty | `tests/test_protocol.py` |
| Catch-up trial merge: skipped / green / conflicting | `tests/test_catchup.py` |
| A night: several cards, card cap, dry run, pause, kill switch, run lock, circuit breaker | `tests/test_night.py` |
| Planner split and its `CANNOT_SPLIT`; `append_stub`; counter-tests report-only; extra gates; build cap | `tests/test_flow_edges.py` |
| 34 regressions from three review rounds | `tests/test_regressions.py` |

**Code:** 7,766 lines of core across 26 modules. `flow.py` (1,698) is the orchestrator and holds the nine steps; `cli.py` (837) is the ten commands; `packs/rust.py` (520) is every Rust command shape and every parser.

**Commands:** all ten the spec names, sharing exit codes 0/1/2/3. Every command takes `--json`.

## 2. Where to look first when reviewing

Read in this order — each depends only on the ones before it:

1. `openfarm_core/flow.py` — `CardRun.run()` and its nine `_step_*` methods. This is the spec's per-card flow; everything else serves it.
2. `openfarm_core/gates/checks.py` — the gate functions that don't need a worktree. Short, and the place a wrong judgement would hide.
3. `openfarm_core/packs/rust.py` — `parse_tests` and `classify_paths`. Two bugs lived here: a test binary with no footer, and workspace crates classed as `other` (which silently disabled mutation).
4. `openfarm_core/git.py` — the allowlist. It is the whole no-push claim.
5. `openfarm_core/edits.py` — `apply_edits`. Path escape, additions-only, production-not-writable.

Then `tests/test_regressions.py`, which lists the bugs that were found and fixed. Each is a thing a reviewer should try to break again.

## 3. Bugs found during the build, fixed

Worth knowing because they are the shape of what this code gets wrong. Three independent review rounds found these.

**Round 1 — security and silent skips.** A path containing `..` could escape the worktree. The test writer could rewrite production code on an ordinary card, and it landed in the frozen commit where no diff gate saw it. The git allowlist checked only the first non-flag word, so `git --namespace config push` passed it. `classify_paths` missed `crates/*/src`, which silently disabled production patterns *and* the mutation gate for every workspace crate. `_units_of` counted first path segments, so `max_units` was both bypassable and wrongly tripped.

**Round 2 — my own fixes.** Fixing the production-write hole handed the raw `touches` to the check, which made `test-backfill` unrunnable: its touches *are* test files. The `--for`/`--until` deadline was computed in `time.monotonic()` and compared with `time.time()`, so every bounded night skipped every card. The stricter git scan then refused honest work — a commit message or a file path named `push`.

**Round 3 — spec conformance.** The checker ran on the implementer's model and temperature, because the config names `checker` and the flow calls `checker_pass1`/`checker_pass2`. A compile error blocked as `infra` instead of bouncing back to the implementer with the compiler's own output. The planner's split was written to `plan.json` and never used.

## 4. What is NOT proven — do not assume these work

- **No real model.** Every gate is proven against a scripted backend. Whether a cheap model can clear the gates is the pilot's question, and it is the whole point of the spec.
- **Mutation is unverified end to end.** The gate, the cap and the parser exist and are tested against a fake tool. `cargo-mutants` is not installed on this machine, and installing it is a system change needing the owner's approval. On Atlas it must also build through `tools/heavy`.
- **Cards run one at a time.** `concurrent_cards` and `concurrent_builds` are parsed and a run records a note that they are inert.
- **Spend caps cannot fire without prices.** `[model] cost_per_1k_in`/`cost_per_1k_out` are what make them mean anything; with both zero the run record says `spend_measured: false`.
- **Only the runner has an external `cmd:` transport.** The card source, model backend, telemetry and pack have protocol classes and a `hello`, but `Context.cards()` opens only the file card source. A repository whose card tool is an external command needs that wiring — this is the Atlas adapter work, spec step 3, and it is **out of scope for what was built**.
- **`coverage` and `findings_per_card` are parsed and inert.** No coverage gate exists; no finding is ever filed, so the per-card cap has nothing to cap. Both say so in the dossier's "not checked" section.
- **`check --baseline` does not use the baseline cache.** It builds base every time: correct, slower than the spec intends.
- **The circuit breaker records placeholder card ids**, not the three cards' real ids.
- **The free-space floor is not checked.** `StateDir.free_bytes` has no caller.
- **The formatter runs before the revert check**, so a touched test file that `rustfmt` rewrites is reported as a frozen-test change.
- **The two diagrams in the spec are missing** from the file (they are `[embedded content]` placeholders), so the nine-step order was reconstructed from the prose in *Per-card flow*, *Roles*, *Gates* and the stop-code table. If the real diagram orders steps differently, only `flow.py` changes.

`docs/openfarm-internals.md` carries the same list, plus the invariants to keep when changing things.

## 5. The second repository

`~/projects/openfarm-second` is a clone of `dtolnay/anyhow` (upstream `origin`
untouched), with `.openfarm/config.toml` and `cards/MECH-1.md` committed on `master`.
Its own branches: `openfarm-cards` (generated card state) and
`openfarm/MECH-1-root-cause-stops-needing` (the card's work branch — one file,
once the intended edit, which is the shape a reviewer should expect).

**It is not a farm target.** It is a portability fixture: `tests/test_portability.py` resets the card, runs it, and asserts openfarm's own tree is byte-for-byte unchanged. If you delete the folder, those 8 tests skip. If you re-clone it, re-create the config and card from the test file's expectations.

## 6. Next steps, in order

1. **Have it reviewed against the spec.** The three rounds run during the build were ad hoc; this is the one that matters. `tests/test_regressions.py` is the list of what already broke.
2. **Decide on mutation.** Install `cargo-mutants` (owner's approval, a system change) or record that `test-backfill` cannot run. Until then that category is honestly blocked: it is the only gate that can prove a backfill card, so a card in it blocks rather than reaching review.
3. **Run one real card** against a hosted ZDR endpoint with `[model] cost_per_1k_in`/`cost_per_1k_out` set. That is the first measurement of whether a cheap model clears the gates, and the pilot cannot start without it.
4. **Then the Atlas adapters** (spec step 3): `cmd:` transport for the card source, a `tools/card` shim, a `tools/heavy` runner with a real infra class, and the fenced-TOML card-field workaround. Appendix A of the spec has the config. This is deliberately untouched.
5. **Then the pilot** — 25 Rust cards, two batches, five pre-chosen frontier baselines. Cutting those cards is real work; four were attempted for the second repo and one was valid.

## 7. Practical notes for whoever continues

- **`/tmp` on the development VM is a small tmpfs with a per-user quota.** Each toy repo carries a build target. If tests are killed mid-run they leak; `rm -rf /tmp/openfarm-toy-*` clears it. Every case now tears its own toy down.
- **The state folders** are `~/.openfarm/openfarm` and `~/.openfarm/anyhow`. Outside every repository, as the spec requires. Delete them freely; they are run records, not inputs.
- **Do not add a project name or path to `openfarm_core/`.** A repository is known only through its config and adapters. The three config hooks are `[[gates.extra]]`, `[runner.env]` and `brief.test_conventions`.
- **The `openfarm` file is a shim**, not a package: it puts its own folder on `sys.path` so the tool runs from a repository root with no install. Keep it that way.
- **`git.py` is the only module allowed to run git.** A test enforces that. If you need a new git call, add it there.

## 8. Where things are

```
/home/prix/projects/openfarm/
  openfarm                     the shim
  openfarm_core/
    flow.py                    the nine steps, every gate, every stop code
    cli.py                     ten commands, exit codes, --json
    config.py globs.py cards.py protocol.py git.py state.py
    edits.py briefs.py budget.py dossier.py errors.py
    gates/eligibility.py       eligibility and shared path rules
    gates/checks.py            the gate functions
    packs/rust.py              commands, parsers, append_stub, patterns
    adapters/files.py          card source (state on its own branch)
    adapters/direct.py         runner and infra classification
    adapters/openai_compat.py  the endpoint, allowlist, scripted backend
    adapters/jsonl.py          telemetry and the record field lists
    roles/__init__.py          the four roles and what they return
  tests/                       238 tests, toy builder, regression list
  docs/HANDOFF.md              start here: proven, unproven, next
  docs/openfarm-internals.md   invariants and the honest-gaps list
  README.md                    what it is, how to run it
```

**Untested in any real use:** the tool has never been run by anyone but its author, on one machine, with a scripted model. Say so in any review that matters.
