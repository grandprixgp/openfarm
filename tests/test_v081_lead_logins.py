"""Lead, from the outside review (M3, M5, m6): copying a refreshed login back.

- The login on disk is replaced only if it is still the one this child's copy was
  taken from: another child that refreshed first, or the owner logging in again,
  is never overwritten with an older view.
- A login the owner keeps behind a symlink is updated where the link points, and
  the link stays a link.
- An Antigravity token has no documented format, so its check is on structure.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core import logins

OLD = {"auth_mode": "chatgpt", "last_refresh": "2026-11-01T00:00:00Z",
       "tokens": {"access_token": "a1", "refresh_token": "r1", "account_id": "acct-1"}}


def _codex(access: str, refresh: str, stamp: str, account: str = "acct-1") -> bytes:
    return json.dumps({"auth_mode": "chatgpt", "last_refresh": stamp,
                       "tokens": {"access_token": access, "refresh_token": refresh,
                                  "account_id": account}}).encode()


class CopyBackOnlyOverItsOwnStartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v081-lead-sync-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.real = self.tmp / "real"
        self.real.mkdir()
        self.dest = self.real / "auth.json"
        self.initial = json.dumps(OLD).encode()
        self.dest.write_bytes(self.initial)

    def _child(self, name: str, content: bytes) -> Path:
        iso = self.tmp / name
        (iso / ".codex").mkdir(parents=True)
        (iso / ".codex" / "auth.json").write_bytes(content)
        return iso

    def _sync(self, iso: Path, target: Path | None = None) -> logins.SyncResult:
        return logins.sync_back("codex", isolated_home=iso, target=target or self.dest,
                                initial=self.initial)

    def test_the_second_child_does_not_overwrite_the_first_ones_refresh(self) -> None:
        first = _codex("a2", "r2", "2026-11-02T00:00:00Z")
        second = _codex("a3", "r3", "2026-11-02T00:00:05Z")
        self.assertEqual(self._sync(self._child("a", first)).status, "synced")
        result = self._sync(self._child("b", second))
        self.assertEqual(result.status, "refused")
        self.assertIn("changed while the child ran", result.reason)
        self.assertNotIn("a3", result.reason)
        self.assertEqual(self.dest.read_bytes(), first)

    def test_a_new_login_by_the_owner_is_never_replaced(self) -> None:
        theirs = _codex("other", "other-r", "2026-11-03T00:00:00Z", account="acct-2")
        self.dest.write_bytes(theirs)
        result = self._sync(self._child("a", _codex("a2", "r2", "2026-11-02T00:00:00Z")))
        self.assertEqual(result.status, "refused")
        self.assertEqual(self.dest.read_bytes(), theirs)
        self.assertFalse((self.real / ("auth.json" + logins.BACKUP_SUFFIX)).exists())

    def test_the_same_refresh_already_on_disk_is_unchanged(self) -> None:
        new = _codex("a2", "r2", "2026-11-02T00:00:00Z")
        self.dest.write_bytes(new)
        self.assertEqual(self._sync(self._child("a", new)).status, "unchanged")

    def test_a_login_removed_meanwhile_is_not_put_back(self) -> None:
        self.dest.unlink()
        result = self._sync(self._child("a", _codex("a2", "r2", "2026-11-02T00:00:00Z")))
        self.assertEqual(result.status, "refused")
        self.assertFalse(self.dest.exists())

    def test_a_symlinked_login_is_updated_where_it_points_and_stays_a_link(self) -> None:
        dotfiles = self.tmp / "dotfiles"
        dotfiles.mkdir()
        kept = dotfiles / "codex-auth.json"
        kept.write_bytes(self.initial)
        self.dest.unlink()
        self.dest.symlink_to(kept)
        new = _codex("a2", "r2", "2026-11-02T00:00:00Z")
        result = self._sync(self._child("a", new))
        self.assertEqual(result.status, "synced", result.reason)
        self.assertTrue(self.dest.is_symlink())
        self.assertEqual(kept.read_bytes(), new)
        self.assertEqual(self.dest.read_bytes(), new)
        self.assertEqual((dotfiles / ("codex-auth.json" + logins.BACKUP_SUFFIX)).read_bytes(),
                         self.initial)

    def test_a_dangling_link_is_refused(self) -> None:
        self.dest.unlink()
        self.dest.symlink_to(self.tmp / "nowhere" / "auth.json")
        result = self._sync(self._child("a", _codex("a2", "r2", "2026-11-02T00:00:00Z")))
        self.assertEqual(result.status, "refused")
        self.assertTrue(self.dest.is_symlink())


class PinnedAndLockedTests(CopyBackOnlyOverItsOwnStartTests):
    """Second outside review, 1 and 2."""

    def test_a_link_made_while_the_child_ran_is_not_followed(self) -> None:
        # The login was a plain file at copy-in; by copy-back the path is a link
        # to another profile that holds the same bytes.
        pin = logins.pin_path(self.dest)
        other = self.tmp / "other" / "auth.json"
        other.parent.mkdir()
        other.write_bytes(self.initial)
        self.dest.unlink()
        self.dest.symlink_to(other)
        iso = self._child("a", _codex("a2", "r2", "2026-11-02T00:00:00Z"))
        result = logins.sync_back("codex", isolated_home=iso, target=self.dest,
                                  initial=self.initial, pinned=pin)
        self.assertEqual(result.status, "refused")
        self.assertIn("leads somewhere else", result.reason)
        self.assertEqual(other.read_bytes(), self.initial)

    def test_a_retargeted_link_is_not_followed(self) -> None:
        store = self.tmp / "store" / "auth.json"
        store.parent.mkdir()
        store.write_bytes(self.initial)
        self.dest.unlink()
        self.dest.symlink_to(store)
        pin = logins.pin_path(self.dest)
        other = self.tmp / "other" / "auth.json"
        other.parent.mkdir()
        other.write_bytes(self.initial)
        self.dest.unlink()
        self.dest.symlink_to(other)
        iso = self._child("a", _codex("a2", "r2", "2026-11-02T00:00:00Z"))
        result = logins.sync_back("codex", isolated_home=iso, target=self.dest,
                                  initial=self.initial, pinned=pin)
        self.assertEqual(result.status, "refused")
        self.assertEqual(other.read_bytes(), self.initial)
        self.assertEqual(store.read_bytes(), self.initial)

    def test_an_unchanged_link_still_updates_where_it_points(self) -> None:
        store = self.tmp / "store" / "auth.json"
        store.parent.mkdir()
        store.write_bytes(self.initial)
        self.dest.unlink()
        self.dest.symlink_to(store)
        new = _codex("a2", "r2", "2026-11-02T00:00:00Z")
        result = logins.sync_back("codex", isolated_home=self._child("a", new), target=self.dest,
                                  initial=self.initial, pinned=logins.pin_path(self.dest))
        self.assertEqual(result.status, "synced")
        self.assertTrue(self.dest.is_symlink())
        self.assertEqual(store.read_bytes(), new)

    def test_two_children_at_once_cannot_both_replace_the_login(self) -> None:
        # Both pass the read of the old login together unless the compare and the
        # write are one step: the first holds the lock inside the compare until
        # the second is waiting for it.
        import threading
        import time
        first = _codex("a2", "r2", "2026-11-02T00:00:00Z")
        second = _codex("a3", "r3", "2026-11-02T00:00:05Z")
        isos = [self._child("a", first), self._child("b", second)]
        real_replace = logins._replace_checked
        inside = threading.Event()

        def slow_replace(dest, raw, initial):
            inside.set()
            time.sleep(0.4)
            return real_replace(dest, raw, initial)

        results: dict[int, logins.SyncResult] = {}

        def run(index: int) -> None:
            results[index] = self._sync(isos[index])

        logins._replace_checked = slow_replace
        self.addCleanup(setattr, logins, "_replace_checked", real_replace)
        one = threading.Thread(target=run, args=(0,))
        one.start()
        self.assertTrue(inside.wait(5))
        two = threading.Thread(target=run, args=(1,))
        two.start()
        one.join(10)
        two.join(10)
        self.assertEqual(sorted(r.status for r in results.values()), ["refused", "synced"])
        self.assertEqual(results[0].status, "synced")
        self.assertEqual(self.dest.read_bytes(), first)
        self.assertEqual((self.real / ("auth.json" + logins.BACKUP_SUFFIX)).read_bytes(),
                         self.initial)


class AntigravityTokenShapeTests(unittest.TestCase):
    def test_a_json_token_with_its_tokens_emptied_is_refused(self) -> None:
        # Second outside review, 3: the fields were there, the values were gone.
        old = json.dumps({"access_token": "at-1", "refresh_token": "rt-1",
                          "email": "a@example.com", "expiry": 100, "scopes": ["x"]}).encode()
        for change in ({"access_token": None}, {"refresh_token": ""}, {"access_token": 7},
                       {"scopes": []}, {"refresh_token": "   "}):
            with self.subTest(change=change):
                new = dict(json.loads(old), **change)
                problem = logins.check_shape("antigravity", json.dumps(new).encode(), old)
                self.assertTrue(problem)
                self.assertNotIn("at-1", problem)
        fine = dict(json.loads(old), access_token="at-2", expiry=200)
        self.assertEqual(logins.check_shape("antigravity", json.dumps(fine).encode(), old), "")

    def _check(self, new: bytes, old: bytes | None) -> str:
        return logins.check_shape("antigravity", new, old)

    def test_a_plain_token_replaced_by_a_plain_token_passes(self) -> None:
        self.assertEqual(self._check(b"ya29.new-token-value\n", b"ya29.old-token-value\n"), "")

    def test_plain_text_that_is_not_one_token_is_refused(self) -> None:
        old = b"ya29.old-token-value\n"
        self.assertTrue(self._check(b"some words a model wrote\n", old))
        self.assertTrue(self._check(b"line-one\nline-two\n", old))
        self.assertTrue(self._check(b"x" * 9000, old))
        self.assertTrue(self._check(b'{"token": "x"}', old))
        self.assertTrue(self._check(b"tok\x07en", old))

    def test_a_json_token_keeps_its_fields_and_its_account(self) -> None:
        old = json.dumps({"access_token": "a", "refresh_token": "r", "email": "x@example.invalid",
                          "expiry": 1}).encode()
        good = json.dumps({"access_token": "a2", "refresh_token": "r2",
                           "email": "x@example.invalid", "expiry": 2, "extra": True}).encode()
        self.assertEqual(self._check(good, old), "")
        lost = json.dumps({"access_token": "a2", "email": "x@example.invalid", "expiry": 2}).encode()
        self.assertIn("lost fields", self._check(lost, old))
        other = json.dumps({"access_token": "a2", "refresh_token": "r2",
                            "email": "y@example.invalid", "expiry": 2}).encode()
        self.assertIn("another account", self._check(other, old))
        self.assertTrue(self._check(b"plain", old))

    def test_nothing_to_compare_with_means_nothing_is_copied_back(self) -> None:
        self.assertIn("no Antigravity token to compare", self._check(b"ya29.token", None))
        self.assertIn("no Antigravity token to compare", self._check(b"ya29.token", b""))

    def test_the_reason_never_carries_the_token(self) -> None:
        for new in (b"SECRET words here", b'{"access_token": "SECRET"}'):
            self.assertNotIn("SECRET", self._check(new, b"old-token"))


if __name__ == "__main__":
    unittest.main()
