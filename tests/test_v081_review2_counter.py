"""Second outside review, 5: a counter-test and the ignored files already there.

git restores no ignored file, so a cache or a local settings file a counter-test
overwrote or deleted stayed that way, even on a clean worktree.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_v081b_checker_flow import _SlugHarness, form_answer

VANDAL = '''\
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


class VandalTests(unittest.TestCase):
    def test_vandal(self):
        (ROOT / "__pycache__" / "settings.json").write_text("overwritten")
        (ROOT / "__pycache__" / "deep" / "cache.bin").unlink()
        self.fail("and it fails, as a counter-test does")
'''


class IgnoredFilesComeBackTests(_SlugHarness):
    def test_an_overwritten_and_a_deleted_ignored_file_are_put_back(self) -> None:
        run = self.card_run(self.script(form_answer(tests=[("test_vandal", VANDAL)])))
        cache = run.worktree / "__pycache__"
        (cache / "deep").mkdir(parents=True)
        (cache / "settings.json").write_text('{"mine": true}')
        (cache / "deep" / "cache.bin").write_bytes(b"\\x00\\x01kept")
        result = run.run()
        self.assertEqual(result["final_state"], "in-review", result.get("detail"))
        self.assertEqual(result["counter_tests"]["failed"], 1)
        self.assertEqual((cache / "settings.json").read_text(), '{"mine": true}')
        self.assertEqual((cache / "deep" / "cache.bin").read_bytes(), b"\\x00\\x01kept")
        self.assert_worktree_untouched()

    def test_a_file_too_large_to_keep_is_said_not_passed_over(self) -> None:
        from openfarm_core import flow
        old = flow._COUNTER_KEEP_BYTES
        flow._COUNTER_KEEP_BYTES = 4
        self.addCleanup(setattr, flow, "_COUNTER_KEEP_BYTES", old)
        run = self.card_run(self.script(form_answer(tests=[("test_vandal", VANDAL)])))
        cache = run.worktree / "__pycache__"
        (cache / "deep").mkdir(parents=True)
        (cache / "settings.json").write_text('{"mine": true}')
        (cache / "deep" / "cache.bin").write_bytes(b"k")
        logged: list[str] = []
        real_log = run.log
        run.log = lambda text, *a, **k: (logged.append(str(text)), real_log(text, *a, **k))[1]
        run.run()
        self.assertTrue(any("could not be put back" in line and "settings.json" in line
                            for line in logged), logged)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(IgnoredFilesComeBackTests))
    return suite


if __name__ == "__main__":
    unittest.main()
