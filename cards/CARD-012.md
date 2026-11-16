+++
id = "CARD-012"
title = "Batch mode for openfarm-grill-me"
state = "ready"
touches = ["openfarm_core/skill_assets/skills/openfarm-grill-me/SKILL.md", "skills/openfarm-grill-me/SKILL.md", "openfarm_core/skill_assets/skills/openfarm-initialize/SKILL.md", "skills/openfarm-initialize/SKILL.md"]
depends = []
category = "small-feature"
reference = "openfarm_core/skill_assets/skills/openfarm-grill-me/SKILL.md"
frontier_estimate = 0.50
new_symbols = false
unit = "."
type_changes = false

[[tests]]
name = "test_both_grill_copies_have_the_batch_section"
file = "tests/test_batch_grill_skill.py"
asserts = "each grill SKILL.md contains the line \"## Batch Mode (`--batch`)\" and the phrases \"needs you\" and \"Confirmed\""

[[tests]]
name = "test_the_copies_are_identical"
file = "tests/test_batch_grill_skill.py"
asserts = "the two grill SKILL.md files are byte-identical, and so are the two initialize SKILL.md files"

[[tests]]
name = "test_initialize_offers_batch_grill"
file = "tests/test_batch_grill_skill.py"
asserts = "each initialize SKILL.md contains \"--batch-grill\""
+++

## Done

- Both copies of the grill skill gain a section headed `## Batch Mode (\`--batch\`)` saying: with `--batch`, take the recommended answer at every decision point; then show one numbered list grouped by branch, one line of reason each; the owner replies with numbers to challenge or `Confirmed`; only challenged numbers reopen as one-at-a-time questions.
- The section says a decision that needs a fact only the owner has is listed as `needs you`, never filled with a guess, and that `GRILL_DECISIONS.md` records `mode: batch` and which numbers were challenged.
- Both copies of the initialize skill say `--batch-grill` runs step 0 in batch mode. Serial questions stay the default.
- Each pair of copies stays byte-identical.
