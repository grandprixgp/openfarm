import unittest

from textkit.slug import slugify


class SlugTests(unittest.TestCase):
    def test_spaces_become_dashes(self):
        self.assertEqual(slugify("hello world"), "hello-world")
