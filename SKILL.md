---
name: openfarm-installer
description: >-
  Install the Openfarm v0.5 CLI package and /openfarm-initialize frontier
  architect skills into Claude Code, Antigravity, or workspace customization
  directories from a single repository or package target.
---

# Openfarm Installer Skill (`v0.5.0`)

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

Check that the output lists `init` and `install-skill` alongside `run`, `status`, `pause`, `resume`, `stop`, `release`, `dossier`, `check`, `verdict`, `escape`, `supervise`, and `harness`, and confirm the installed skill files exist in at least one active discovery root:
- `~/.claude/skills/openfarm-initialize/SKILL.md` and `~/.claude/commands/openfarm-initialize.md`
- `~/.gemini/config/skills/openfarm-initialize/SKILL.md` and `~/.gemini/config/plugins/openfarm/plugin.json`
- `~/.agents/skills/openfarm-initialize/SKILL.md`

## Step 3 — Confirm Readiness to the Owner

Report:
1. Where `openfarm` was installed (`which openfarm` or `~/.local/bin/openfarm`).
2. Which customization roots received `openfarm-initialize` and `openfarm`.
3. Tell the human owner they can now start any project from an empty directory with:
   ```text
   /openfarm-initialize <project_name> <project_idea>
   ```
   (for example: `/openfarm-initialize calorie-log An app to track my calories`).
