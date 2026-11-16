import unittest

from ledger.money import format_cents


class FormatTests(unittest.TestCase):
    def test_format_cents(self):
        self.assertEqual(format_cents(1234), "12.34")
        self.assertEqual(format_cents(-5), "-0.05")
