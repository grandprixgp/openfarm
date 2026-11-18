"""M2 and m15: an ACP child the confinement rules refuse stops the run before it starts.

A supervisor (or ACP worker, or ACP coordinator) whose hold is refused would
fail every op of every card, and a supervisor's ops degrade one by one, so a
night would pass with no supervisor and notes that read as clean. The run is
refused up front instead, with the hold's own code and reason, and nothing is
claimed. Only children the run would really start count: a supervisor whose
mode is `off`, a role a script stands in for, or a seat that is not ACP is not
one. The run's notes use the same answer. And a card's in-review note says how
many gates passed and how many did not.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openfarm_core import confine as confine_lib
from openfarm_core.cli import EXIT_OK, EXIT_REFUSED
from openfarm_core.cli import main as cli_main
from openfarm_core.flow import CardRun
from openfarm_core.gates.checks import GateResult
from tests.test_python_e2e import GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy

UNUSABLE = (False, "bwrap is not installed (test)")


def _run_cli(*args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_main(list(args))
    return code, out.getvalue(), err.getvalue()


class RefusedChildStopsTheRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy = build_python_toy()
        self.addCleanup(self.toy.cleanup)
        self.toy.write_card(
            "CARD-1", title="A full bundle refuses a push", category="pinned-bugfix",
            unit=".", touches=["tally/bounded.py"],
            tests=[{"name": "test_a_full_bundle_refuses_a_push", "file": "tests/test_full.py",
                    "asserts": "push raises FullError"}],
            done_text="`Bounded(1).push(8)` after one push raises `FullError`.")
        # A standalone codex-acp server, never run: named by path, so it is the
        # server transport whatever is on PATH. It needs the boundary.
        self.server = Path(self.toy.base) / "bin" / "codex-acp"
        self.server.parent.mkdir(parents=True, exist_ok=True)
        self.server.write_text("#!/bin/sh\necho 'the fake codex-acp must never run' >&2\nexit 9\n")
        self.server.chmod(0o755)
        base = Path(self.toy.base)
        self.worker = base / "worker.json"
        self.worker.write_text(json.dumps({
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
            "CARD-1/implementer": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: a full bundle refuses a push",
            "CARD-1/checker_pass2": "{}",
        }))
        self.mm = base / "mm.json"
        self.mm.write_text(json.dumps({
            "CARD-1/shape_and_plan": {"verdict": "ready", "clarified_notes": ""},
            "CARD-1/pre_review": {"verdict": "approve", "reviewer_brief": "ok",
                                  "missed_clause": "", "findings": []},
        }))

    def _configure(self, *, mode: str = "active", worker: str = "scripted",
                   supervisor: str = "codex-acp", swarm: str = "") -> None:
        cfg = self.toy.config
        text = cfg.read_text().replace(
            'model = "scripted"\n', f'model = "{worker}"\nmicromanager = "{supervisor}"\n')
        text += (f'\n[micromanager]\nbinary = "{self.server}"\nconfine = "auto"\n'
                 f'mode = "{mode}"\n')
        if swarm:
            text += "\n[swarm]\nenabled = true\n" + swarm
        cfg.write_text(text)
        self.toy.commit("card, codex-acp server, confine auto")

    def _run(self, *extra: str, human: bool = False) -> tuple[int, dict | str, str]:
        with mock.patch.object(confine_lib, "bwrap_status", return_value=UNUSABLE):
            args = ["--repo", str(self.toy.root)] + ([] if human else ["--json"])
            rc, out, err = _run_cli(*args, "run", "--card", "CARD-1", "--cards", "1", *extra)
        return rc, (out if human else json.loads(out)), err

    def _assert_nothing_claimed(self) -> None:
        self.assertFalse((self.toy.state_dir / "cards" / "CARD-1").exists())
        self.assertFalse(list((self.toy.state_dir / "runs").glob("*/run.json")))
        log = subprocess.run(["git", "-C", str(self.toy.root), "log", "--all", "--format=%s"],
                             capture_output=True, text=True, check=True).stdout
        self.assertNotIn("claimed", log)

    def test_a_codex_server_supervisor_without_bwrap_refuses_the_run_up_front(self) -> None:
        self._configure()
        rc, payload, err = self._run("--script", str(self.worker))
        self.assertEqual(rc, EXIT_REFUSED, (payload, err))
        self.assertTrue(payload["refused"])
        self.assertEqual(payload["code"], "acp-confine-unavailable")
        self.assertEqual((payload["child"], payload["provider"]), ("supervisor", "codex"))
        self.assertIn("a standalone Codex ACP server needs the boundary", payload["reason"])
        self.assertIn("bwrap is not installed (test)", payload["reason"])
        self.assertEqual(payload["confine"]["held_by"], "refused")
        self._assert_nothing_claimed()
        self.assertNotIn("must never run", err)

    def test_the_human_output_names_the_refused_child(self) -> None:
        self._configure()
        rc, out, _err = self._run("--script", str(self.worker), human=True)
        self.assertEqual(rc, EXIT_REFUSED)
        self.assertIn("Run refused: a standalone Codex ACP server needs the boundary", out)
        self.assertIn("acp-confine-unavailable", out)
        self.assertIn("supervisor (codex)", out)

    def test_the_same_supervisor_with_mode_off_runs(self) -> None:
        self._configure(mode="off")
        rc, payload, err = self._run("--script", str(self.worker))
        self.assertEqual(rc, EXIT_OK, err[-1500:])
        self.assertEqual(payload["outcomes"][0]["final_state"], "in-review", payload["outcomes"])

    def test_the_same_supervisor_scripted_runs(self) -> None:
        self._configure()
        rc, payload, err = self._run("--script", str(self.worker), "--mm-script", str(self.mm))
        self.assertEqual(rc, EXIT_OK, err[-1500:])
        self.assertEqual(payload["outcomes"][0]["final_state"], "in-review", payload["outcomes"])

    def test_a_codex_server_worker_without_bwrap_refuses_and_a_scripted_one_runs(self) -> None:
        self._configure(worker="codex-acp", supervisor="off")
        rc, payload, _err = self._run()
        self.assertEqual(rc, EXIT_REFUSED, payload)
        self.assertEqual((payload["code"], payload["child"]), ("acp-confine-unavailable", "worker"))
        self._assert_nothing_claimed()
        rc, payload, err = self._run("--script", str(self.worker))
        self.assertEqual(rc, EXIT_OK, err[-1500:])
        self.assertEqual(payload["outcomes"][0]["final_state"], "in-review", payload["outcomes"])

    def test_a_swarm_codex_seat_is_used_whatever_mode_says(self) -> None:
        # `open_micromanager` lets an ACP supervisor seat override `mode = "off"`.
        self._configure(mode="off", supervisor="off", swarm='supervisor = "codex-acp"\n')
        rc, payload, _err = self._run("--script", str(self.worker))
        self.assertEqual(rc, EXIT_REFUSED, payload)
        self.assertEqual((payload["code"], payload["child"]), ("acp-confine-unavailable",
                                                               "supervisor"))
        self._assert_nothing_claimed()

    def test_a_codex_server_coordinator_without_bwrap_refuses_the_run(self) -> None:
        self._configure(supervisor="off", swarm='coordinator = "codex-acp"\n')
        rc, payload, _err = self._run()
        self.assertEqual(rc, EXIT_REFUSED, payload)
        self.assertEqual((payload["code"], payload["child"]), ("acp-confine-unavailable",
                                                               "coordinator"))
        self._assert_nothing_claimed()

    def test_a_managed_drive_whose_run_is_refused_says_so(self) -> None:
        from openfarm_core.config import load_config
        from openfarm_core.driver import drive_run
        from openfarm_core.state import StateDir
        self._configure(worker="codex-acp", supervisor="off")
        driver = Path(self.toy.base) / "driver.json"
        driver.write_text(json.dumps({"driver/*": {"actions": [], "answers": []}}))
        with mock.patch.object(confine_lib, "bwrap_status", return_value=UNUSABLE):
            res = drive_run(self.toy.root, load_config(self.toy.root),
                            StateDir(self.toy.state_dir), agent="claude", seat="supervisor",
                            card_ids=["CARD-1"], script=driver)
        self.assertTrue(res["refused"], res)
        self.assertEqual(res["code"], "acp-confine-unavailable")
        self.assertEqual(res["driver_turns"], 0)
        self._assert_nothing_claimed()

    def test_a_swarm_seat_set_to_off_is_no_acp_child(self) -> None:
        self._configure(swarm='supervisor = "off"\n')
        rc, payload, err = self._run("--script", str(self.worker))
        self.assertEqual(rc, EXIT_OK, err[-1500:])
        self.assertEqual(payload["outcomes"][0]["final_state"], "in-review", payload["outcomes"])

    def test_a_hold_that_cannot_be_worked_out_up_front_is_left_to_the_adapter(self) -> None:
        self._configure(mode="off")
        with mock.patch("openfarm_core.cli._SharedAcpSupervisor.hold_of",
                        side_effect=RuntimeError("no hold here")):
            rc, _payload, err = self._run("--script", str(self.worker))
        self.assertEqual(rc, EXIT_OK, err[-1500:])


class NotesDescribeOnlyChildrenTheRunUsedTests(unittest.TestCase):
    """m15: a supervisor whose mode is off is not described in the run's notes."""

    def setUp(self) -> None:
        self.toy = build_python_toy()
        self.addCleanup(self.toy.cleanup)

    def _notes(self, extra: str) -> list[str]:
        cfg = self.toy.config
        cfg.write_text(cfg.read_text().replace(
            'model = "scripted"\n', 'model = "scripted"\nmicromanager = "codex-acp"\n') + extra)
        self.toy.commit("codex supervisor, confine off")
        with mock.patch.object(confine_lib, "bwrap_status", return_value=(True, "bwrap works")):
            rc, out, err = _run_cli("--repo", str(self.toy.root), "--json", "run")
        self.assertEqual(rc, EXIT_OK, err)
        return json.loads(out)["notes"]

    def test_swarm_on_one_card_at_a_time_and_mode_off_gives_no_supervisor_note(self) -> None:
        notes = self._notes('\n[swarm]\nenabled = true\nsupervisor = "config"\n'
                            '\n[micromanager]\nbinary = "codex-exec"\nconfine = "off"\n'
                            'mode = "off"\n')
        self.assertNotIn(confine_lib.off_note("codex"), notes)

    def test_the_same_supervisor_in_use_is_described(self) -> None:
        notes = self._notes('\n[swarm]\nenabled = true\nsupervisor = "config"\n'
                            '\n[micromanager]\nbinary = "codex-exec"\nconfine = "off"\n')
        self.assertIn(confine_lib.off_note("codex"), notes)


class NoteHeaderTests(unittest.TestCase):
    """The in-review note's header counts the gates that did not pass."""

    def _note(self, gates: list[GateResult]) -> str:
        fake = types.SimpleNamespace(
            card=types.SimpleNamespace(id="CARD-9"),
            gates=gates,
            budget=types.SimpleNamespace(implementer_attempts=1, model_builds=2,
                                         fixed_builds=1, mutation_builds=0),
            named_passing=1,
            dossier_payload=None,
            dossier_path="/nowhere/dossier.md",
            stop_code=None,
            detail="",
        )
        return CardRun._note(fake, "in-review")

    def test_a_failed_gate_is_counted_not_read_as_a_pass(self) -> None:
        note = self._note([GateResult("suite", "pass"),
                           GateResult("micromanager-pre-review", "fail", "refused"),
                           GateResult("mutation", "infra")])
        header = note.split("Attempts:")[0]
        self.assertNotIn("all passed", header)
        self.assertIn("3 gates: 1 passed, 2 did not "
                      "(micromanager-pre-review=fail, mutation=infra).", header)

    def test_all_gates_passing_reads_as_before(self) -> None:
        note = self._note([GateResult("suite", "pass"), GateResult("revert", "pass")])
        self.assertTrue(note.startswith("CARD-9: in review\n\n2 gates, all passed. Attempts: 1."),
                        note)


if __name__ == "__main__":
    unittest.main()
