+++
id = "CARD-1"
title = "Take stock out of the inventory"
state = "ready"
category = "small-feature"
unit = "."
touches = ["stock/inventory.py"]
reference = "stock/inventory.py"
new_symbols = true
depends = []

[[tests]]
name = "test_remove_lowers_the_count"
file = "tests/test_remove.py"
asserts = "after add(\"a\", 5), remove(\"a\", 2) returns 3 and count(\"a\") == 3"

[[tests]]
name = "test_removing_more_than_on_hand_is_refused"
file = "tests/test_remove.py"
asserts = "after add(\"a\", 1), remove(\"a\", 2) raises StockError"
+++

## Done

- New method `Inventory.remove(sku: str, qty: int) -> int`, written like `add`: it takes `qty` of `sku` away and returns the new count.
- It raises `StockError` when `qty` is not positive or is more than `count(sku)`, and then leaves the counts unchanged.
- When a count reaches 0 the SKU is dropped from `counts`, so `counts` never holds a zero.
