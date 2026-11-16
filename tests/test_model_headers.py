"""`[model] headers`: extra request headers such as a zero-retention opt-in."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adapters.openai_compat import OpenAICompatibleBackend, open_backend
from openfarm_core.config import Config, ConfigError

ENDPOINT = "https://api.example.invalid/v1"


def _config(state: Path, headers=None) -> dict:
    model = {"endpoint": ENDPOINT, "allowed_endpoints": [ENDPOINT],
             "implementer": {"model": "impl-m"}}
    if headers is not None:
        model["headers"] = headers
    return {"project": {"name": "demo", "state_dir": str(state)},
            "adapters": {"languages": ["python"], "model": "openai-compatible"},
            "model": model, "policy": {"categories": ["pattern"]}}


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({"choices": [{"message": {"content": "ok"},
                                        "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 3, "completion_tokens": 1}}).encode()


class ModelHeadersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.state = self.tmp / "state"
        self.key = self.tmp / "worker.key"
        self.key.write_text("secret-key\n")

    def _sent_headers(self, backend: OpenAICompatibleBackend) -> dict:
        with mock.patch.object(backend._opener, "open", return_value=_Resp()) as opened:
            backend.complete("implementer", "m", 0.2, [{"role": "user", "content": "hi"}])
        request = opened.call_args.args[0]
        return {k.lower(): v for k, v in request.header_items()}

    def test_no_headers_by_default(self) -> None:
        cfg = Config(_config(self.state), self.repo)
        self.assertEqual(cfg.model_headers, {})

    def test_a_configured_header_is_sent_with_every_call(self) -> None:
        cfg = Config(_config(self.state, {"x-cmd-zdr": "1"}), self.repo)
        cfg.key_file = self.key
        sent = self._sent_headers(open_backend(cfg))
        self.assertEqual(sent["x-cmd-zdr"], "1")
        self.assertEqual(sent["authorization"], "Bearer secret-key")
        self.assertEqual(sent["content-type"], "application/json")

    def test_a_header_cannot_replace_the_key_or_body_type(self) -> None:
        for name in ("Authorization", "authorization", "Content-Type", "Host"):
            with self.assertRaises(ConfigError, msg=name):
                Config(_config(self.state, {name: "x"}), self.repo)

    def test_bad_header_tables_are_refused(self) -> None:
        for bad in ({"x-zdr": 1}, {"bad name": "1"}, {"x-zdr": "1\r\nX-Evil: 1"}, ["x-zdr"]):
            with self.assertRaises(ConfigError, msg=repr(bad)):
                Config(_config(self.state, bad), self.repo)


if __name__ == "__main__":
    unittest.main()
