"""A worker answer cut off at the token limit is refused, not frozen half-written."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adapters.openai_compat import ScriptedBackend
from openfarm_core.briefs import Brief
from openfarm_core.config import Config, ConfigError
from openfarm_core.errors import Invalid
from openfarm_core.roles import Roles, parse_answer_edits
from tests.test_python_e2e import GOOD_TEST, build_python_toy, run_cli, script

ENDPOINT = "https://api.example.invalid/v1"
HALF_FILE = "=== whole_file: tests/test_x.py\ndef test_x():\n    assert f("


def _config(state: Path, max_tokens=None) -> dict:
    model = {"endpoint": ENDPOINT, "allowed_endpoints": [ENDPOINT],
             "implementer": {"model": "impl-m"}}
    if max_tokens is not None:
        model["max_tokens"] = max_tokens
    return {"project": {"name": "demo", "state_dir": str(state)},
            "adapters": {"languages": ["python"], "model": "openai-compatible"},
            "model": model, "policy": {"categories": ["pattern"]}}


class _Recording(ScriptedBackend):
    def complete(self, *args, **kwargs):
        self.max_tokens_seen = kwargs.get("max_tokens")
        return super().complete(*args, **kwargs)


class CutOffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.state = self.tmp / "state"

    def test_max_tokens_defaults_to_4096_and_takes_a_setting(self) -> None:
        self.assertEqual(Config(_config(self.state), self.repo).model_max_tokens, 4096)
        cfg = Config(_config(self.state, 32768), self.repo)
        self.assertEqual(cfg.model_max_tokens, 32768)

    def test_a_bad_max_tokens_is_refused(self) -> None:
        for bad in (0, -1, "lots", 1.5, True):
            with self.assertRaises(ConfigError, msg=repr(bad)):
                Config(_config(self.state, bad), self.repo)

    def test_every_role_call_sends_the_configured_limit(self) -> None:
        cfg = Config(_config(self.state, 20000), self.repo)
        backend = _Recording({"*": "ok"})
        Roles(cfg, backend, card_id="CARD-1").call(
            "test_writer", Brief("test_writer", "hi"))
        self.assertEqual(backend.max_tokens_seen, 20000)

    def test_a_cut_off_answer_is_refused_with_a_reason(self) -> None:
        cfg = Config(_config(self.state), self.repo)
        backend = ScriptedBackend({"*": {"content": HALF_FILE, "tokens_out": 4096,
                                         "finish_reason": "length"}})
        call = Roles(cfg, backend).call("test_writer", Brief("test_writer", "hi"))
        self.assertTrue(call.cut_off)
        self.assertEqual(call.as_dict()["finish_reason"], "length")
        with self.assertRaises(Invalid) as caught:
            parse_answer_edits(call)
        self.assertEqual(caught.exception.code, "answer-cut-off")
        self.assertIn("[model] max_tokens", caught.exception.message)

    def test_a_finished_answer_parses_as_before(self) -> None:
        cfg = Config(_config(self.state), self.repo)
        content = "=== whole_file: tests/test_x.py\ndef test_x():\n    assert True\n"
        call = Roles(cfg, ScriptedBackend({"*": content})).call(
            "test_writer", Brief("test_writer", "hi"))
        self.assertFalse(call.cut_off)
        edits = parse_answer_edits(call)
        self.assertEqual(len(edits), 1)


class CutOffCardTests(unittest.TestCase):
    """A real CLI run whose test writer keeps hitting the limit."""

    def setUp(self) -> None:
        self.toy = build_python_toy()
        self.addCleanup(self.toy.cleanup)
        self.toy.write_card(
            "CARD-1", title="A full bundle refuses a push", category="pinned-bugfix",
            unit=".", touches=["tally/bounded.py"],
            tests=[{"name": "test_a_full_bundle_refuses_a_push",
                    "file": "tests/test_full.py"}],
            done_text="`Bounded(1).push(8)` after one push raises `FullError`.")
        self.toy.commit("tally: CARD-1")

    def test_the_card_stops_saying_the_answer_was_cut_off(self) -> None:
        cut = {"content": "=== whole_file: tests/test_full.py\n" + GOOD_TEST[:120],
               "tokens_out": 4096, "finish_reason": "length"}
        result = run_cli(self.toy.root, "run", "--card", "CARD-1", "--cards", "1",
                         "--script", str(script(**{"CARD-1/test_writer": cut})))
        outcome = json.loads(result.stdout)["outcomes"][0]
        self.assertEqual(outcome["final_state"], "blocked")
        self.assertEqual(outcome["stop_code"], "tests-unprovable")
        self.assertIn("cut off at the token limit", outcome["detail"])
        run_dir = next((self.toy.state_dir / "cards" / "CARD-1").iterdir())
        record = json.loads((run_dir / "card-record.json").read_text())
        self.assertIn("cut off at the token limit", record["detail"])
        self.assertIn("Why it stopped: ", (run_dir / "dossier.md").read_text())
        # Nothing half-written was frozen as the card's tests.
        self.assertFalse((self.toy.root / "tests" / "test_full.py").exists())


if __name__ == "__main__":
    unittest.main()
