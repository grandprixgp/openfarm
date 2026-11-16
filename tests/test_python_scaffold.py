"""The supervisor's scaffold on a Python card: stubs that raise, and nothing else."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.cards import Card
from openfarm_core.errors import Refused
from openfarm_core.micromanager import Micromanager, ScriptedMicromanager, _build_op_prompt
from openfarm_core.packs.python import valid_python_scaffold

MODULE = '''\
"""`openfarm bench`."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["BenchTask", "load_task"]


@dataclass(frozen=True)
class BenchTask:
    """One task."""

    id: str
    root: Path
    retries: int = 0


def load_task(task_dir: Path) -> BenchTask:
    """Read a task."""
    raise NotImplementedError
'''


def _card(touches=("openfarm_core/bench.py", "tests/test_bench_tasks.py")) -> Card:
    return Card(
        id="CARD-8", title="Load a bench task", state="ready", done_text="loads",
        touches=list(touches),
        fields={"category": "feature", "new_symbols": True,
                "tests": [{"name": "test_a_valid_task_loads",
                           "file": "tests/test_bench_tasks.py"}]})


class ValidPythonScaffoldTests(unittest.TestCase):
    def test_declarations_and_raising_stubs_pass(self) -> None:
        self.assertTrue(valid_python_scaffold(MODULE))
        method = "    def size(self) -> int:\n        raise NotImplementedError()\n"
        self.assertTrue(valid_python_scaffold(method))

    def test_any_behaviour_fails(self) -> None:
        for body in (
            "def f(x):\n    return x + 1\n",
            "def f(x):\n    if x:\n        raise NotImplementedError\n    return 1\n",
            "LIMIT = compute()\n",
            "print('hi')\n",
            "class A:\n    def f(self):\n        return 2\n",
            "def f(:\n",
            "",
        ):
            with self.subTest(body=body):
                self.assertFalse(valid_python_scaffold(body))


class PlanStepTouchTests(unittest.TestCase):
    def test_a_step_naming_the_cards_test_file_keeps_the_plan(self) -> None:
        steps = [{"n": 1, "does": "load", "tests": ["test_a_valid_task_loads"],
                  "touches": ["openfarm_core/bench.py", "tests/test_bench_tasks.py"]},
                 {"n": 2, "does": "check", "tests": ["test_a_valid_task_loads"],
                  "touches": ["openfarm_core/bench.py"]}]
        mm = Micromanager(ScriptedMicromanager({"shape_and_plan": {
            "verdict": "ready", "steps": steps}}))
        # Like CARD-010: touches name only the production file.
        decision = mm.shape_and_plan(_card(touches=["openfarm_core/bench.py"]), {}, {})
        self.assertEqual([s["touches"] for s in decision.steps],
                         [["openfarm_core/bench.py"], ["openfarm_core/bench.py"]])

    def test_a_step_touching_another_file_is_still_refused(self) -> None:
        mm = Micromanager(ScriptedMicromanager({"shape_and_plan": {
            "verdict": "ready",
            "steps": [{"n": 1, "does": "x", "tests": ["test_a_valid_task_loads"],
                       "touches": ["openfarm_core/cli.py"]},
                      {"n": 2, "does": "y", "tests": ["test_a_valid_task_loads"],
                       "touches": ["openfarm_core/bench.py"]}]}}))
        with self.assertRaises(Refused):
            mm.shape_and_plan(_card(touches=["openfarm_core/bench.py"]), {}, {})


class ScaffoldContractTests(unittest.TestCase):
    def _scaffold(self, edits: list[dict]):
        mm = Micromanager(ScriptedMicromanager({"scaffold_contract": {"edits": edits}}))
        return mm.scaffold_contract(_card(), {}, {})

    def test_a_python_stub_module_is_accepted(self) -> None:
        edits = self._scaffold([{"format": "whole_file", "path": "openfarm_core/bench.py",
                                 "content": MODULE}])
        self.assertEqual([e.path for e in edits], ["openfarm_core/bench.py"])

    def test_python_logic_is_refused_with_python_words(self) -> None:
        with self.assertRaises(Refused) as caught:
            self._scaffold([{"format": "whole_file", "path": "openfarm_core/bench.py",
                             "content": "def load_task(d):\n    return d\n"}])
        self.assertIn("raise NotImplementedError", caught.exception.message)
        self.assertNotIn("todo!", caught.exception.message)

    def test_the_supervisor_is_told_the_rules(self) -> None:
        prompt = _build_op_prompt("scaffold_contract", {"card_id": "CARD-8"})
        self.assertIn("never a test file", prompt)
        self.assertIn("raise NotImplementedError", prompt)
        self.assertIn("todo!()", prompt)
        self.assertNotIn("Rules:", _build_op_prompt("coach", {}))


if __name__ == "__main__":
    unittest.main()
