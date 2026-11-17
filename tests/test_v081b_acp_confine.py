"""A1: Codex and Antigravity children are locked down (flags, a bwrap boundary, refusals)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openfarm_core import confine as confine_lib
from openfarm_core.acp_setup import inspect_acp_status
from openfarm_core.adapters.acp import (
    AGY_NATIVE_ACP,
    CODEX_NATIVE_ACP,
    ClaudeAcpAdapter,
)
from openfarm_core.adapters.claude_worker import open_acp_worker
from openfarm_core.config import Config, ConfigError
from openfarm_core.errors import Refused
from openfarm_core.micromanager import open_micromanager

STAND_IN = ROOT / "tests" / "acp_stand_in.py"
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

# A fake codex: records its argv and tries to read the files named in the prompt.
_FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
reads = {}
for line in prompt.splitlines():
    if line.startswith("READ "):
        p = line[5:].strip()
        try:
            with open(p) as fh:
                reads[p] = fh.read()
        except Exception as exc:
            reads[p] = "ERR " + type(exc).__name__
body = {"seen_args": args, "reads": reads, "home": os.environ.get("HOME", ""),
        "home_exists": os.path.isdir(os.path.expanduser("~"))}
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(body)}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 1}}))
'''


def _base_config(state_dir: Path) -> dict:
    return {
        "project": {"name": "demo", "state_dir": str(state_dir)},
        "adapters": {"languages": ["python"], "model": "openai-compatible", "micromanager": "off"},
        "model": {
            "endpoint": "https://api.example.invalid/v1",
            "allowed_endpoints": ["https://api.example.invalid/v1"],
            "implementer": {"model": "lesser-coder"},
        },
        "policy": {"categories": ["pattern"]},
    }


class CodexFlagsAndDecisionTests(unittest.TestCase):
    def test_codex_none_flags_are_the_live_strict_set(self) -> None:
        self.assertEqual(confine_lib.codex_tool_flags("none"), LIVE_STRICT)
        ro = confine_lib.codex_tool_flags("read-only")
        self.assertNotIn("shell_tool", ro)
        # Codex's shell needs the code-mode host (seen live), so read-only keeps it.
        self.assertNotIn("code_mode_host", ro)
        self.assertIn("apps", ro)
        self.assertEqual(ro[-2:], ["-c", 'web_search="disabled"'])

    def test_decide_matrix(self) -> None:
        d = confine_lib.decide
        # auto: Claude is left as it is.
        h = d("claude", transport="bridge", tool_policy="read-only", mode="auto", bwrap_usable=False)
        self.assertEqual(h.describe(), "nothing")
        self.assertFalse(h.refused)
        # auto: Codex none through the bridge -> flags, plus boundary when bwrap works.
        self.assertEqual(d("codex", transport="bridge", tool_policy="none", mode="auto",
                           bwrap_usable=True).describe(), "flags + boundary")
        self.assertEqual(d("codex", transport="bridge", tool_policy="none", mode="auto",
                           bwrap_usable=False).describe(), "flags")
        # auto: everything else needs the boundary, refused without it.
        for prov, transport, policy in (("codex", "bridge", "read-only"), ("codex", "server", "none"),
                                        ("antigravity", "bridge", "none"),
                                        ("antigravity", "server", "read-only")):
            refused = d(prov, transport=transport, tool_policy=policy, mode="auto",
                        bwrap_usable=False, bwrap_reason="bwrap is not installed")
            self.assertTrue(refused.refused, (prov, transport, policy))
            self.assertIn("bwrap is not installed", refused.refused)
            self.assertIn('[micromanager] confine = "off"', refused.refused)
            held = d(prov, transport=transport, tool_policy=policy, mode="auto", bwrap_usable=True)
            self.assertTrue(held.boundary)
        # bwrap: every provider, Claude included; refused when unavailable.
        self.assertTrue(d("claude", transport="server", tool_policy="none", mode="bwrap",
                          bwrap_usable=True).boundary)
        self.assertIn('"bwrap"', d("claude", transport="server", tool_policy="none",
                                   mode="bwrap", bwrap_usable=False).refused)
        # off: nothing is confined and the note says so in plain words.
        off = d("antigravity", transport="server", tool_policy="none", mode="off", bwrap_usable=True)
        self.assertFalse(off.boundary)
        self.assertIn("can read whatever your user can", off.note)

    def test_config_setting_is_validated(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="of-v081b-cfg-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "repo").mkdir()
        data = _base_config(tmp / "state")
        tmp = tmp / "repo"
        cfg = Config(data, tmp)
        self.assertEqual(cfg.micromanager.confine, "auto")
        self.assertEqual(cfg.micromanager.confine_read_roots, [])
        data["micromanager"] = {"confine": "bwrap", "confine_read_roots": ["/opt/tool"]}
        cfg = Config(data, tmp)
        self.assertEqual(cfg.micromanager.confine, "bwrap")
        self.assertEqual(cfg.micromanager.confine_read_roots, ["/opt/tool"])
        for bad in ({"confine": "sometimes"}, {"confine_read_roots": ["relative/dir"]}):
            data["micromanager"] = bad
            with self.assertRaises(ConfigError):
                Config(data, tmp)


class BoundaryArgvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v081b-bx-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.session = self.tmp / "session"
        (self.session / "seams").mkdir(parents=True)
        (self.session / "seams" / "core.rs").write_text("SEAM\n")
        (self.session / "src").mkdir()
        (self.session / "src" / "lib.rs").write_text("pub fn ok() {}\n")
        (self.session / "bench.key").write_text("KEY\n")
        (self.session / ".openfarm").mkdir()
        (self.session / ".openfarm" / "config.toml").write_text("x = 1\n")
        self.home = self.tmp / "home"
        self.home.mkdir()

    def test_argv_is_an_allow_list(self) -> None:
        argv = confine_lib.boundary_argv(
            ["/usr/bin/true", "x"], session_dir=self.session, home_dir=self.home,
            hide_globs=["seams/**", "*.key", ".openfarm/**"], bwrap="/usr/bin/bwrap",
        )
        self.assertEqual(argv[0], "/usr/bin/bwrap")
        for flag in ("--die-with-parent", "--unshare-all", "--share-net", "--new-session"):
            self.assertIn(flag, argv)
        self.assertNotIn("--unshare-net", argv)
        joined = " ".join(argv)
        sess = str(self.session.resolve())
        self.assertIn(f"--ro-bind {sess} {sess}", joined)
        self.assertIn(f"--tmpfs {sess}/seams --remount-ro {sess}/seams", joined)
        self.assertIn(f"--tmpfs {sess}/.openfarm", joined)
        self.assertIn(f"--ro-bind /dev/null {sess}/bench.key", joined)
        self.assertIn(f"--bind {self.home.resolve()} {self.home.resolve()}", joined)
        self.assertIn(f"--chdir {sess}", joined)
        self.assertEqual(argv[-3:], ["--", "/usr/bin/true", "x"])
        # Nothing mounts / or the real home.
        pairs = list(zip(argv, argv[1:], argv[2:]))
        for a, src, _dst in pairs:
            if a in ("--bind", "--ro-bind"):
                self.assertNotEqual(src, "/")
                self.assertNotEqual(Path(src).resolve(), Path.home().resolve())

    def test_session_holding_the_home_is_refused(self) -> None:
        with self.assertRaises(confine_lib.ConfineError) as caught:
            confine_lib.boundary_argv(["/usr/bin/true"], session_dir=Path.home(),
                                      home_dir=self.home, bwrap="/usr/bin/bwrap")
        self.assertEqual(caught.exception.code, "confine-session-is-home")

    def test_program_install_tree_for_node_packages(self) -> None:
        pkg = self.tmp / "lib" / "node_modules" / "@vendor" / "tool"
        (pkg / "bin").mkdir(parents=True)
        script = pkg / "bin" / "tool.js"
        script.write_text("#!/bin/sh\necho hi\n")
        script.chmod(0o755)
        link_dir = self.tmp / "bin"
        link_dir.mkdir()
        (link_dir / "tool").symlink_to(script)
        roots, links = confine_lib.program_mounts(str(link_dir / "tool"))
        # The whole node_modules folder: npm installs a package's dependencies beside it.
        self.assertIn(pkg.parent.parent, roots)
        self.assertEqual(links, [(str(script), str(link_dir / "tool"))])


@unittest.skipUnless(BWRAP_OK, f"bwrap is not usable here: {BWRAP_WHY}")
class RealBwrapBoundaryTests(unittest.TestCase):
    """Runs a small Python program as the fake CLI inside the real boundary."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v081b-rb-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.session = self.tmp / "worktree"
        (self.session / "seams").mkdir(parents=True)
        (self.session / "seams" / "core.rs").write_text("NEVER_TOUCH_CONTENT\n")
        (self.session / "notes.secret").write_text("FORBIDDEN_CONTENT\n")
        (self.session / "src").mkdir()
        (self.session / "src" / "lib.rs").write_text("ORDINARY_CONTENT\n")
        self.outside = self.tmp / "other-card"
        self.outside.mkdir()
        (self.outside / "dossier.json").write_text("OTHER_CARD_CONTENT\n")
        self.home = self.tmp / "iso-home"
        self.home.mkdir()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.fake = bin_dir / "probe"
        self.fake.write_text(r'''#!/usr/bin/env python3
import json, os, shutil, sys
out = {}
for name, p in json.loads(sys.argv[1]).items():
    try:
        with open(p) as fh:
            out[name] = fh.read()
    except Exception as exc:
        out[name] = "ERR " + type(exc).__name__
try:
    with open("written-in-session.txt", "w") as fh:
        fh.write("x")
    out["session_write"] = "ok"
except OSError as exc:
    out["session_write"] = "ERR " + type(exc).__name__
with open(os.path.join(os.environ["HOME"], "written-in-home.txt"), "w") as fh:
    fh.write("x")
out["home_write"] = "ok"
try:
    with open(sys.argv[4], "w") as fh:
        fh.write("x")
    out["root_write"] = "ok"
except OSError as exc:
    out["root_write"] = "ERR " + type(exc).__name__
try:
    with open("/tmp/scratch.txt", "w") as fh:
        fh.write("x")
    out["tmp_write"] = "ok"
except OSError as exc:
    out["tmp_write"] = "ERR " + type(exc).__name__
out["python"] = shutil.which("python3") or ""
out["sys_executable_exists"] = os.path.exists(sys.executable)
out["real_home_visible"] = os.path.exists(sys.argv[2])
out["state_visible"] = os.path.exists(sys.argv[3])
print(json.dumps(out))
''')
        self.fake.chmod(0o755)

    def test_boundary_hides_everything_but_the_session(self) -> None:
        targets = {
            "outside": str(self.outside / "dossier.json"),
            "never_touch": str(self.session / "seams" / "core.rs"),
            "forbidden": str(self.session / "notes.secret"),
            "ordinary": str(self.session / "src" / "lib.rs"),
            "shadow": "/etc/shadow",
        }
        argv = confine_lib.boundary_argv(
            [str(self.fake), json.dumps(targets), str(Path.home()), str(self.outside),
             str(self.tmp / "beside-the-mounts.txt")],
            session_dir=self.session, home_dir=self.home,
            hide_globs=["seams/**", "*.secret"],
        )
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertTrue(out["outside"].startswith("ERR"), out)
        self.assertNotEqual(out["never_touch"], "NEVER_TOUCH_CONTENT\n")
        self.assertIn(out["never_touch"], ("", "ERR FileNotFoundError", "ERR PermissionError"))
        self.assertNotEqual(out["forbidden"], "FORBIDDEN_CONTENT\n")
        self.assertEqual(out["ordinary"], "ORDINARY_CONTENT\n")
        self.assertTrue(out["shadow"].startswith("ERR"))
        self.assertTrue(out["session_write"].startswith("ERR"))
        self.assertEqual(out["home_write"], "ok")
        self.assertTrue(out["root_write"].startswith("ERR"), out)
        self.assertEqual(out["tmp_write"], "ok")
        self.assertTrue((self.home / "written-in-home.txt").is_file())
        self.assertFalse((self.session / "written-in-session.txt").exists())
        self.assertTrue(out["python"])
        self.assertTrue(out["sys_executable_exists"])
        self.assertFalse(out["real_home_visible"])
        self.assertFalse(out["state_visible"])

    def test_codex_bridge_child_cannot_read_outside_its_folder(self) -> None:
        bin_dir = self.tmp / "codex-bin"
        bin_dir.mkdir()
        codex = bin_dir / "codex"
        codex.write_text(_FAKE_CODEX)
        codex.chmod(0o755)
        prompt = "\n".join([
            f"READ {self.outside / 'dossier.json'}",
            f"READ {self.session / 'seams' / 'core.rs'}",
            f"READ {self.session / 'src' / 'lib.rs'}",
        ])
        with ClaudeAcpAdapter(
            [sys.executable, str(CODEX_NATIVE_ACP), "--codex-bin", str(codex)],
            cwd=self.session, provider="codex", auth_mode="api-key",
            never_touch=["seams/**"], confine="auto",
        ) as acp:
            sid = acp.new_session(cwd=self.session, tool_policy="none")
            body = json.loads(acp.prompt(sid, prompt, timeout_s=60).text)
        self.assertEqual(body["seen_args"][8:8 + len(LIVE_STRICT)], LIVE_STRICT)
        self.assertTrue(body["reads"][str(self.outside / "dossier.json")].startswith("ERR"))
        self.assertNotEqual(body["reads"][str(self.session / "seams" / "core.rs")],
                            "NEVER_TOUCH_CONTENT\n")
        self.assertEqual(body["reads"][str(self.session / "src" / "lib.rs")], "ORDINARY_CONTENT\n")

    def test_agy_server_runs_inside_the_boundary(self) -> None:
        # The stand-in plays a standalone Antigravity ACP server; it reports its HOME.
        with ClaudeAcpAdapter(
            [sys.executable, str(STAND_IN), "--mode", "normal"],
            cwd=self.session, provider="antigravity", confine="auto",
        ) as acp:
            acp.initialize()
            self.assertEqual(acp.process_hold.describe(), "boundary")
            self.assertEqual(acp.agent_capabilities["home"], str(acp._isolated_home))
            sid = acp.new_session(cwd=self.session / "src")
            self.assertTrue(sid)
            with self.assertRaises(Refused) as caught:
                acp.new_session(cwd=self.outside)
            self.assertEqual(caught.exception.code, "acp-confine-session-outside")


class RefusalWithoutBwrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v081b-nobw-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch.object(
            confine_lib, "bwrap_status",
            return_value=(False, "bwrap is not installed (not found on PATH)"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_agy_server_is_refused_with_a_message_naming_the_setting(self) -> None:
        acp = ClaudeAcpAdapter([sys.executable, str(STAND_IN)], cwd=self.tmp,
                               provider="antigravity", confine="auto")
        self.addCleanup(acp.close)
        with self.assertRaises(Refused) as caught:
            acp.initialize()
        self.assertEqual(caught.exception.code, "acp-confine-unavailable")
        self.assertIn("bwrap is not installed", caught.exception.message)
        self.assertIn('[micromanager] confine = "off"', caught.exception.message)
        self.assertIsNone(acp._proc)

    def test_codex_read_only_is_refused_but_none_runs_on_flags(self) -> None:
        codex = self.tmp / "codex"
        codex.write_text(_FAKE_CODEX)
        codex.chmod(0o755)
        with ClaudeAcpAdapter([sys.executable, str(CODEX_NATIVE_ACP), "--codex-bin", str(codex)],
                              cwd=self.tmp, provider="codex", auth_mode="api-key",
                              confine="auto") as acp:
            with self.assertRaises(Refused) as caught:
                acp.new_session(cwd=self.tmp, tool_policy="read-only")
            self.assertEqual(caught.exception.code, "acp-confine-unavailable")
            sid = acp.new_session(cwd=self.tmp, tool_policy="none")
            body = json.loads(acp.prompt(sid, "hi", timeout_s=30).text)
            self.assertEqual(body["seen_args"][8:8 + len(LIVE_STRICT)], LIVE_STRICT)

    def test_agy_bridge_is_refused(self) -> None:
        with ClaudeAcpAdapter([sys.executable, str(AGY_NATIVE_ACP), "--agy-bin", "/nonexistent/agy"],
                              cwd=self.tmp, provider="antigravity", confine="auto") as acp, \
                self.assertRaises(Refused):
            acp.new_session(cwd=self.tmp, tool_policy="none")

    def test_bwrap_mode_refuses_claude_too(self) -> None:
        acp = ClaudeAcpAdapter([sys.executable, str(STAND_IN)], cwd=self.tmp,
                               provider="claude", confine="bwrap")
        self.addCleanup(acp.close)
        with self.assertRaises(Refused):
            acp.initialize()

    def test_auto_leaves_claude_alone(self) -> None:
        with ClaudeAcpAdapter([sys.executable, str(STAND_IN)], cwd=self.tmp,
                              provider="claude", confine="auto") as acp:
            acp.initialize()
            self.assertEqual(acp.process_hold.describe(), "nothing")
            self.assertTrue(acp.new_session(cwd=self.tmp, tool_policy="read-only"))

    def test_off_runs_unconfined_and_says_so(self) -> None:
        with ClaudeAcpAdapter([sys.executable, str(STAND_IN)], cwd=self.tmp,
                              provider="antigravity", confine="off") as acp:
            acp.initialize()
            self.assertEqual(acp.process_hold.describe(), "nothing")
            self.assertTrue(any("can read whatever your user can" in n for n in acp.notes))


class WiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v081b-wire-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.state = self.tmp / "state"
        self.state.mkdir()
        (self.state / "dossier.json").write_text("{}")

    def test_acp_workers_get_an_empty_folder_not_the_state_folder(self) -> None:
        data = _base_config(self.state)
        data["adapters"]["model"] = "codex-acp"
        cfg = Config(data, self.repo)
        backend = open_acp_worker(cfg)
        self.addCleanup(backend.close)
        self.assertNotEqual(backend.cwd.resolve(), self.state.resolve())
        self.assertFalse(self.state.resolve() in backend.cwd.resolve().parents)
        self.assertEqual(list(backend.cwd.iterdir()), [])
        own = backend.cwd
        backend.close()
        self.assertFalse(own.exists())

    def test_confine_setting_reaches_the_micromanager_adapter(self) -> None:
        data = _base_config(self.state)
        data["adapters"]["micromanager"] = "codex-acp"
        data["micromanager"] = {"mode": "active", "binary": "codex", "confine": "off",
                                "confine_read_roots": ["/opt/odd-install"]}
        cfg = Config(data, self.repo)
        mm = open_micromanager(cfg, self.state, self.repo, worktree=self.repo)
        self.addCleanup(mm.close)
        backend = mm.backend
        adapter = ClaudeAcpAdapter(command=backend.command, cwd=backend.cwd,
                                   provider=backend.provider)
        self.assertEqual(adapter.confine.mode, "off")
        self.assertEqual(adapter.confine.read_roots, ("/opt/odd-install",))

    def test_acp_status_shows_how_each_provider_is_held(self) -> None:
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        codex = bin_dir / "codex"
        codex.write_text("#!/bin/sh\necho 'Not logged in'\nexit 1\n")
        codex.chmod(0o755)
        with mock.patch.object(confine_lib, "bwrap_status", return_value=(True, "bwrap works")), \
                mock.patch.object(confine_lib, "nested_status", return_value=(True, "ok")):
            status = inspect_acp_status(
                None, provider="all", home=self.tmp / "home", claude_bin="", acp_bin="",
                agy_bin="", antigravity_acp_bin="", codex_bin=str(codex), codex_acp_bin="",
                env={})
        held = {name: info["confinement"] for name, info in status["providers"].items()}
        self.assertEqual(held["claude"]["none"], "nothing")
        self.assertEqual(held["codex"]["none"], "flags + boundary")
        self.assertEqual(held["codex"]["read-only"], "flags + boundary")
        self.assertEqual(held["antigravity"]["none"], "boundary")
        self.assertEqual(status["confinement"]["mode"], "auto")
        with mock.patch.object(confine_lib, "bwrap_status", return_value=(False, "missing")):
            status = inspect_acp_status(
                None, provider="codex", home=self.tmp / "home", codex_bin=str(codex),
                codex_acp_bin="", env={})
        self.assertEqual(status["providers"]["codex"]["confinement"]["none"], "flags")
        self.assertEqual(status["providers"]["codex"]["confinement"]["read-only"], "refused")


if __name__ == "__main__":
    unittest.main()
