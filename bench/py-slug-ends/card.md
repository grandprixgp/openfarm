+++
id = "CARD-1"
title = "A slug has no dash at either end"
state = "ready"
category = "pinned-bugfix"
unit = "."
touches = ["textkit/slug.py"]
depends = []

[[tests]]
name = "test_punctuation_at_the_ends_leaves_no_dash"
file = "tests/test_slug_ends.py"
asserts = "slugify(\"Hello, World!\") == \"hello-world\""
+++

## Done

- `slugify` never returns a slug that starts or ends with `-`: `slugify("Hello, World!")` is `"hello-world"` and `slugify("  --Draft--  ")` is `"draft"`.
- Text with no letters or digits gives `""`: `slugify("!!!")` is `""`.
- Runs of letters and digits inside the text are still joined by single dashes.
