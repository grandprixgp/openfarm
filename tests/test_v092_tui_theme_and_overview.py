"""v0.9.2 high-contrast default TUI theme and Overview automode hero tests."""

from __future__ import annotations

import curses
import os
import unittest
from unittest import mock

from openfarm_core.tui import _format_overview_lines, _init_curses_theme, render_snapshot
from tests.test_python_e2e import build_python_toy


class TuiHighContrastAndOverviewTests(unittest.TestCase):
    def test_default_theme_is_high_contrast_with_no_dim(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NO_COLOR", None)
            os.environ.pop("OPENFARM_TUI_THEME", None)
            with mock.patch("curses.has_colors", return_value=True), \
                 mock.patch("curses.start_color"), \
                 mock.patch("curses.use_default_colors"), \
                 mock.patch("curses.init_pair"), \
                 mock.patch("curses.color_pair", side_effect=lambda n: n << 8):
                theme = _init_curses_theme()
        for key in ("brand", "header", "tab_active", "tab_idle", "border", "selected", "key", "muted"):
            self.assertEqual(
                theme[key] & curses.A_DIM, 0,
                f"high-contrast theme[{key!r}] must not use A_DIM",
            )

    def test_soft_theme_opt_in_uses_dim(self) -> None:
        with mock.patch.dict(os.environ, {"OPENFARM_TUI_THEME": "soft"}):
            os.environ.pop("NO_COLOR", None)
            with mock.patch("curses.has_colors", return_value=True), \
                 mock.patch("curses.start_color"), \
                 mock.patch("curses.use_default_colors"), \
                 mock.patch("curses.init_pair"), \
                 mock.patch("curses.color_pair", side_effect=lambda n: n << 8):
                theme = _init_curses_theme()
        self.assertNotEqual(theme["muted"] & curses.A_DIM, 0)

    def test_overview_renders_automode_pipeline_hero_and_no_dollars(self) -> None:
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        rendered = render_snapshot(toy.root, view="overview")
        self.assertIn("Automode Pipeline", rendered)
        self.assertIn("[Enter]", rendered)
        self.assertNotIn("spend=$", rendered)
        self.assertIn("[:]palette", rendered.lower().replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
