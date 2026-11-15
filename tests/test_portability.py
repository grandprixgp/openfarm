"""Portability: the core runs on a second, real repository with no change.

The spec's build step 2: "The core isn't called done until it has run there
unchanged." These cases point `openfarm` at a real public crate (`anyhow`, cloned to
`~/projects/openfarm-second`) and check that nothing about the core had to know it
was not the toy.

They also record a finding from doing it: cut cards are real work. Three of the four
cards written for this repository were genuinely ineligible or genuinely wrong, and
the gates said so — a bad premise, an illegal edit to a trait item, and a banned
`.unwrap()` that the crate already had on the line being changed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ENABLED = os.environ.get("OPENFARM_E2E") == "1"
REPO_ROOT = Path(__file__).resolve().parent.parent
OPENFARM = REPO_ROOT / "openfarm"
SECOND = Path.home() / "projects" / "openfarm-second"


def cli(*args: str, repo: Path = SECOND) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(OPENFARM), "--repo", str(repo), "--json", *args],
        capture_output=True, text=True)


def available() -> bool:
    return ENABLED and (SECOND / ".openfarm" / "config.toml").exists()


@unittest.skipUnless(ENABLED, "set OPENFARM_E2E=1")
class PortabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        if not available():
            self.skipTest(f"the second repository is not set up at {SECOND}")

    def test_the_core_runs_with_no_code_change(self) -> None:
        """The openfarm tree is untouched by a run in another repository."""
        before = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(REPO_ROOT), capture_output=True, text=True).stdout
        cli("status")
        after = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(REPO_ROOT), capture_output=True, text=True).stdout
        self.assertEqual(before, after,
                         "running openfarm must not change openfarm")

    def test_eligibility_reads_a_card_in_the_second_repository(self) -> None:
        result = cli("check", "MECH-1")
        self.assertIn(result.returncode, (0, 1), result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["card"], "MECH-1")
        self.assertIn("card_hash", payload)

    def test_the_baseline_gate_sees_the_real_crate(self) -> None:
        """`anyhow`'s own suite runs, and the cache records how many tests it has."""
        from openfarm_core.config import load_config
        from openfarm_core.packs.base import pack_for

        cfg = load_config(SECOND)
        pack = pack_for("rust", cfg.cargo_flags)
        pack.configure(SECOND)
        argv = pack.commands("list", unit="anyhow")["argv"]
        result = subprocess.run(argv, cwd=str(SECOND), capture_output=True,
                                text=True, timeout=600)
        listing = pack.parse_list(result.stdout)
        self.assertGreater(len(listing["tests"]), 20,
                           "the real crate's tests should be listed")

    def test_a_library_without_a_lock_file_drops_the_locked_flag(self) -> None:
        """This crate commits no Cargo.lock, and the pack says so rather than fail."""
        from openfarm_core.config import load_config
        from openfarm_core.packs.base import pack_for

        cfg = load_config(SECOND)
        pack = pack_for("rust", cfg.cargo_flags)
        pack.configure(SECOND)
        if (SECOND / "Cargo.lock").exists():
            self.assertIn("--locked", pack.cargo_flags)
        else:
            self.assertNotIn("--locked", pack.cargo_flags)
            self.assertTrue(any("Cargo.lock" in note for note in pack.notes),
                            "the dossier must say the resolution is not pinned")

    def test_the_banned_pattern_gate_catches_a_real_unwrap(self) -> None:
        """`anyhow`'s own `root_cause` contains `.unwrap()`, and the gate sees it."""
        from openfarm_core.packs.rust import RustPack

        source = (SECOND / "src" / "error.rs").read_text()
        line_no = next(
            index for index, line in enumerate(source.splitlines(), start=1)
            if "self.chain().last().unwrap()" in line)
        found = RustPack().patterns(
            [{"path": "src/error.rs", "line": line_no,
              "text": source.splitlines()[line_no - 1]}],
            [], {"src/error.rs"})
        self.assertTrue(any(entry["pattern"] == "production-unwrap"
                            for entry in found["banned"]),
                        "a production .unwrap() must be banned")

    def test_the_config_is_forbidden_to_the_farm_here_too(self) -> None:
        from openfarm_core.config import load_config
        cfg = load_config(SECOND)
        self.assertTrue(cfg.is_forbidden(".openfarm/config.toml"))


if __name__ == "__main__":
    unittest.main()
