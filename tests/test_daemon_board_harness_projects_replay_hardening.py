"""Regression tests for Wave 1 daemon, board, harness, projects, and replay hardening."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from openfarm_core.board import Board
from openfarm_core.bootstrap import initialize_project
from openfarm_core.config import HarnessConfig, load_config
from openfarm_core.daemon import daemon_status, read_daemon_logs, start_daemon, stop_daemon
from openfarm_core.errors import Infra, Refused
from openfarm_core.harness_global import GlobalLibrary
from openfarm_core.harness_memory import (
    IMMUTABLE_GATE_KEYS,
    HarnessLibrary,
    validate_harness_proposal,
)
from openfarm_core.projects import edit_project, list_projects, register_project, remove_project
from openfarm_core.replay import add_replay, run_replays
from openfarm_core.state import StateDir, _pid_alive, process_identity, write_json


class DaemonHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.repo = self.base / "dmn-repo"
        self.state_dir = self.base / "st"
        initialize_project(
            "dmn-repo",
            directory=self.repo,
            language="python",
            state_dir=self.state_dir,
            secrets_dir=self.base / "sec",
            commit=True,
        )
        self.cfg = load_config(self.repo)

    def test_daemon_stores_identity_and_refuses_reused_pid_or_foreign_host(self) -> None:
        started = start_daemon(
            self.repo,
            command_override=[sys.executable, "-c", "import time; time.sleep(30)"],
        )
        try:
            meta = json.loads((self.state_dir / "daemon.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["pid"], started["pid"])
            self.assertTrue(meta.get("host"))
            if Path("/proc/self/stat").is_file():
                self.assertTrue(meta.get("start"))
                self.assertTrue(meta.get("boot_id"))
            self.assertTrue(daemon_status(self.repo)["running"])
        finally:
            stop_daemon(self.repo)
            for _ in range(30):
                if not daemon_status(self.repo)["running"]:
                    break
                time.sleep(0.05)

        # Simulate PID reuse (current test PID with a bogus start time): must NOT signal or report running!
        if Path("/proc/self/stat").is_file():
            reused = dict(process_identity(os.getpid()))
            reused["start"] = "1"
            write_json(self.state_dir / "daemon.json", reused)
            st = daemon_status(self.repo)
            self.assertFalse(st["running"])
            self.assertTrue(st["stale"])
            with (
                mock.patch("openfarm_core.daemon.os.killpg") as killpg,
                mock.patch("openfarm_core.daemon.os.kill", wraps=os.kill) as kill,
            ):
                stopped = stop_daemon(self.repo)
            self.assertFalse(stopped["signaled"])
            killpg.assert_not_called()
            self.assertFalse(any(c.args[1] != 0 for c in kill.call_args_list))

        # Simulate foreign host record with our live PID: must NOT signal or report running!
        foreign = dict(process_identity(os.getpid()))
        foreign["host"] = "remote-host.invalid"
        write_json(self.state_dir / "daemon.json", foreign)
        self.assertFalse(daemon_status(self.repo)["running"])
        with (
            mock.patch("openfarm_core.daemon.os.killpg") as killpg,
            mock.patch("openfarm_core.daemon.os.kill", wraps=os.kill) as kill,
        ):
            stopped = stop_daemon(self.repo)
        self.assertFalse(stopped["signaled"])
        killpg.assert_not_called()
        self.assertFalse(any(c.args[1] != 0 for c in kill.call_args_list))

    def test_start_daemon_preserves_stop_when_live_run_lock_exists(self) -> None:
        st = StateDir(self.cfg.state_dir)
        self.assertTrue(st.acquire_run_lock("live-run"))
        ks = st.kill_switch(self.cfg.kill_switch)
        ks.write_text("stop please\n", encoding="utf-8")
        try:
            start_daemon(
                self.repo,
                command_override=[sys.executable, "-c", "import time; time.sleep(0.1)"],
            )
            self.assertTrue(ks.exists(), "start_daemon must not unlink STOP while a live run.lock exists")
        finally:
            stop_daemon(self.repo)
            for _ in range(30):
                if not daemon_status(self.repo)["running"]:
                    break
                time.sleep(0.05)
            st.release_run_lock("live-run")

    def test_read_daemon_logs_tails_from_eof(self) -> None:
        lp = self.state_dir / "daemon.log"
        # ~1.5 MB log file
        chunk = ("A" * 100 + "\n") * 15000
        tail_expected = [f"tail-line-{i}" for i in range(5)]
        lp.write_text(chunk + "\n".join(tail_expected) + "\n", encoding="utf-8")

        orig_read_text = Path.read_text

        def _forbid_log_read_text(p: Path, *a, **kw):
            if p.resolve() == lp.resolve():
                raise AssertionError("daemon.log must be tailed from EOF, not read_text() in full")
            return orig_read_text(p, *a, **kw)

        with mock.patch.object(Path, "read_text", autospec=True, side_effect=_forbid_log_read_text):
            res = read_daemon_logs(self.repo, tail=5)
        self.assertTrue(res["exists"])
        self.assertEqual(res["lines"], tail_expected)


class BoardHardeningTests(unittest.TestCase):
    def test_locked_releases_rlock_when_open_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            board = Board(Path(tmp) / "board")
            with mock.patch("builtins.open", side_effect=PermissionError("no lock")):
                with self.assertRaises(PermissionError):
                    with board._locked():
                        pass
            # Another thread must be able to acquire the board lock (no leaked RLock).
            acquired = threading.Event()

            def _other() -> None:
                with board._locked():
                    acquired.set()

            t = threading.Thread(target=_other)
            t.start()
            self.assertTrue(acquired.wait(2.0), "RLock was leaked after open() failed inside _locked()")
            t.join(timeout=2.0)

    def test_board_does_not_wipe_members_on_read_error_or_corrupt_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            board = Board(Path(tmp) / "board")
            board.ensure()
            board.upsert_member("alice", kind="code", role="coordinator")
            board.upsert_member("bob", kind="code", role="implementer")

            # Corrupt JSON must raise Infra and preserve file contents.
            board.members_path.write_text("{corrupt", encoding="utf-8")
            with self.assertRaises(Infra):
                board.upsert_member("charlie", kind="code", role="checker")
            self.assertEqual(board.members_path.read_text(encoding="utf-8"), "{corrupt")

            # Restore valid roster, then simulate transient OSError on read.
            valid = json.dumps({
                "alice": {"name": "alice", "kind": "code", "role": "coordinator", "state": "active"},
            })
            board.members_path.write_text(valid, encoding="utf-8")
            orig_read = Path.read_text

            def _fail_members(p, *a, **kw):
                if p == board.members_path:
                    raise PermissionError("transient EACCES")
                return orig_read(p, *a, **kw)

            with mock.patch.object(Path, "read_text", autospec=True, side_effect=_fail_members):
                with self.assertRaises(Infra):
                    board.upsert_member("charlie", kind="code", role="checker")
            self.assertEqual(json.loads(board.members_path.read_text(encoding="utf-8")), json.loads(valid))

    def test_board_wait_tails_incrementally_without_reparsing_full_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            board = Board(Path(tmp) / "board")
            board.ensure()
            board.upsert_member("alice", kind="code", role="coordinator")
            board.upsert_member("bob", kind="code", role="implementer")
            for i in range(25):
                board.post(author="alice", to=["alice"], kind="note", body=f"noise-{i}", trusted=True)

            offsets_seen: list[int] = []
            orig_tail = board._load_posts_from_offset_unlocked

            def _spy_tail(offset: int):
                offsets_seen.append(offset)
                return orig_tail(offset)

            def _delayed_post() -> None:
                time.sleep(0.15)
                board.post(author="alice", to=["bob"], kind="note", body="wake-bob", trusted=True)

            poster = threading.Thread(target=_delayed_post)
            poster.start()
            try:
                with mock.patch.object(board, "_load_posts_from_offset_unlocked", side_effect=_spy_tail):
                    with mock.patch.object(board, "_load_posts_unlocked", side_effect=AssertionError("must not full-reparse")):
                        res = board.wait("bob", since=0, timeout_s=3.0)
            finally:
                poster.join(timeout=2.0)

            self.assertTrue(res["woke"])
            self.assertEqual(len(res["posts"]), 1)
            self.assertEqual(res["posts"][0]["body"], "wake-bob")
            # Initial call reads from 0; subsequent poll ticks pass non-zero byte offset.
            self.assertEqual(offsets_seen[0], 0)
            self.assertTrue(any(off > 0 for off in offsets_seen[1:]), f"expected incremental offsets, got {offsets_seen}")


class HarnessHardeningTests(unittest.TestCase):
    def test_dual_instance_same_thread_no_deadlock_project_and_global(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hdir = Path(tmp) / "harness"
            lib1 = HarnessLibrary(hdir)
            lib2 = HarnessLibrary(hdir)
            with lib1._locked():
                with lib2._locked():
                    self.assertIsInstance(lib2._load_overlay(), dict)

            gdir = Path(tmp) / "global"
            g1 = GlobalLibrary(gdir)
            g2 = GlobalLibrary(gdir)
            with g1._locked():
                with g2._locked():
                    self.assertEqual(g2.list_entries(), [])

    def test_record_card_outcome_releases_lock_during_micromanager_reflect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = HarnessConfig(
                enabled=True,
                allow_micromanager_reflection=True,
                capture_on=("fail", "retry_recovery"),
                reflect_on=("fail", "retry_recovery"),
            )
            lib = HarnessLibrary(Path(tmp) / "harness", config=cfg)
            concurrent_ok = threading.Event()

            class _SlowMM:
                enabled = True

                def reflect_harness(self, *args, **kwargs):
                    def _probe_lock() -> None:
                        with lib._locked():
                            concurrent_ok.set()

                    t = threading.Thread(target=_probe_lock)
                    t.start()
                    t.join(timeout=2.0)
                    return {"lessons": [], "rejected": []}

            lib.record_card_outcome(
                {"id": "C-1", "language": "python", "kind": "small-feature"},
                [{"passed": False, "outcome": "fail", "error": "AssertionError: boom"}],
                failure="AssertionError: boom",
                micromanager=_SlowMM(),
                reflect=True,
            )
            self.assertTrue(concurrent_ok.is_set(), "HarnessLibrary held _locked() during micromanager.reflect_harness()")

    def test_probe_rollback_restores_cap_evicted_entry_to_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = HarnessConfig(
                enabled=True,
                max_active_adaptations=1,
                probe_window=1,
                auto_demote=True,
                min_confidence=0.5,
                allow_micromanager_reflection=True,
                capture_on=("fail", "retry_recovery"),
                reflect_on=("fail", "retry_recovery"),
            )
            lib = HarnessLibrary(Path(tmp) / "harness", config=cfg)

            class _OneLessonMM:
                enabled = True

                def __init__(self, cue: str, conf: float) -> None:
                    self.cue = cue
                    self.conf = conf

                def reflect_harness(self, *args, **kwargs):
                    return {
                        "lessons": [{
                            "title": self.cue,
                            "surface": "implementer_guidance",
                            "role": "implementer",
                            "language": "python",
                            "kind": "small-feature",
                            "failure_class": "runtime-assert",
                            "cue": self.cue,
                            "lesson": f"Always handle {self.cue}",
                            "confidence": self.conf,
                        }],
                        "rejected": [],
                    }

            # 1. Establish a passing baseline (pass_rate=1.0) and promote E1 (confidence 0.6).
            lib.record_card_outcome(
                {"id": "C-0", "language": "python", "kind": "small-feature"},
                [{"passed": True, "outcome": "pass"}],
                reflect=False,
            )
            r1 = lib.record_card_outcome(
                {"id": "C-1", "language": "python", "kind": "small-feature"},
                [{"passed": True, "outcome": "pass"}, {"passed": True, "outcome": "pass"}],
                micromanager=_OneLessonMM("first-cue", 0.6),
                reflect=True,
            )
            e1_id = r1["lessons_added"][0]
            # Confirm E1 with a passing card.
            lib.record_card_outcome(
                {"id": "C-2", "language": "python", "kind": "small-feature"},
                [{"passed": True, "outcome": "pass"}],
                reflect=False,
            )
            self.assertEqual(lib._load_entry(e1_id)["status"], "active")
            self.assertEqual(lib._load_overlay()["state"], "confirmed")

            # 2. Introduce higher-confidence E2 (0.95) -> evicts E1 by cap and enters probing.
            r2 = lib.record_card_outcome(
                {"id": "C-3", "language": "python", "kind": "small-feature"},
                [{"passed": False, "outcome": "fail"}, {"passed": True, "outcome": "pass"}],
                micromanager=_OneLessonMM("second-cue", 0.95),
                reflect=True,
            )
            e2_id = r2["lessons_added"][0]
            self.assertEqual(lib._load_overlay()["state"], "probing")
            self.assertEqual(lib._load_entry(e1_id)["status"], "demoted")
            self.assertEqual(lib._load_entry(e1_id)["demote_reason"], "evicted_by_cap")

            # 3. Fail during probe window -> rollback must demote E2 AND restore E1 to active + surfaces!
            r3 = lib.record_card_outcome(
                {"id": "C-4", "language": "python", "kind": "small-feature"},
                [{"passed": False, "outcome": "fail"}],
                failure="AssertionError: regressed",
                reflect=False,
            )
            self.assertEqual(r3["probe_transition"]["action"], "rolled_back")
            self.assertEqual(lib._load_entry(e2_id)["status"], "demoted")
            self.assertEqual(lib._load_entry(e1_id)["status"], "active")
            self.assertNotIn("demote_reason", lib._load_entry(e1_id))
            surface_ids = [item["id"] for item in lib._load_overlay()["surfaces"]["implementer_guidance"]]
            self.assertIn(e1_id, surface_ids)

    def test_no_phantom_probing_when_no_new_entry_survives_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = HarnessConfig(
                enabled=True,
                max_active_adaptations=1,
                probe_window=2,
                min_confidence=0.5,
                allow_micromanager_reflection=True,
                capture_on=("fail", "retry_recovery"),
                reflect_on=("fail", "retry_recovery"),
            )
            lib = HarnessLibrary(Path(tmp) / "harness", config=cfg)

            class _MM:
                enabled = True

                def __init__(self, cue: str, conf: float) -> None:
                    self.cue = cue
                    self.conf = conf

                def reflect_harness(self, *args, **kwargs):
                    return {
                        "lessons": [{
                            "title": self.cue,
                            "surface": "implementer_guidance",
                            "role": "implementer",
                            "language": "python",
                            "kind": "small-feature",
                            "failure_class": "runtime-assert",
                            "cue": self.cue,
                            "lesson": f"Always handle {self.cue}",
                            "confidence": self.conf,
                        }],
                        "rejected": [],
                    }

            # Seed E1, confirm it, give it high utility (+5 helped).
            r1 = lib.record_card_outcome(
                {"id": "C-1", "language": "python", "kind": "small-feature"},
                [{"passed": False, "outcome": "fail"}, {"passed": True, "outcome": "pass"}],
                micromanager=_MM("strong-cue", 0.9),
                reflect=True,
            )
            e1_id = r1["lessons_added"][0]
            for k in range(2):
                lib.record_card_outcome(
                    {"id": f"C-confirm-{k}", "language": "python", "kind": "small-feature"},
                    [{"passed": True, "outcome": "pass"}],
                    cues_applied=[e1_id],
                    reflect=False,
                )
            rev_before = lib._load_overlay()["revision"]
            self.assertEqual(lib._load_overlay()["state"], "confirmed")

            # New lower-utility entry E2 gets immediately evicted by cap; must NOT enter phantom probing!
            r2 = lib.record_card_outcome(
                {"id": "C-new", "language": "python", "kind": "small-feature"},
                [{"passed": False, "outcome": "fail"}, {"passed": True, "outcome": "pass"}],
                micromanager=_MM("weak-cue", 0.55),
                reflect=True,
            )
            self.assertEqual(r2["state"], "confirmed")
            self.assertEqual(r2["revision"], rev_before)

    def test_immutable_gate_keys_include_newer_policy_keys(self) -> None:
        required = {
            "checker_mode",
            "planner",
            "invariants",
            "acceptance",
            "factory",
            "auto_land",
            "confine",
            "confine_network",
            "python_tests_in_test_files",
        }
        self.assertTrue(required.issubset(IMMUTABLE_GATE_KEYS))
        for key in required:
            cleaned, reason = validate_harness_proposal(
                {
                    "title": f"override {key}",
                    "surface": "implementer_guidance",
                    "cue": "helpful cue",
                    "lesson": "helpful lesson",
                    "proposed_patch": {key: False},
                    "confidence": 0.9,
                },
                allowed_surfaces=("implementer_guidance",),
            )
            self.assertIsNone(cleaned, f"{key} should be rejected in proposed_patch")
            self.assertTrue(reason)


class ProjectsAndReplayHardeningTests(unittest.TestCase):
    def test_projects_cross_process_flock_and_corrupt_registry_not_wiped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            reg = base / "cfg" / "projects.toml"
            repos = [base / f"proj-{i}" for i in range(4)]
            for r in repos:
                r.mkdir()

            repo_root = Path(__file__).resolve().parent.parent
            worker = (
                "import sys\n"
                "from pathlib import Path\n"
                "from openfarm_core.projects import register_project\n"
                "register_project(Path(sys.argv[1]), path=Path(sys.argv[2]))\n"
            )
            env = dict(os.environ)
            env["PYTHONPATH"] = str(repo_root)
            procs = [
                subprocess.Popen([sys.executable, "-c", worker, str(r), str(reg)], env=env)
                for r in repos
            ]
            for p in procs:
                self.assertEqual(p.wait(timeout=20.0), 0)
            ids = {item["id"] for item in list_projects(path=reg)}
            self.assertEqual(ids, {f"proj-{i}" for i in range(4)})

            # Corrupt registry must NOT be silently wiped on register/edit/remove.
            reg.write_text("[projects\nbroken = !!!\n", encoding="utf-8")
            with self.assertRaises(Refused) as ctx:
                register_project(repos[0], path=reg)
            self.assertEqual(ctx.exception.code, "corrupt-projects")
            self.assertIn("broken = !!!", reg.read_text(encoding="utf-8"))

    def test_projects_does_not_chmod_home_or_dot_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            dot_config = fake_home / ".config"
            dot_config.mkdir(parents=True)
            os.chmod(fake_home, 0o755)
            os.chmod(dot_config, 0o755)
            repo = Path(tmp) / "app"
            repo.mkdir()
            with mock.patch("openfarm_core.projects.Path.home", return_value=fake_home):
                register_project(repo, path=fake_home / "projects.toml")
                register_project(repo, path=dot_config / "projects.toml")
            self.assertEqual(fake_home.stat().st_mode & 0o777, 0o755)
            self.assertEqual(dot_config.stat().st_mode & 0o777, 0o755)

    def test_projects_edit_config_atomic_and_remove_refuses_active_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            reg = base / "cfg" / "projects.toml"
            state_root = base / "state-root"
            repo = base / "app"
            initialize_project(
                "app",
                directory=repo,
                language="python",
                state_dir=state_root / "app",
                secrets_dir=base / "sec",
                no_git=True,
            )
            register_project(repo, state_dir=state_root / "app", path=reg)
            with mock.patch("openfarm_core.projects.os.replace", wraps=os.replace) as spy:
                edit_project("app", base_branch="trunk", path=reg)
            self.assertTrue(
                any(str(call.args[1]).endswith("config.toml") for call in spy.call_args_list),
                "config.toml update must use atomic os.replace",
            )

            # Live run.lock refuses clear_state_dir=True without removing registry entry or state dir.
            st = StateDir(state_root / "app")
            self.assertTrue(st.acquire_run_lock("active-run"))
            try:
                with self.assertRaises(Refused) as ctx:
                    remove_project("app", clear_state_dir=True, allowed_state_root=state_root, path=reg)
                self.assertEqual(ctx.exception.code, "state-dir-active")
                self.assertTrue((state_root / "app").is_dir())
                self.assertEqual([p["id"] for p in list_projects(path=reg)], ["app"])
            finally:
                st.release_run_lock("active-run")

            # Live daemon refuses clear_state_dir=True without removing registry entry or state dir.
            write_json(state_root / "app" / "daemon.json", process_identity(os.getpid()))
            with self.assertRaises(Refused) as ctx2:
                remove_project("app", clear_state_dir=True, allowed_state_root=state_root, path=reg)
            self.assertEqual(ctx2.exception.code, "state-dir-active")
            self.assertTrue((state_root / "app").is_dir())
            self.assertEqual([p["id"] for p in list_projects(path=reg)], ["app"])

    def test_replay_validates_cmd_refuses_symlinks_and_kills_process_group_on_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "rep"
            repo.mkdir()

            # Non-list or empty cmd must be refused.
            with self.assertRaises(Refused) as ctx:
                add_replay(repo, "R-1", title="str cmd", cmd="echo hi")  # type: ignore[arg-type]
            self.assertEqual(ctx.exception.code, "empty-replay-cmd")
            with self.assertRaises(Refused):
                add_replay(repo, "R-1", title="empty", cmd=[])

            # Symlink target must be refused.
            rdir = repo / ".openfarm" / "replays"
            rdir.mkdir(parents=True)
            victim = Path(tmp) / "victim.json"
            victim.write_text("precious", encoding="utf-8")
            (rdir / "R-SYM.json").symlink_to(victim)
            with self.assertRaises(Refused) as sym_ctx:
                add_replay(repo, "R-SYM", title="sym", cmd=[sys.executable, "-c", "pass"])
            self.assertEqual(sym_ctx.exception.code, "symlink-replay")
            self.assertEqual(victim.read_text(encoding="utf-8"), "precious")

            # Hand-edited replay with string cmd fails cleanly in run_replays.
            (rdir / "R-BAD.json").write_text(json.dumps({"id": "R-BAD", "cmd": "echo hi"}), encoding="utf-8")
            bad_res = run_replays(repo, replay_id="R-BAD")
            self.assertFalse(bad_res["ok"])
            self.assertIn("non-empty list", bad_res["results"][0]["error"])

            # Timeout must kill the entire process group including background grandchild!
            grandchild_pid_file = Path(tmp) / "gc.pid"
            script = (
                "import subprocess, sys, time\n"
                "from pathlib import Path\n"
                "gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "Path(sys.argv[1]).write_text(str(gc.pid), encoding='utf-8')\n"
                "time.sleep(60)\n"
            )
            add_replay(
                repo,
                "R-TO",
                title="timeout kills group",
                cmd=[sys.executable, "-c", script, str(grandchild_pid_file)],
                timeout_s=1.0,
            )
            res = run_replays(repo, replay_id="R-TO")
            self.assertFalse(res["ok"])
            self.assertTrue(grandchild_pid_file.is_file(), "grandchild should have spawned before timeout")
            gc_pid = int(grandchild_pid_file.read_text(encoding="utf-8").strip())
            # Give OS a short moment to complete SIGKILL teardown.
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and _pid_alive(gc_pid):
                time.sleep(0.05)
            self.assertFalse(_pid_alive(gc_pid), f"grandchild pid {gc_pid} survived replay timeout!")


if __name__ == "__main__":
    unittest.main()
