"""Regression tests for Wave 1 state, store, cards-source, git, and doctor hardening."""

from __future__ import annotations

import fcntl
import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from openfarm_core.adapters.files import FileCardSource, render_card_file
from openfarm_core.bootstrap import initialize_project
from openfarm_core.cards import Card
from openfarm_core.config import load_config
from openfarm_core.doctor import run_doctor
from openfarm_core.errors import Infra, Invalid, Refused
from openfarm_core.git import Git, GitRefused
from openfarm_core.state import StateDir, _pid_alive, append_jsonl, utc_after
from openfarm_core.store import Store, record


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _header(cid: str, **extra) -> dict:
    return {
        "id": cid,
        "title": f"Task {cid}",
        "state": "ready",
        "category": "small-feature",
        "touches": ["tally/core.py"],
        "reference": "tally/core.py",
        "depends": [],
        "tests": [{"name": f"test_{cid.lower().replace('-', '_')}", "file": "tests/test_core.py"}],
        **extra,
    }


BODY = "## Done\n\n- `tally.core.total([1, 2])` returns `3`.\n"


class _RepoBase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        patcher = mock.patch.dict(
            os.environ,
            {
                "OPENFARM_PROJECTS_FILE": str(self.base / "projects.toml"),
                "GIT_AUTHOR_NAME": "Ada",
                "GIT_COMMITTER_NAME": "Ada",
                "GIT_AUTHOR_EMAIL": "ada@example.invalid",
                "GIT_COMMITTER_EMAIL": "ada@example.invalid",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = self.base / "tally"
        initialize_project(
            "tally",
            directory=self.root,
            idea="A tally counter",
            language="python",
            state_dir=self.base / "state",
            secrets_dir=self.base / "secrets",
            commit=True,
        )
        _git(self.root, "config", "user.name", "Ada")
        _git(self.root, "config", "user.email", "ada@example.invalid")
        (self.root / "tally").mkdir(exist_ok=True)
        (self.root / "tally" / "core.py").write_text("def total(xs):\n    return sum(xs)\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "core")
        self.cfg = load_config(self.root)
        self.store = Store(self.root, self.cfg)


class CardsSourceAndStoreHardeningTests(_RepoBase):
    def test_dual_instance_same_thread_flock_does_not_deadlock(self) -> None:
        src1 = self.store.src
        src2 = FileCardSource(
            self.root,
            src1.folder,
            branch=src1.branch,
            worktree=src1.worktree,
            base_branch=src1.base_branch,
        )
        with src1._lock:
            with src2._lock:
                self.assertTrue(src2.migrated)

    def test_cross_process_flock_serializes_cards_source_writers(self) -> None:
        lock_file = self.store.src.worktree.parent / f"{self.store.src.worktree.name}.lock"
        # Hold flock in a child process and verify Store.write_doc blocks until released.
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import fcntl, sys, time\n"
                    "f = open(sys.argv[1], 'a+')\n"
                    "fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
                    "print('LOCKED', flush=True)\n"
                    "sys.stdin.readline()\n"
                    "fcntl.flock(f.fileno(), fcntl.LOCK_UN)\n"
                    "f.close()\n"
                ),
                str(lock_file),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout is not None and holder.stdin is not None
            self.assertEqual(holder.stdout.readline().strip(), "LOCKED")
            done = threading.Event()

            def _writer() -> None:
                self.store.write_doc("spec", "# Updated under flock\n", why="flock test")
                done.set()

            t = threading.Thread(target=_writer)
            t.start()
            self.assertFalse(done.wait(0.25), "write_doc should block while another process holds cards-source.lock")
            holder.stdin.write("go\n")
            holder.stdin.flush()
            self.assertTrue(done.wait(5.0), "write_doc should finish once flock is released")
            t.join(timeout=2.0)
        finally:
            if holder.stdin:
                holder.stdin.close()
            if holder.stdout:
                holder.stdout.close()
            holder.wait(timeout=5.0)

        # Spawn 3 concurrent subprocesses calling next_id + record + log_decision.
        repo_root = Path(__file__).resolve().parent.parent
        worker_code = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "from openfarm_core.config import load_config\n"
            "from openfarm_core.store import Store, record\n"
            "root = Path(sys.argv[1])\n"
            "worker = sys.argv[2]\n"
            "cfg = load_config(root)\n"
            "store = Store(root, cfg)\n"
            "ids = []\n"
            "for i in range(4):\n"
            "    cid = store.next_card_id(1)\n"
            "    ids.append(cid)\n"
            "    store.log_decision('answer', f'entry-{worker}-{i}', question=f'Q-{worker}-{i}')\n"
            "    record(store.src, root, 'W1-00', 'note', f'{worker}-{i}', {'i': i}, by=f'worker/{worker}')\n"
            "print(json.dumps(ids))\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(repo_root)
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", worker_code, str(self.root), f"w{k}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            for k in range(3)
        ]
        all_ids: list[str] = []
        for p in procs:
            out, err = p.communicate(timeout=30.0)
            self.assertEqual(p.returncode, 0, f"worker failed: {err}")
            all_ids.extend(json.loads(out.strip()))
        self.assertEqual(len(all_ids), 12)
        self.assertEqual(len(set(all_ids)), 12, f"duplicate IDs generated across processes: {all_ids}")
        self.assertEqual(len(self.store.history("W1-00")), 12)

    def test_save_meta_uses_atomic_replace(self) -> None:
        with mock.patch("openfarm_core.adapters.files.os.replace", wraps=os.replace) as spy:
            meta = self.store.src.meta()
            meta["probe"] = "atomic"
            self.store.src.save_meta(meta, "atomic meta")
        self.assertGreaterEqual(spy.call_count, 1)
        self.assertEqual(self.store.src.meta().get("probe"), "atomic")
        leftovers = list(self.store.src.worktree.glob(".store.json.*.tmp"))
        self.assertEqual(leftovers, [])

    def test_stale_detects_expired_leases_and_next_filters_lane_and_holder(self) -> None:
        src = self.store.src
        # Ready card in lane alpha, ready card in lane beta, ready card with holder, ready card with legacy deps.
        src._write("W1-01", _header("W1-01", lane="alpha"), BODY, "add W1-01")
        src._write("W1-02", _header("W1-02", lane="beta"), BODY, "add W1-02")
        src._write("W1-03", _header("W1-03", lane="alpha", holder="lane-x"), BODY, "add W1-03")
        h4 = _header("W1-04", lane="alpha")
        h4.pop("depends")
        h4["deps"] = ["W1-02"]
        src._write("W1-04", h4, BODY, "add W1-04")

        self.assertEqual(src.next(lane="alpha")["cards"], ["W1-01"])
        self.assertEqual(src.next(lane="beta")["cards"], ["W1-02"])

        # Claim W1-01 and expire its lease; claim W1-02 with an active lease.
        src.claim("W1-01", holder="runner-a", lease_ttl_s=60)
        src.claim("W1-02", holder="runner-b", lease_ttl_s=3600)
        h1, b1, _ = src._read_raw("W1-01")
        h1["lease_expires_at"] = utc_after(-120)
        src._write("W1-01", h1, b1, "expire W1-01")

        stale_all = src.stale()["cards"]
        self.assertEqual([c["id"] for c in stale_all], ["W1-01"])
        self.assertEqual(stale_all[0]["holder"], "runner-a")
        self.assertEqual([c["id"] for c in src.stale(lane="alpha")["cards"]], ["W1-01"])
        self.assertEqual(src.stale(lane="beta")["cards"], [])

        # Card hash matches read() and remains invariant across heartbeat().
        claim2 = src.heartbeat("W1-02", src._read_raw("W1-02")[0]["lease_id"], lease_ttl_s=7200)
        self.assertTrue(claim2["expires_at"])
        card_from_read = Card.from_dict(src.read("W1-02")["card"])
        h_before = src.hash("W1-02")["hash"]
        self.assertEqual(card_from_read.hash(), h_before)
        src.heartbeat("W1-02", src._read_raw("W1-02")[0]["lease_id"], lease_ttl_s=9000)
        self.assertEqual(src.hash("W1-02")["hash"], h_before)

    def test_split_card_refuses_edit_and_release_leaving_worktree_clean(self) -> None:
        self.store.change_card("W1-01", "create", header=_header("W1-01"), body=BODY)
        self.store.change_card("W1-01", "split", why="too big")
        self.assertEqual(self.store.read("W1-01")[0]["state"], "split")
        hist_before = len(self.store.history("W1-01"))

        with self.assertRaises(Refused) as ctx:
            self.store.change_card("W1-01", "edit", apply=lambda h, b: (h, b), why="nope")
        self.assertEqual(ctx.exception.code, "card-not-editable")

        with self.assertRaises(Refused) as ctx2:
            self.store.src.release("W1-01")
        self.assertEqual(ctx2.exception.code, "not-held")

        with self.assertRaises(Refused) as ctx3:
            self.store.change_card("W1-01", "release", why="nope")
        self.assertEqual(ctx3.exception.code, "not-held")

        self.assertEqual(len(self.store.history("W1-01")), hist_before)
        self.assertTrue(Git(self.store.src.worktree).is_clean())

    def test_refused_or_invalid_change_card_never_dirties_history_or_worktree(self) -> None:
        self.store.change_card("W1-01", "create", header=_header("W1-01"), body=BODY)
        self.store.src.claim("W1-01", holder="live-lane", lease_ttl_s=3600)
        hist_before = len(self.store.history("W1-01"))

        # Refused release on a live lease must not append history or dirty worktree.
        with self.assertRaises(Refused) as ctx:
            self.store.change_card("W1-01", "release", why="force")
        self.assertEqual(ctx.exception.code, "held")
        self.assertEqual(len(self.store.history("W1-01")), hist_before)
        self.assertTrue(Git(self.store.src.worktree).is_clean())

        # Refused accept on a non-blocked card must not append history or dirty worktree.
        with self.assertRaises(Refused) as ctx2:
            self.store.change_card("W1-01", "accept", why="checked")
        self.assertEqual(ctx2.exception.code, "not-blocked")
        self.assertEqual(len(self.store.history("W1-01")), hist_before)
        self.assertTrue(Git(self.store.src.worktree).is_clean())

        # Unwritable card on create must not append history or dirty worktree.
        bad_header = _header("W1-02", note="line1\n+++\nline2")
        with self.assertRaises(Invalid) as ctx3:
            self.store.change_card("W1-02", "create", header=bad_header, body=BODY)
        self.assertEqual(ctx3.exception.code, "unwritable-card")
        self.assertEqual(self.store.history("W1-02"), [])
        self.assertTrue(Git(self.store.src.worktree).is_clean())


class StoreMigrationHardeningTests(_RepoBase):
    def _setup_legacy(self, extra_cards: dict[str, str] | None = None) -> None:
        self.store.change_card("W1-01", "create", header=_header("W1-01"), body=BODY)
        self.store.change_card("W1-02", "create", header=_header("W1-02"), body=BODY)
        self.store.change_card("W1-03", "create", header=_header("W1-03"), body=BODY)
        self.store.change_card("W1-04", "create", header=_header("W1-04"), body=BODY)
        # Put W1-02 in done, W1-03 in in_review, W1-04 in dropped on cards branch.
        c2 = self.store.src.claim("W1-02", "lane")
        self.store.src.move("W1-02", c2["lease_id"], "in-review")
        self.store.src.mark_done("W1-02")
        c3 = self.store.src.claim("W1-03", "lane")
        self.store.src.move("W1-03", c3["lease_id"], "in-review")
        self.store.change_card("W1-04", "drop", why="obsolete")

        meta = self.store.src.meta()
        meta.pop("migrated", None)
        self.store.src.save_meta(meta, "test: pre-migration")

        (self.root / "cards").mkdir()
        (self.root / "cards" / "W1-01.md").write_text(
            render_card_file(_header("W1-01", title="Main W1-01"), BODY)
        )
        (self.root / "cards" / "W1-02.md").write_text(
            render_card_file(_header("W1-02", title="Main W1-02"), BODY)
        )
        (self.root / "cards" / "W1-03.md").write_text(
            render_card_file(_header("W1-03", title="Main W1-03"), BODY)
        )
        (self.root / "cards" / "W1-04.md").write_text(
            render_card_file(_header("W1-04", title="Main W1-04"), BODY)
        )
        for name, text in (extra_cards or {}).items():
            (self.root / "cards" / name).write_text(text)
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "legacy cards")
        self.store = Store(self.root, self.cfg)

    def test_migrate_validates_upfront_and_handles_done_in_review_dropped_take_main(self) -> None:
        self._setup_legacy()
        # Invalid choice rejects upfront without mutating W1-01.
        with self.assertRaises(Refused) as bad:
            self.store.migrate({"W1-01": "main", "W1-02": "bogus", "W1-03": "main", "W1-04": "main"})
        self.assertEqual(bad.exception.code, "migration-choice-invalid")
        self.assertEqual(self.store.read("W1-01")[0]["title"], "Task W1-01")

        # Valid take=main for done, in_review, dropped cards succeeds without crashing mid-loop.
        res = self.store.migrate({
            "W1-01": "main",
            "W1-02": "main",
            "W1-03": "main",
            "W1-04": "main",
        })
        self.assertTrue(res["migrated"])
        self.assertEqual(self.store.read("W1-01")[0]["title"], "Main W1-01")
        self.assertEqual(self.store.read("W1-02")[0]["title"], "Main W1-02")
        self.assertEqual(self.store.read("W1-02")[0]["state"], "done")
        self.assertEqual(self.store.read("W1-03")[0]["title"], "Main W1-03")
        self.assertEqual(self.store.read("W1-03")[0]["state"], "in_review")
        self.assertEqual(self.store.read("W1-04")[0]["title"], "Main W1-04")
        self.assertEqual(self.store.read("W1-04")[0]["state"], "dropped")

    def test_migrate_refuses_unparseable_legacy_card_instead_of_deleting(self) -> None:
        self._setup_legacy(extra_cards={"BROKEN.md": "not a valid card file at all\n"})
        status = self.store.migration()
        self.assertEqual([u["file"] for u in status["unparseable"]], ["BROKEN.md"])
        with self.assertRaises(Refused) as ctx:
            self.store.migrate({
                "W1-01": "state",
                "W1-02": "state",
                "W1-03": "state",
                "W1-04": "state",
            })
        self.assertEqual(ctx.exception.code, "migration-unparseable")
        self.assertTrue((self.root / "cards" / "BROKEN.md").is_file())
        self.assertFalse(self.store.src.migrated)

    def test_migrate_does_not_mark_migrated_if_git_rm_fails(self) -> None:
        self._setup_legacy()
        orig_run = Git._run

        def _fail_rm(git_self, args, *a, **kw):
            if args and args[0] == "rm" and git_self.cwd.resolve() == self.root.resolve():
                raise Infra("git-failed", "simulated git rm failure")
            return orig_run(git_self, args, *a, **kw)

        with mock.patch.object(Git, "_run", autospec=True, side_effect=_fail_rm):
            with self.assertRaises(Infra):
                self.store.migrate({
                    "W1-01": "state",
                    "W1-02": "state",
                    "W1-03": "state",
                    "W1-04": "state",
                })
        self.assertFalse(self.store.src.migrated)
        self.assertTrue(self.store.migration()["needed"])
        self.assertTrue((self.root / "cards" / "W1-01.md").is_file())


class StateDoctorAndGitHardeningTests(_RepoBase):
    def test_release_run_lock_guards_and_never_unlinks_empty_or_corrupt_lock(self) -> None:
        st = StateDir(self.base / "lock-state")
        # Empty run.lock must not be unlinked.
        st.run_lock.write_text("", encoding="utf-8")
        st.release_run_lock("run-1")
        self.assertTrue(st.run_lock.exists())

        # Corrupt JSON run.lock must not be unlinked.
        st.run_lock.write_text("{not-json", encoding="utf-8")
        st.release_run_lock("run-1")
        self.assertTrue(st.run_lock.exists())

        # Matching run_id IS unlinked and blocks on run.lock.guard.
        st.run_lock.unlink()
        self.assertTrue(st.acquire_run_lock("run-1"))
        st.release_run_lock("run-other")
        self.assertTrue(st.run_lock.exists())

        guard = st.root / "run.lock.guard"
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import fcntl, sys\n"
                    "f = open(sys.argv[1], 'a+')\n"
                    "fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
                    "print('LOCKED', flush=True)\n"
                    "sys.stdin.readline()\n"
                    "fcntl.flock(f.fileno(), fcntl.LOCK_UN)\n"
                ),
                str(guard),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout is not None and holder.stdin is not None
            self.assertEqual(holder.stdout.readline().strip(), "LOCKED")
            done = threading.Event()
            t = threading.Thread(target=lambda: (st.release_run_lock("run-1"), done.set()))
            t.start()
            self.assertFalse(done.wait(0.2), "release_run_lock should wait on run.lock.guard")
            holder.stdin.write("go\n")
            holder.stdin.flush()
            self.assertTrue(done.wait(5.0))
            t.join(timeout=2.0)
        finally:
            if holder.stdin:
                holder.stdin.close()
            if holder.stdout:
                holder.stdout.close()
            holder.wait(timeout=5.0)
        self.assertFalse(st.run_lock.exists())

    def test_state_pid_alive_rejects_nonpositive_and_zombie(self) -> None:
        self.assertFalse(_pid_alive(0))
        self.assertFalse(_pid_alive(-1))
        self.assertFalse(_pid_alive(-os.getpid()))
        self.assertFalse(_pid_alive(False))
        self.assertTrue(_pid_alive(os.getpid()))

        if Path("/proc/self/stat").is_file():
            child = subprocess.Popen([sys.executable, "-c", "pass"])
            try:
                # Wait briefly for child to become a zombie (without calling wait()).
                deadline = time.monotonic() + 3.0
                saw_zombie = False
                while time.monotonic() < deadline:
                    try:
                        stat_txt = Path(f"/proc/{child.pid}/stat").read_text(encoding="ascii", errors="replace")
                    except OSError:
                        break
                    fields = stat_txt.rsplit(")", 1)[1].split()
                    if fields and fields[0] == "Z":
                        saw_zombie = True
                        break
                    time.sleep(0.02)
                if saw_zombie:
                    self.assertFalse(_pid_alive(child.pid))
            finally:
                child.wait(timeout=5.0)

    def test_append_jsonl_cross_process_no_interleaving(self) -> None:
        target = self.base / "events.jsonl"
        repo_root = Path(__file__).resolve().parent.parent
        worker = (
            "import sys\n"
            "from pathlib import Path\n"
            "from openfarm_core.state import append_jsonl\n"
            "p = Path(sys.argv[1])\n"
            "wid = int(sys.argv[2])\n"
            "payload = 'x' * 4096\n"
            "for i in range(40):\n"
            "    append_jsonl(p, {'w': wid, 'i': i, 'blob': payload})\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(repo_root)
        procs = [
            subprocess.Popen([sys.executable, "-c", worker, str(target), str(k)], env=env)
            for k in range(4)
        ]
        for p in procs:
            self.assertEqual(p.wait(timeout=20.0), 0)
        lines = target.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 160)
        parsed = [json.loads(line) for line in lines]
        self.assertEqual(len({(r["w"], r["i"]) for r in parsed}), 160)

    def test_doctor_respects_stale_lock_holder_prunes_worktrees_and_guards_chmod(self) -> None:
        st = StateDir(self.cfg.state_dir)
        # Foreign-host lock must NOT be treated as stale or deleted by doctor --fix.
        st.run_lock.write_text(
            json.dumps({"run_id": "foreign", "pid": 999999, "host": "other-host.invalid"}),
            encoding="utf-8",
        )
        rep = run_doctor(self.root, fix=True)
        by_id = {c["id"]: c for c in rep["checks"]}
        self.assertEqual(by_id["run_lock"]["status"], "ok")
        self.assertTrue(st.run_lock.exists(), "doctor --fix must never delete a foreign-host run.lock")

        # Same-host dead PID IS stale and removed by doctor --fix.
        st.run_lock.write_text(
            json.dumps({"run_id": "dead-local", "pid": 999999, "host": platform.node()}),
            encoding="utf-8",
        )
        rep2 = run_doctor(self.root, fix=True)
        self.assertFalse(st.run_lock.exists())
        self.assertIn("removed stale", " ".join(rep2["fixes_applied"]))

        # Orphaned worktree removal runs git worktree prune.
        wt = st.worktrees / "orphan-wt"
        Git(self.root).worktree_add(wt, "HEAD")
        self.assertIn(str(wt), Git(self.root).worktree_list())
        rep3 = run_doctor(self.root, fix=True)
        self.assertFalse(wt.exists())
        self.assertNotIn(str(wt), Git(self.root).worktree_list())
        self.assertIn("pruned 1 orphaned worktree(s)", " ".join(rep3["fixes_applied"]))

        # Guard os.chmod OSError on worker key file.
        kf = self.cfg.key_file
        assert kf is not None
        kf.write_text("sk-secret\n", encoding="utf-8")
        os.chmod(kf, 0o644)
        with mock.patch("openfarm_core.doctor.os.chmod", side_effect=PermissionError("denied")):
            rep4 = run_doctor(self.root, fix=True)
        by_id4 = {c["id"]: c for c in rep4["checks"]}
        self.assertEqual(by_id4["key_permissions"]["status"], "fail")

    def test_git_create_branch_raises_infra_quotepath_and_config_hardening(self) -> None:
        git = Git(self.root)
        # Invalid branch name with no worktree holder must raise Infra instead of returning silently.
        with self.assertRaises(Infra):
            git.create_branch("bad..branch", "HEAD")

        # Non-ASCII filename must not be octal-escaped (core.quotepath=false).
        unicode_file = self.root / "café.txt"
        unicode_file.write_text("bonjour\n", encoding="utf-8")
        self.assertIn("café.txt", git.changed_files())
        unicode_file.unlink()

        # _run passes core.quotepath=false and core.fsmonitor=false.
        with mock.patch("openfarm_core.git.subprocess.run", wraps=subprocess.run) as spy:
            git.head()
        argv = spy.call_args[0][0]
        self.assertIn("core.quotepath=false", argv)
        self.assertIn("core.fsmonitor=false", argv)

        # _check_config_args refuses -f/--file and code-executing keys.
        for bad_args in (
            ["config", "-f", "/tmp/evil", "user.name", "x"],
            ["config", "-f/tmp/evil", "user.name", "x"],
            ["config", "--file", "/tmp/evil", "user.name", "x"],
            ["config", "--file=/tmp/evil", "user.name", "x"],
            ["config", "credential.helper", "!sh"],
            ["config", "credential.https://example.com.helper", "!sh"],
            ["config", "core.editor", "sh"],
            ["config", "core.askpass", "/bin/sh"],
            ["config", "diff.external", "/bin/sh"],
            ["config", "filter.lfs.smudge", "sh"],
            ["config", "include.path", "/etc/passwd"],
            ["config", "includeIf.gitdir:/.path", "/tmp/x"],
        ):
            with self.assertRaises(GitRefused, msg=f"expected GitRefused for {bad_args}"):
                git._run(bad_args)

        # Safe local config still works.
        self.assertEqual(git._run(["config", "user.name"]).stdout.strip(), "Ada")


if __name__ == "__main__":
    unittest.main()
