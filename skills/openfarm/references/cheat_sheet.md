# Openfarm v0.8.1 Cheat Sheet

## 22 CLI Subcommands (`--repo`, `--config`, `--json`)

Exit codes: `0` ok/eligible | `1` refused/ineligible | `2` usage/config | `3` infra/trouble

| Command | Usage |
| --- | --- |
| `init` | `openfarm init <NAME> [--dir PATH] [--idea TEXT] [--profile auto\|solo\|advisory\|active] [--acp-provider claude\|antigravity\|codex] [--mutation auto\|off\|report\|required] [--endpoint URL] [--model NAME] [--commit]` |
| `adopt` | `openfarm adopt [--dir DIR] [--lang rust\|python\|typescript] [--state-dir PATH] [--key-file PATH] [--no-suite]` |
| `bench` | `openfarm bench list [--dir bench]` / `openfarm bench run [--task ID]... [--arm farm=FILE] [--arm solo=FILE] [--repeats N] [--out DIR] [--dry-run]` |
| `install-skill` | `openfarm install-skill [--target all\|claude\|antigravity\|codex\|agents\|workspace] [--dest PATH] [--install-bin PATH] [--dry-run]` |
| `acp` | `openfarm acp [status\|login\|configure\|probe] [--provider claude\|antigravity\|codex\|all] [--mode active\|advisory\|off] [--auth oauth\|api-key\|auto] [--binary auto\|claude\|claude-agent-acp\|agy\|agy-print\|antigravity-acp\|codex\|codex-acp] [--model M] [--effort low\|medium\|high\|xhigh\|max] [--tool-policy none\|read-only] [--claudeai\|--console] [--setup-token]` |
| `tui` | `openfarm tui [--run ID] [--refresh SEC] [--once] [--view overview\|cards\|dossier\|board\|swarm\|harness\|ops]` |
| `check` | `openfarm check <CARD> [--baseline]` |
| `supervise` | `openfarm supervise (--triage-blocked \| --prepare-queue) [--card ID] [--apply] [--mm-script PATH]` |
| `run` | `openfarm run [--cards N] [--spend AMT] [--for DUR \| --until UTC] [--card ID] [--dry-run] [--script PATH] [--mm-script PATH] [--run-id ID] [--learn-from both\|project\|global]` |
| `status` | `openfarm status [--card ID]` |
| `pause` / `resume` | `openfarm pause [--reason TEXT]` / `openfarm resume [--clear-breaker] [--clear-kill]` |
| `stop` / `release` | `openfarm stop [--reason TEXT]` / `openfarm release <CARD> --reason TEXT` |
| `dossier` | `openfarm dossier <CARD> [--run RUN] [--attempt N] [--format md\|json]` |
| `verdict` / `escape` | `openfarm verdict <CARD> (clean\|minor-fix\|bounced\|scrapped) --review-tokens N [--minutes M] [--note TEXT]` / `openfarm escape <CARD> --note TEXT` |
| `done` | `openfarm done <CARD> [--note TEXT]` |
| `harness` | `openfarm harness [status\|index\|show\|promote\|demote\|reset] [TARGET] [--reason TEXT] [--yes]`; `status`, `promote <gid>` and `demote <gid>` take `--global` for the global library |
| `board` | `openfarm board (read \| post \| answer \| wait) [--run ID] [--as MEMBER] ...` |
| `swarm` | `openfarm swarm (status \| tiers \| join \| start \| set \| hint \| block \| remove) [--run ID] ...` |
| `drive` | `openfarm drive [--agent claude\|antigravity\|agy\|codex] [--seat both\|coordinator\|supervisor] [--run ID] [--cards N] [--card ID]... [--spend AMT] [--driver-name NAME] [--driver-bin PATH] [--model M] [--effort E] [--max-turns N] [--wave] [--record-verdict] [--script PATH]` |

## 9 Card Categories & Rules

- Original: `test-backfill` (no `new_symbols`), `mechanical`, `pattern` (needs `reference`), `pinned-bugfix` (no `new_symbols`), `small-feature` (needs `reference`).
- Expanded v0.4/v0.5: `feature`, `refactor`, `integration`, `port`.
- Multi-file `new_symbols = true` requires `type_changes = true` or `scaffold_contracts = true`.
- Optional fenced `[cards]`: `replan_if = [...]` triggers a clean worker hand-back (`<<<HANDBACK ... >>>`) when met; coordinator resolves (`hint`/`set`/`block`).
- Never put vague words (`handle`, `properly`, `robust`, `decide how`, `design`, `choose an approach`, `figure out how`) in `## Done`.
- Config enforces **15 strict tables** (`[project]`, `[adapters]`, `[model]`, `[policy]`, `[eligibility]`, `[brief]`, `[runner]`, `[budget]`, `[gates]`, `[cards]`, `[micromanager]`, `[state]`, `[harness]`, `[tiers.*]`, `[swarm]`). `.openfarm/**` and `.claude/**` stay in `forbidden_files`; `state_dir`, `key_file`, `auth_file`, and `[harness] dir` must stay outside the repo.

## 19 Per-Card Stop Codes (+ `killed`, `released`, `paused`)

- Non-harness-polluting (isolated): `ineligible`, `dirty-baseline`, `card-changed`, `stale-conflict`, `infra`, `lease-expired`, `handed-back`, `seat-silent`, `killed`, `released`, `paused`.
- Learning / retry-feedback codes: `tests-unprovable`, `attempts`, `no-progress`, `build-cap`, `mutation-cap`, `budget`, `cannot-split`, `checker-reject`, `supervisor-reject`, `extra-gate`, `refused` (only when model ran).
