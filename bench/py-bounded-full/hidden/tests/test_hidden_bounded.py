import unittest

from tally.bounded import Bounded, FullError


class HiddenBoundedTests(unittest.TestCase):
    def test_push_below_capacity_returns_indices(self):
        bundle = Bounded(2)
        self.assertEqual(bundle.push(5), 0)
        self.assertEqual(bundle.push(6), 1)

    def test_a_refused_push_leaves_segments_alone(self):
        bundle = Bounded(2)
        bundle.push(1)
        bundle.push(2)
        with self.assertRaises(FullError):
            bundle.push(3)
        self.assertEqual(bundle.segments, [1, 2])

    def test_zero_capacity_refuses_the_first_push(self):
        with self.assertRaises(FullError):
            Bounded(0).push(1)
