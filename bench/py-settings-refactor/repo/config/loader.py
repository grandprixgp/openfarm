"""Read `key = value` settings files."""


class LoadError(ValueError):
    """A settings line could not be read."""


def load_settings(text: str) -> dict[str, str]:
    """Settings from `key = value` lines; blank lines and `#` comments are skipped."""
    settings: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise LoadError(f"line {number}: expected key = value")
        key, value = line.split("=", 1)
        settings[key.strip()] = value.strip()
    return settings


def load_overrides(text: str, base: dict[str, str]) -> dict[str, str]:
    """`base` with each `key = value` line applied over it; unknown keys refused."""
    merged = dict(base)
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise LoadError(f"line {number}: expected key = value")
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in merged:
            raise LoadError(f"line {number}: unknown key {key!r}")
        merged[key] = value.strip()
    return merged
