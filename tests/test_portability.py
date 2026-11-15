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
import shutil
import subprocess
import tempfile
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


def _reset_card(card_id: str) -> None:
    """Put a card back on the cards branch and clear its state, so a case repeats.

    Uses the same card source the run uses, and removes the branch it produced, so
    the next run starts from a clean base rather than from the last run's branch.
    """
    from openfarm_core.config import load_config

    cfg = load_config(SECOND)
    worktree = Path(cfg.state_dir) / "cards-source"
    if worktree.exists():
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                       cwd=str(SECOND), capture_output=True)
        subprocess.run(["git", "worktree", "prune"], cwd=str(SECOND),
                       capture_output=True)
    if subprocess.run(["git", "rev-parse", "--verify", "--quiet",
                       "refs/heads/openfarm-cards"], cwd=str(SECOND),
                      capture_output=True).returncode == 0:
        subprocess.run(["git", "branch", "-D", "openfarm-cards"],
                       cwd=str(SECOND), capture_output=True)
    shutil.rmtree(Path(cfg.state_dir), ignore_errors=True)

    # Any work branch a previous run left is removed, so the next run makes a fresh
    # one from the base rather than reusing a branch that already has the edit.
    branches = subprocess.run(
        ["git", "branch", "--list", f"openfarm/{card_id}*"], cwd=str(SECOND),
        capture_output=True, text=True).stdout
    for line in branches.splitlines():
        name = line.lstrip("*+ ").strip()
        if name:
            subprocess.run(["git", "worktree", "remove", "--force",
                            str(Path("/tmp") / f"wt-{card_id}")],
                           cwd=str(SECOND), capture_output=True)
            subprocess.run(["git", "branch", "-D", name], cwd=str(SECOND),
                           capture_output=True)


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

    def test_workspace_style_paths_classify_like_the_real_crate(self) -> None:
        """This crate is flat, so its own paths must come out as one unit."""
        from openfarm_core.config import load_config
        from openfarm_core.gates.eligibility import _units_of

        cfg = load_config(SECOND)
        self.assertEqual(_units_of(["src/error.rs", "tests/test_chain.rs"],
                                   SECOND), {"."})

    def test_a_card_run_on_the_real_crate_leaves_openfarm_untouched(self) -> None:
        """The strongest form of the claim: a run, in another repo, changes nothing.

        The card is the one the pilot would use here: `root_cause` reads the end of
        the chain directly and drops its `#[allow]`. The model is scripted, because
        no real model has run yet; what this proves is the harness, not the model.

        The card is put back to Ready first, so the case can run more than once.
        """
        # Put the card back to Ready and forget the previous run's folders.
        _reset_card("MECH-1")

        before = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(REPO_ROOT),
            capture_output=True, text=True).stdout
        script = Path(tempfile.mkdtemp()) / "anyhow.json"
        script.write_text(json.dumps({
            "MECH-1/implementer": (
                "=== search_replace: src/error.rs\n"
                "<<< SEARCH\n"
                "    #[cfg(any(feature = \"std\", not(anyhow_no_core_error)))]\n"
                "    #[allow(clippy::double_ended_iterator_last)]\n"
                "    pub fn root_cause(&self) -> &(dyn StdError + 'static) {\n"
                "        self.chain().last().unwrap()\n"
                "    }\n"
                ">>> REPLACE\n"
                "    #[cfg(any(feature = \"std\", not(anyhow_no_core_error)))]\n"
                "    pub fn root_cause(&self) -> &(dyn StdError + 'static) {\n"
                "        self.chain().next_back().unwrap()\n"
                "    }\n"),
            "MECH-1/checker_pass1":
                "RESTATEMENT: root_cause returns the last error in the chain",
            "MECH-1/checker_pass2": "{}",
        }))
        result = cli("run", "--card", "MECH-1", "--script", str(script),
                     "--cards", "1")
        payload = json.loads(result.stdout) if result.stdout else {}
        outcome = payload.get("outcomes", [{}])[0]
        self.assertEqual(outcome.get("final_state"), "in-review",
                         f"{outcome.get('stop_code')}: {outcome.get('detail')}")

        after = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(REPO_ROOT),
            capture_output=True, text=True).stdout
        self.assertEqual(before, after,
                         "the core ran unchanged: openfarm's own tree is untouched")

        # The branch carries the edit, and the base does not.
        branch = subprocess.run(
            ["git", "branch", "--list", "openfarm/MECH-1*"], cwd=str(SECOND),
            capture_output=True, text=True).stdout
        self.assertIn("openfarm/MECH-1", branch)
        diff = subprocess.run(
            ["git", "diff", "master.." + branch.split()[0].lstrip("*").strip()],
            cwd=str(SECOND), capture_output=True, text=True).stdout
        self.assertIn("-    #[allow(clippy::double_ended_iterator_last)]", diff)
        self.assertIn("+        self.chain().next_back().unwrap()", diff)


if __name__ == "__main__":
    unittest.main()
