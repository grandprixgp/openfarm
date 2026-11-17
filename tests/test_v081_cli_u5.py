"""U5: the cards view's git calls and the kill switch's confirmation.

- v0.8's cards view ran about three git commands per card on every 1-second refresh.
- In the overview, `x` then Enter armed the kill switch with no confirmation.
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core import git as git_mod  # noqa: E402
from openfarm_core import tui  # noqa: E402
from openfarm_core.state import StateDir  # noqa: E402
from tests.test_v081_cli_toy import make_toy  # noqa: E402
from tests.test_v081_cli_tui_background import FakeStdscr  # noqa: E402


class _GitCounter:
    """Counts the git commands `Git._run` starts."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._real = git_mod.Git._run

    def __enter__(self):
        counter = self

        def counted(self_git, args, *a, **kw):
            counter.calls.append(list(args))
            return counter._real(self_git, args, *a, **kw)

        self._patch = mock.patch.object(git_mod.Git, "_run", counted)
        self._patch.start()
        return self

    def __exit__(self, *exc) -> None:
        self._patch.stop()


class CardsViewGitCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        tui._CARD_GIT_CACHE.clear()
        self.addCleanup(tui._CARD_GIT_CACHE.clear)
        self.toy, _script = make_toy(6)
        self.addCleanup(self.toy.cleanup)
        g = git_mod.Git(self.toy.root)
        # A branch with one commit for each card, as runs leave behind.
        for n in range(1, 7):
            g._run(["branch", f"openfarm/CARD-{n}-a1", "HEAD"])
        self.git = g

    def _refresh(self) -> dict:
        return tui.collect_snapshot(self.toy.root, view="cards")

    def test_refreshes_reuse_the_branch_facts(self) -> None:
        self._refresh()  # the first look fills the cache
        with _GitCounter() as counter:
            for _ in range(5):
                snap = self._refresh()
        cards = snap["data"]["cards"]
        self.assertEqual(len(cards), 6)
        self.assertTrue(all(c["branches"] for c in cards), cards)
        expensive = [c for c in counter.calls if c and c[0] in ("rev-list", "diff")]
        self.assertEqual(expensive, [], "count and diff re-ran on a refresh with nothing changed")
        # What is left per refresh is a few cheap calls, however many cards there are.
        per_refresh = len(counter.calls) / 5
        self.assertLessEqual(per_refresh, 5, counter.calls[:20])

    def test_a_moved_branch_head_is_seen_on_the_next_refresh(self) -> None:
        before = {c["id"]: c for c in self._refresh()["data"]["cards"]}
        self.assertEqual(before["CARD-2"]["unmerged_commits"], 0)
        # Move CARD-2's branch on by one commit.
        wt = self.toy.root.parent / "wt-card2"
        self.git._run(["worktree", "add", str(wt), "openfarm/CARD-2-a1"])
        self.addCleanup(shutil.rmtree, wt, True)
        (wt / "extra.txt").write_text("x\n", encoding="utf-8")
        g2 = git_mod.Git(wt)
        g2._run(["add", "extra.txt"])
        g2._run(["commit", "-qm", "extra"])
        after = {c["id"]: c for c in self._refresh()["data"]["cards"]}
        self.assertEqual(after["CARD-2"]["unmerged_commits"], 1)
        self.assertIn("extra.txt", after["CARD-2"]["diff_preview"])
        self.assertEqual(after["CARD-3"]["unmerged_commits"], 0)

    def test_old_entries_are_refreshed_on_a_slow_timer(self) -> None:
        self._refresh()
        with mock.patch.object(tui, "_CARD_GIT_MAX_AGE_S", 0.0), _GitCounter() as counter:
            self._refresh()
        self.assertEqual(sum(1 for c in counter.calls if c and c[0] == "rev-list"), 6)


class KillSwitchConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy, _script = make_toy(1)
        self.addCleanup(self.toy.cleanup)
        self.state = StateDir(self.toy.state_dir)
        self.ctrl = tui.TuiController(self.toy.root)

    def _press(self, keys: list) -> FakeStdscr:
        # `q` closes a modal the command opened, then quits.
        screen = FakeStdscr(keys + [ord("q")] * 3)
        with mock.patch("curses.curs_set", return_value=0), \
                mock.patch.object(tui, "_init_curses_theme", return_value={}):
            tui._curses_main(screen, self.ctrl)
        return screen

    def test_x_asks_and_no_leaves_it_unarmed(self) -> None:
        self.assertEqual(self.ctrl.view, "overview")
        screen = self._press([ord("x"), *map(ord, "late"), 10, ord("n")])
        self.assertFalse(self.state.kill_requested(None))
        self.assertIn("kill switch not armed", self.ctrl.status_message)
        joined = "\n".join(screen.drawn)
        self.assertIn("Arm the kill switch?", joined)
        self.assertIn("resume --clear-kill", joined)

    def test_enter_alone_does_not_arm_it(self) -> None:
        self._press([ord("x"), 10, 10])
        self.assertFalse(self.state.kill_requested(None))

    def test_yes_arms_it(self) -> None:
        self._press([ord("x"), *map(ord, "late"), 10, ord("y")])
        self.assertTrue(self.state.kill_requested(None))

    def test_the_question_names_the_live_run(self) -> None:
        self.assertTrue(self.state.acquire_run_lock("run-live-x"))
        self.addCleanup(self.state.release_run_lock, "run-live-x")
        self.ctrl.refresh()
        q = tui.kill_switch_question(self.ctrl)
        self.assertIn("run run-live-x", q)
        self.assertIn("[y/N]", q)


if __name__ == "__main__":
    unittest.main()
