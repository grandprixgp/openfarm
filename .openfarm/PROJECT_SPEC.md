# openfarm, as a project openfarm works on

openfarm runs cheaper models on narrowly scoped cards and hands back branches for a
frontier reviewer. From v0.6 it can work on its own source. This file is the
contract for that: what workers may touch, what they may not, and how v0.6 is cut
into waves. The design decisions behind it are in `GRILL_DECISIONS.md`.

## The protected seam (`[policy] never_touch`)

A worker never edits the code that judges its card:

| Path | Why it is protected |
| --- | --- |
| `openfarm_core/gates/**` | every gate and eligibility rule |
| `openfarm_core/flow.py` | the per-card pipeline that runs the gates |
| `openfarm_core/protocol.py` | the adapter protocol and `cmd:` packs |
| `openfarm_core/config.py` | config validation, including `never_touch` itself |
| `openfarm_core/edits.py` | how model edits are parsed and applied |
| `openfarm_core/git.py` | the only git access, including the no-push guarantee |
| `openfarm_core/errors.py` | stop codes |
| `openfarm_core/selfhost.py` | the pinned-runner guard |
| `openfarm_core/adapters/acp.py`, `adapters/claude_native_acp.py` | the frontier link |

`pyproject.toml`, `.openfarm/**` and `.claude/**` are forbidden files: no new
dependencies, and no edits to the farm's own configuration.

## Running openfarm on itself

openfarm refuses a real run when its own code lives inside the repository or the
state folder it would use (`openfarm_core/selfhost.py`). Run a pinned copy:

```bash
git -C ~/projects/openfarm tag v0.6.0-wave0        # once per pin
git -C ~/projects/openfarm worktree add --detach ~/.openfarm/runners/v0.6.0-wave0 v0.6.0-wave0
python3 ~/.openfarm/runners/v0.6.0-wave0/openfarm --repo ~/projects/openfarm run --cards 2
```

Every dossier names the copy that judged it (`Judged by openfarm ... (commit ...)`).

## Module ownership map

| Area | Files | Who changes it |
| --- | --- | --- |
| Gates and pipeline | the seam above | frontier only |
| Language packs | `openfarm_core/packs/python.py`, `packs/rust.py`, `packs/base.py` | cards |
| CLI presentation | `openfarm_core/cli.py` (output, new subcommands) | cards |
| Bootstrap and adopt | `openfarm_core/bootstrap.py`, a new `adopt.py` | cards |
| Records | `openfarm_core/dossier.py`, `state.py`, `budget.py` | cards |
| Bench | a new `openfarm_core/bench.py` and `bench/` tasks | cards |
| Skills | `openfarm_core/skill_assets/**`, `skills/**` (kept identical) | cards |

## Reference slice

`openfarm_core/packs/python.py` is the reference for pattern cards: one class
behind a small interface, pure functions for parsing, exact errors from
`openfarm_core.errors`, and a test file per concern (`tests/test_python_pack.py`).

## Waves

- **Wave 0 (frontier, done by hand):** the core asks the pack instead of assuming
  cargo; the Python pack; this config; the pinned-runner guard.
- **Wave 1:** two starter cards on openfarm itself that prove the pipeline end to
  end on Python: one in the Python pack, one in CLI output.
- **Wave 2:** `openfarm adopt`, `openfarm bench`, batch grill mode.
- **Wave 3:** docs and test backfill.

Workers stay on the scripted backend until the owner picks a zero-retention,
no-training endpoint.
