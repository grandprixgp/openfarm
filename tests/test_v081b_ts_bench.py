"""v0.8.1b ts, T7: bench scoring.

A TypeScript task passes only when its hidden test files really ran and reported
passing tests, read with the pack's parser. Relative `--out`/`--dir` work, and a
run that never started is an error with its reason, never a failed task.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core import bench
from openfarm_core.bench import load_task, render_report, run_bench, run_once, score

ROOT = Path(__file__).resolve().parent.parent
OPENFARM = ROOT / "openfarm"
NODE = shutil.which("node")


def _tmp(owner: unittest.TestCase) -> Path:
    root = Path(tempfile.mkdtemp(prefix="openfarm-tsb-", dir=os.environ.get("TMPDIR")))
    owner.addCleanup(shutil.rmtree, root, True)
    return root


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(text), encoding="utf-8")


def _record(task: str, arm: str, passed: bool, **extra) -> dict:
    record = {"task": task, "language": "python", "category": "pinned-bugfix",
              "arm": arm, "repeat": 1, "passed": passed, "spend": 0.0, "tokens": 10,
              "seconds": 1.0, "diff_lines": 2, "final_state": "in-review",
              "stop_code": "", "run_dir": "x"}
    record.update(extra)
    return record


CLAMP = """\
export function clamp(value: number, low: number, high: number): number {
  if (value < low) {
    return low;
  }
  return value;
}
"""

FIX = """\
=== search_replace: src/clamp.ts
<<< SEARCH
  return value;
>>> REPLACE
  if (value > high) {
    return high;
  }
  return value;
"""

WRITTEN_TEST = """\
=== whole_file: tests/clamp.test.ts
import test from "node:test";
import assert from "node:assert/strict";
import { clamp } from "../src/clamp.ts";

test("a value above high is clamped", () => {
  assert.equal(clamp(9, 0, 5), 5);
});
"""

CARD = """\
+++
id = "CARD-1"
title = "clamp keeps a value at most high"
state = "ready"
category = "pinned-bugfix"
unit = "."
touches = ["src/clamp.ts"]
depends = []

[[tests]]
name = "a value above high is clamped"
file = "tests/clamp.test.ts"
asserts = "clamp(9, 0, 5) == 5"
+++

## Done

- `clamp(9, 0, 5)` returns `5`.
"""

HIDDEN_PASSING = """\
import test from "node:test";
import assert from "node:assert/strict";
import { clamp } from "../src/clamp.ts";

test("hidden: high is the ceiling", () => {
  assert.equal(clamp(100, 0, 5), 5);
});
"""

HIDDEN_FAILING = """\
import test from "node:test";
import assert from "node:assert/strict";
import { clamp } from "../src/clamp.ts";

test("hidden: low and high swapped are refused", () => {
  assert.throws(() => clamp(1, 5, 0));
});
"""

HIDDEN_ALL_SKIPPED = """\
import test from "node:test";

test.skip("hidden: nothing really runs", () => {});
"""

HIDDEN_EXITS_EARLY = """\
import test from "node:test";
import assert from "node:assert/strict";
import { clamp } from "../src/clamp.ts";

test("hidden: never reported", () => {
  assert.equal(clamp(1, 5, 0), 0);
});
process.exit(0);
"""


def _ts_task(owner: unittest.TestCase, hidden: dict[str, str]) -> Path:
    """A TypeScript bench task whose scripted card fixes `clamp`."""
    task = _tmp(owner) / "ts-clamp"
    _write(task, {
        "task.toml": textwrap.dedent(f"""\
            id = "ts-clamp"
            language = "typescript"
            category = "pinned-bugfix"
            card = "card.md"
            hidden_tests = {json.dumps(sorted("hidden/" + rel for rel in hidden))}
        """),
        "card.md": CARD,
        "repo/package.json": '{"name": "clamp", "type": "module"}\n',
        "repo/.gitignore": "node_modules/\n",
        "repo/src/clamp.ts": CLAMP,
        "repo/tests/base.test.ts": textwrap.dedent("""\
            import test from "node:test";
            import assert from "node:assert/strict";
            import { clamp } from "../src/clamp.ts";

            test("a value below low is raised", () => {
              assert.equal(clamp(-1, 0, 5), 0);
            });
        """),
        **{"hidden/" + rel: text for rel, text in hidden.items()},
    })
    (task / "script.json").write_text(json.dumps({
        "CARD-1/test_writer": WRITTEN_TEST,
        "CARD-1/implementer": FIX,
        "CARD-1/checker_pass1": "RESTATEMENT: clamp keeps a value at most high",
        "CARD-1/checker_pass2": "{}",
    }))
    return task


class NeverStartedTests(unittest.TestCase):
    def test_an_error_record_is_listed_and_not_scored(self) -> None:
        records = [_record("a", "farm", True),
                   _record("b", "farm", False, final_state="", stop_code="openfarm-exit-3",
                           error="config error: no config at /x | y")]
        summary = score(records)
        self.assertEqual(summary["arms"]["farm"]["runs"], 1)
        self.assertEqual(summary["arms"]["farm"]["pass_rate"], 1.0)
        self.assertEqual(summary["categories"]["pinned-bugfix"]["farm"], 1.0)
        self.assertEqual(summary["errors"], [
            {"task": "b", "arm": "farm", "repeat": 1,
             "error": "config error: no config at /x | y"}])
        text = render_report(summary)
        self.assertIn("## Runs that did not start", text)
        self.assertIn("| b | farm | 1 | config error: no config at /x \\| y |", text)

    def test_a_summary_without_errors_keeps_its_shape(self) -> None:
        self.assertNotIn("errors", score([_record("a", "farm", True)]))

    def test_a_child_that_refuses_is_an_error_with_its_reason(self) -> None:
        task = load_task(ROOT / "bench" / "py-slug-ends")
        refusal = ('import sys; print(\'{"error": "no config at /nowhere", '
                   '"kind": "config error"}\'); sys.exit(3)')
        with mock.patch.object(bench, "_openfarm_cmd",
                               return_value=[sys.executable, "-c", refusal]):
            record = run_once(task, "farm", None, 1, _tmp(self), dry_run=True)
        self.assertEqual(record["error"], "config error: no config at /nowhere")
        self.assertEqual(record["stop_code"], "openfarm-exit-3")
        self.assertFalse(record["passed"])

    def test_a_child_that_crashes_is_an_error_with_its_output(self) -> None:
        task = load_task(ROOT / "bench" / "py-slug-ends")
        crash = "import sys; sys.stderr.write('Traceback\\nKeyError: x\\n'); sys.exit(1)"
        with mock.patch.object(bench, "_openfarm_cmd",
                               return_value=[sys.executable, "-c", crash]):
            out = _tmp(self) / "out"
            report = run_bench([task], {"farm": None}, 1, out, dry_run=True)
        record = report["records"][0]
        self.assertEqual(record["error"], "openfarm exited 1: Traceback | KeyError: x")
        self.assertEqual(report["summary"]["arms"], {})
        self.assertIn("KeyError: x", (out / "bench-report.md").read_text())


class RelativePathTests(unittest.TestCase):
    def test_relative_out_and_tasks_dir_work(self) -> None:
        work = _tmp(self)
        shutil.copytree(ROOT / "bench" / "py-slug-ends", work / "tasks" / "py-slug-ends")
        res = subprocess.run(
            [sys.executable, str(OPENFARM), "--json", "bench", "run", "--dry-run",
             "--tasks", "tasks", "--out", "rel-out"],
            cwd=work, capture_output=True, text=True, timeout=900)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        records = [json.loads(line) for line in
                   (work / "rel-out" / "records.jsonl").read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["final_state"], "in-review", records[0])
        self.assertTrue(records[0]["passed"], records[0])
        self.assertNotIn("error", records[0])
        self.assertTrue(Path(records[0]["run_dir"]).is_absolute())


@unittest.skipUnless(NODE and shutil.which("git"), "needs node and git")
class TypeScriptScoringTests(unittest.TestCase):
    def _run(self, hidden: dict[str, str]) -> dict:
        task = load_task(_ts_task(self, hidden))
        record = run_once(task, "farm", None, 1, _tmp(self), dry_run=True)
        self.assertEqual(record["final_state"], "in-review",
                         f"{record['stop_code']}: {record['run_dir']}")
        return record

    def test_passing_hidden_spec_file_passes(self) -> None:
        record = self._run({"tests/clamp.spec.ts": HIDDEN_PASSING})
        self.assertTrue(record["passed"], record.get("hidden_detail"))

    def test_a_failing_hidden_test_fails_the_task(self) -> None:
        # Bare `node --test` never ran *.spec.ts and called this a pass.
        record = self._run({"tests/clamp.spec.ts": HIDDEN_FAILING})
        self.assertFalse(record["passed"])
        log = (Path(record["run_dir"]) / "hidden.log").read_text()
        self.assertIn("hidden: low and high swapped are refused", log)

    def test_a_failing_hidden_test_under_dunder_tests_fails_the_task(self) -> None:
        record = self._run({"__tests__/clamp.ts": HIDDEN_FAILING})
        self.assertFalse(record["passed"])

    def test_a_hidden_file_that_runs_no_test_fails_the_task(self) -> None:
        record = self._run({"tests/skipped.test.ts": HIDDEN_ALL_SKIPPED})
        self.assertFalse(record["passed"])
        self.assertIn("reported no passing test", record["hidden_detail"])

    def test_a_hidden_file_that_exits_early_fails_the_task(self) -> None:
        record = self._run({"tests/early.test.ts": HIDDEN_EXITS_EARLY})
        self.assertFalse(record["passed"])

    def test_a_passing_hidden_file_beside_a_skipped_one_still_fails(self) -> None:
        record = self._run({"tests/clamp.spec.ts": HIDDEN_PASSING,
                            "tests/skipped.test.ts": HIDDEN_ALL_SKIPPED})
        self.assertFalse(record["passed"])


if __name__ == "__main__":
    unittest.main()
