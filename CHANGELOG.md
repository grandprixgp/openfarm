# Changelog

Newest first. Build logs for older releases are in [`docs/`](docs/).

- **`v0.20.0` hardening & dogfooding fixes (21 Nov 2026):**
  - **Security & boundaries**: fixed `[runner] confine = "bwrap"` worktree overmount (`state_dir` hide ordering) and added a real-`bwrap` test; hardened `openfarm web` (fixed Findings Dismiss status to `"dismissed"`, eliminated `onclick` XSS, blocked CLI flag injection, added `Host`/`Origin` DNS-rebinding protection, enforced non-loopback token checks, and fixed full wildcard IP parsing); hardened ACP read-only `.git/**` & `search-overlaps-protected`, `dir_fd`/`O_NOFOLLOW` login refresh, and HTTP redirect `Authorization` stripping.
  - **Reliability & concurrency**: added cross-process `flock` locking on `cards-source` (`Store` / `FileCardSource`), fixed `FileCardSource.stale()` & `next(lane=...)`, made `store.json` writes atomic with proper migration ordering, added process-identity safety to `run.lock` & `daemon.json` (plus detached child reaping eliminating `ResourceWarning`s), added `Git.create_branch` error checking + `quotepath`/`fsmonitor` hardening, switched `Board.wait()` to byte-offset tailing, and fixed `HarnessLibrary` lock release during LLM reflection & probe-rollback restore.
  - **Build pipeline, CLI, packs & Journey UX**: circuit breaker now halts active sequential, `--follow`, and swarm runs mid-wave without silent clearing; `_check_progress` no longer aborts cards when 100% of named tests pass; deferred `land --accept-blocked` state/verdict mutations until after trial-merge and tests succeed; fixed Journey crashes/traps (walking back to `Plan` after all goals accepted, `"Something's wrong…"` reopening finished goals, filtering `"split"` cards, retrying `given_back` tasks, preserving `self.fix` across `d` full-diff + `Esc`, and per-step cancel isolation); fixed `model_choices` overlay for string/omitted roles, `[tiers]`, and custom/empty tier efforts; defaulted `[runner] typescript` to `"auto"` preserving Vitest/Jest detection; improved Go subtest & crashed-package parsing; made `revert-check` binary-safe with `PYTHONDONTWRITEBYTECODE=1`; fixed Python 3.14 `tests/` without `__init__.py` & `src/`-layout `openfarm try`; added `daemon start --follow`; made `card skip` return `ok: true`.
  - **Feature wiring & lean cleanup**: wired `[notifications]` into run and card lifecycle events and fixed + wired `[factory] auto_land` (`source.mark_done`, locking, and summary output); consolidated the three native ACP bridges onto `BaseNativeAcpServer` (-721 lines, fixing SIGTERM deadlock, prompt-thread crashes, concurrent session overwrite, `agy` temp-dir leaks, and `claude` fail-open default); synced skill/plugin versions to `0.20.0`, pruned dead zero-caller helpers, and untracked `openfarm spec v0.7.md`/`v0.8.md`.

- **`v0.20.0` — Go anywhere on the journey, and what v0.15.0 left open:**
  - **The journey can be walked**: `[` and `]`, the mouse, or `Ctrl+K` open any step already reached and any goal, as it stands. Looking changes nothing, a step not reached yet says what has to happen first, and `n` or `Esc` returns to the step that needs you.
  - **Three more plan checks**, each with its repair shown on the Plan step: a task whose work needs the network without `network = true`, a task listing a test that already exists and passes, and a task that contradicts a test already in the project.
  - **A build restarts by itself** after a builder's question is answered, a fix is applied or a task is retried. A task that stops the same way twice, or three times in any way, waits for you instead of retrying. A build that paused itself says why, and Resume carries on building.
  - **Starting stubs are repaired, not refused**: a TypeScript stub with a parameter default or `void name;` lines is tidied and accepted, a member added to an existing interface counts as a declaration, and a stub that is still refused is named as that on the stopped-task screen, with Retry.
  - **Builders' questions are sorted first**: the Architect answers what it can from the spec, a note about tooling is filed, and only a question that is yours is shown as one.
  - **Try it judges the first screen**, at desktop and phone size separately: what you would have to scroll to is not "seen". It also shows the page's main colours as swatches.
  - **The token share counts the Architect** too (questions, spec, plans, fixes, answers), beside builders and reviewers.
  - **Projects, providers and sign-in, settings and builders' notes** are panes of the one screen (`openfarm_tui/journey_more.py`). Nothing in `Ctrl+K` opens an older screen; `openfarm tui --view studio|review` still does.
  - **The theme follows the terminal** by asking it for its background colour; `COLORFGBG` is only the fallback.
  - **Pick the model and the effort for each job** (Architect, reviewer, builders), for one project or for all of them: `openfarm models`, the Models pane behind `Ctrl+K`, and `.openfarm/models.toml`. `config.toml` is never rewritten. See [`docs/models.md`](docs/models.md).
  - **`openfarm setup`** asks, in order, who plans and reviews, where the builders' requests go, which of the models on offer builds, and how much effort each job gets. Every answer is also a flag, so it runs unattended too.
  - **One more plan check**: a task whose words still ask for a file the project forbids changing is held until the Architect rewords it. It used to pass the plan and stop at build time.
  - **An unreadable reviewer answer is asked for once more** before the task stops. One bad answer used to pause the whole build.

- **`v0.15.0` — One screen, one home for tasks, try it:**
  - **One screen** (`openfarm_tui/journey.py`) replaces the five tabs: a journey rail (Idea, Questions, Spec, Plan, Build, Try it, Accept), the one thing in front of you, one main button that Enter presses, a fixed activity strip, and two themes. Which step shows is worked out from the project's state (`openfarm_tui/journey_state.py`), so reopening lands where the project is. The older screens are behind `Ctrl+K`.
  - **Tasks, the spec and the decisions log live in one place**, the `openfarm-cards` branch, and every change goes through one function that records who changed what and why (`openfarm_core/store.py`, `openfarm card add|show|history|skip|migrate`). Nothing is written to `cards/` or `.openfarm/PROJECT_SPEC.md` on your main branch any more; an older project is moved over once with `openfarm card migrate`, which shows any task whose two copies differ and waits for your choice.
  - **Plan checks** (`openfarm_core/preflight.py`) run before a plan is shown: scaffolding, files named but not listed, order, and shared names two tasks must agree on. Repairs are shown with their reason; a task that still fails is held back, not built.
  - **A stopped task says what kind of stop it is** (`openfarm_core/blocked.py`): the task is wrong (apply a shown fix and retry), the builder fell short (retry), or a check may be wrong (run it yourself and see the raw output).
  - **Try it** (`openfarm_core/tryit.py`, `openfarm try`): starts the app, takes desktop and phone screenshots with a headless Chromium, and checks that what the goal names can be seen, not merely that it is in the page.
  - **Iteration keeps what exists**: "Something's wrong" becomes fix tasks under the current goal, "New feature" adds a goal, and task numbers are never reused.
  - **Builds belong to the background service** and `openfarm run --follow` carries one build through tasks that wait on each other.
  - Words: task, goal, plan, build, accept in the UI and help; `card`, `chunk` and `wave` stay in file names and commands.

- **`v0.14.0` — Detached Daemon, Tailscale Web Companion & Notifications:**
  - `openfarm daemon {start,stop,status,logs}` (`openfarm_core/daemon.py`) + TUI detached controls & `● DAEMON` pill.
  - `openfarm web [--tailscale] [--bind 127.0.0.1|tailscale] [--port 8787]` (`openfarm_core/web.py`): stdlib HTTP + SSE + per-session CSRF token + first-class `tailscale serve` HTTPS & `tailscale ip -4` integration.
  - Optional `[notifications]` config table (`openfarm_core/notify.py`) for webhook JSON POSTs and desktop `notify-send` alerts with event filtering.
- **`v0.13.0` — Conversational Frontier Architect, Spec & Chunks, Live Architect Log, Adaptive Router & Timeline:**
  - Replaced rigid 3-box Rust-biased Grill with **Idea → Conversational Grill → Living Spec & Ordered Chunks (`.openfarm/CHUNKS.json`) → Split Chunk into Cards → Iterate**.
  - Stack-authentic Architect prompts (authentic runnable TypeScript/Web apps instead of Rust `Result<T, DomainError>` libraries), deterministic `.js → .ts` relative import auto-repair, and automatic placeholder modules for brand-new card `touches` files.
  - Always-visible shared **Live Architect Progress Banner & Streaming Log** (`#architect-panel`, `#architect-log`) across Studio sub-views with `[MM:SS]` timer, 15s heartbeats, and file-by-file write progress.
  - **Adaptive Model Router** (`openfarm routing`, `openfarm_core/routing.py`) & **Attempt Timeline / Phase-B Resume Inspection** (`openfarm timeline <CARD>`, `openfarm_core/timeline.py`), plus `Spec` and `Routing` tabs in `4 Engine`.
  - Global provider `endpoint` + `x-cmd-zdr: 1` header inheritance in `init` and `load_config`.
- **`v0.12.0` — Architectural Invariants, Runtime Acceptance & Golden Replay Vault:**
  - `[invariants]` deterministic gate (`forbidden_imports`, `max_file_bytes`, `require_exports`; `openfarm_core/invariants.py`).
  - `[acceptance]` runtime "Proof-of-Life" CLI & ephemeral loopback HTTP `{PORT}` smoke gate (`openfarm_core/acceptance.py`).
  - `openfarm replay {list,add,run}` Golden Replay Vault (`openfarm_core/replay.py`).
- **`v0.11.0` — Cards-as-the-DAG & Autonomous Integration Branch:**
  - `openfarm plan` topological DAG planner with `wave` + `depends` ordering, cycle detection (`cyclic-dag`), and parallel batches (`openfarm_core/dag.py`).
  - Deterministic trial-rebase (`openfarm_core/factory.py`, `StopCode.REBASE_CONFLICT`) and opt-in `[factory] auto_land` onto local `openfarm/integration` (`openfarm land --promote-integration`).
- **`v0.10.0`–`v0.10.2` — Textual Workbench, Global Providers & Projects Registry, Factory Ergonomics:**
  - Optional `openfarm_tui/` Textual workbench (`0 Projects`, `1 Studio`, `2 Review`, `3 Findings`, `4 Engine`, `Ctrl+K` Palette) with automatic stdlib `curses` fallback.
  - Global `providers.toml` (`openfarm providers {status,set,probe}`) & global `projects.toml` (`openfarm projects {list,add,edit,remove,discover}`, strictly non-destructive to project files on disk).
  - In-TUI Card Editor & Split, deduplicated Findings Inbox + Wave Synthesis, Touches Conflict Matrix, and `openfarm doctor [--fix] [--probe]`.


- **v0.9.0–v0.9.2:** `[gates] counter_tests = "reject"` default, named supervision profiles (`--profile lean|balanced|strict`), `openfarm review`, local serial merge queue (`openfarm land`), Go language pack (`openfarm_core/packs/go.py`), TypeScript `vitest`/`jest` support, polyglot per-unit languages (`[units.<name>]`), `openfarm auto`, `openfarm triage`, `openfarm frontier-review`, and high-contrast stdlib `curses` Automode TUI. Build logs: [`v0.9.0`](docs/v0.9.0-build-log.md), [`v0.9.1`](docs/v0.9.1-build-log.md), [`v0.9.2`](docs/v0.9.2-build-log.md).
- **v0.8.5–v0.8.8:** Lean supervisor prompts & compact payloads, merged `shape_and_scaffold`, conditional supervision (`shape_when`, `pre_review_when`), default `medium → high → max` retry effort ladder, fresh-vs-cached token accounting, changed-line mutation for Python/TypeScript and `--in-diff` `cargo-mutants` for Rust, opt-in runner bubblewrap confinement (`[runner] confine = "bwrap"`, `confine_network = "on"`), pre-review Done-text rule, and pre-freeze test linting. Build logs & benches: [`bench-v0.8.7.md`](docs/bench-v0.8.7.md), [`bench-v0.8.8.md`](docs/bench-v0.8.8.md), [`pilot-v0.8.6.md`](docs/pilot-v0.8.6.md).
- **v0.8.0–v0.8.1:** Swarm board (`board.jsonl`), model tiers & effort (`[tiers.*]`), parallel non-colliding cards, `StallMonitor`, `codex-acp` & `antigravity-acp`, `openfarm drive`, TypeScript pack, global harness memory, and efficacy bench (`openfarm bench`).

