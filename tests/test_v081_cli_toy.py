"""Shared helpers for the v0.8.1 `cli` unit tests: a toy repo with N cards, a
scripted model, and in-process command runs.

The toy is the Python toy from `tests/test_python_e2e.py` with one tiny module
per card, so every card can be driven end to end by the scripted backend.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core import cli as cli_mod  # noqa: E402
from openfarm_core.cli import main as cli_main  # noqa: E402
from tests.test_python_e2e import build_python_toy  # noqa: E402

SWARM_ON = '\n[swarm]\nenabled = true\nsupervisor = "off"\n'
DSF_TIER = ('\n[tiers.dsf]\nmodel = "stub"\nefforts = ["low", "medium", "high", "max"]\n'
            'effort_param = "reasoning_effort"\n')
COORD_TIER = ('\n[tiers.coord]\nmodel = "coord-model"\nefforts = ["low","medium","high","max"]\n'
              '\n[swarm]\nenabled = true\ncoordinator = "tier:coord"\nsupervisor = "off"\n'
              'default_tier = "dsf"\n')


def make_toy(n_cards: int = 2, *, extra_config: str = "", concurrent_cards: int = 1,
             tiers: bool = True):
    """A toy repo with `n_cards` ready cards and the script that passes them all."""
    toy = build_python_toy()
    names = [chr(ord("a") + i) for i in range(n_cards)]
    for nm in names:
        toy.write(f"tally/m_{nm}.py", f"def {nm}(x: int) -> int:\n    return x\n")
    toy.commit("add modules")
    cfg_path = toy.root / ".openfarm" / "config.toml"
    text = cfg_path.read_text()
    text = text.replace("concurrent_cards = 1", f"concurrent_cards = {concurrent_cards}")
    if tiers:
        text += DSF_TIER
    text += extra_config
    cfg_path.write_text(text)
    script: dict = {}
    for i, nm in enumerate(names, 1):
        cid = f"CARD-{i}"
        toy.write_card(
            cid, title=f"Mod {nm}", category="pinned-bugfix", unit=".",
            touches=[f"tally/m_{nm}.py"],
            tests=[{"name": f"test_{nm}", "file": f"tests/test_{nm}.py",
                    "asserts": f"{nm}(1)==2"}],
            done_text=f"`{nm}(x)` returns `x + 1`.",
        )
        script[f"{cid}/test_writer"] = (
            f"=== whole_file: tests/test_{nm}.py\nimport unittest\n"
            f"from tally.m_{nm} import {nm}\n"
            f"class T(unittest.TestCase):\n"
            f"    def test_{nm}(self): self.assertEqual({nm}(1), 2)\n")
        script[f"{cid}/implementer"] = (
            f"=== whole_file: tally/m_{nm}.py\ndef {nm}(x: int) -> int:\n    return x + 1\n")
        script[f"{cid}/checker_pass1"] = f"RESTATEMENT: {nm} returns x + 1"
        script[f"{cid}/checker_pass2"] = "{}"
    toy.commit("add cards")
    return toy, script


def write_script(script, tmp: Path | None = None) -> Path:
    folder = Path(tmp) if tmp is not None else Path(tempfile.mkdtemp(prefix="of-v081-script-"))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "script.json"
    path.write_text(json.dumps(script), encoding="utf-8")
    return path


def run_cli(*argv: str) -> tuple[int, str, str]:
    """Run `openfarm` in-process; returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli_main(list(argv))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return int(code), out.getvalue(), err.getvalue()


def run_json(repo: Path, *argv: str) -> tuple[int, dict, str]:
    code, out, err = run_cli("--repo", str(repo), "--json", *argv)
    try:
        payload = json.loads(out) if out.strip() else {}
    except ValueError:
        payload = {"_raw": out}
    return code, payload, err


@contextlib.contextmanager
def spy_human_payloads():
    """Record every (kind, payload) the CLI hands to its human formatter."""
    seen: list[tuple[str, dict]] = []
    real = cli_mod.format_human

    def _spy(kind, payload, *args, **kwargs):
        seen.append((kind, json.loads(json.dumps(payload, default=str))))
        return real(kind, payload, *args, **kwargs)

    with mock.patch.object(cli_mod, "format_human", _spy):
        yield seen


def run_human(repo: Path, *argv: str) -> tuple[int, str, str, list[tuple[str, dict]]]:
    """Run without `--json`; also returns the payloads the formatter received."""
    with spy_human_payloads() as seen:
        code, out, err = run_cli("--repo", str(repo), *argv)
    return code, out, err, list(seen)


class ToyHelpersTests(unittest.TestCase):
    def test_make_toy_writes_ready_cards(self) -> None:
        toy, script = make_toy(2)
        self.addCleanup(toy.cleanup)
        self.assertTrue((toy.root / "cards" / "CARD-1.md").is_file())
        self.assertTrue((toy.root / "cards" / "CARD-2.md").is_file())
        self.assertIn("CARD-2/implementer", script)


if __name__ == "__main__":
    unittest.main()
