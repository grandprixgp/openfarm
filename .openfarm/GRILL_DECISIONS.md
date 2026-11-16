# openfarm v0.6 Design Decisions (openfarm-grill-me)

- **Idea:** openfarm v0.6. openfarm works on itself, works on or initializes into an existing project, ships an efficacy test suite anyone can run with their own frontier and worker choice, and gains a batch mode for grill-me.
- **Grill mode:** Q1 and Q2 serial, then batch (the owner switched at Q2). Batch list confirmed 16 Nov 2026 with no challenges.
- **Execution setup:** Frontier = Claude ACP (`auth = "oauth"`, `mode = "active"`) | Workers = open, scripted backend until the owner picks a zero-retention, no-training endpoint (key outside the repo).

## Facts found before asking

- Only a Rust pack exists (`openfarm_core/packs/rust.py`). `flow.py` imports `RustPack` and `banned_patterns_rust` directly, filters `.rs` files, and derives units from `Cargo.toml`.
- `openfarm init` scaffolds a new Rust crate only and refuses a non-empty directory unless forced.
- Nothing measures efficacy yet; the README pilot is a TODO.
- No worker endpoint or key exists (`~/.config/openfarm/` is absent). `ruff`, `pytest`, `mutmut` are not installed. openfarm is stdlib-only and tests with `unittest`.

## Q1. Python pack scope (serial)

`unittest` first; the pack uses pytest only when the repo configures it (`[tool.pytest...]` in `pyproject.toml`, `pytest.ini`, or `[tool:pytest]` in `setup.cfg`). Mutation testing is `off` for Python in v0.6.

## A. Working on itself

1. **Order:** wave 0 is written by hand by the frontier lead: Rust specifics in `flow.py`, `eligibility.py`, `briefs.py` and `gates/checks.py` move behind the pack interface; the Python pack; openfarm's own `.openfarm/config.toml`. The farm then builds the rest of v0.6 on itself as cards.
2. **Pinned runner:** when openfarm works on itself it runs from an installed copy of a tagged commit, never from the worktree it edits. The dossier records which copy ran.
3. **`never_touch`:** `openfarm_core/gates/**`, `flow.py`, `protocol.py`, `config.py`, `edits.py`, `git.py`, `errors.py`, `adapters/acp.py`, `adapters/claude_native_acp.py`. **`forbidden_files`:** `pyproject.toml`, `.openfarm/**`, `.claude/**`.
4. **Python banned patterns:** bare `except:`, `except ...: pass`, `# noqa`, `# type: ignore`, `breakpoint()`, `pdb`, skip decorators, and `raise NotImplementedError` in production code.
5. **Python tests:** `tests/test_<subject>.py`, one per card; tests reach only names without a leading underscore.
6. **Python checks:** the unit is the directory holding `pyproject.toml`/`setup.py`. Every card gets a syntax check (an `ast` parse of every `.py` file; it writes no `.pyc`). `ruff check` and `ruff format --check` run only when `ruff` is on `PATH`; otherwise formatting is recorded as not checked. No type checking in v0.6.
7. **Dependencies:** openfarm stays stdlib-only.

## B. Existing projects

8. **`openfarm adopt`:** writes only `.openfarm/config.toml`, a starter `.openfarm/PROJECT_SPEC.md` and `cards/`; never touches existing source. `init` on a non-empty directory points at `adopt`.
9. **Detection:** `Cargo.toml` → rust; `pyproject.toml`/`setup.py` → python; both → refuse without `--lang`; anything else → refuse with a clear message.
10. **Refusals:** not a git repository, or uncommitted changes. Base branch comes from git.
11. **Red baseline:** `adopt` runs the suite once and proposes the failing tests as `[policy] known_failing` for the owner to confirm, rather than refusing.
12. **`/openfarm-initialize` on an existing repo:** grill, then the frontier proposes `never_touch` from the code and picks an existing module as the reference slice.

## C. Efficacy suite

13. **`openfarm bench`** runs a task set shipped in `bench/`: 12 tasks (6 Rust, 6 Python) over small-feature, pinned-bugfix, pattern and refactor; 3 repeats by default.
14. **Hidden acceptance tests** per task, copied in only at scoring.
15. **Two arms:** farm (workers + optional frontier micromanager) and frontier-solo through the same harness.
16. **Measures:** hidden-test pass rate, tokens, spend, wall time, diff size as a review-effort proxy; real review minutes when recorded with `openfarm verdict`.
17. **Model choice:** frontier = Claude ACP or any OpenAI-compatible endpoint; workers = any OpenAI-compatible endpoint; the scripted backend gives a zero-cost dry run that CI runs.
18. **Output:** local JSON + Markdown report with per-category pass rates and cost per merged card. Nothing is uploaded.

## D. Batch grill mode

19. `/openfarm-grill-me --batch` and `/openfarm-initialize --batch-grill`; serial stays the default.
20. Numbered list grouped by branch; the owner replies with numbers to challenge or "Confirmed"; only challenged items reopen serially. `GRILL_DECISIONS.md` records the mode and which items were challenged.
21. Decisions that need owner-only facts are marked "needs you", never guessed.

## E. Runtime and release

22. Frontier: Claude ACP over the owner's existing claude.ai OAuth login, `active`. Ready (`oauth_ready: true`).
23. **Workers: needs you.** Zero data retention and no training on requests. Scripted backend until chosen.
24. `openfarm spec v0.6.md` beside the earlier specs (local, like them: specs are gitignored); version 0.6.0; README "Not yet tested" stays honest.
25. **Waves:** 0 by hand (A1); 1 = two starter cards on openfarm itself (Python pack, CLI output); 2 = `adopt`, `bench`, batch grill; 3 = docs and test backfill.
