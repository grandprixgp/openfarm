import unittest

from textkit.slug import slugify


class HiddenSlugTests(unittest.TestCase):
    def test_both_ends_are_trimmed(self):
        self.assertEqual(slugify("  --Already--Slugged--  "), "already-slugged")

    def test_nothing_sluggable_gives_empty(self):
        self.assertEqual(slugify("!!!"), "")
        self.assertEqual(slugify(""), "")

    def test_the_middle_is_unchanged(self):
        self.assertEqual(slugify("a1 b2...c3"), "a1-b2-c3")
        self.assertEqual(slugify("Hello World"), "hello-world")
