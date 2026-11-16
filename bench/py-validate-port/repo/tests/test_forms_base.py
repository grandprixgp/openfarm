import unittest

from forms.validators import ValidationError, validate_username


class UsernameTests(unittest.TestCase):
    def test_a_good_username_is_trimmed(self):
        self.assertEqual(validate_username(" ada_99 "), "ada_99")

    def test_capitals_are_refused(self):
        with self.assertRaises(ValidationError):
            validate_username("Ada")
