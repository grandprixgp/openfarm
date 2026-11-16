"""Stock counts per SKU."""


class StockError(ValueError):
    """A stock change was refused."""


class Inventory:
    """How many of each SKU are on hand."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def add(self, sku: str, qty: int) -> int:
        """Add `qty` of `sku` and return the new count."""
        if qty <= 0:
            raise StockError(f"quantity must be positive: {qty}")
        self.counts[sku] = self.counts.get(sku, 0) + qty
        return self.counts[sku]

    def count(self, sku: str) -> int:
        """How many of `sku` are on hand."""
        return self.counts.get(sku, 0)
