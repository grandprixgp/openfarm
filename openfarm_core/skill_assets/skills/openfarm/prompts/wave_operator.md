# Wave Operator Protocol (Anti-Context-Rot Guide)

Follow this protocol whenever running Openfarm waves on a project:

1. **Never paste raw `cargo` build logs or intermediate failed attempt diffs into frontier conversation.** Rely on `openfarm run --cards N --json` (ACP mode) or `openfarm drive --agent claude|antigravity|codex --seat both --cards N --json` (Driver mode) to return compact per-card summaries (`final_state`, `stop_code`, `branch`, `harness_revision`).
2. **Respect the local-only boundary**:
   - Openfarm never pushes branches.
   - Review `openfarm dossier <CARD-ID>` (note: test diff precedes production diff) and `git diff main..<branch>` (`openfarm drive --wave` can add the driver's own first look, but it does not replace this review) before merging onto `main` with `git merge --no-ff`.
3. **Always record `openfarm verdict`** (`clean`, `minor-fix`, `bounced`, `scrapped` with `--review-tokens <n>`) yourself; a `verdict.json` marked `by: driver/<agent>` from `--wave --record-verdict` is the driver's opinion, not yours: check it and record your own, so payback and review metrics remain accurate.
4. **Recover cleanly from blocks**:
   - `dirty-baseline`: inspect failing test on `main`, fix or add exact test name to `known_failing` outside the farm, clear cache if needed, re-run `--dry-run`.
   - `infra`: fix disk/endpoint/ACP issue (if OAuth expired, run `openfarm acp login --provider claude|antigravity|codex`), run `openfarm resume`, `openfarm release <ID> --reason "infra retry"`, and re-run.
   - `acp-auth-required`: Frontier OAuth credentials are missing or expired — run `openfarm acp login --provider <provider>` (or `claude auth login --claudeai` / `agy acp --login` / `codex login`), verify with `openfarm acp probe --provider <provider> --json`, then `openfarm release <ID> --reason "oauth refreshed"`.
   - Circuit breaker tripped (`3` consecutive blocks with same stop code): inspect `breaker.json`, run `openfarm supervise --triage-blocked`, fix card scope, then `openfarm resume --clear-breaker`.
5. **Curate `<state_dir>/harness/` carefully**:
   - Let `HarnessLibrary` auto-promote, probe for `probe_window`, and auto-demote regressions automatically.
   - Only intervene via `openfarm harness demote <ID_OR_rev-XXXX> --reason "..."` or `promote <ID>` if an entry introduces noisy guidance. Never try to bypass `IMMUTABLE_GATE_KEYS` (they are ignored/rejected).
6. **Refresh `openfarm-cards` after editing card files on `main`**:
   - If you rewrite a blocked card on `main` and merge/commit it, release stale card state via `openfarm release <ID> --reason "re-scoped card"` or update the card on `openfarm-cards` if needed before re-running.
