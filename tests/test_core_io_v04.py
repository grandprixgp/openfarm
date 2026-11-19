"""Unit tests for Wave 1 OpenFarm v0.4 core-io changes."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock

from openfarm_core.adapters.direct import (
    FAIL,
    INFRA,
    PASS,
    CmdRunner,
    DirectRunner,
)
from openfarm_core.adapters.files import (
    FileCardSource,
    open_card_source,
    open_file_source,
    parse_card_file,
    render_card_file,
)
from openfarm_core.adapters.jsonl import (
    CARD_FIELDS as TELEMETRY_CARD_FIELDS,
    JsonlTelemetry,
    TelemetrySink,
    complete_record,
    open_telemetry,
)
from openfarm_core.adapters.openai_compat import (
    Completion,
    OpenAICompatibleBackend,
    _within,
    open_backend,
)
from openfarm_core.cards import Card, extract_fenced_fields
from openfarm_core.config import Config, ConfigError
from openfarm_core.errors import Infra, Invalid, Refused, StopCode
from openfarm_core.globs import compile_glob, matches, resolves_within
from openfarm_core.protocol import (
    OPTIONAL_OPS,
    REQUIRED_OPS,
    CmdCardSource,
    CmdModelBackend,
    CmdPack,
    CmdTelemetry,
)
from openfarm_core.state import StateDir


def _base_config_data(state_dir: Path) -> dict:
    return {
        "project": {"name": "demo", "state_dir": str(state_dir)},
        "adapters": {"languages": ["rust"], "model": "scripted"},
        "model": {
            "endpoint": "https://api.example.invalid/v1",
            "allowed_endpoints": ["https://api.example.invalid/v1"],
            "implementer": {"model": "impl-m"},
        },
        "policy": {"categories": ["pattern"]},
    }


class StopCodeTests(unittest.TestCase):
    def test_supervisor_reject_stop_code(self) -> None:
        self.assertEqual(StopCode.SUPERVISOR_REJECT.value, "supervisor-reject")
        self.assertEqual(StopCode.CHECKER_REJECT.value, "checker-reject")


class GlobAndPathTests(unittest.TestCase):
    def test_brackets_in_glob_match_literally(self) -> None:
        self.assertTrue(matches("routes/[id].tsx", "routes/[id].tsx"))
        self.assertTrue(matches("routes/[id].tsx", "routes/**"))
        self.assertFalse(matches("routes/i.tsx", "routes/[id].tsx"))
        self.assertFalse(matches("routes/d.tsx", "routes/[id].tsx"))
        regex = compile_glob("a/[0]/b.rs").pattern
        self.assertIn(r"\[", regex)
        self.assertIn(r"\]", regex)

    def test_resolves_within(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        root = tmp / "repo"
        outside = tmp / "outside"
        (root / "sub").mkdir(parents=True)
        outside.mkdir()
        (root / "sub" / "ok.rs").write_text("ok")
        (root / "sub" / "link_out").symlink_to(outside)
        (root / "sub" / "link_in").symlink_to(root / "sub")

        self.assertEqual(
            resolves_within(root, "sub/ok.rs"),
            (root / "sub" / "ok.rs").resolve(),
        )
        self.assertEqual(
            resolves_within(root, "sub/link_in/ok.rs"),
            (root / "sub" / "ok.rs").resolve(),
        )
        self.assertIsNone(resolves_within(root, "../outside/x"))
        self.assertIsNone(resolves_within(root, "sub/../../outside/x"))
        self.assertIsNone(resolves_within(root, "/etc/passwd"))
        self.assertIsNone(resolves_within(root, str(root / "sub" / "ok.rs")))
        self.assertIsNone(resolves_within(root, "sub/ok.rs\x00bad"))
        self.assertIsNone(resolves_within(root, "sub/link_out/secret.txt"))
        self.assertIsNone(resolves_within(root, "sub/link_out/../outside/secret.txt"))


class CardTestsV04(unittest.TestCase):
    def test_optional_properties_and_frontier_estimate(self) -> None:
        card = Card(
            "C-1",
            fields={
                "context_files": ["src/ctx.rs", "src/types.rs"],
                "type_changes": True,
                "frontier_estimate": "2.5",
            },
        )
        self.assertEqual(card.context_files, ["src/ctx.rs", "src/types.rs"])
        self.assertTrue(card.type_changes)
        self.assertEqual(card.frontier_estimate, 2.5)
        self.assertTrue(card.frontier_estimate_valid)

        empty = Card("C-2")
        self.assertEqual(empty.context_files, [])
        self.assertFalse(empty.type_changes)
        self.assertIsNone(empty.frontier_estimate)
        self.assertTrue(empty.frontier_estimate_valid)

        for bad in ("not-a-float", "nan", "inf", True, ["bad"]):
            bad_card = Card("C-bad", fields={"frontier_estimate": bad})
            self.assertIsNone(bad_card.frontier_estimate, repr(bad))
            self.assertFalse(bad_card.frontier_estimate_valid, repr(bad))

    def test_extract_fenced_fields(self) -> None:
        body = (
            "## Done\n\n"
            "```openfarm\n"
            'category = "small-feature"\n'
            'context_files = ["src/a.rs"]\n'
            "type_changes = true\n"
            "```\n\n"
            "Returns `Ok(())`.\n"
        )
        parsed, cleaned = extract_fenced_fields(body, "toml")
        self.assertEqual(parsed["category"], "small-feature")
        self.assertEqual(parsed["context_files"], ["src/a.rs"])
        self.assertTrue(parsed["type_changes"])
        self.assertEqual(cleaned, "## Done\n\nReturns `Ok(())`.")

        toml_body = "```toml\ncategory = \"pattern\"\n```\nBody line.\n"
        parsed2, cleaned2 = extract_fenced_fields(toml_body, "toml")
        self.assertEqual(parsed2["category"], "pattern")
        self.assertEqual(cleaned2, "Body line.")

        self.assertEqual(extract_fenced_fields("plain body", "toml"), ({}, "plain body"))
        self.assertEqual(extract_fenced_fields(body, ""), ({}, body))

        with self.assertRaises(Invalid) as caught:
            extract_fenced_fields("```toml\ncategory = [\n```\n", "toml")
        self.assertEqual(caught.exception.code, "bad-fields-block")


class ConfigTestsV04(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.state = self.tmp / "state"

    def test_unknown_keys_rejected_on_all_tables(self) -> None:
        cases = [
            ("project", {"bogus": 1}),
            ("adapters", {"bogus": "x"}),
            ("model", {"bogus": "x"}),
            ("policy", {"bogus": []}),
            ("eligibility", {"bogus": 1}),
            ("brief", {"bogus": "x"}),
            ("runner", {"bogus": []}),
            ("budget", {"bogus": 1}),
            ("gates", {"bogus": "off"}),
            ("cards", {"bogus": "x"}),
            ("micromanager", {"bogus": 1}),
            ("state", {"bogus": 1}),
        ]
        for table, extra in cases:
            data = _base_config_data(self.state)
            data.setdefault(table, {}).update(extra)
            with self.assertRaises(ConfigError, msg=table):
                Config(data, self.repo)

        # Role sub-table unknown key
        data = _base_config_data(self.state)
        data["model"]["checker"] = {"model": "m", "bogus": 1}
        with self.assertRaises(ConfigError):
            Config(data, self.repo)

        # Extra gate unknown key
        data = _base_config_data(self.state)
        data["gates"] = {"extra": [{"name": "g", "run": "true", "bogus": 1}]}
        with self.assertRaises(ConfigError):
            Config(data, self.repo)

    def test_role_model_inheritance_and_checker_defaults(self) -> None:
        data = _base_config_data(self.state)
        cfg = Config(data, self.repo)
        self.assertEqual(cfg.role_models["implementer"].model, "impl-m")
        self.assertEqual(cfg.role_models["test_writer"].model, "impl-m")
        self.assertEqual(cfg.role_models["planner"].model, "impl-m")
        self.assertEqual(cfg.role_models["checker"].model, "impl-m")
        self.assertEqual(cfg.role_models["checker"].temperature, 0.35)
        self.assertTrue(cfg.role_models["checker"].vary_seed)

        # Explicit checker settings survive
        data["model"]["checker"] = {"model": "chk-m", "temperature": 0.4, "vary_seed": False}
        cfg2 = Config(data, self.repo)
        self.assertEqual(cfg2.role_models["checker"].model, "chk-m")
        self.assertEqual(cfg2.role_models["checker"].temperature, 0.4)
        self.assertFalse(cfg2.role_models["checker"].vary_seed)

    def test_v04_tables_adapters_and_validation(self) -> None:
        data = _base_config_data(self.state)
        data["adapters"].update({
            "micromanager": "claude-acp",
            "telemetry": "cmd:python3 tel.py",
            "pack": "cmd:python3 pack.py",
        })
        data["eligibility"] = {"allow_type_changes": False, "max_context_files": 4}
        data["gates"] = {"planner": "on"}
        data["cards"] = {"fields_block": "toml"}
        data["state"] = {"min_free_mb": 512}
        data["brief"] = {"test_conventions": ".openfarm/conventions.md"}
        auth = self.tmp / "auth.json"
        data["micromanager"] = {
            "mode": "advisory",
            "effort": "high",
            "tool_policy": "read-only",
            "timeout": "2m",
            "max_calls_per_card": 6,
            "tokens_per_card": 50000,
            "spend_per_night": 5.0,
            "cost_per_1k_in": 0.01,
            "cost_per_1k_out": 0.02,
            "auth_file": str(auth),
            "isolate_home": False,
            "pre_review": False,
        }
        cfg = Config(data, self.repo)
        self.assertTrue(cfg.micromanager_enabled)
        self.assertEqual(cfg.micromanager.mode, "advisory")
        self.assertEqual(cfg.micromanager.timeout_s, 120.0)
        self.assertEqual(cfg.micromanager.auth_file, auth.resolve())
        self.assertFalse(cfg.micromanager.pre_review)
        self.assertEqual(cfg.micromanager.as_dict()["effort"], "high")
        self.assertFalse(cfg.allow_type_changes)
        self.assertEqual(cfg.max_context_files, 4)
        self.assertEqual(cfg.planner, "on")
        self.assertEqual(cfg.fields_block, "toml")
        self.assertEqual(cfg.min_free_mb, 512)
        self.assertEqual(cfg.test_conventions_path, Path(".openfarm/conventions.md"))

        # Adapter micromanager = "off" disables micromanager_enabled even when mode = "active"
        data2 = _base_config_data(self.state)
        cfg_off = Config(data2, self.repo)
        self.assertFalse(cfg_off.micromanager_enabled)

        # Both timeout and timeout_s rejected
        bad_mm = _base_config_data(self.state)
        bad_mm["micromanager"] = {"timeout_s": 10, "timeout": "10s"}
        with self.assertRaises(ConfigError):
            Config(bad_mm, self.repo)

        # auth_file inside repo rejected
        bad_auth = _base_config_data(self.state)
        bad_auth["micromanager"] = {"auth_file": "secret.json"}
        with self.assertRaises(ConfigError):
            Config(bad_auth, self.repo)

        # brief.test_conventions escaping repo rejected
        bad_brief = _base_config_data(self.state)
        bad_brief["brief"] = {"test_conventions": "../outside.md"}
        with self.assertRaises(ConfigError):
            Config(bad_brief, self.repo)

        # bad adapter specs rejected
        for key, bad_val in (("micromanager", "unknown"), ("telemetry", "sqlite"), ("pack", "python")):
            bad_ad = _base_config_data(self.state)
            bad_ad["adapters"][key] = bad_val
            with self.assertRaises(ConfigError):
                Config(bad_ad, self.repo)


class StateTestsV04(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.state = StateDir(self.tmp / "state")

    def test_safe_id_enforcement(self) -> None:
        for bad in ("", ".", "..", "a/b", "a\\b", "a..b", ".hidden", "a\x00b"):
            with self.assertRaises(Invalid, msg=repr(bad)):
                self.state.run_dir(bad)
            with self.assertRaises(Invalid, msg=repr(bad)):
                self.state.card_dir(bad, "run-1")
            with self.assertRaises(Invalid, msg=repr(bad)):
                self.state.check_dir(bad)

        # cmd_status passes run_id="" to card_dir
        self.assertEqual(
            self.state.card_dir("CARD-1", ""),
            self.state.cards / "CARD-1",
        )
        # create_card requires a non-empty run_id
        with self.assertRaises(Invalid):
            self.state.create_card("CARD-1", "")

    def test_create_card_has_v04_subdirs_and_disk_floor(self) -> None:
        cdir = self.state.create_card("CARD-1", "run-1")
        for sub in ("frozen", "briefs", "responses", "edits", "gates",
                    "partial", "micromanager", "attempts"):
            self.assertTrue((cdir / sub).is_dir(), sub)

        with mock.patch.object(self.state, "free_bytes", return_value=10 * 1024 * 1024):
            self.assertEqual(self.state.check_disk_space(0), 10 * 1024 * 1024)
            self.assertEqual(self.state.check_disk_space(5), 10 * 1024 * 1024)
            with self.assertRaises(Infra) as caught:
                self.state.check_disk_space(50)
            self.assertEqual(caught.exception.code, "disk-full")


class ProtocolCmdWrappersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.script = self.tmp / "cmd_adapter.py"
        self.script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "req = json.load(sys.stdin)\n"
            "adapter = req['adapter']\n"
            "op = req['op']\n"
            "args = req['args']\n"
            "rid = req['request_id']\n"
            "def ok(res):\n"
            "    print(json.dumps({'openfarm': 1, 'request_id': rid, 'ok': True, 'result': res}))\n"
            "    sys.exit(0)\n"
            "def err(kind, code, msg):\n"
            "    print(json.dumps({'openfarm': 1, 'request_id': rid, 'ok': False,\n"
            "                      'error': {'kind': kind, 'code': code, 'message': msg, 'retry_after_s': None}}))\n"
            "    sys.exit(0)\n"
            "if adapter == 'cards':\n"
            "    if op == 'hello': ok({'name': 'ext-cards', 'versions': [1], 'ops': ['hello','next','read','claim','move','release','finding']})\n"
            "    elif op == 'hash': err('unsupported', 'no-hash', 'fallback')\n"
            "    elif op == 'heartbeat': err('unsupported', 'no-hb', 'fallback')\n"
            "    elif op == 'stale': err('unsupported', 'no-stale', 'fallback')\n"
            "    elif op == 'all': err('unsupported', 'no-all', 'fallback')\n"
            "    elif op == 'read': ok({'card': {'id': args['id'], 'title': 'T', 'state': 'ready', 'done_text': 'd', 'touches': ['a.rs'], 'deps': [], 'fields': {}}})\n"
            "    elif op == 'next': ok({'cards': ['CARD-1']})\n"
            "    elif op in ('claim','move','release','finding'): ok({'ok': True, 'op': op})\n"
            "elif adapter == 'model':\n"
            "    if op == 'hello': ok({'name': 'ext-model', 'versions': [1], 'ops': ['hello','complete']})\n"
            "    elif op == 'complete': ok({'content': 'done', 'tokens_in': 100, 'tokens_out': 50, 'model': 'm1'})\n"
            "elif adapter == 'telemetry':\n"
            "    if op == 'hello': ok({'name': 'ext-tel', 'versions': [1], 'ops': ['hello','write','flush']})\n"
            "    elif op == 'write': ok({'written': True})\n"
            "    elif op == 'flush': ok({'flushed': True})\n"
            "elif adapter == 'pack':\n"
            "    if op == 'hello': ok({'name': 'ext-pack', 'versions': [1], 'ops': ['hello','commands','parse_list','parse_tests','classify_paths','signatures','edit_formats','apply_edit','format','format_check','patterns','error_signature']})\n"
            "    else: ok({'op': op, 'args': args})\n"
        )
        self.script.chmod(0o755)
        self.spec = f"cmd:{sys.executable} {self.script}"

    def test_required_and_optional_ops_include_micromanager(self) -> None:
        self.assertEqual(REQUIRED_OPS["micromanager"], {"hello", "assist"})
        self.assertIn("micromanager", OPTIONAL_OPS)

    def test_cmd_card_source(self) -> None:
        src = CmdCardSource(self.spec, self.tmp)
        self.assertEqual(src.hello()["name"], "ext-cards")
        self.assertEqual(src.next(), {"cards": ["CARD-1"]})
        self.assertEqual(src.read("CARD-1")["card"]["id"], "CARD-1")
        self.assertEqual(len(src.hash("CARD-1")["hash"]), 64)
        self.assertEqual(src.claim("CARD-1", "h1")["op"], "claim")
        self.assertEqual(src.heartbeat("CARD-1", "L-1"), {})
        self.assertEqual(src.move("CARD-1", "L-1", "in-review")["op"], "move")
        self.assertEqual(src.release("CARD-1")["op"], "release")
        self.assertEqual(src.finding("CARD-1", "t", "b")["op"], "finding")
        self.assertEqual(src.stale(), {"cards": []})
        self.assertEqual(src.all_cards(), [])

    def test_cmd_model_telemetry_and_pack(self) -> None:
        mb = CmdModelBackend(self.spec, self.tmp, cost_per_1k_in=1.0, cost_per_1k_out=2.0)
        self.assertTrue(mb.prices_spend)
        self.assertEqual(mb.hello()["name"], "ext-model")
        comp = mb.complete("implementer", "m1", 0.2, [{"role": "user", "content": "hi"}])
        self.assertEqual(comp.content, "done")
        self.assertAlmostEqual(comp.cost, 0.2)

        tel = CmdTelemetry(self.spec, self.tmp)
        self.assertEqual(tel.hello()["name"], "ext-tel")
        self.assertEqual(tel.write("attempt", {"a": 1}), {"written": True})
        self.assertEqual(tel.flush(), {"flushed": True})

        pack = CmdPack(self.spec, self.tmp)
        self.assertEqual(pack.hello()["name"], "ext-pack")
        pack.configure(self.tmp)
        self.assertEqual(pack.commands("test")["op"], "commands")
        self.assertEqual(pack.parse_list("")["op"], "parse_list")
        self.assertEqual(pack.parse_tests("")["op"], "parse_tests")
        self.assertEqual(pack.parse_mutation("")["op"], "parse_mutation")
        self.assertEqual(pack.error_signature("")["op"], "error_signature")
        self.assertEqual(pack.classify_paths(["a.rs"])["op"], "classify_paths")
        self.assertEqual(pack.signatures(Path("a.rs"))["op"], "signatures")
        self.assertEqual(pack.edit_formats()["op"], "edit_formats")
        self.assertEqual(pack.apply_edit("whole_file", Path("a.rs"), {"content": "x"})["op"], "apply_edit")
        self.assertEqual(pack.append_stub(Path("a.rs"), "t", "fn t(){}")["op"], "append_stub")
        self.assertEqual(pack.format([Path("a.rs")])["op"], "format")
        self.assertEqual(pack.format_check([Path("a.rs")])["op"], "format_check")
        self.assertEqual(pack.patterns([])["op"], "patterns")
        self.assertEqual(pack.mutation(["a.rs"])["op"], "mutation")


class FilesAdapterTestsV04(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t",
             "commit", "-q", "--allow-empty", "-m", "init"],
            cwd=str(self.repo),
            check=True,
        )

    def test_toml_control_char_round_trip(self) -> None:
        for val in (
            "single line with \r\t\b\f\x00\x1f\x7f",
            "multi\nline\r\nwith\tcontrols\b\f\x00\x1f\x7f and \"\"\"quotes\"\"\" and \"\"",
        ):
            text = render_card_file({"id": "C-1", "note": val}, "body\n")
            parsed, _body = parse_card_file(text)
            self.assertEqual(parsed["note"], val)

    def test_context_files_type_changes_and_fields_block(self) -> None:
        wt = self.tmp / "cards-wt"
        wt.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "openfarm-cards", str(wt)], check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t",
             "commit", "-q", "--allow-empty", "-m", "init"],
            cwd=str(wt),
            check=True,
        )
        src = FileCardSource(self.repo, "cards", worktree=wt, fields_block="toml")
        (wt / "cards").mkdir()
        card = Card(
            "CARD-1",
            title="Test",
            state="ready",
            done_text="Plain body.",
            touches=["src/lib.rs"],
            fields={
                "category": "small-feature",
                "context_files": ["src/ctx.rs"],
                "type_changes": True,
            },
        )
        src.add_card(card, commit=True)
        loaded = Card.from_dict(src.read("CARD-1")["card"])
        self.assertEqual(loaded.context_files, ["src/ctx.rs"])
        self.assertTrue(loaded.type_changes)

        # Fenced fields block in body
        (wt / "cards" / "CARD-2.md").write_text(
            "+++\n"
            'id = "CARD-2"\n'
            'title = "Block card"\n'
            'state = "ready"\n'
            'touches = ["src/a.rs"]\n'
            "+++\n\n"
            "```openfarm\n"
            'category = "pattern"\n'
            'reference = "src/ref.rs"\n'
            'context_files = ["src/ctx2.rs"]\n'
            "type_changes = true\n"
            "```\n\n"
            "Returns `Ok(())`.\n"
        )
        card2 = Card.from_dict(src.read("CARD-2")["card"])
        self.assertEqual(card2.category, "pattern")
        self.assertEqual(card2.reference, "src/ref.rs")
        self.assertEqual(card2.context_files, ["src/ctx2.rs"])
        self.assertTrue(card2.type_changes)
        self.assertEqual(card2.done_text, "Returns `Ok(())`.")

    def test_open_card_source_dispatches(self) -> None:
        data = _base_config_data(self.tmp / "state")
        cfg = Config(data, self.repo)
        self.assertIsInstance(open_card_source(cfg, self.tmp / "state"), FileCardSource)
        self.assertIsInstance(open_file_source(cfg, self.tmp / "state"), FileCardSource)

        data["adapters"]["cards"] = f"cmd:{sys.executable} fake.py"
        cfg_cmd = Config(data, self.repo)
        self.assertIsInstance(open_card_source(cfg_cmd, self.tmp / "state"), CmdCardSource)


class DirectRunnerTestsV04(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def test_classify_signals_and_linker(self) -> None:
        runner = DirectRunner(self.tmp)
        self.assertEqual(runner.classify(1, "", "error: linker command failed", "build"), FAIL)
        self.assertEqual(runner.classify(-9, "", "", "test"), INFRA)
        self.assertEqual(runner.classify(-9, "", "", "build"), INFRA)
        self.assertEqual(runner.classify(-6, "", "", "test"), FAIL)
        self.assertEqual(runner.classify(-11, "", "", "test"), FAIL)
        for kind in ("build", "lint", "list"):
            self.assertEqual(runner.classify(-6, "", "", kind), INFRA)
            self.assertEqual(runner.classify(-11, "", "", kind), INFRA)

    def test_timeout_expired_bytes_decoded(self) -> None:
        runner = DirectRunner(self.tmp, timeout_s=0.1)
        exc = subprocess.TimeoutExpired(
            cmd=["echo"], timeout=0.1, output=b"partial out\xff", stderr=b"partial err"
        )

        class _FakeProc:
            pid = 999999
            returncode = -9

            def __init__(self) -> None:
                self._calls = 0

            def communicate(self, timeout=None):
                self._calls += 1
                if self._calls == 1:
                    raise exc
                return b"partial out\xff", b"partial err"

            def kill(self) -> None:
                return None

        with mock.patch("openfarm_core.adapters.direct.subprocess.Popen", return_value=_FakeProc()):
            res = runner.exec("test", ["echo", "x"])
        self.assertEqual(res.cls, INFRA)
        self.assertIn("partial out", res.stdout)
        self.assertEqual(res.stderr, "partial err")

    def test_cmd_runner_sets_timeout_s(self) -> None:
        runner = CmdRunner("cmd:echo ok", self.tmp, timeout_s=42.0)
        self.assertEqual(runner.timeout_s, 42.0)


class OpenAICompatTestsV04(unittest.TestCase):
    def test_within_hardened(self) -> None:
        allowed = ["https://API.Example.com/v1"]
        self.assertTrue(_within("https://api.example.com/v1/chat/completions", allowed))
        self.assertTrue(_within("https://api.example.com/v1", allowed))
        self.assertFalse(_within("https://api.example.com/v10/chat/completions", allowed))
        self.assertFalse(_within("https://api.example.com/v1/../admin", allowed))
        self.assertFalse(_within("https://api.example.com/v1/%2e%2e/admin", allowed))
        self.assertFalse(_within("https://api.example.com/v1?x=1", allowed))
        self.assertFalse(_within("https://api.example.com/v1#frag", allowed))
        self.assertFalse(_within("http://api.example.com/v1/chat/completions", allowed))
        self.assertFalse(_within("https://evil.com/v1", allowed))

    def test_retries_and_usage_parsing(self) -> None:
        sleeps: list[float] = []
        backend = OpenAICompatibleBackend(
            "https://api.example.invalid/v1",
            ["https://api.example.invalid/v1"],
            retries=2,
            sleep_fn=sleeps.append,
        )
        headers = Message()
        headers["Retry-After"] = "45"
        http_429 = urllib.error.HTTPError(
            "https://api.example.invalid/v1/chat/completions",
            429,
            "Too Many Requests",
            headers,
            io.BytesIO(b""),
        )
        ok_body = json.dumps({
            "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": None, "completion_tokens": "bad"},
        }).encode("utf-8")

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return ok_body

        with mock.patch.object(
            backend._opener,
            "open",
            side_effect=[http_429, TimeoutError("slow"), _FakeResp()],
        ):
            comp = backend.complete("implementer", "m", 0.2, [{"role": "user", "content": "hi"}])

        self.assertIsInstance(comp, Completion)
        self.assertEqual(comp.content, "hello")
        self.assertEqual(comp.tokens_in, 0)
        self.assertEqual(comp.tokens_out, 0)
        self.assertEqual(sleeps, [30.0, 1.0])

        # Non-retryable 400 fails immediately without sleeping
        sleeps.clear()
        http_400 = urllib.error.HTTPError(
            "https://api.example.invalid/v1/chat/completions",
            400,
            "Bad Request",
            Message(),
            io.BytesIO(b""),
        )
        with mock.patch.object(backend._opener, "open", side_effect=http_400):
            with self.assertRaises(Infra):
                backend.complete("implementer", "m", 0.2, [{"role": "user", "content": "hi"}])
        self.assertEqual(sleeps, [])

        # Refused is never retried
        with mock.patch.object(backend._opener, "open", side_effect=Refused("redirect-off-allowlist", "no")):
            with self.assertRaises(Refused):
                backend.complete("implementer", "m", 0.2, [{"role": "user", "content": "hi"}])

        http_429.close()
        http_400.close()

    def test_open_backend_cmd(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        repo = tmp / "repo"
        repo.mkdir()
        data = _base_config_data(tmp / "state")
        data["adapters"]["model"] = f"cmd:{sys.executable} fake.py"
        cfg = Config(data, repo)
        self.assertIsInstance(open_backend(cfg), CmdModelBackend)


class JsonlTelemetryTestsV04(unittest.TestCase):
    def test_card_fields_v04_defaults(self) -> None:
        for field in (
            "micromanager_calls",
            "micromanager_tokens_in",
            "micromanager_tokens_out",
            "micromanager_spend",
            "false_rejects_dismissed",
        ):
            self.assertIn(field, TELEMETRY_CARD_FIELDS)
        rec = complete_record({}, TELEMETRY_CARD_FIELDS)
        self.assertEqual(rec["micromanager_calls"], 0)
        self.assertEqual(rec["micromanager_tokens_in"], 0)
        self.assertEqual(rec["micromanager_tokens_out"], 0)
        self.assertEqual(rec["micromanager_spend"], 0.0)
        self.assertEqual(rec["false_rejects_dismissed"], 0)

    def test_telemetry_sink_propagates_inner_problems_without_dupes(self) -> None:
        class _Inner:
            def __init__(self) -> None:
                self.problems: list[str] = []

            def write(self, stream, record):
                if "disk err" not in self.problems:
                    self.problems.append("disk err")

            def flush(self):
                if "flush err" not in self.problems:
                    self.problems.append("flush err")

        sink = TelemetrySink(_Inner())
        sink.write("attempt", {})
        sink.write("attempt", {})
        sink.flush()
        sink.flush()
        self.assertEqual(sink.problems, ["disk err", "flush err"])

    def test_open_telemetry_dispatches(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        repo = tmp / "repo"
        repo.mkdir()
        run_dir = tmp / "run"
        data = _base_config_data(tmp / "state")
        cfg = Config(data, repo)
        self.assertIsInstance(open_telemetry(cfg, run_dir), JsonlTelemetry)

        data["adapters"]["telemetry"] = f"cmd:{sys.executable} tel.py"
        cfg_cmd = Config(data, repo)
        self.assertIsInstance(open_telemetry(cfg_cmd, run_dir), CmdTelemetry)


if __name__ == "__main__":
    unittest.main()
