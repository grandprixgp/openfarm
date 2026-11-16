---
description: Grill the owner along Openfarm's decision tree before initializing or expanding a project
argument-hint: <project_idea_or_feature_scope>
---

# `/openfarm-grill-me`

Topic / target received: `$ARGUMENTS`

Follow the installed `openfarm-grill-me` skill strictly:

1. Silently gather facts first via `openfarm acp status --json`, `rustc --version`, `cargo --version`, and workspace inspection (existing `.openfarm/config.toml`, `src/`, `cards/`). Never ask the user facts you can look up directly.
2. Map unresolved decisions across the 5 Openfarm branches in `openfarm-grill-me/prompts/grill_protocol.md` (Protected Seam Invariants -> Public API & Golden Slice -> v1 Scope & Dependencies -> Frontier Claude ACP OAuth + Worker API Key Setup -> Wave Card Boundaries).
3. Ask **one frontier question at a time**, formatted with `❓ **Q<N>** - **<Title>**` and `➡️ **Recommended:** <choice>` worded so `"yes"` accepts your recommendation, waiting for the user's answer before moving on.
4. Once the decision frontier is empty and the user confirms shared understanding, write `.openfarm/GRILL_DECISIONS.md` (never writing secrets/keys inside the repo) and proceed smoothly into `/openfarm-initialize` or Wave card authoring.
