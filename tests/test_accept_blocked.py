"""R1.17: a person can accept a blocked card they have checked, and the CLI says how."""

from __future__ import annotations

import json
import subprocess
import unittest

from openfarm_core.adapters.files import FileCardSource
from openfarm_core.config import load_config
from tests.test_python_e2e import build_python_toy, run_cli


class AcceptBlockedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy = build_python_toy()
        self.addCleanup(self.toy.cleanup)
        self.toy.write_card(
            "CARD-1", title="Add extra", category="small-feature", touches=["tally/extra.py"],
            tests=[{"name": "test_a", "file": "tests/test_a.py", "asserts": "returns 3"}],
            done_text="## Done\n\n- `extra()` returns 3.")
        self.toy.commit("add card")
        self.cfg = load_config(self.toy.root)
        self.src = FileCardSource(self.toy.root, "cards", worktree=self.cfg.state_dir / "cards-source")
        self.src.prepare()
        # The farm built the work on the card's branch, then a gate blocked it.
        card = self.src.all_cards()[0]
        self.branch = f"openfarm/{card.id}-{card.slug()}"
        self._git("checkout", "-q", "-b", self.branch)
        self.toy.write("tally/extra.py", "def extra():\n    return 3\n")
        self.toy.commit("CARD-1: add extra")
        self._git("checkout", "-q", self.cfg.base_branch)
        claim = self.src.claim("CARD-1", holder="lane")
        self.src.move("CARD-1", claim["lease_id"], "blocked", stop_code="tests-unprovable")
        self.run_dir = self.cfg.state_dir / "cards" / "CARD-1" / "run-1"
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "card-record.json").write_text("{}")

    def _git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.toy.root), *args], check=True,
                              capture_output=True, text=True).stdout

    def test_done_and_land_point_to_the_supported_way(self) -> None:
        for command in (("done", "CARD-1"), ("land", "CARD-1")):
            result = run_cli(self.toy.root, *command)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("openfarm land CARD-1 --accept-blocked --note", json.loads(result.stdout)["reason"])
        self.assertEqual(self.src.read("CARD-1")["card"]["state"], "blocked")

    def test_accepting_needs_a_note(self) -> None:
        result = run_cli(self.toy.root, "land", "CARD-1", "--accept-blocked")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("needs --note", result.stdout + result.stderr)
        self.assertEqual(self.src.read("CARD-1")["card"]["state"], "blocked")

    def test_accepting_records_who_and_why_then_merges_like_land(self) -> None:
        result = run_cli(self.toy.root, "land", "CARD-1", "--accept-blocked",
                         "--note", "ran the four tests by hand, they fail without the change")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.src.read("CARD-1")["card"]["state"], "done")
        self.assertTrue((self.toy.root / "tally" / "extra.py").is_file())
        self.assertIn(f"Merge {self.branch}", self._git("log", "-1", "--format=%s"))
        header, _body, _path = self.src._read_raw("CARD-1")
        self.assertTrue(header["accepted_by"])
        self.assertIn("by hand", header["accepted_note"])
        verdict = json.loads((self.run_dir / "verdict.json").read_text())
        self.assertEqual((verdict["verdict"], verdict["by"], verdict["accepted_blocked"]),
                         ("clean", f"person/{header['accepted_by']}", True))

    def test_a_verdict_from_a_person_needs_no_token_count(self) -> None:
        result = run_cli(self.toy.root, "verdict", "CARD-1", "clean")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads((self.run_dir / "verdict.json").read_text())["review_tokens"], 0)

    def test_escape_help_says_what_it_records(self) -> None:
        text = run_cli(self.toy.root, "--help").stdout + run_cli(self.toy.root, "escape", "--help").stdout
        self.assertIn("escaped review and was found later", " ".join(text.split()))


if __name__ == "__main__":
    unittest.main()
