"""Turn titles into URL slugs."""

import re


def slugify(text: str) -> str:
    """Lowercase `text` and join its runs of letters and digits with single dashes."""
    lowered = text.lower()
    return re.sub(r"[^a-z0-9]+", "-", lowered)
