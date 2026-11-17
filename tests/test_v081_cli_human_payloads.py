"""U1: every human formatter handles the payload its real command produces.

The v0.8 human `run` summary crashed with `KeyError: 'card'` because real card
outcomes carry `card_id` (only dry-run and skipped records carry `card`), and the
tests only fed hand-made dictionaries. Here every payload is captured from a real
command, run in-process against a toy repo and the scripted backend: the human
run records exactly what the CLI handed its formatter, and a twin `--json` run
gives the exit code to compare against.
"""

from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core import human_cli  # noqa: E402
from openfarm_core.human_cli import Style, format_human, format_human_strict  # noqa: E402
from tests.test_v081_cli_toy import (  # noqa: E402
    SWARM_ON,
    make_toy,
    run_cli,
    run_human,
    run_json,
    write_script,
)

ROOT = Path(__file__).resolve().parent.parent
PLAIN = Style(enabled=False)


def _driver_script(card: str) -> dict:
    return {
        "driver/plan": {"actions": [{"action": "start", "card": card}], "answers": []},
        "driver/question/shape_and_plan": {
            "actions": [],
            "answers": [{"seq": "auto", "data": {"verdict": "ready", "clarified_notes": "ok"}}],
        },
        "driver/question/pre_review": {
            "actions": [],
            "answers": [{"seq": "auto", "data": {"verdict": "approve", "reviewer_brief": "ok"}}],
        },
        "driver/question/reflect_harness": {
            "actions": [], "answers": [{"seq": "auto", "data": {"lessons": []}}],
        },
        f"driver/wave/{card}": {"verdict": "clean", "note": "Scripted clean review",
                                "review_tokens": 111},
    }


class _Captured:
    """One command run both ways: the human run's payloads and both exit codes."""

    def __init__(self, label: str, human_code: int, json_code: int, out: str, err: str,
                 seen: list[tuple[str, dict]], json_payload: dict) -> None:
        self.label = label
        self.human_code = human_code
        self.json_code = json_code
        self.out = out
        self.err = err
        self.seen = seen
        self.json_payload = json_payload


class RealPayloadFormattingTests(unittest.TestCase):
    """All captured once in `setUpClass`; each test reads the captures."""

    captured: dict[str, _Captured] = {}
    toys: list = []

    @classmethod
    def _both(cls, label: str, human_repo: Path, json_repo: Path, *argv: str,
              json_argv: tuple[str, ...] | None = None) -> _Captured:
        h_code, out, err, seen = run_human(human_repo, *argv)
        j_code, payload, _ = run_json(json_repo, *(json_argv if json_argv is not None else argv))
        cap = _Captured(label, h_code, j_code, out, err, seen, payload)
        cls.captured[label] = cap
        return cap

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="of-v081-human-"))
        bench_dir = ROOT / "bench"
        for label, swarm in (("off", ""), ("on", SWARM_ON)):
            # Twin toys: a run mutates state, so the human and JSON runs each get one.
            twins = []
            for _ in range(2):
                toy, script = make_toy(3, extra_config=swarm)
                cls.toys.append(toy)
                script["CARD-2/implementer"] = "<<<HANDBACK\nnot sure\n>>>"
                twins.append((toy, write_script(script, cls.tmp / f"s-{label}-{len(cls.toys)}")))
            (h_toy, h_sp), (j_toy, j_sp) = twins
            h, j = h_toy.root, j_toy.root
            cls._both(f"run-dry-{label}", h, j, "run", "--dry-run", "--cards", "3")
            cls._both(f"run-skipped-{label}", h, j, "run", "--cards", "3", "--for", "0",
                      "--script", str(h_sp))
            # One real run in each twin (each with its own script path).
            h_code, out, err, seen = run_human(h, "run", "--cards", "3", "--script", str(h_sp))
            j_code, payload, _ = run_json(j, "run", "--cards", "3", "--script", str(j_sp))
            cls.captured[f"run-real-{label}"] = _Captured(
                f"run-real-{label}", h_code, j_code, out, err, seen, payload)
            rid_h = (seen[-1][1] if seen else {}).get("run_id", "")
            rid_j = payload.get("run_id", "")
            cls._both(f"status-{label}", h, j, "status")
            cls._both(f"status-card-{label}", h, j, "status", "--card", "CARD-1")
            cls._both(f"check-{label}", h, j, "check", "CARD-3")
            cls._both(f"check-baseline-{label}", h, j, "check", "CARD-3", "--baseline")
            cls._both(f"dossier-missing-{label}", h, j, "dossier", "CARD-3")
            cls._both(f"dossier-attempt-{label}", h, j, "dossier", "CARD-1", "--attempt", "99")
            # The success path prints the dossier's markdown, not a format_human kind.
            cls._both(f"dossier-{label}", h, j, "dossier", "CARD-1")
            cls._both(f"verdict-{label}", h, j, "verdict", "CARD-1", "clean",
                      "--review-tokens", "100")
            cls._both(f"escape-{label}", h, j, "escape", "CARD-1", "--note", "later")
            cls._both(f"release-{label}", h, j, "release", "CARD-2", "--reason", "retry")
            cls._both(f"done-{label}", h, j, "done", "CARD-1")
            cls._both(f"harness-{label}", h, j, "harness", "status")
            cls._both(f"harness-index-{label}", h, j, "harness", "index")
            cls._both(f"swarm-bare-{label}", h, j, "swarm")
            cls._both(f"swarm-tiers-{label}", h, j, "swarm", "tiers")
            if label == "on":
                # Each twin has its own run id: they match only if both runs
                # started in the same second.
                for name, tail in (("swarm-status", ("swarm", "status")),
                                   ("board-read", ("board", "read")),
                                   ("board-status", ("board", "read"))):
                    extra = {"board-read": ("--as", "*"), "board-status": ("--status",)}.get(name, ())
                    cls._both(name, h, j, *tail, "--run", rid_h, *extra,
                              json_argv=(*tail, "--run", rid_j, *extra))
            cls._both(f"pause-{label}", h, j, "pause", "--reason", "x")
            cls._both(f"run-paused-{label}", h, j, "run", "--cards", "1")
            cls._both(f"resume-{label}", h, j, "resume")
            cls._both(f"stop-{label}", h, j, "stop", "--reason", "y")
            cls._both(f"resume-kill-refused-{label}", h, j, "resume")
            cls._both(f"resume-clear-{label}", h, j, "resume", "--clear-kill")

        # `drive` with a scripted driver holding both seats.
        twins = []
        for _ in range(2):
            toy, script = make_toy(1, extra_config=SWARM_ON)
            cls.toys.append(toy)
            script.update(_driver_script("CARD-1"))
            twins.append((toy, write_script(script, cls.tmp / f"d-{len(cls.toys)}")))
        (h_toy, h_sp), (j_toy, j_sp) = twins
        h_code, out, err, seen = run_human(h_toy.root, "drive", "--agent", "claude", "--seat",
                                           "both", "--card", "CARD-1", "--wave",
                                           "--script", str(h_sp))
        j_code, payload, _ = run_json(j_toy.root, "drive", "--agent", "claude", "--seat",
                                      "both", "--card", "CARD-1", "--wave",
                                      "--script", str(j_sp))
        cls.captured["drive"] = _Captured("drive", h_code, j_code, out, err, seen, payload)

        # `bench`: list and a dry run against the repo's own tasks.
        any_repo = cls.toys[0].root
        cls._both("bench-list", any_repo, any_repo, "bench", "list", "--dir", str(bench_dir))
        h_code, out, err, seen = run_human(
            any_repo, "bench", "run", "--dry-run", "--task", "py-bounded-full",
            "--dir", str(bench_dir), "--out", str(cls.tmp / "bench-h"))
        j_code, payload, _ = run_json(
            any_repo, "bench", "run", "--dry-run", "--task", "py-bounded-full",
            "--dir", str(bench_dir), "--out", str(cls.tmp / "bench-j"))
        cls.captured["bench-run"] = _Captured("bench-run", h_code, j_code, out, err, seen, payload)

    @classmethod
    def tearDownClass(cls) -> None:
        for toy in cls.toys:
            toy.cleanup()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- helpers -------------------------------------------------------------

    def _payloads(self):
        for label, cap in self.captured.items():
            for kind, payload in cap.seen:
                yield label, kind, payload
            if cap.json_payload and not cap.json_payload.get("_raw"):
                kind = cap.seen[-1][0] if cap.seen else ""
                if kind:
                    yield f"{label}[json]", kind, cap.json_payload

    # -- the tests -----------------------------------------------------------

    def test_real_run_human_summary_prints_and_names_every_card(self) -> None:
        for label in ("run-real-off", "run-real-on"):
            with self.subTest(label=label):
                cap = self.captured[label]
                self.assertNotIn("Traceback", cap.err)
                self.assertEqual(cap.human_code, cap.json_code, cap.err[-800:])
                self.assertEqual(cap.human_code, 0, cap.err[-800:])
                self.assertIn("Outcomes (3)", cap.out)
                kind, payload = cap.seen[-1]
                self.assertEqual(kind, "run")
                outcomes = payload["outcomes"]
                self.assertTrue(all("card_id" in o and "card" not in o for o in outcomes),
                                "a real run's outcomes carry card_id")
                states = {o["card_id"]: o["final_state"] for o in outcomes}
                self.assertEqual(states, {"CARD-1": "in-review", "CARD-2": "blocked",
                                          "CARD-3": "in-review"})
                rows = [ln for ln in cap.out.splitlines()
                        if "in-review" in ln or "blocked" in ln]
                for cid in ("CARD-1", "CARD-2", "CARD-3"):
                    self.assertTrue(any(ln.strip().startswith(cid) for ln in rows),
                                    f"{cid} missing from the CARD column:\n{cap.out}")
                self.assertIn("openfarm dossier CARD-1", cap.out)
                self.assertIn("openfarm dossier CARD-2", cap.out)

    def test_skipped_and_dry_run_rows_name_their_card(self) -> None:
        for label in ("run-skipped-off", "run-skipped-on", "run-dry-off", "run-dry-on"):
            with self.subTest(label=label):
                cap = self.captured[label]
                self.assertEqual(cap.human_code, cap.json_code)
                kind, payload = cap.seen[-1]
                self.assertEqual(kind, "run")
                self.assertEqual(len(payload["outcomes"]), 3)
                rendered = format_human_strict("run", payload, style=PLAIN)
                for cid in ("CARD-1", "CARD-2", "CARD-3"):
                    self.assertTrue(
                        any(ln.strip().startswith(cid) for ln in rendered.splitlines()),
                        f"{cid} missing:\n{rendered}")
                if label.startswith("run-skipped"):
                    self.assertIn("skipped", rendered)

    def test_every_command_exits_the_same_with_and_without_json(self) -> None:
        for label, cap in self.captured.items():
            with self.subTest(label=label):
                self.assertNotIn("Traceback", cap.err, cap.err[-1500:])
                self.assertNotIn("could not format", cap.err, cap.err[-800:])
                self.assertTrue(cap.out.strip(), f"{label} printed nothing")
                if label.startswith("swarm-bare"):
                    # Bare `swarm` shows an overview for people; with --json it is a
                    # usage error by design (as for bare `bench`).
                    continue
                self.assertEqual(cap.human_code, cap.json_code,
                                 f"{label}: human {cap.human_code} vs json {cap.json_code}\n"
                                 f"{cap.err[-800:]}")

    def test_every_captured_payload_formats_strictly(self) -> None:
        kinds = set()
        for label, kind, payload in self._payloads():
            kinds.add(kind)
            with self.subTest(label=label, kind=kind):
                text = format_human_strict(kind, payload, style=PLAIN)
                self.assertTrue(text.strip())
        # The brief's minimum coverage, as the CLI names the kinds.
        for wanted in ("run", "status", "check", "swarm_status", "board_read",
                       "board_status", "dossier", "drive", "bench_list"):
            self.assertIn(wanted, kinds)

    def test_no_formatter_raises_on_a_missing_key(self) -> None:
        for label, kind, payload in self._payloads():
            for key in list(payload):
                trimmed = {k: v for k, v in payload.items() if k != key}
                with self.subTest(label=label, kind=kind, missing=key):
                    format_human_strict(kind, trimmed, style=PLAIN)
            for list_key in ("outcomes", "reviews", "posts", "members", "roster", "history"):
                items = payload.get(list_key)
                if not isinstance(items, list) or not items or not isinstance(items[0], dict):
                    continue
                for key in list(items[0]):
                    trimmed = copy.deepcopy(payload)
                    for item in trimmed[list_key]:
                        if isinstance(item, dict):
                            item.pop(key, None)
                    with self.subTest(label=label, kind=kind, missing=f"{list_key}[].{key}"):
                        format_human_strict(kind, trimmed, style=PLAIN)

    def test_drive_rows_name_their_card(self) -> None:
        cap = self.captured["drive"]
        self.assertEqual(cap.human_code, 0, cap.err[-800:])
        rows = [ln for ln in cap.out.splitlines() if "in-review" in ln]
        self.assertTrue(rows and rows[0].strip().startswith("CARD-1"), cap.out)
        self.assertIn("Wave Reviews (1)", cap.out)
        review_rows = [ln for ln in cap.out.splitlines() if "Scripted clean review" in ln]
        self.assertTrue(review_rows and review_rows[0].strip().startswith("CARD-1"), cap.out)

    def test_bench_human_output(self) -> None:
        cap = self.captured["bench-run"]
        self.assertEqual(cap.human_code, 0, cap.err[-800:])
        self.assertIn("# openfarm bench report", cap.out)
        self.assertIn("report:", cap.out)
        self.assertIn("summary", cap.json_payload)
        listing = self.captured["bench-list"]
        self.assertIn("py-bounded-full", listing.out)


class FormatterFailureFallbackTests(unittest.TestCase):
    """A formatter bug prints the JSON and keeps the command's own exit code."""

    def test_format_human_falls_back_to_json_on_error(self) -> None:
        payload = {"outcomes": [{"card_id": "CARD-9", "final_state": "in-review"}]}

        def boom(*_a, **_k):
            raise KeyError("card")

        with mock.patch.object(human_cli, "_format_run", boom):
            err = __import__("io").StringIO()
            with mock.patch.object(sys, "stderr", err):
                text = format_human("run", payload, style=PLAIN)
        self.assertEqual(json.loads(text), payload)
        self.assertIn("could not format", err.getvalue())

    def test_a_formatter_error_never_changes_the_exit_code(self) -> None:
        toy, _script = make_toy(1)
        self.addCleanup(toy.cleanup)

        def boom(*_a, **_k):
            raise RuntimeError("formatter bug")

        with mock.patch.object(human_cli, "_format_status", boom):
            code, out, err = run_cli("--repo", str(toy.root), "status")
        self.assertEqual(code, 0, err)
        self.assertIn("formatter bug", err)
        self.assertIn("spend_cap", json.loads(out))


if __name__ == "__main__":
    unittest.main()
