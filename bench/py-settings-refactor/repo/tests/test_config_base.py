import unittest

from config.loader import load_overrides, load_settings


class LoaderTests(unittest.TestCase):
    def test_load_settings(self):
        self.assertEqual(load_settings("a = 1\n# c\n"), {"a": "1"})

    def test_load_overrides(self):
        self.assertEqual(load_overrides("a = 2", {"a": "1"}), {"a": "2"})
