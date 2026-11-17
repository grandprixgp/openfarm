---
name: openfarm-installer
description: >-
  Install the Openfarm v0.8.1 CLI package, multi-provider ACP OAuth bridges and
  Driver mode (Claude Code, Antigravity agy, Codex), /openfarm-grill-me design
  interview, and /openfarm-initialize skills into Claude Code, Antigravity,
  Codex, or workspace customization directories from a single repository or
  package target.
---

# Openfarm Installer Skill (`v0.8.1`)

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

If deploying into a specific runtime or project workspace only (for example `--target codex`, `--target claude`, `--target antigravity`, or `--target workspace` without touching global home folders), run:

```bash
openfarm install-skill --target codex --json
openfarm install-skill --target workspace --dest /path/to/workspace --json
```

## Step 2 — Verify Installation

Confirm both the CLI binary and installed customization files exist:

```bash
openfarm --help
```

Check that the output lists all 22 subcommands (`init`, `adopt`, `bench`, `install-skill`, `acp`, `tui` alongside `run`, `status`, `pause`, `resume`, `stop`, `release`, `dossier`, `check`, `verdict`, `escape`, `supervise`, `harness`, `done`, `board`, `swarm`, and `drive`), and confirm the installed skill files exist in at least one active discovery root (Claude Code's root is `$CLAUDE_CONFIG_DIR` when set, else `~/.claude`; Codex's is `$CODEX_HOME` when set, else `~/.codex`):
- `~/.claude/skills/openfarm-initialize/SKILL.md`, `~/.claude/skills/openfarm-grill-me/SKILL.md`, `~/.claude/commands/openfarm-initialize.md`, and `~/.claude/commands/openfarm-grill-me.md`
- `~/.gemini/config/skills/openfarm-initialize/SKILL.md`, `~/.gemini/config/skills/openfarm-grill-me/SKILL.md`, and `~/.gemini/config/plugins/openfarm/plugin.json`
- `~/.codex/skills/openfarm-initialize/SKILL.md`, `~/.codex/skills/openfarm-grill-me/SKILL.md`, and `~/.codex/openfarm-AGENTS.md` (whether Codex reads these by itself is untested)
- `~/.agents/skills/openfarm-initialize/SKILL.md` and `~/.agents/skills/openfarm-grill-me/SKILL.md`

Check Frontier OAuth readiness across Claude, Antigravity (`agy`), and Codex (`codex`) for ACP mode or Driver mode (`openfarm drive`). `acp status` never starts `agy`:
```bash
openfarm acp status --provider all --json
# If oauth_ready is false for your chosen Frontier provider (claude|antigravity|codex):
# openfarm acp login --provider claude
# Zero-token handshake for one provider; --provider all probes Claude and Codex and skips Antigravity:
# openfarm acp probe --provider claude --json
```

Before you pick a Frontier provider, know how each one's child is held (details: `docs/USAGE-AND-FIRST-DEPLOYMENT.md`, section 5):
- **Claude** (default, recommended with `tool_policy = "none"`): held by Claude Code's own settings. openfarm puts no OS boundary around it unless `[micromanager] confine = "bwrap"`.
- **Codex:** set `[micromanager] binary = "codex-exec"` with `tool_policy = "none"`. openfarm turns Codex's tools off with flags and adds a bubblewrap (`bwrap`) boundary when `bwrap` works. With `binary = "auto"` a standalone `codex-acp` on `PATH` is used instead, and that server completed no turn on a ChatGPT login in testing.
- **Antigravity:** always inside the `bwrap` boundary; refused without `bwrap`. openfarm starts `agy` with no person typing. Before using it on an Antigravity OAuth login, tell the owner to check that their Antigravity plan's terms allow that; the other route is an API key (`auth = "api-key"` with `auth_file`). Do not set up Antigravity OAuth as the default. Every Antigravity change in v0.8.1 is untested live.
- `[micromanager] confine = "off"` removes the boundary: the child can then read whatever the owner's user can. Do not set it without the owner's say-so.

## Step 3 — Confirm Readiness to the Owner

Report:
1. Where `openfarm` was installed (`which openfarm` or `~/.local/bin/openfarm`).
2. Which customization roots received `openfarm-grill-me`, `openfarm-initialize`, and `openfarm`.
3. Whether Frontier ACP OAuth is ready (`openfarm acp status --provider all --json`) for ACP mode (`init --acp-provider claude|antigravity|codex`) and Driver mode (`openfarm drive --agent claude|antigravity|codex`) — noting Workers still authenticate via external API `key_file`, whether `bwrap` works (the "Confinement" part of `acp status`), and, if the owner wants Antigravity, that they must check their plan's terms first.
4. Tell the human owner they can now grill/initialize any project from an empty directory with:
   ```text
   /openfarm-initialize <project_name> <project_idea>
   ```
   or stress-test decisions first with `/openfarm-grill-me <project_idea>` (for example: `/openfarm-initialize calorie-log An app to track my calories`), or adopt an existing Rust/Python/TypeScript repository with `openfarm adopt --dir . [--lang rust|python|typescript]` (a repo with both a Rust or Python manifest and a `package.json` adopts as Rust or Python; pass `--lang typescript` for its TypeScript).
