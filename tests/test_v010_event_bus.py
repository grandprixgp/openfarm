"""v0.10.0 Pillar 5 tests: Non-blocking RunEventBus (`on_event` + `events.jsonl`)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from openfarm_core.state import StateDir


class RunEventBusV010Tests(unittest.TestCase):
    def test_event_bus_emits_to_callback_and_appends_jsonl(self) -> None:
        from openfarm_core.coordinator import RunEventBus

        received: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp:
            state = StateDir(Path(tmp) / "state")
            bus = RunEventBus(state, "run-test-01", on_event=received.append)
            bus.emit(kind="run_start", message="Wave 1 started")
            bus.emit(
                kind="phase",
                card_id="W1-01",
                phase="implementer",
                attempt=2,
                effort="high",
                message="Escalated effort medium -> high",
                worker_tokens=1500,
                supervisor_tokens=200,
                tokens_in_fresh=700,
                tokens_in_cached=1000,
            )
            bus.emit(kind="run_finish", message="Wave 1 complete")

            self.assertEqual(len(received), 3)
            self.assertEqual(received[1]["card_id"], "W1-01")
            self.assertEqual(received[1]["phase"], "implementer")
            self.assertEqual(received[1]["effort"], "high")
            self.assertEqual(received[1]["worker_tokens"], 1500)

            events_file = state.run_dir("run-test-01") / "events.jsonl"
            self.assertTrue(events_file.is_file())
            rows = [json.loads(ln) for ln in events_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]["kind"], "run_start")
            self.assertEqual(rows[1]["tokens_in_cached"], 1000)
            self.assertEqual(rows[2]["kind"], "run_finish")

    def test_card_run_emits_phase_events_when_on_event_provided(self) -> None:
        from openfarm_core.cli import Context, cmd_run
        from types import SimpleNamespace
        from tests.test_python_e2e import build_python_toy

        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        toy.write_card(
            "CARD-1",
            title="Simple bugfix",
            category="pinned-bugfix",
            touches=["tally/bounded.py"],
            tests=[{"name": "test_missing_event", "file": "tests/test_missing_event.py"}],
            done_text="## Done\n\n- `bounded.py` returns 0 on empty input.",
        )
        toy.commit("add CARD-1")

        seen: list[dict] = []
        ctx = Context(toy.root, None, json_output=True)
        args = SimpleNamespace(
            cards=1,
            spend=None,
            hours=None,
            dry_run=True,
            card="CARD-1",
            run_id=None,
            profile=None,
            once=False,
            no_micromanager=True,
            json=True,
        )
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_run(ctx, args, on_event=seen.append)
        self.assertEqual(rc, 0)
        kinds = [e.get("kind") for e in seen]
        self.assertIn("run_start", kinds)
        self.assertIn("run_finish", kinds)
        # events.jsonl written under state_dir/runs/<run_id>/events.jsonl
        data = json.loads(buf.getvalue())
        run_id = data.get("run_id")
        self.assertTrue(run_id)
        events_p = ctx.state.run_dir(run_id) / "events.jsonl"
        self.assertTrue(events_p.is_file())


if __name__ == "__main__":
    unittest.main()
