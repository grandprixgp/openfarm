import unittest

from stock.inventory import Inventory, StockError


class HiddenInventoryTests(unittest.TestCase):
    def test_removing_everything_drops_the_sku(self):
        inv = Inventory()
        inv.add("a", 2)
        self.assertEqual(inv.remove("a", 2), 0)
        self.assertNotIn("a", inv.counts)
        self.assertEqual(inv.count("a"), 0)

    def test_refusals_leave_counts_alone(self):
        inv = Inventory()
        inv.add("a", 3)
        for qty in (0, -1, 4):
            with self.subTest(qty=qty), self.assertRaises(StockError):
                inv.remove("a", qty)
        self.assertEqual(inv.counts, {"a": 3})

    def test_an_unknown_sku_is_refused(self):
        inv = Inventory()
        with self.assertRaises(StockError):
            inv.remove("missing", 1)
        self.assertEqual(inv.counts, {})
