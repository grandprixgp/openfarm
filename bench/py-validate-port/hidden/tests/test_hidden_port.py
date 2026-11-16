import unittest

from forms.validators import ValidationError, validate_port


class HiddenPortTests(unittest.TestCase):
    def test_accepts_the_range_and_trims(self):
        self.assertEqual(validate_port(" 443 "), 443)
        self.assertEqual(validate_port("1"), 1)
        self.assertEqual(validate_port("65535"), 65535)

    def test_refuses_out_of_range_and_non_digits(self):
        for value in ("65536", "", "   ", "80a", "-1", "+80", "1_000", "8 0"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_port(value)
