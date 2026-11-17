"""v0.8.1b ts, T9: mutation settings the TypeScript pack cannot serve are refused
up front, at config load or check, never after a card has spent its calls."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.cards import Card
from openfarm_core.config import ConfigError, load_config
from openfarm_core.config_template import render_config
from openfarm_core.gates.eligibility import check_eligibility


def _repo(owner: unittest.TestCase, language: str, mutation: str,
          required_for: list[str] | None = None) -> Path:
    base = Path(tempfile.mkdtemp(prefix="openfarm-tsm-", dir=os.environ.get("TMPDIR")))
    owner.addCleanup(shutil.rmtree, base, True)
    root = base / "repo"
    (root / ".openfarm").mkdir(parents=True)
    text = render_config(name="t", language=language, base_branch="main",
                         state_dir=base / "state", key_file=base / "k",
                         mutation=mutation)
    if required_for is not None:
        import json
        lines = [f"mutation_required_for = {json.dumps(required_for)}"
                 if line.startswith("mutation_required_for =") else line
                 for line in text.splitlines()]
        text = "\n".join(lines) + "\n"
    (root / ".openfarm" / "config.toml").write_text(text)
    return root


def _card(category: str) -> Card:
    return Card("C-1", "t", done_text="`f()` returns `1`", touches=["src/f.ts"],
                fields={"category": category,
                        "tests": [{"name": "f", "file": "tests/f.test.ts"}]})


class MutationTests(unittest.TestCase):
    def test_required_is_refused_at_config_load(self) -> None:
        root = _repo(self, "typescript", "required")
        with self.assertRaises(ConfigError) as caught:
            load_config(root)
        self.assertIn("the TypeScript pack has no mutation tool", str(caught.exception))

    def test_required_still_loads_for_rust_and_python(self) -> None:
        for language in ("rust", "python"):
            with self.subTest(language):
                self.assertEqual(load_config(_repo(self, language, "required")).mutation,
                                 "required")

    def test_a_card_whose_category_needs_mutation_is_refused_at_check(self) -> None:
        root = _repo(self, "typescript", "report", ["test-backfill"])
        cfg = load_config(root)
        failures = {f["rule"]: f["detail"] for f in
                    check_eligibility(_card("test-backfill"), cfg, worktree=root).failures}
        self.assertIn("typescript-no-mutation", failures)
        self.assertIn("TypeScript pack has no mutation tool",
                      failures["typescript-no-mutation"])
        other = check_eligibility(_card("small-feature"), cfg, worktree=root)
        self.assertNotIn("typescript-no-mutation", [f["rule"] for f in other.failures])

    def test_with_mutation_off_nothing_is_refused(self) -> None:
        root = _repo(self, "typescript", "off", ["test-backfill"])
        cfg = load_config(root)
        rules = [f["rule"] for f in check_eligibility(_card("test-backfill"), cfg,
                                                      worktree=root).failures]
        self.assertNotIn("typescript-no-mutation", rules)

    def test_rust_test_backfill_is_not_touched(self) -> None:
        root = _repo(self, "rust", "report", ["test-backfill"])
        cfg = load_config(root)
        card = Card("C-1", "t", done_text="`f()` returns `1`", touches=["src/f.rs"],
                    fields={"category": "test-backfill",
                            "tests": [{"name": "f", "file": "tests/f.rs"}]})
        rules = [f["rule"] for f in check_eligibility(card, cfg).failures]
        self.assertNotIn("typescript-no-mutation", rules)


if __name__ == "__main__":
    unittest.main()
