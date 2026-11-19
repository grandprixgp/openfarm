"""v0.9.0 polyglot per-unit languages tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openfarm_core.cli import _configured_pack
from openfarm_core.config import ConfigError, load_config
from openfarm_core.config_template import render_config


def _write_cfg(root: Path, extra_toml: str = "") -> Path:
    text = render_config(
        name="poly",
        language="python",
        base_branch="main",
        state_dir=root.parent / "state",
        key_file=root.parent / "key",
    )
    if extra_toml:
        text += "\n" + extra_toml + "\n"
    p = root / ".openfarm" / "config.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


class PolyglotUnitsTests(unittest.TestCase):
    def test_unit_languages_parse_and_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            _write_cfg(root, (
                '[units.api]\nlanguage = "go"\n\n'
                '[units.web]\nlanguage = "typescript"\n\n'
                '[units.ml]\nlanguage = "python"\n\n'
                '[units.core]\nlanguage = "rust"\n'
            ))
            cfg = load_config(root)
            self.assertEqual(cfg.language_for_unit("api"), "go")
            self.assertEqual(cfg.language_for_unit("web"), "typescript")
            self.assertEqual(cfg.language_for_unit("ml"), "python")
            self.assertEqual(cfg.language_for_unit("core"), "rust")
            # Unknown or empty unit falls back to [adapters] languages[0]
            self.assertEqual(cfg.language_for_unit("other"), "python")
            self.assertEqual(cfg.language_for_unit(""), "python")

    def test_invalid_unit_language_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            _write_cfg(root, '[units.api]\nlanguage = "cobol"\n')
            with self.assertRaises(ConfigError):
                load_config(root)

    def test_configured_pack_selects_pack_per_unit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            (root / "api").mkdir()
            (root / "web").mkdir()
            (root / "ml").mkdir()
            _write_cfg(root, (
                '[units.api]\nlanguage = "go"\n\n'
                '[units.web]\nlanguage = "typescript"\n\n'
                '[units.ml]\nlanguage = "python"\n'
            ))
            cfg = load_config(root)
            self.assertEqual(_configured_pack(cfg, root, unit="api").name, "go")
            self.assertEqual(_configured_pack(cfg, root, unit="web").name, "typescript")
            self.assertEqual(_configured_pack(cfg, root, unit="ml").name, "python")
            self.assertEqual(_configured_pack(cfg, root, unit="").name, "python")


if __name__ == "__main__":
    unittest.main()
