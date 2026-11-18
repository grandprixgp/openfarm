"""v0.8.5: fixes to the v0.8.1 known limits (docs/HANDOFF.md)."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openfarm_core.board import Board
from openfarm_core.cli import EXIT_REFUSED, main as cli_main
from openfarm_core.config import load_config
from openfarm_core.state import StateDir
from openfarm_core.swarm import swarm_join
from tests.test_python_e2e import build_python_toy


class JoinErrorCodeTests(unittest.TestCase):
    """`swarm join` JSON errors carry the refusal's code."""

    def _join(self, toy, name, seat):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli_main(["--repo", str(toy.root), "--json", "swarm", "join", "--run", "run-x",
                           "--name", name, "--seat", seat])
        return rc, json.loads(out.getvalue())

    def test_seat_occupied_and_name_taken_differ(self) -> None:
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        toy.config.write_text(toy.config.read_text()
                              + '\n[swarm]\nenabled = true\nsupervisor = "external"\n'
                              'coordinator = "external"\n')
        toy.commit("outside seats")
        board = Board(toy.state_dir / "runs" / "run-x" / "board", state_dir=StateDir(toy.state_dir))
        board.ensure()
        swarm_join(board, load_config(toy.root), name="lead", seat="coordinator", owner_kind="agent")
        rc, payload = self._join(toy, "other", "coordinator")
        self.assertEqual(rc, EXIT_REFUSED)
        self.assertEqual(payload["code"], "seat-occupied")
        rc, payload = self._join(toy, "lead", "supervisor")
        self.assertEqual(rc, EXIT_REFUSED)
        self.assertEqual(payload["code"], "name-taken")


import importlib.util

HAS_PYTEST = importlib.util.find_spec("pytest") is not None


@unittest.skipUnless(HAS_PYTEST, "the Python running the suite cannot import pytest")
class KnownFailingRowsTests(unittest.TestCase):
    """A parametrized named test with every case on `known_failing` is refused;
    one with only some cases there goes on as before."""

    def _run(self, known):
        from tests.test_python_e2e import GOOD_IMPLEMENTATION, run_cli, script
        from tests.test_v081b_packs_pytest import PARAM_TEST, PYTEST_PYPROJECT

        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        toy.write("pyproject.toml", PYTEST_PYPROJECT)
        toy.config.write_text(toy.config.read_text().replace(
            "known_failing = []", "known_failing = [" + ", ".join(f'"{k}"' for k in known) + "]"))
        toy.write_card(
            "CARD-1", title="A full bundle refuses a push", category="pinned-bugfix",
            unit=".", touches=["tally/bounded.py"],
            tests=[{"name": "test_a_full_bundle_refuses_a_push", "file": "tests/test_full.py"}],
            done_text="`Bounded(1).push(8)` after one push raises `FullError`.")
        toy.commit("tally: pytest, CARD-1")
        sp = script(**{
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + PARAM_TEST,
            "CARD-1/implementer": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: a full bundle refuses a push",
            "CARD-1/checker_pass2": "{}"})
        result = run_cli(toy.root, "run", "--card", "CARD-1", "--script", str(sp), "--cards", "1")
        return json.loads(result.stdout)["outcomes"][0]

    def test_every_case_known_failing_is_refused(self) -> None:
        rows = [f"test_a_full_bundle_refuses_a_push[{n}]" for n in (1, 2, 3)]
        outcome = self._run(rows)
        self.assertEqual(outcome["stop_code"], "ineligible", outcome)
        self.assertIn("known_failing list in every case", outcome.get("detail", ""))

    def test_some_cases_known_failing_is_not_refused_here(self) -> None:
        outcome = self._run(["test_a_full_bundle_refuses_a_push[1]"])
        self.assertNotIn("known_failing list in every case", outcome.get("detail", ""))


class HolderIdentityTests(unittest.TestCase):
    """Locks and seats: a holder is gone only when it was provably this system's."""

    def _rec(self, **over):
        from openfarm_core.state import process_identity

        rec = process_identity()
        rec.update(over)
        return rec

    def _dead_pid(self) -> int:
        import subprocess

        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def test_a_dead_process_of_this_system_is_gone(self) -> None:
        from openfarm_core.state import holder_provably_gone

        self.assertTrue(holder_provably_gone(self._rec(pid=self._dead_pid())))
        self.assertFalse(holder_provably_gone(self._rec()), "this very process is alive")

    def test_another_pid_namespace_or_machine_or_boot_is_never_gone(self) -> None:
        from openfarm_core.state import holder_provably_gone

        dead = self._dead_pid()
        for key, value in (("pidns", "pid:[1]"), ("machine_id", "0" * 32),
                           ("boot_id", "00000000-0000-0000-0000-000000000000")):
            with self.subTest(key=key):
                self.assertFalse(holder_provably_gone(self._rec(pid=dead, **{key: value})))

    def test_a_reused_pid_is_gone(self) -> None:
        import os

        from openfarm_core.state import holder_provably_gone

        rec = self._rec(pid=os.getpid())
        if "start" not in rec or "boot_id" not in rec:
            self.skipTest("no /proc here")
        rec["start"] = str(int(rec["start"]) + 1)
        self.assertTrue(holder_provably_gone(rec))

    def test_an_old_record_is_judged_as_before(self) -> None:
        from openfarm_core.state import _this_host, holder_provably_gone

        self.assertTrue(holder_provably_gone({"pid": self._dead_pid(), "host": _this_host()}))
        self.assertFalse(holder_provably_gone({"pid": self._dead_pid(), "host": "elsewhere"}))

    def test_the_lock_and_the_seat_record_carry_it(self) -> None:
        import tempfile

        from openfarm_core.swarm import _holder_record, new_seat_holder

        rec = _holder_record(new_seat_holder())
        self.assertNotIn("token", rec)
        tmp = Path(tempfile.mkdtemp(prefix="of-v085-lock-"))
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        state = StateDir(tmp)
        state.root.mkdir(parents=True, exist_ok=True)
        self.assertTrue(state._create_run_lock("r1"))
        holder = state.lock_holder()
        for key in ("pid", "host"):
            self.assertIn(key, holder)
        if Path("/proc/self/ns/pid").exists():
            self.assertIn("pidns", holder)
            self.assertIn("pidns", rec)


class SpawnRetryTests(unittest.TestCase):
    """A CLI missing for a moment (mid-update) is tried again; others are not."""

    def test_missing_then_there(self) -> None:
        import errno

        from openfarm_core.spawn import popen_with_retry

        tries, waits = [], []

        def popen(argv, **kw):
            tries.append(argv)
            if len(tries) < 3:
                raise FileNotFoundError(errno.ENOENT, "No such file", argv[0])
            return "proc"

        self.assertEqual(popen_with_retry(["claude"], delays=(1, 2, 4), popen=popen,
                                          sleep=waits.append), "proc")
        self.assertEqual(waits, [1, 2])

    def test_gives_up_after_the_last_wait_and_never_retries_other_errors(self) -> None:
        import errno

        from openfarm_core.spawn import popen_with_retry

        def missing(argv, **kw):
            raise FileNotFoundError(errno.ENOENT, "gone", argv[0])

        waits = []
        with self.assertRaises(FileNotFoundError):
            popen_with_retry(["claude"], delays=(1, 2), popen=missing, sleep=waits.append)
        self.assertEqual(waits, [1, 2])

        def other(argv, **kw):
            raise OSError(errno.E2BIG, "argument list too long")

        waits = []
        with self.assertRaises(OSError):
            popen_with_retry(["claude"], delays=(1, 2), popen=other, sleep=waits.append)
        self.assertEqual(waits, [])

    def test_the_default_waits_and_the_suite_switch(self) -> None:
        import os
        from unittest import mock

        from openfarm_core.spawn import SPAWN_RETRY_DELAYS, retry_delays

        self.assertGreaterEqual(sum(SPAWN_RETRY_DELAYS), 10)
        with mock.patch.dict(os.environ, {"OPENFARM_SPAWN_RETRY_DELAYS": "0.5,1"}):
            self.assertEqual(retry_delays(), (0.5, 1.0))
        env = {k: v for k, v in os.environ.items() if k != "OPENFARM_SPAWN_RETRY_DELAYS"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(retry_delays(), SPAWN_RETRY_DELAYS)

    def test_the_bridges_and_the_driver_use_it(self) -> None:
        for name in ("adapters/claude_native_acp.py", "adapters/codex_native_acp.py",
                     "adapters/agy_native_acp.py", "driver.py"):
            text = (ROOT / "openfarm_core" / name).read_text()
            self.assertIn("popen_with_retry(", text, name)


class ConfineLinkTests(unittest.TestCase):
    """A never_touch path written through a symlinked folder is hidden; a read
    root that holds the home folder is refused."""

    def setUp(self) -> None:
        import os
        import tempfile

        self.base = Path(tempfile.mkdtemp(prefix="of-v085-confine-"))
        self.addCleanup(__import__("shutil").rmtree, self.base, ignore_errors=True)
        self.sess = self.base / "sess"
        (self.sess / "sub").mkdir(parents=True)
        (self.sess / "sub" / "secret").write_text("s")
        (self.sess / "sub" / "open.txt").write_text("o")
        os.symlink("sub", self.sess / "link")
        os.symlink(".", self.sess / "sub" / "self")  # a loop is walked once

    def _argv(self, globs, **kw):
        from openfarm_core import confine

        return confine.boundary_argv(["/bin/true"], session_dir=self.sess, home_dir=None,
                                     hide_globs=globs, bwrap="/usr/bin/bwrap", **kw)

    def test_a_glob_through_a_link_hides_the_real_file(self) -> None:
        text = " ".join(self._argv(["link/secret"]))
        self.assertIn(str((self.sess / "sub" / "secret").resolve()), text)
        self.assertNotIn(str((self.sess / "sub" / "open.txt").resolve()), text)

    def test_a_read_root_holding_home_is_refused(self) -> None:
        from openfarm_core import confine

        home = confine._real_home()
        if home is None:
            self.skipTest("no home folder")
        with self.assertRaises(confine.ConfineError) as caught:
            self._argv([], read_roots=[str(home.parent)])
        self.assertEqual(caught.exception.code, "confine-read-root-holds-home")

    def test_in_a_real_boundary_the_link_reads_nothing(self) -> None:
        import subprocess

        from openfarm_core import confine

        ok, _why = confine.bwrap_status()
        if not ok:
            self.skipTest("bwrap cannot run here")
        argv = confine.boundary_argv(["/bin/cat", str(self.sess / "link" / "secret")],
                                     session_dir=self.sess, home_dir=None,
                                     hide_globs=["link/secret"])
        result = subprocess.run(argv, capture_output=True, text=True)
        self.assertNotIn("s", result.stdout)


class StandaloneCodexReadOnlyTests(unittest.TestCase):
    def test_a_standalone_server_with_read_only_is_refused_without_nesting(self) -> None:
        from openfarm_core import confine

        self.assertTrue(confine.needs_nested("codex", "server", "read-only", "auto"))
        hold = confine.decide("codex", transport="server", tool_policy="read-only",
                              mode="auto", bwrap_usable=True, nested_usable=False,
                              nested_reason="nested sandboxes are refused")
        self.assertTrue(hold.refused)
        self.assertIn("read-only", hold.refused)
        ok = confine.decide("codex", transport="server", tool_policy="read-only",
                            mode="auto", bwrap_usable=True, nested_usable=True)
        self.assertFalse(ok.refused)
        self.assertTrue(ok.boundary)
        plain = confine.decide("codex", transport="server", tool_policy="none",
                               mode="auto", bwrap_usable=True, nested_usable=False)
        self.assertFalse(plain.refused)


if __name__ == "__main__":
    unittest.main()
