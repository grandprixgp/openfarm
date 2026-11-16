from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.packs.python import PythonPack


class PythonPackListingTests(unittest.TestCase):
    def test_a_parametrised_id_with_spaces_is_listed(self):
        result = PythonPack(runner="pytest", ruff=False).parse_list(
            "tests/test_x.py::test_p[a b]\n"
        )
        self.assertEqual(
            result,
            {"tests": [{"name": "test_p[a b]", "binary": "tests/test_x.py"}]},
        )

    def test_a_class_parametrised_id_with_spaces_is_listed(self):
        result = PythonPack(runner="pytest", ruff=False).parse_list(
            "tests/test_x.py::TestK::test_p[1 2]\n"
        )
        self.assertEqual(
            result,
            {"tests": [{"name": "test_p[1 2]", "binary": "tests/test_x.py::TestK"}]},
        )
