import unittest

from tally.bounded import Bounded


class EmptyTests(unittest.TestCase):
    def test_an_empty_bundle_has_no_segments(self):
        self.assertEqual(Bounded(3).segments, [])
