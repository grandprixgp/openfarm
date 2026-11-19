---
name: openfarm-grill-me
description: >-
  Interview the owner one question at a time along Openfarm's design decision
  tree before initialization or new feature waves. Maps protected seam
  invariants, public APIs, golden slice, Frontier Claude ACP OAuth + Worker API
  setup, and concrete card Done assertions into .openfarm/GRILL_DECISIONS.md.
---

# Openfarm Grill-Me Skill (`v0.8.8`)

Use this skill whenever the user invokes `/openfarm-grill-me`, asks to stress-test a project idea before `/openfarm-initialize`, or wants to sharpen a multi-card feature specification on an existing Openfarm repository.

Read [prompts/grill_protocol.md](./prompts/grill_protocol.md) for the exact 5-branch decision tree and `.openfarm/GRILL_DECISIONS.md` output template.

## Core Rules

1. **Find Facts Silently First — Never Ask the User What Tools Can Read**:
   Before asking your first question, silently inspect the environment and workspace:
   ```bash
   openfarm acp status --json
   ls -la ~/.config/openfarm/ 2>/dev/null || true
   rustc --version && cargo --version
   command -v cargo-mutants || echo "no cargo-mutants"
   ```
   On existing repositories, also inspect `.openfarm/config.toml`, `.openfarm/PROJECT_SPEC.md`, `src/`, and `cards/` directly (or via a read-only subagent). Ask the user only for **decisions**, never inspectable facts.

2. **One Frontier Question at a Time, Worded for `"yes"`**:
   Compute the decision tree **frontier** (decisions whose prerequisites are already settled) and ask **exactly one question at a time**—never dump a full questionnaire in one message:

   ```text
   ❓ **Q1** - **<Decision Title>**: <concise question with options or trade-offs>

   ➡️ **Recommended:** <concrete recommendation>
   ```

   Word each question so a simple `"yes"` accepts your recommended answer. Wait for the reply, recompute the frontier, and ask the next unsettled question.

3. **Cover Every Branch Needed for Openfarm Eligibility**:
   Walk through all five branches in [prompts/grill_protocol.md](./prompts/grill_protocol.md):
   - **Branch 1 — Protected Seam Invariants (`src/invariants.rs`)**: bounds, overflow rules, validation laws, and `DomainError` variants locked under `never_touch`.
   - **Branch 2 — Public Surface & Golden Reference Slice**: `pub` types (`rust_tests_in_separate_files = true`) and which single vertical slice workers will imitate.
   - **Branch 3 — Scope Boundaries, Crate Dependencies & Extra Gates**: non-goals for v1 and pinned dependencies (`**/Cargo.toml` and `Cargo.lock` are `forbidden_files` for workers).
   - **Branch 4 — Frontier (Claude / Antigravity / Codex ACP or Driver Mode) & Worker (API Key) Runtime**:
     - Default Frontier supervision is native ACP via OAuth (`claude-acp`, `antigravity-acp`/`agy-acp`, or `codex-acp` via `init --acp-provider <provider>`, or automated Driver mode `openfarm drive --agent claude|antigravity|codex`). If `openfarm acp status --json` shows `oauth_ready: false`, confirm whether the owner wants to run `openfarm acp login --provider <provider>` now (default) or fall back to `--profile solo`.
     - Recommend Claude with `tool_policy = "none"`. For Codex recommend `binary = "codex-exec"` with `tool_policy = "none"`. For Antigravity, openfarm starts `agy` with no person typing: ask the owner to confirm their Antigravity plan's terms allow that before choosing OAuth, and offer an API key (`auth = "api-key"`, `auth_file`) as the other route. Codex and Antigravity need bubblewrap (`bwrap`) for their boundary; never recommend `confine = "off"`, which lets the child read whatever the owner's user can.
     - Confirm Worker ZDR OpenAI-compatible endpoint/model + `~/.config/openfarm/<project>.key` and spend caps (unless offline/scripted test only).
   - **Branch 5 — Concrete Wave 1–3 Boundaries & Eligibility**: resolve edge-case outputs so card `## Done` bullets never contain banned vague words (`handle`, `properly`, `robust`, `decide how`).

4. **Shared Understanding Gate Before Writing Code**:
   Do **not** run `openfarm init` edits or write application code while frontier questions remain open. When the frontier is empty, summarize the agreed design tree concisely and wait for the owner's confirmation (`"Confirmed"` / `"Proceed"`). Then write `.openfarm/GRILL_DECISIONS.md` (with zero secrets or API keys) and hand off into `/openfarm-initialize` (greenfield) or `openfarm` wave card authoring (existing repo).

## Batch Mode (`--batch`)

With `--batch`, take the recommended answer at every decision point. Then show one numbered list grouped by branch, one line of reason each. The owner replies with numbers to challenge or `Confirmed`; only challenged numbers reopen as one-at-a-time questions.

A decision that needs a fact only the owner has is listed as `needs you`, never filled with a guess. `.openfarm/GRILL_DECISIONS.md` records `mode: batch` and which numbers were challenged.
