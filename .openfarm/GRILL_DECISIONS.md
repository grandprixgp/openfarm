# openfarm v0.8 Design Decisions (openfarm-grill-me, cut short)

- **Idea:** openfarm v0.8. A shared board, a team whose members a coordinator picks (tier and effort), parallel cards, and seats an outside agent or person can hold.
- **Grill mode:** one question asked, then the owner said "draft it up" (16 Nov 2026). Only the three decisions below are the owner's. Everything else in `openfarm spec v0.8.md` is my default, listed there under "Open for the owner", and not yet confirmed.

## Decisions (the owner's)

1. **Scope.** The full version in one release: a coordinator that picks its own members, and parallel cards. Not the smaller board-and-one-seat version I recommended.
2. **Effort.** The coordinator sets effort per member. It matters most for now, because every member is the same model.
3. **Members.** DeepSeek V4.1 Flash only for now, at `low`, `medium`, `high` or `max`.

## Build route

By hand on branch `v0.8`, by another agent overnight, from `docs/v0.8-BUILD-HANDOFF.md`. No live model calls during the build; the owner runs the first live test.

---

# Earlier: openfarm v0.7 Design Decisions (openfarm-grill-me)

- **Idea:** openfarm v0.7. Learning extraction can mark lessons as project-independent, proven ones become global across every openfarm project, and each project chooses whether it applies its own lessons, global ones, or both.
- **Grill mode:** serial, Q1 to Q8, every recommendation taken. Confirmed 16 Nov 2026.
- **Execution setup:** as v0.6. Frontier = Claude ACP (`auth = "oauth"`, `mode = "active"`) | Workers = DeepSeek V4.1 Flash on Command Code's Provider API with `x-cmd-zdr: 1` (key outside the repo).

## Facts found before asking

- Each project keeps one harness library at `<state_dir>/harness` (or `[harness] dir`, outside the repo): entries, the active overlay, revisions, a ledger and metrics. Nothing records which project a lesson came from.
- Entries are already scoped by language, card kind, role and failure class, each of which may be `*`.
- Pointing two projects' `[harness] dir` at one folder shares a library crudely: one probe cycle, one score set, and each project's `never_touch` judging the other's lessons.
- openfarm's own library holds 34 lessons, 24 of them supervisor notes on harness bookkeeping, most of the rest template cues ("double-check constraints related to `runtime-assert`"). anyhow's holds none.

## Decisions

1. **Storage.** A second, global library at `[harness] global_dir` (default `~/.openfarm/global/harness`, refused inside any repo), in the same format. Each project keeps its own library. A lesson enters the global library only by promotion and records its source project. Projects read both libraries and write only their own.
2. **Scope at extraction.** Every new lesson gets `scope = "project" | "general"`. The supervisor's reflection chooses, with a reason. Plain code forces `project` when the lesson's text names one of this repo's tracked file paths, a module or symbol name from them, a card ID, or the project name.
3. **Promotion.** A `general` lesson is copied to the global library automatically once it is `active` in its home project, has `helped >= 3`, and `hurt < helped`. `openfarm harness promote <id> --global` and `openfarm harness demote <id> --global` force either way. Every move is a global ledger event naming the source project.
4. **Applying.** `[harness] learn_from = "both" | "project" | "global"` (default `"both"`) and `[harness] share = true | false` (default `true`, whether this project's proven lessons may go global). `openfarm run --learn-from X` overrides `learn_from` for one run. On equal fit a project lesson ranks above a global one, and `max_cues_per_brief` caps both together.
5. **Judging.** Each project scores global lessons in its own ledger. A global lesson with `hurt > helped` in a project is vetoed for that project only. It is retired globally once 2 or more projects veto it, or when its help minus hurt summed across projects goes below 0. A new global lesson enters a project's probe cycle like the project's own new lessons.
6. **Eligibility.** Only worker-surface lessons (planner, test writer, implementer, context hints) and supervisor coaching about code may go global. Supervisor notes on harness bookkeeping stay local, spotted by plain code from harness terms. Template cues from the deterministic extractor stay local. The 34 existing lessons are marked `project`, so the global library starts empty.
7. **Privacy.** Promotion re-runs the existing scans (gate-bypass text, assertion leaks, unsafe paths) and the project-name check from 2, and refuses a lesson over 400 characters or holding a code block. `openfarm harness status --global` lists global lessons with their source projects.
8. **Build route.** The frontier lead builds the protected core: config keys, flow wiring, and the two-library read in `harness_memory.py`. The farm works the contained cards live on openfarm itself (scope check, promotion, veto and retire, CLI, bench arm option, docs). Everything lands on branch `v0.7`. Nothing is pushed without the owner's ask.

## Done

- Offline tests: a lesson proven in project A appears in project B's brief; B vetoes it; two vetoes retire it; `learn_from` and `share` are obeyed; a lesson naming project A's file is never promoted.
- Full suite green, end-to-end tests included.
- Global learning across two real projects stays **untested** until it has happened.

---

# Earlier: openfarm v0.6 Design Decisions (openfarm-grill-me)

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
    Answered 16 Nov 2026: Command Code's Provider API on the GOAT plan's credits, DeepSeek V4.1 Flash (`deepseek/deepseek-v4.1-flash`), with `x-cmd-zdr: 1` on every call through `[model] headers` (a model without a zero-retention upstream fails rather than falls back). The key is outside the repo at the config's `key_file`.
24. `openfarm spec v0.6.md` beside the earlier specs (local, like them: specs are gitignored); version 0.6.0; README "Not yet tested" stays honest.
25. **Waves:** 0 by hand (A1); 1 = two starter cards on openfarm itself (Python pack, CLI output); 2 = `adopt`, `bench`, batch grill; 3 = docs and test backfill.
