import unittest

from ledger.money import MoneyError, format_cents, parse_cents


class HiddenMoneyTests(unittest.TestCase):
    def test_whole_and_short_amounts(self):
        self.assertEqual(parse_cents("7"), 700)
        self.assertEqual(parse_cents("0.5"), 50)
        self.assertEqual(parse_cents("-1.25"), -125)
        self.assertEqual(parse_cents("-0.05"), -5)

    def test_round_trips_with_format_cents(self):
        for cents in (0, 1, 99, 100, 12345, -5, -100):
            self.assertEqual(parse_cents(format_cents(cents)), cents)

    def test_refuses_what_is_not_an_amount(self):
        for text in ("", "abc", "1.2.3", "+1.00", " 1.00", "1.", ".5", "1,00"):
            with self.subTest(text=text), self.assertRaises(MoneyError):
                parse_cents(text)
