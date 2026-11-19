"""v0.8.8: conditional supervisor triggers (`shape_when`, `pre_review_when`)."""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openfarm_core.config import ConfigError, load_config
from openfarm_core.flow import CardRun


def _write_cfg(root: Path, mm_extra: str = "") -> Path:
    of = root / ".openfarm"
    of.mkdir(parents=True, exist_ok=True)
    state = root.parent / "state"
    key = root.parent / "key.txt"
    key.write_text("k\n")
    (of / "config.toml").write_text(
        f"""
[project]
name = "demo"
state_dir = "{state}"

[adapters]
model = "scripted"
micromanager = "scripted"

[model]
key_file = "{key}"

[policy]
categories = ["pinned-bugfix"]

[micromanager]
mode = "active"
{mm_extra}
""".strip() + "\n"
    )
    return root


class ConditionalSupervisionConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v088-cond-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()

    def test_defaults_preserve_always_behaviour(self) -> None:
        cfg = load_config(_write_cfg(self.repo))
        self.assertEqual(cfg.micromanager.shape_when, "always")
        self.assertEqual(cfg.micromanager.pre_review_when, "always")
        self.assertEqual(cfg.micromanager.pre_review_max_lines, 60)

    def test_valid_and_invalid_choices(self) -> None:
        cfg = load_config(_write_cfg(
            self.repo,
            'shape_when = "new-symbols"\npre_review_when = "on-flag"\npre_review_max_lines = 40\n',
        ))
        self.assertEqual(cfg.micromanager.shape_when, "new-symbols")
        self.assertEqual(cfg.micromanager.pre_review_when, "on-flag")
        self.assertEqual(cfg.micromanager.pre_review_max_lines, 40)

        with self.assertRaises(ConfigError):
            load_config(_write_cfg(self.repo, 'shape_when = "maybe"\n'))
        with self.assertRaises(ConfigError):
            load_config(_write_cfg(self.repo, 'pre_review_when = "sometimes"\n'))


class ConditionalSupervisionFlowTests(unittest.TestCase):
    def _make_flow(self, *, shape_when="always", pre_review_when="always",
                   pre_review_max_lines=60, new_symbols=False, touches=("a.py",),
                   category="pinned-bugfix", impl_attempts=1, counter_failed=0,
                   handbacks=()):
        flow = object.__new__(CardRun)
        flow.cfg = SimpleNamespace(
            micromanager=SimpleNamespace(
                shape_when=shape_when,
                pre_review_when=pre_review_when,
                pre_review_max_lines=pre_review_max_lines,
            )
        )
        flow.card = SimpleNamespace(
            id="CARD-1",
            new_symbols=new_symbols,
            touches=list(touches),
            category=category,
        )
        flow.attempts = [{"role": "implementer"} for _ in range(impl_attempts)]
        flow.counter_summary = {"failed": counter_failed}
        flow.handbacks = list(handbacks)
        return flow

    def test_shape_when_new_symbols_skips_single_file_no_new_symbols(self) -> None:
        ok, reason = self._make_flow(shape_when="new-symbols", new_symbols=False)._should_shape()
        self.assertFalse(ok)
        self.assertIn("new-symbols", reason)

        ok2, _ = self._make_flow(shape_when="new-symbols", new_symbols=True)._should_shape()
        self.assertTrue(ok2)

        ok3, _ = self._make_flow(shape_when="new-symbols", touches=("a.py", "b.py"))._should_shape()
        self.assertTrue(ok3)

        ok4, reason4 = self._make_flow(shape_when="off")._should_shape()
        self.assertFalse(ok4)
        self.assertIn("off", reason4)

    def test_pre_review_when_on_flag_skips_clean_small_first_try(self) -> None:
        ok, reason = self._make_flow(pre_review_when="on-flag")._should_pre_review(20, 5)
        self.assertFalse(ok)
        self.assertIn("on-flag", reason)

        # Retry triggers pre_review
        self.assertTrue(self._make_flow(pre_review_when="on-flag", impl_attempts=2)._should_pre_review(20, 5)[0])
        # Counter-test failure triggers pre_review
        self.assertTrue(self._make_flow(pre_review_when="on-flag", counter_failed=1)._should_pre_review(20, 5)[0])
        # Large diff triggers pre_review
        self.assertTrue(self._make_flow(pre_review_when="on-flag", pre_review_max_lines=20)._should_pre_review(20, 5)[0])
        # High-risk category triggers pre_review
        self.assertTrue(self._make_flow(pre_review_when="on-flag", category="refactor")._should_pre_review(10, 2)[0])
        # Handback triggers pre_review
        self.assertTrue(self._make_flow(pre_review_when="on-flag", handbacks=[{"reason": "x"}])._should_pre_review(10, 2)[0])
        # Off always skips
        self.assertFalse(self._make_flow(pre_review_when="off", impl_attempts=3)._should_pre_review(200, 50)[0])


if __name__ == "__main__":
    unittest.main()
