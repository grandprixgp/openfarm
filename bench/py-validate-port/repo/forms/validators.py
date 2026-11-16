"""Field validators. Each returns the cleaned value or raises `ValidationError`."""


class ValidationError(ValueError):
    """A field value was refused."""


def validate_username(value: str) -> str:
    """A username: 3 to 20 ASCII lowercase letters, digits or underscores, trimmed."""
    cleaned = value.strip()
    if not 3 <= len(cleaned) <= 20:
        raise ValidationError("username must be 3 to 20 characters")
    if not all("a" <= ch <= "z" or "0" <= ch <= "9" or ch == "_" for ch in cleaned):
        raise ValidationError("username may hold only a-z, 0-9 and _")
    return cleaned
