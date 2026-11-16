import unittest

from stock.inventory import Inventory


class AddTests(unittest.TestCase):
    def test_add_returns_the_new_count(self):
        inv = Inventory()
        self.assertEqual(inv.add("a", 2), 2)
        self.assertEqual(inv.add("a", 3), 5)
