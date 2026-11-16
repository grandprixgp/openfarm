import unittest
from unittest import mock

from config import loader
from config.loader import LoadError, load_overrides, load_settings, parse_line


class HiddenLoaderTests(unittest.TestCase):
    def test_behaviour_is_unchanged(self):
        text = "# top\n\na = 1\nurl = x=y\n"
        self.assertEqual(load_settings(text), {"a": "1", "url": "x=y"})
        self.assertEqual(load_overrides("a = 2\n", {"a": "1", "b": "3"}),
                         {"a": "2", "b": "3"})
        with self.assertRaisesRegex(LoadError, "line 3: expected key = value"):
            load_settings("a = 1\n\nbroken\n")
        with self.assertRaisesRegex(LoadError, "line 1: unknown key 'z'"):
            load_overrides("z = 1", {"a": "1"})

    def test_parse_line_reports_its_line_number(self):
        with self.assertRaisesRegex(LoadError, "line 7: expected key = value"):
            parse_line("novalue", 7)
        self.assertIsNone(parse_line("   ", 2))

    def test_both_loaders_use_parse_line(self):
        for call in (lambda: load_settings("a = 1\nb = 2"),
                     lambda: load_overrides("a = 1\nb = 2", {"a": "", "b": ""})):
            with mock.patch.object(loader, "parse_line",
                                   wraps=loader.parse_line) as spy:
                call()
            self.assertEqual(spy.call_count, 2)
