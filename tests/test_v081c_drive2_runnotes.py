"""J5: a run whose ACP children are not confined says so in its notes, once per provider."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openfarm_core import confine as confine_lib
from openfarm_core.cli import EXIT_OK
from openfarm_core.cli import main as cli_main
from openfarm_core.state import read_json
from tests.test_python_e2e import build_python_toy


def _run_cli(*args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_main(list(args))
    return code, out.getvalue(), err.getvalue()


def _confine_notes(notes: list[str]) -> list[str]:
    return [n for n in notes if "confine" in n or "tool flags alone" in n]


class RunConfinementNotesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy = build_python_toy()
        self.addCleanup(self.toy.cleanup)

    def _configure(self, *, provider: str, confine: str, binary: str) -> None:
        cfg = self.toy.root / ".openfarm" / "config.toml"
        text = cfg.read_text().replace(
            'model = "scripted"\n', f'model = "{provider}-acp"\nmicromanager = "{provider}-acp"\n')
        text += f'\n[micromanager]\nbinary = "{binary}"\nconfine = "{confine}"\n'
        cfg.write_text(text)
        self.toy.commit(f"{provider} acp, confine {confine}")

    def _run(self, *, usable: bool, human: bool = False) -> tuple[dict, str]:
        status = (True, "bwrap works") if usable else (False, "bwrap is not installed (test)")
        with mock.patch.object(confine_lib, "bwrap_status", return_value=status):
            rc, out, err = _run_cli("--repo", str(self.toy.root), "--json", "run")
            self.assertEqual(rc, EXIT_OK, err + out)
            payload = json.loads(out)
            h_out = ""
            if human:
                h_rc, h_out, h_err = _run_cli("--repo", str(self.toy.root), "run")
                self.assertEqual(h_rc, EXIT_OK, h_err)
        record = read_json(self.toy.state_dir / "runs" / payload["run_id"] / "run.json")
        self.assertEqual(record["notes"], payload["notes"])
        return record, h_out

    def test_off_is_noted_once_for_codex_workers_and_supervisor(self) -> None:
        self._configure(provider="codex", confine="off", binary="codex-exec")
        record, human = self._run(usable=True, human=True)
        self.assertEqual(_confine_notes(record["notes"]), [confine_lib.off_note("codex")])
        self.assertIn(confine_lib.off_note("codex"), human)

    def test_codex_on_flags_alone_is_noted(self) -> None:
        self._configure(provider="codex", confine="auto", binary="codex-exec")
        record, _ = self._run(usable=False)
        notes = _confine_notes(record["notes"])
        self.assertEqual(len(notes), 1, record["notes"])
        self.assertIn("codex child is held by its tool flags alone", notes[0])
        self.assertIn("bwrap is not installed (test)", notes[0])

    def test_a_confined_child_gives_no_note(self) -> None:
        self._configure(provider="codex", confine="auto", binary="codex-exec")
        record, _ = self._run(usable=True)
        self.assertEqual(_confine_notes(record["notes"]), [])

    def test_claude_under_auto_gives_no_note_and_under_off_does(self) -> None:
        self._configure(provider="claude", confine="auto", binary="auto")
        record, _ = self._run(usable=False)
        self.assertEqual(_confine_notes(record["notes"]), [])
        cfg = self.toy.root / ".openfarm" / "config.toml"
        cfg.write_text(cfg.read_text().replace('confine = "auto"', 'confine = "off"'))
        self.toy.commit("confine off")
        record, _ = self._run(usable=False)
        self.assertEqual(_confine_notes(record["notes"]), [confine_lib.off_note("claude")])

    def test_swarm_coordinator_and_shared_supervisor_are_noted_once(self) -> None:
        cfg = self.toy.root / ".openfarm" / "config.toml"
        text = cfg.read_text().replace("concurrent_cards = 1", "concurrent_cards = 2")
        text += ('\n[swarm]\nenabled = true\ncoordinator = "codex-acp"\n'
                 'supervisor = "codex-acp"\n'
                 '\n[micromanager]\nbinary = "codex-exec"\nconfine = "off"\n')
        cfg.write_text(text)
        self.toy.commit("swarm on codex acp")
        record, _ = self._run(usable=True)
        self.assertEqual(_confine_notes(record["notes"]), [confine_lib.off_note("codex")])

    def test_a_note_that_cannot_be_worked_out_never_costs_the_run_its_record(self) -> None:
        self._configure(provider="codex", confine="off", binary="codex-exec")
        # One card at a time, so no shared seat: only the notes ask `hold_of`.
        with mock.patch("openfarm_core.cli._SharedAcpSupervisor.hold_of",
                        side_effect=RuntimeError("no worker here")):
            record, _ = self._run(usable=True)
        self.assertIn("could not work out how this run's ACP children were held: "
                      "RuntimeError: no worker here", record["notes"])

    def test_a_run_without_acp_is_unchanged(self) -> None:
        record, _ = self._run(usable=False)
        self.assertEqual(_confine_notes(record["notes"]), [])


if __name__ == "__main__":
    unittest.main()
