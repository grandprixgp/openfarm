"""U6: parallel cards do not share one ACP supervisor conversation.

In v0.8, `run` with swarm on, `concurrent_cards > 1` and an ACP supervisor opened
one `ClaudeAcpMicromanager` for the whole run. It held one adapter and one session,
rooted at the repository, so every card's supervisor prompts and answers went into
the same conversation, and the per-card artifact numbering ran across cards.
Now the cards share the one ACP process (as the spec says), but each card has its
own session, rooted at its own worktree.
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core import cli as cli_mod  # noqa: E402
from openfarm_core.adapters.acp import ClaudeAcpAdapter  # noqa: E402
from openfarm_core.micromanager import ClaudeAcpMicromanager  # noqa: E402
from openfarm_core.state import StateDir  # noqa: E402
from tests.test_v081_cli_toy import make_toy, run_json, write_script  # noqa: E402

STAND_IN = Path(__file__).resolve().parent / "acp_stand_in.py"

ACP_SUPERVISOR = (
    '\n[micromanager]\n'
    f'binary = "{sys.executable}"\n'
    f'args = ["{STAND_IN}", "--mode", "normal", "--cost", "0.0"]\n'
    'tool_policy = "none"\n'
    'isolate_home = false\n'
    '\n[swarm]\nenabled = true\n'
)


def _acp_toy(n_cards: int, concurrent: int):
    toy, script = make_toy(n_cards, extra_config=ACP_SUPERVISOR, concurrent_cards=concurrent)
    cfg = toy.root / ".openfarm" / "config.toml"
    text = cfg.read_text()
    text = text.replace('model = "scripted"\n', 'model = "scripted"\nmicromanager = "claude-acp"\n', 1)
    cfg.write_text(text)
    toy.commit("acp supervisor")
    return toy, script


class _Spy:
    """Records adapter processes, sessions, and which session each prompt used."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.adapters: list[ClaudeAcpAdapter] = []
        self.sessions: list[tuple[int, str]] = []     # (adapter index, session cwd)
        self.calls: list[tuple[str, str, int]] = []   # (supervisor cwd, op, its session no.)
        self._session_of: dict[int, int] = {}

    def __enter__(self):
        spy = self
        real_init = ClaudeAcpAdapter.__init__
        real_new = ClaudeAcpAdapter.new_session
        real_ensure = ClaudeAcpMicromanager._ensure_session
        real_assist = ClaudeAcpMicromanager.assist

        def init(self_a, *a, **kw):
            real_init(self_a, *a, **kw)
            with spy.lock:
                spy.adapters.append(self_a)

        def new_session(self_a, *a, **kw):
            sid = real_new(self_a, *a, **kw)
            with spy.lock:
                spy.sessions.append((spy.adapters.index(self_a), str(kw.get("cwd"))))
            return sid

        def ensure(self_m):
            before = self_m._session_id
            out = real_ensure(self_m)
            if self_m._session_id != before or id(self_m) not in spy._session_of:
                with spy.lock:
                    spy._session_of[id(self_m)] = len(spy.sessions) - 1
            return out

        def assist(self_m, op, payload):
            out = real_assist(self_m, op, payload)
            with spy.lock:
                spy.calls.append((str(self_m.cwd), op, spy._session_of.get(id(self_m), -1)))
            return out

        self._patches = [
            mock.patch.object(ClaudeAcpAdapter, "__init__", init),
            mock.patch.object(ClaudeAcpAdapter, "new_session", new_session),
            mock.patch.object(ClaudeAcpMicromanager, "_ensure_session", ensure),
            mock.patch.object(ClaudeAcpMicromanager, "assist", assist),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc) -> None:
        for p in reversed(self._patches):
            p.stop()


class ParallelAcpSupervisorTests(unittest.TestCase):
    def test_each_card_has_its_own_session_rooted_at_its_worktree(self) -> None:
        toy, script = _acp_toy(2, concurrent=2)
        self.addCleanup(toy.cleanup)
        with _Spy() as spy:
            code, payload, err = run_json(toy.root, "run", "--cards", "2",
                                          "--script", str(write_script(script)))
        self.assertEqual(code, 0, err[-1500:])
        states = {o.get("card_id") or o.get("card"): o.get("final_state")
                  for o in payload["outcomes"]}
        self.assertEqual(states, {"CARD-1": "in-review", "CARD-2": "in-review"}, payload)
        run_id = payload["run_id"]

        # One ACP process for the run...
        self.assertEqual(len(spy.adapters), 1, spy.sessions)
        # ...with one session per card, each rooted at that card's own worktree.
        worktrees = {f"CARD-{n}": str((StateDir(toy.state_dir).worktrees / f"CARD-{n}-{run_id}").resolve())
                     for n in (1, 2)}
        self.assertEqual(sorted(cwd for _a, cwd in spy.sessions), sorted(worktrees.values()))
        # Every supervisor call ran in its own card's session, at its own worktree.
        self.assertTrue(spy.calls)
        by_card: dict[str, set[int]] = {}
        for cwd, _op, session_no in spy.calls:
            card = next(c for c, wt in worktrees.items() if wt == cwd)
            by_card.setdefault(card, set()).add(session_no)
            self.assertEqual(spy.sessions[session_no][1], cwd)
        self.assertEqual(set(by_card), {"CARD-1", "CARD-2"})
        self.assertTrue(by_card["CARD-1"].isdisjoint(by_card["CARD-2"]), by_card)

        # Each card's supervisor artifacts are numbered from 01 on their own.
        for n in (1, 2):
            folder = StateDir(toy.state_dir).card_dir(f"CARD-{n}", run_id) / "micromanager"
            names = sorted(p.name for p in folder.glob("*.json"))
            self.assertTrue(names and names[0].startswith("01-"), (n, names))

        # The shared night budget still counts every card's supervisor tokens.
        self.assertEqual(payload["tokens"]["supervisor"], (120 + 45) * len(spy.calls))

    def test_one_card_at_a_time_is_unchanged(self) -> None:
        # Without parallel cards each card opens its own supervisor, as in v0.7.
        toy, script = _acp_toy(2, concurrent=1)
        self.addCleanup(toy.cleanup)
        with _Spy() as spy:
            code, payload, err = run_json(toy.root, "run", "--cards", "2",
                                          "--script", str(write_script(script)))
        self.assertEqual(code, 0, err[-1500:])
        self.assertEqual(len(spy.adapters), 2)
        self.assertEqual(len(spy.sessions), 2)


class SharedSeatTests(unittest.TestCase):
    def setUp(self) -> None:
        import shutil
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v081-seat-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        proto = ClaudeAcpMicromanager(
            command=[sys.executable, str(STAND_IN), "--mode", "normal"],
            cwd=self.tmp, isolate_home=False, tool_policy="none", timeout_s=30.0)
        self.seat = cli_mod._SharedAcpSupervisor(proto)
        self.addCleanup(self.seat.close)
        self.cards = []
        for n in (1, 2):
            wt = self.tmp / f"wt-{n}"
            wt.mkdir()
            self.cards.append(self.seat.for_card(wt, self.tmp / f"card-{n}"))

    def test_cards_asking_at_once_take_turns_on_the_one_process(self) -> None:
        errors: list[BaseException] = []
        start = threading.Barrier(2)

        def ask(sup) -> None:
            try:
                start.wait(5)
                for _ in range(3):
                    sup.assist("shape_and_plan", {"card": {"id": "X"}})
            except BaseException as exc:  # the test reports whatever a thread hit
                errors.append(exc)

        threads = [threading.Thread(target=ask, args=(c,)) for c in self.cards]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        adapter = self.seat.adapter
        self.assertIsNotNone(adapter)
        self.assertTrue(all(c._adapter is adapter for c in self.cards))
        for n, card in enumerate(self.cards, 1):
            names = sorted(p.name for p in (self.tmp / f"card-{n}" / "micromanager").glob("*.json"))
            self.assertEqual(names, ["01-shape_and_plan.json", "02-shape_and_plan.json",
                                     "03-shape_and_plan.json"])

    def test_a_dead_process_is_replaced_once_for_every_card(self) -> None:
        a, b = self.cards
        a.assist("shape_and_plan", {"card": {"id": "A"}})
        first = self.seat.adapter
        first._proc.kill()
        first._proc.wait(10)
        b.assist("shape_and_plan", {"card": {"id": "B"}})
        a.assist("shape_and_plan", {"card": {"id": "A"}})
        second = self.seat.adapter
        self.assertIsNot(second, first)
        self.assertIs(a._adapter, second)
        self.assertIs(b._adapter, second)
        # Closing a card ends its session only; the run's process lives on.
        a.close()
        self.assertIsNone(second._proc.poll())
        self.seat.close()
        self.assertIsNone(self.seat.adapter)


if __name__ == "__main__":
    unittest.main()
