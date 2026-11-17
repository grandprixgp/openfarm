# Openfarm Grill-Me Decision Tree & Record Template

## 5-Branch Decision Tree

Advance through branches sequentially, skipping any branch already settled by repository state or earlier answers:

1. **Seam & Domain Laws (`never_touch`: `src/invariants.rs`)**
   - What numeric bounds, overflow/underflow behaviors, units, or validation laws must hold unconditionally?
   - What exact `DomainError` variants should represent violations?
   - What must lesser coding models never see or relax?

2. **Public Crate Surface & Golden Reference Slice**
   - Reminder: Openfarm runs integration tests from `tests/<subject>_tests.rs`, so every tested type/method must be `pub`.
   - Which **one** ~50–120 LOC module cleanly demonstrates our checked error style, constructors, and doc comments for workers to copy via `reference = "src/<slice>.rs"`?

3. **v1 Scope, Crates & Custom Gates**
   - What stays strictly out of v1 (non-goals: UI framework, async runtime, external DB, concurrency primitives)?
   - Should v1 use zero external crates (faster offline builds, zero lockfile churn) or specific upfront dependencies?
   - Do generated files or extra tools require a `[[gates.extra]]` command?

4. **Frontier (Claude / Antigravity / Codex ACP or Driver Mode) & Workers (ZDR API Key)**
   - Frontier micromanager supports `claude-acp`, `antigravity-acp`/`agy-acp`, and `codex-acp` with `auth = "oauth"`, `binary = "auto"` (or external Driver mode via `openfarm drive --agent claude|antigravity|codex`). Confirm `active` vs `advisory` vs `solo`, and verify `oauth_ready: true` (`openfarm acp status --provider all` / `openfarm acp login --provider ...`).
   - Recommended: Claude with `tool_policy = "none"`; Codex with `binary = "codex-exec"` and `tool_policy = "none"`. Antigravity only after the owner confirms their plan's terms allow openfarm to start `agy` unattended on OAuth, or with an API key. Codex and Antigravity run inside a bubblewrap boundary; `confine = "off"` lets the child read whatever the owner's user can.
   - Confirm Worker ZDR OpenAI-compatible endpoint, model ID, card spend cap, and whether mutation testing is enabled (`cargo-mutants` present vs `off`).

5. **Wave 1–3 Card Plan & Concrete Assertions**
   - Break work into non-overlapping `touches` + `tests/<subject>_tests.rs` cards.
   - Pin exact edge cases (empty inputs, boundaries, duplicates, max limits) so every card's `## Done` names exact inputs, outputs, and `Result` variants.

---

## Output Artifact: `.openfarm/GRILL_DECISIONS.md`

Persist settled decisions cleanly (no API keys or credentials):

```markdown
# <project_slug> Design Decisions (openfarm-grill-me)

- **Idea:** <plain-English idea>
- **Execution Setup:** Frontier = <Claude|Codex|Antigravity> ACP (`auth = "<oauth|api-key>"`, `mode = "<active|advisory|off>"`, `binary = "<auto|codex-exec|...>"`) | Workers = `<endpoint>` / `<model>` (key outside repo)

## 1. Protected Seam (`src/invariants.rs` — `never_touch`)
- ...

## 2. Public API & Golden Reference Slice
- Golden slice: `src/<golden>.rs`
- Key `pub` types: ...

## 3. v1 Boundaries & Dependencies
- Dependencies: ...
- Non-goals: ...

## 4. Wave 1–3 Card Outline
- Wave 1: ...
- Wave 2: ...
- Wave 3: ...

## 5. Settled Edge Cases (Zero Vague Clauses)
- ...
```
