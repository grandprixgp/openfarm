"""J1: the driver's CLI children are held by the bridges' rules (flags, boundary, refusals)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import pwd
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openfarm_core import confine as confine_lib
from openfarm_core import driver as driver_mod
from openfarm_core.cli import EXIT_OK, EXIT_REFUSED, main as cli_main
from openfarm_core.driver import DriverAgent
from openfarm_core.errors import Refused
from openfarm_core.state import read_json
from tests.test_python_e2e import GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy

BWRAP_OK, BWRAP_WHY = confine_lib.bwrap_status()

# The strict set from the lead's live check against codex-cli 0.166.0.
LIVE_STRICT = [
    "--disable", "shell_tool", "--disable", "unified_exec", "--disable", "apps",
    "--disable", "plugins", "--disable", "multi_agent", "--disable", "browser_use",
    "--disable", "computer_use", "--disable", "image_generation", "--disable", "view_image",
    "--disable", "multi_agent_v2", "--disable", "goals", "--disable", "sleep_tool",
    "--disable", "code_mode_host", "--disable", "tool_suggest", "--disable", "skill_search",
    "--disable", "hooks", "-c", 'web_search="disabled"',
]

_ANSWER = {
    "codex": (
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "{}"}})
        + "\n"
        + json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 1}})
        + "\n"
    ),
    "antigravity": json.dumps({"response": "{}", "usage": {"promptTokenCount": 3,
                                                           "candidatesTokenCount": 1}}),
    "claude": json.dumps({"type": "result", "result": "{}", "usage": {"input_tokens": 3,
                                                                      "output_tokens": 1}}),
}


class _FakeProc:
    def __init__(self, record: list, provider: str, argv, **kwargs) -> None:
        self.argv = list(argv)
        self.kwargs = kwargs
        self.pid = -1
        self.returncode = 0
        self._provider = provider
        cwd = Path(kwargs.get("cwd") or ".")
        record.append({"argv": self.argv, "cwd": str(cwd), "cwd_is_dir": cwd.is_dir(),
                       "cwd_entries": sorted(os.listdir(cwd)) if cwd.is_dir() else None})

    def communicate(self, input=None, timeout=None):
        return _ANSWER[self._provider], ""

    def poll(self):
        return 0


def _bin(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


class DriverArgvTests(unittest.TestCase):
    """The argv and folder each provider's child gets under each setting."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v081c-d2-argv-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        (self.repo / "seams").mkdir(parents=True)
        (self.repo / "seams" / "core.rs").write_text("NEVER_TOUCH\n")
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        self.home = self.tmp / "host-home"
        self.home.mkdir()
        self.key = self.tmp / "bench.key"
        self.key.write_text("KEY\n")
        self.calls: list[dict] = []
        patcher = mock.patch.object(driver_mod, "_end_process_group", lambda *a, **k: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _drive_once(self, provider: str, mode: str, *, usable: bool,
                    prompt: str = "hello") -> tuple[DriverAgent, dict | None, Exception | None]:
        name = {"claude": "claude", "antigravity": "agy", "codex": "codex"}[provider]
        exe = _bin(self.bin_dir, name)
        settings = confine_lib.ConfineSettings(mode=mode, hide_paths=(str(self.key),))
        status = (True, "bwrap works") if usable else (False, "bwrap is not installed (test)")
        drv = DriverAgent(agent=provider, cwd=self.repo, driver_bin=str(exe),
                          never_touch=["seams/**"], env={"HOME": str(self.home),
                                                         "PATH": os.environ.get("PATH", "")},
                          confine=settings)
        self.addCleanup(drv.close)
        error: Exception | None = None
        with mock.patch.object(confine_lib, "bwrap_status", return_value=status) as st, \
                mock.patch.object(confine_lib, "find_bwrap", return_value="/fake/bwrap"), \
                mock.patch.object(driver_mod, "_POPEN",
                                  lambda argv, **kw: _FakeProc(self.calls, provider, argv, **kw)):
            try:
                drv.complete(prompt, system_prompt="SYS")
            except Refused as exc:
                error = exc
            self.status_calls = st.call_count
        return drv, (self.calls[-1] if self.calls else None), error

    @staticmethod
    def _inner(argv: list[str]) -> list[str]:
        return argv[argv.index("--") + 1:] if argv and argv[0] == "/fake/bwrap" else argv

    def test_codex_auto_with_bwrap_gets_the_strict_flags_and_the_boundary(self) -> None:
        drv, call, err = self._drive_once("codex", "auto", usable=True)
        self.assertIsNone(err)
        argv = call["argv"]
        self.assertEqual(argv[0], "/fake/bwrap")
        inner = self._inner(argv)
        self.assertEqual(inner[1:9], ["exec", "--json", "--ephemeral", "--skip-git-repo-check",
                                      "--ignore-user-config", "--ignore-rules", "-s", "read-only"])
        self.assertEqual(inner[9:9 + len(LIVE_STRICT)], LIVE_STRICT)
        child = inner[inner.index("-C") + 1]
        # The session folder is an empty private one, never the repo.
        self.assertEqual(call["cwd"], child)
        self.assertNotEqual(Path(child), self.repo.resolve())
        self.assertEqual(call["cwd_entries"], [])
        bound = [argv[i + 1] for i, a in enumerate(argv) if a == "--ro-bind"]
        self.assertIn(child, bound)
        self.assertNotIn(str(self.repo.resolve()), " ".join(argv[:argv.index("--")]))
        self.assertNotIn(str(self.home), bound)
        self.assertEqual(drv.hold.describe(), "flags + boundary")
        self.assertEqual(drv.notes, [])
        drv.close()
        self.assertFalse(Path(child).exists(), "the empty folder goes on close")

    def test_codex_auto_without_bwrap_runs_on_flags_and_says_so(self) -> None:
        drv, call, err = self._drive_once("codex", "auto", usable=False)
        self.assertIsNone(err)
        argv = call["argv"]
        self.assertNotEqual(argv[0], "/fake/bwrap")
        self.assertEqual(argv[9:9 + len(LIVE_STRICT)], LIVE_STRICT)
        self.assertNotEqual(Path(call["cwd"]), self.repo.resolve())
        self.assertEqual(drv.hold.describe(), "flags")
        self.assertEqual(len(drv.notes), 1)
        self.assertIn("tool flags alone", drv.notes[0])

    def test_antigravity_auto_needs_the_boundary(self) -> None:
        drv, call, err = self._drive_once("antigravity", "auto", usable=True)
        self.assertIsNone(err)
        self.assertEqual(call["argv"][0], "/fake/bwrap")
        inner = self._inner(call["argv"])
        self.assertEqual(Path(inner[0]).name, "agy")
        self.assertNotEqual(Path(call["cwd"]), self.repo.resolve())
        self.assertEqual(drv.hold.describe(), "boundary")

    def test_antigravity_auto_without_bwrap_is_refused_before_spawn(self) -> None:
        drv, call, err = self._drive_once("antigravity", "auto", usable=False)
        self.assertIsNone(call and call["argv"][:1], "nothing may start")
        self.assertIsInstance(err, Refused)
        self.assertEqual(err.code, "acp-confine-unavailable")
        expected = confine_lib.decide("antigravity", transport="bridge", tool_policy="none",
                                      mode="auto", bwrap_usable=False,
                                      bwrap_reason="bwrap is not installed (test)").refused
        self.assertEqual(err.message, expected)
        self.assertIn("every Antigravity child needs the boundary", err.message)

    def test_claude_auto_is_unchanged(self) -> None:
        drv, call, err = self._drive_once("claude", "auto", usable=True)
        self.assertIsNone(err)
        self.assertEqual(Path(call["argv"][0]).name, "claude")
        self.assertEqual(Path(call["cwd"]), self.repo.resolve())
        self.assertEqual(self.status_calls, 0, "Claude under auto never asks for bwrap")
        self.assertEqual(drv.hold.describe(), "nothing")
        self.assertEqual(drv.notes, [])

    def test_claude_bwrap_is_confined_or_refused(self) -> None:
        drv, call, err = self._drive_once("claude", "bwrap", usable=True)
        self.assertIsNone(err)
        self.assertEqual(call["argv"][0], "/fake/bwrap")
        self.assertNotEqual(Path(call["cwd"]), self.repo.resolve())
        self.calls.clear()
        drv2, call2, err2 = self._drive_once("claude", "bwrap", usable=False)
        self.assertIsNone(call2)
        self.assertEqual(err2.code, "acp-confine-unavailable")
        self.assertIn('confine = "bwrap" confines every provider', err2.message)

    def test_off_confines_nothing_and_says_so(self) -> None:
        for provider in ("codex", "antigravity", "claude"):
            with self.subTest(provider=provider):
                self.calls.clear()
                drv, call, err = self._drive_once(provider, "off", usable=True)
                self.assertIsNone(err)
                self.assertNotEqual(call["argv"][0], "/fake/bwrap")
                self.assertEqual(self.status_calls, 0)
                self.assertEqual(drv.notes, [confine_lib.off_note(provider)])
                if provider == "codex":
                    # The tool flags cost nothing and stay, as in the bridge.
                    self.assertEqual(call["argv"][9:9 + len(LIVE_STRICT)], LIVE_STRICT)


def _run_cli(*args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_main(list(args))
    return code, out.getvalue(), err.getvalue()


_LOGGING_CLI = """#!/usr/bin/env python3
import json, sys
open({log!r}, "a").write("called\\n")
print(json.dumps({{"type": "result", "result": "{{}}"}}))
"""


class DriveRunConfineTests(unittest.TestCase):
    """`openfarm drive` takes the setting from the config and refuses up front."""

    def setUp(self) -> None:
        self.toy = build_python_toy()
        self.addCleanup(self.toy.cleanup)
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v081c-d2-run-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.toy.write_card(
            "CARD-1", title="A full bundle refuses a push", category="pinned-bugfix", unit=".",
            touches=["tally/bounded.py"],
            tests=[{"name": "test_a_full_bundle_refuses_a_push", "file": "tests/test_full.py",
                    "asserts": "push raises FullError"}],
            done_text="push on a full bundle raises `FullError`.",
        )
        self.toy.commit("add CARD-1")
        self.worker = self.tmp / "worker.json"
        self.worker.write_text(json.dumps({
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
            "CARD-1/implementer": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: full bundle refuses push with FullError",
            "CARD-1/checker_pass2": "{}",
        }))

    def _set_confine(self, mode: str) -> None:
        cfg = self.toy.root / ".openfarm" / "config.toml"
        cfg.write_text(cfg.read_text() + f'\n[micromanager]\nconfine = "{mode}"\n')
        self.toy.commit(f"confine {mode}")

    def test_an_unconfinable_child_is_refused_before_the_run_starts(self) -> None:
        log = self.tmp / "agy.log"
        agy = self.tmp / "agy"
        agy.write_text(_LOGGING_CLI.format(log=str(log)))
        agy.chmod(0o755)
        with mock.patch.object(confine_lib, "bwrap_status",
                               return_value=(False, "bwrap is not installed (test)")):
            rc, out, err = _run_cli("--repo", str(self.toy.root), "--json", "drive",
                                    "--agent", "antigravity", "--cards", "1",
                                    "--driver-bin", str(agy), "--script", str(self.worker))
        self.assertEqual(rc, EXIT_REFUSED, err + out)
        payload = json.loads(out)
        self.assertTrue(payload["refused"])
        self.assertEqual(payload["code"], "acp-confine-unavailable")
        self.assertIn("every Antigravity child needs the boundary", payload["reason"])
        self.assertFalse(log.exists(), "agy must never start")
        runs = self.toy.state_dir / "runs"
        self.assertEqual(list(runs.iterdir()) if runs.exists() else [], [],
                         "no run was started")
        self.assertFalse((self.toy.state_dir / "run.lock").exists())

    def test_off_is_said_in_the_drives_notes_and_run_json(self) -> None:
        self._set_confine("off")
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        from tests.test_driver_mode import _write_mock_driver_cli
        codex = _write_mock_driver_cli(bin_dir, "codex", mode="drive")
        rc, out, err = _run_cli("--repo", str(self.toy.root), "--json", "drive",
                                "--agent", "codex", "--cards", "1",
                                "--driver-bin", str(codex), "--script", str(self.worker))
        self.assertEqual(rc, EXIT_OK, err + out)
        payload = json.loads(out)
        note = confine_lib.off_note("codex")
        self.assertIn(note, payload["notes"])
        self.assertEqual(payload["confine"]["mode"], "off")
        record = read_json(self.toy.state_dir / "runs" / payload["run_id"] / "run.json")
        self.assertIn(note, record["notes"])
        h_rc, h_out, h_err = _run_cli("--repo", str(self.toy.root), "drive",
                                      "--agent", "codex", "--cards", "1",
                                      "--driver-bin", str(codex), "--script", str(self.worker))
        self.assertIn('confine = "off"', h_out + h_err)


def _real_home_file() -> Path | None:
    """A regular file in the real home this process can read, or None."""
    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        for entry in sorted(home.iterdir()):
            if entry.is_file() and not entry.is_symlink() and os.access(entry, os.R_OK):
                return entry
    except OSError:
        return None
    return None


# A fake codex / agy: reads each `READ <path>` line of its prompt and answers
# with what it could read, in the shape its real CLI answers in.
_READING_CLI = r'''#!/usr/bin/env python3
import json, os, sys
provider = PROVIDER
args = sys.argv[1:]
if provider == "antigravity":
    prompt = args[args.index("-p") + 1]
else:
    prompt = sys.stdin.read()
reads = {}
for line in prompt.splitlines():
    if line.startswith("READ "):
        p = line[5:].strip()
        try:
            with open(p) as fh:
                reads[p] = "READ:" + fh.read()
        except Exception as exc:
            reads[p] = "ERR " + type(exc).__name__
body = json.dumps({"reads": reads, "cwd": os.getcwd(), "entries": sorted(os.listdir("."))})
if provider == "antigravity":
    print(json.dumps({"response": body, "usage": {"promptTokenCount": 3, "candidatesTokenCount": 1}}))
else:
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": body}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 1}}))
'''


@unittest.skipUnless(BWRAP_OK, f"bwrap is not usable here: {BWRAP_WHY}")
class RealBoundaryDriverTests(unittest.TestCase):
    """Fake CLIs inside the real boundary try to read what they must not."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v081c-d2-real-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        (self.repo / "seams").mkdir(parents=True)
        self.seam = self.repo / "seams" / "core.rs"
        self.seam.write_text("NEVER_TOUCH_CONTENT\n")
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.state_file = self.state / "runs.json"
        self.state_file.write_text("STATE_CONTENT\n")
        self.key = self.tmp / "keys" / "bench.key"
        self.key.parent.mkdir()
        self.key.write_text("KEY_CONTENT\n")
        self.host_home = self.tmp / "host-home"
        self.host_home.mkdir()
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()

    def _fake(self, provider: str) -> Path:
        name = "agy" if provider == "antigravity" else "codex"
        path = self.bin_dir / name
        path.write_text(_READING_CLI.replace("PROVIDER", repr(provider)))
        path.chmod(0o755)
        return path

    def _turn(self, provider: str, mode: str, targets: list[Path]) -> dict:
        drv = DriverAgent(
            agent=provider, cwd=self.repo, driver_bin=str(self._fake(provider)),
            never_touch=["seams/**"],
            env={"HOME": str(self.host_home), "PATH": os.environ.get("PATH", "/usr/bin")},
            confine=confine_lib.ConfineSettings(mode=mode, hide_paths=(str(self.key),)),
        )
        with drv:
            prompt = "\n".join(f"READ {p}" for p in targets)
            resp = drv.complete(prompt)
            return json.loads(resp.text)

    def test_codex_and_agy_children_read_nothing_outside(self) -> None:
        planted = [self.seam, self.state_file, self.key]
        home_file = _real_home_file()
        targets = planted + ([home_file] if home_file is not None else [])
        for provider in ("codex", "antigravity"):
            with self.subTest(provider=provider):
                body = self._turn(provider, "auto", targets)
                for target in targets:
                    self.assertTrue(body["reads"][str(target)].startswith("ERR"),
                                    f"{provider} read {target}")
                self.assertEqual(body["entries"], [], "the child's folder is empty")
                self.assertNotEqual(Path(body["cwd"]), self.repo.resolve())

    def test_under_off_the_same_fake_reads_them(self) -> None:
        # The control: the fakes really try, so the boundary is what stops them.
        planted = [self.seam, self.state_file, self.key]
        body = self._turn("codex", "off", planted)
        for target in planted:
            self.assertTrue(body["reads"][str(target)].startswith("READ:"), body)


if __name__ == "__main__":
    unittest.main()
