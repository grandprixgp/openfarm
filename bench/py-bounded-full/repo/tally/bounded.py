"""A bounded list of segments."""


class FullError(Exception):
    """The bundle is at capacity."""


class Bounded:
    """Up to `capacity` segments."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.segments: list[int] = []

    def push(self, value: int) -> int:
        """Add a segment and return its index."""
        self.segments.append(value)
        return len(self.segments) - 1
