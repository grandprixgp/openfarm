# Openfarm v0.5.1 Cheat Sheet

## 15 CLI Subcommands (`--repo`, `--config`, `--json`)

Exit codes: `0` ok/eligible | `1` refused/ineligible | `2` usage/config | `3` infra/trouble

| Command | Usage |
| --- | --- |
| `init` | `openfarm init <NAME> [--dir PATH] [--idea TEXT] [--profile auto\|solo\|advisory\|active] [--mutation auto\|off\|report\|required] [--endpoint URL] [--model NAME] [--commit]` |
| `install-skill` | `openfarm install-skill [--target all\|claude\|antigravity\|agents\|workspace] [--dest PATH] [--install-bin PATH] [--dry-run]` |
| `acp` | `openfarm acp [status\|login\|configure\|probe] [--mode active\|advisory\|off] [--auth oauth\|api-key\|auto] [--binary auto\|claude\|claude-agent-acp] [--model M] [--effort low\|medium\|high] [--tool-policy none\|read-only] [--claudeai\|--console] [--setup-token]` |
| `check` | `openfarm check <CARD> [--baseline]` |
| `supervise` | `openfarm supervise (--triage-blocked \| --prepare-queue) [--card ID] [--apply] [--mm-script PATH]` |
| `run` | `openfarm run [--cards N] [--spend AMT] [--for DUR \| --until UTC] [--card ID] [--dry-run] [--script PATH] [--mm-script PATH] [--run-id ID]` |
| `status` | `openfarm status [--card ID]` |
| `pause` / `resume` | `openfarm pause [--reason TEXT]` / `openfarm resume [--clear-breaker] [--clear-kill]` |
| `stop` / `release` | `openfarm stop [--reason TEXT]` / `openfarm release <CARD> --reason TEXT` |
| `dossier` | `openfarm dossier <CARD> [--run RUN] [--attempt N] [--format md\|json]` |
| `verdict` / `escape` | `openfarm verdict <CARD> (clean\|minor-fix\|bounced\|scrapped) --review-tokens N [--minutes M] [--note TEXT]` / `openfarm escape <CARD> --note TEXT` |
| `harness` | `openfarm harness [status\|index\|show\|promote\|demote\|reset] [TARGET] [--reason TEXT] [--yes]` |

## 9 Card Categories & Rules

- Original: `test-backfill` (no `new_symbols`), `mechanical`, `pattern` (needs `reference`), `pinned-bugfix` (no `new_symbols`), `small-feature` (needs `reference`).
- Expanded v0.4/v0.5: `feature`, `refactor`, `integration`, `port`.
- Multi-file `new_symbols = true` requires `type_changes = true` or `scaffold_contracts = true`.
- Never put vague words (`handle`, `properly`, `robust`, `decide how`, `design`, `choose an approach`, `figure out how`) in `## Done`.
- Config always appends `.openfarm/**` and `.claude/**` to `forbidden_files`. `state_dir`, `key_file`, `auth_file`, and `[harness] dir` must stay outside the repo.

## 17 Per-Card Stop Codes (+ `killed`, `released`, `paused`)

- Non-harness-polluting (isolated): `ineligible`, `dirty-baseline`, `card-changed`, `stale-conflict`, `infra`, `lease-expired`, `killed`, `released`, `paused`.
- Learning / retry-feedback codes: `tests-unprovable`, `attempts`, `no-progress`, `build-cap`, `mutation-cap`, `budget`, `cannot-split`, `checker-reject`, `supervisor-reject`, `extra-gate`, `refused` (only when model ran).
