---
name: openfarm-installer
description: >-
  Install the Openfarm v0.7.0 CLI package, native Claude ACP OAuth bridge,
  /openfarm-grill-me design interview, and /openfarm-initialize skills into
  Claude Code, Antigravity, or workspace customization directories from a single
  repository or package target.
---

# Openfarm Installer Skill (`v0.7.0`)

Follow these steps when the user asks you to read and install this skill (`"Read and install this skill <github_or_local_link>"`):

## Step 1 — Obtain and Install Openfarm

### Option A — Pure Python 3.11+ (Zero Dependencies, Works Without `pip`/`uv`/`pipx`)

If you already cloned or downloaded this repository locally at `<OPENFARM_REPO>` (or clone `<REPO_URL>` into `~/.local/share/openfarm`):

```bash
python3 --version   # Must be >= 3.11
python3 -m openfarm_core.cli install-skill \
  --target all \
  --install-bin ~/.local/bin \
  --json
```

Ensure `~/.local/bin` is on `PATH` (`export PATH="$HOME/.local/bin:$PATH"`), or pass another directory on `PATH` to `--install-bin`.

### Option B — Package Install (`uv`, `pipx`, or `pip`)

```bash
# Using uv (recommended if installed):
uv tool install <GITHUB_URL_OR_LOCAL_PATH>

# Or pipx:
pipx install <GITHUB_URL_OR_LOCAL_PATH>

# Or pip (editable or standard):
pip install --user <GITHUB_URL_OR_LOCAL_PATH>

# Then install the bundled skill, slash command, and plugin files:
openfarm install-skill --target all --json
```

If deploying into a specific project workspace only (without touching `~/.claude` or `~/.gemini/config`), run:

```bash
openfarm install-skill --target workspace --dest /path/to/workspace --json
```

## Step 2 — Verify Installation

Confirm both the CLI binary and installed customization files exist:

```bash
openfarm --help
```

Check that the output lists `init`, `install-skill`, and `acp` alongside `run`, `status`, `pause`, `resume`, `stop`, `release`, `dossier`, `check`, `verdict`, `escape`, `supervise`, and `harness`, and confirm the installed skill files exist in at least one active discovery root:
- `~/.claude/skills/openfarm-initialize/SKILL.md`, `~/.claude/skills/openfarm-grill-me/SKILL.md`, `~/.claude/commands/openfarm-initialize.md`, and `~/.claude/commands/openfarm-grill-me.md`
- `~/.gemini/config/skills/openfarm-initialize/SKILL.md`, `~/.gemini/config/skills/openfarm-grill-me/SKILL.md`, and `~/.gemini/config/plugins/openfarm/plugin.json`
- `~/.agents/skills/openfarm-initialize/SKILL.md` and `~/.agents/skills/openfarm-grill-me/SKILL.md`

Check Frontier Claude OAuth readiness (optional interactive login once if not signed in):
```bash
openfarm acp status --json
# If oauth_ready is false and you want Frontier ACP supervision via OAuth:
# openfarm acp login
```

## Step 3 — Confirm Readiness to the Owner

Report:
1. Where `openfarm` was installed (`which openfarm` or `~/.local/bin/openfarm`).
2. Which customization roots received `openfarm-grill-me`, `openfarm-initialize`, and `openfarm`.
3. Whether Frontier Claude ACP OAuth is ready (`openfarm acp status --json`) — noting Workers still authenticate via external API `key_file`.
4. Tell the human owner they can now grill/initialize any project from an empty directory with:
   ```text
   /openfarm-initialize <project_name> <project_idea>
   ```
   or stress-test decisions first with `/openfarm-grill-me <project_idea>` (for example: `/openfarm-initialize calorie-log An app to track my calories`).
