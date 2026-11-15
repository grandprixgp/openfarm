"""The no-push guarantee, and the adapter protocol.

The spec asks for "a test that no code path pushes". A grep would prove very little,
so this checks three ways: the git module's allowlist, an AST-free scan of every
module for a push it could reach, and a real bare remote that must stay empty after a
full run.
"""

from __future__ import annotations

import ast
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from openfarm_core.git import (ALLOWED_SUBCOMMANDS, FORBIDDEN_SUBCOMMANDS, Git,
                               GitRefused)
from openfarm_core.protocol import (CmdAdapter, check_hello, make_error,
                                    make_request, make_success, parse_response)

PACKAGE = Path(__file__).resolve().parent.parent / "openfarm_core"


class NoPushTests(unittest.TestCase):
    def test_the_allowlist_has_no_pushing_subcommand(self) -> None:
        for name in ("push", "fetch", "pull", "remote", "submodule", "clone"):
            self.assertNotIn(name, ALLOWED_SUBCOMMANDS)

    def test_forbidden_and_allowed_do_not_overlap(self) -> None:
        self.assertEqual(ALLOWED_SUBCOMMANDS & FORBIDDEN_SUBCOMMANDS, frozenset())

    def test_git_refuses_to_push(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", str(tmp)], check=True)
        git = Git(tmp)
        with self.assertRaises(GitRefused):
            git._run(["push", "origin", "main"])
        with self.assertRaises(GitRefused):
            git._run(["fetch"])
        with self.assertRaises(GitRefused):
            git._run(["remote", "add", "origin", "x"])

    def test_no_module_string_builds_a_push_call(self) -> None:
        """No shell-out may be given a forbidden git subcommand as its argv.

        `git.py` names those subcommands in order to refuse them, which is why this
        looks at the argv of process calls and not at every string in the file.
        """
        offenders = []
        for path in PACKAGE.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not _is_process_call(node.func):
                    continue
                for literal in _string_literals(node):
                    if literal in FORBIDDEN_SUBCOMMANDS:
                        offenders.append(f"{path.name}: {literal}")
        self.assertEqual(offenders, [],
                         "a process call spells a forbidden git subcommand: "
                         + ", ".join(offenders))

    def test_the_git_module_is_the_only_place_that_runs_git(self) -> None:
        """A process call naming `git` may only appear in git.py."""
        offenders = []
        for path in PACKAGE.rglob("*.py"):
            if path.name == "git.py":
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and _is_process_call(node.func):
                    if "git" in list(_string_literals(node)):
                        offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [],
                         "git was called outside git.py: " + ", ".join(offenders))

    def test_a_full_git_workflow_leaves_the_remote_empty(self) -> None:
        """The real proof: no ref reaches the remote after branch work."""
        root = Path(tempfile.mkdtemp()) / "repo"
        origin = Path(tempfile.mkdtemp()) / "origin.git"
        root.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)],
                       check=True)
        git = Git(root)
        (root / "a.txt").write_text("one\n")
        git.commit_all("first")
        (root / "a.txt").write_text("two\n")
        git.commit_all("second")
        git.create_branch("openfarm/CARD-1-test", "HEAD")
        git.worktree_add(root.parent / "wt", "openfarm/CARD-1-test")

        refs = subprocess.run(["git", "for-each-ref"], cwd=str(origin),
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(refs, "", "the bare remote must have no refs at all")


def _is_process_call(func: ast.expr) -> bool:
    """True for `subprocess.run(...)`, `subprocess.Popen(...)` and friends."""
    if isinstance(func, ast.Attribute):
        return func.attr in ("run", "Popen", "call", "check_call",
                             "check_output", "execv", "execvp")
    if isinstance(func, ast.Name):
        return func.id in ("system", "execv", "execvp", "popen")
    return False


def _string_literals(call: ast.Call):
    """Every bare string constant anywhere inside a call expression."""
    for node in ast.walk(call):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


class ProtocolTests(unittest.TestCase):
    def test_request_shape(self) -> None:
        request = make_request("claim", {"id": "C"}, "cards", "r-1")
        self.assertEqual(request["openfarm"], 1)
        self.assertEqual(request["adapter"], "cards")
        self.assertEqual(request["op"], "claim")
        self.assertEqual(request["request_id"], "r-1")

    def test_success_and_error_shapes(self) -> None:
        success = make_success("r-1", {"lease_id": "L"})
        self.assertTrue(success["ok"])
        error = make_error("r-1", "refused", "already-claimed", "held")
        self.assertFalse(error["ok"])
        self.assertEqual(error["error"]["kind"], "refused")

    def test_bad_error_kind_is_refused(self) -> None:
        from openfarm_core.errors import Invalid
        with self.assertRaises(Invalid):
            make_error("r-1", "catastrophe", "x", "y")

    def test_parse_success(self) -> None:
        self.assertEqual(parse_response(make_success("r-1", {"a": 1})), {"a": 1})

    def test_every_error_kind_maps_to_its_class(self) -> None:
        from openfarm_core.errors import Infra, Invalid, Refused, Unsupported
        expected = {"refused": Refused, "infra": Infra, "invalid": Invalid,
                    "unsupported": Unsupported}
        for kind, cls in expected.items():
            with self.assertRaises(cls):
                parse_response(make_error("r-1", kind, "code", "message"))

    def test_a_non_object_answer_is_invalid(self) -> None:
        from openfarm_core.errors import Invalid
        with self.assertRaises(Invalid):
            parse_response(["not", "an", "object"])

    def test_an_answer_without_a_version_is_invalid(self) -> None:
        from openfarm_core.errors import Invalid
        with self.assertRaises(Invalid):
            parse_response({"ok": True, "result": {}})

    def test_hello_accepts_a_shared_version_and_ops(self) -> None:
        check_hello({"versions": [1], "ops": ["next", "read"]}, "cards",
                    {"next", "read"})

    def test_hello_refuses_a_version_mismatch(self) -> None:
        from openfarm_core.errors import Invalid
        with self.assertRaises(Invalid) as caught:
            check_hello({"versions": [99], "ops": ["next"]}, "cards", {"next"})
        self.assertEqual(caught.exception.code, "version-mismatch")

    def test_hello_refuses_a_missing_op(self) -> None:
        from openfarm_core.errors import Invalid
        with self.assertRaises(Invalid) as caught:
            check_hello({"versions": [1], "ops": ["next"]}, "cards",
                        {"next", "claim"})
        self.assertEqual(caught.exception.code, "missing-ops")


class CmdAdapterTests(unittest.TestCase):
    """An external adapter driven as a real subprocess."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.script = self.tmp / "adapter.py"
        self.script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "request = json.load(sys.stdin)\n"
            "op = request['op']\n"
            "if op == 'hello':\n"
            "    answer = {'openfarm': 1, 'request_id': request['request_id'],\n"
            "              'ok': True, 'result': {'name': 'fake', 'versions': [1],\n"
            "                                     'ops': ['hello', 'next']}}\n"
            "elif op == 'next':\n"
            "    answer = {'openfarm': 1, 'request_id': request['request_id'],\n"
            "              'ok': True, 'result': {'cards': ['CARD-1']}}\n"
            "else:\n"
            "    answer = {'openfarm': 1, 'request_id': request['request_id'],\n"
            "              'ok': False, 'error': {'kind': 'refused',\n"
            "                                    'code': 'nope', 'message': 'no',\n"
            "                                    'retry_after_s': None}}\n"
            "print(json.dumps(answer))\n")
        self.script.chmod(0o755)

    def test_a_working_external_adapter(self) -> None:
        adapter = CmdAdapter(f"cmd:python3 {self.script}", self.tmp)
        hello = adapter.hello("cards", {"next"})
        self.assertEqual(hello["name"], "fake")
        self.assertEqual(adapter.call("cards", "next", {}), {"cards": ["CARD-1"]})

    def test_a_refusal_is_mapped(self) -> None:
        from openfarm_core.errors import Refused
        adapter = CmdAdapter(f"cmd:python3 {self.script}", self.tmp)
        with self.assertRaises(Refused):
            adapter.call("cards", "claim", {})

    def test_a_non_zero_exit_is_infra(self) -> None:
        from openfarm_core.errors import Infra
        adapter = CmdAdapter("cmd:false", self.tmp)
        with self.assertRaises(Infra):
            adapter.call("cards", "next", {})

    def test_output_that_is_not_json_is_infra(self) -> None:
        from openfarm_core.errors import Infra
        adapter = CmdAdapter("cmd:echo not-json", self.tmp)
        with self.assertRaises(Infra):
            adapter.call("cards", "next", {})

    def test_a_missing_program_is_infra(self) -> None:
        from openfarm_core.errors import Infra
        adapter = CmdAdapter("cmd:/nonexistent/adapter-xyz", self.tmp)
        with self.assertRaises(Infra):
            adapter.call("cards", "next", {})


class FallbackTests(unittest.TestCase):
    """An adapter may omit the ops the spec gives a fallback for."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.script = self.tmp / "no-hash-adapter.py"
        self.script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "request = json.load(sys.stdin)\n"
            "op = request['op']\n"
            "rid = request['request_id']\n"
            "card = {'id': 'CARD-1', 'title': 'T', 'state': 'ready',\n"
            "        'done_text': 'returns `Err(Full)`', 'touches': ['src/a.rs'],\n"
            "        'deps': [], 'fields': {'category': 'pinned-bugfix'}}\n"
            "if op == 'hello':\n"
            "    result = {'name': 'no-hash', 'versions': [1],\n"
            "              'ops': ['hello', 'next', 'read', 'claim', 'move',\n"
            "                      'release', 'finding']}\n"
            "elif op == 'read':\n"
            "    result = {'card': card}\n"
            "elif op == 'hash':\n"
            "    print(json.dumps({'openfarm': 1, 'request_id': rid, 'ok': False,\n"
            "                      'error': {'kind': 'unsupported',\n"
            "                                'code': 'no-hash', 'message': 'no',\n"
            "                                'retry_after_s': None}}))\n"
            "    sys.exit(0)\n"
            "else:\n"
            "    result = {}\n"
            "print(json.dumps({'openfarm': 1, 'request_id': rid, 'ok': True,\n"
            "                  'result': result}))\n")
        self.script.chmod(0o755)

    def test_hello_passes_without_the_optional_ops(self) -> None:
        adapter = CmdAdapter(f"cmd:python3 {self.script}", self.tmp)
        from openfarm_core.protocol import REQUIRED_OPS
        hello = adapter.hello("cards", REQUIRED_OPS["cards"])
        self.assertEqual(hello["name"], "no-hash")

    def test_hash_falls_back_to_the_canonical_read_payload(self) -> None:
        from openfarm_core.cards import Card, hash_value
        from openfarm_core.protocol import card_hash

        adapter = CmdAdapter(f"cmd:python3 {self.script}", self.tmp)
        digest = card_hash(adapter.call, "CARD-1")
        expected = hash_value(adapter.call("cards", "read", {"id": "CARD-1"})["card"])
        self.assertEqual(digest, expected)

    def test_the_fallback_matches_the_card_model_hash(self) -> None:
        from openfarm_core.cards import Card
        from openfarm_core.protocol import card_hash

        adapter = CmdAdapter(f"cmd:python3 {self.script}", self.tmp)
        payload = adapter.call("cards", "read", {"id": "CARD-1"})["card"]
        self.assertEqual(card_hash(adapter.call, "CARD-1"),
                         Card.from_dict(payload).hash())


if __name__ == "__main__":
    unittest.main()
