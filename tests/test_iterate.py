"""R2.5: iteration is feedback on the current goal, not a new project."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openfarm_core.adapters.files import parse_card_file
from openfarm_core.automode import load_project_chunks, new_feature, next_goal, save_project_chunks, something_wrong
from openfarm_core.bootstrap import initialize_project
from openfarm_core.config import load_config
from openfarm_core.errors import Refused
from openfarm_core.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "dogfood-w1"
IDEA = "A website showing Wikimedia's image of the day, themed from the photo."
REQUEST = "I only see the photo, make the information visible"

FIX_CARDS = {
    "cards/W1-05.md": '''+++
id = "W1-05"
title = "Lift the information above the photo"
state = "ready"
category = "small-feature"
wave = 1
touches = ["src/client/style.css"]
reference = "src/client/style.css"
depends = []
tests = [{name = "stylesheet lifts the text above the photo", file = "tests/style.test.ts", asserts = "each text selector has z-index 1"}]
+++

## Done

- `src/client/style.css` gives `#date`, `#title`, `#story` and `#credit` the rules `position: relative` and `z-index: 1`, so they sit above `#art`.
- `tests/style.test.ts` reads the stylesheet and asserts each of the four selectors has `z-index: 1`.
''',
}


class IterationTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"OPENFARM_PROJECTS_FILE": str(base / "projects.json")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = base / "iotd"
        initialize_project("iotd", directory=self.root, idea=IDEA, language="typescript",
                           state_dir=base / "state", secrets_dir=base / "secrets", commit=True)
        for rel in ("src/client/style.css", "src/client/main.ts", "src/client/sample.ts", "src/client/apply.ts",
                    "src/feed.ts", "src/proxy.ts", "src/router.ts", "src/server.ts", "src/shared/theme.ts",
                    "src/shared/palette.ts", "tests/slice.test.ts"):
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text("/* built */\n" if rel.endswith(".css") else "export const built = 1;\n")
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        "commit", "-q", "-m", "the built app"], check=True)
        self.cfg = load_config(self.root)
        self.store = Store(self.root, self.cfg)
        self.store.log_decision("idea", IDEA)
        save_project_chunks(self.root, [
            {"id": "C1", "title": "Living image", "goal": "Show the photo with its title, story and credit.", "status": "active"},
            {"id": "C2", "title": "Threads", "goal": "Explore related images.", "status": "pending"}])
        for path in sorted(FIXTURES.glob("W1-*.md")):  # the four tasks of the real run, all built
            header, body = parse_card_file(path.read_text())
            self.store.src._write(path.stem, dict(header, state="done"), body, f"{path.stem}: built")
        self.before = {cid: self.store.read(cid) for cid in ("W1-01", "W1-02", "W1-03", "W1-04")}
        self.prompts: list[str] = []

    def _architect(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return json.dumps({"files": FIX_CARDS})

    def test_feedback_becomes_new_tasks_under_the_same_goal(self) -> None:
        plan = something_wrong(self.root, REQUEST, backend=self._architect)

        self.assertEqual(plan["new_cards"], ["W1-05"])  # a new ID under goal 1
        self.assertEqual(plan["retired_cards"], [])
        header, _body = self.store.read("W1-05")
        self.assertEqual((header["wave"], header["state"]), (1, "ready"))
        self.assertTrue(plan["eligibility_ok"], plan["cards"])
        self.assertEqual(self.store.src.next()["cards"], ["W1-05"])  # and it can run

        for cid, was in self.before.items():  # finished tasks are untouched
            self.assertEqual(self.store.read(cid), was, cid)
        log = self.store.read_doc("decisions")
        self.assertLess(log.index(IDEA), log.index(REQUEST))  # the idea is still first
        self.assertIn("· Something's wrong", log)
        self.assertEqual(log.count(IDEA), 1)
        self.assertEqual([c["status"] for c in load_project_chunks(self.root)], ["active", "pending"])

        prompt = self.prompts[0]
        self.assertIn(REQUEST, prompt)
        self.assertIn("goal C1 (Living image)", prompt)
        self.assertIn("`cards/W1-05.md`", prompt)
        self.assertIn("must not be re-issued or rewritten", prompt)
        self.assertIn("W1-03", prompt)

    def test_feedback_needs_words_and_an_architect(self) -> None:
        with self.assertRaises(Refused):
            something_wrong(self.root, "  ", backend=self._architect)
        with self.assertRaises(Refused) as caught:
            something_wrong(self.root, REQUEST)
        self.assertEqual(caught.exception.code, "architect-needed")
        self.assertNotIn(REQUEST, self.store.read_doc("decisions"))

    def test_next_goal_moves_on_in_the_spec(self) -> None:
        self.assertEqual(next_goal(self.root)["id"], "C2")
        self.assertEqual([c["status"] for c in load_project_chunks(self.root)], ["done", "active"])
        self.assertIsNone(next_goal(self.root))

    def test_a_new_feature_is_a_new_goal_added_to_the_spec_and_the_log(self) -> None:
        goal = new_feature(self.root, "Let people share a day's image.\nA share button copies the link.")
        self.assertEqual((goal["id"], goal["title"], goal["status"]), ("C3", "Let people share a day's image", "pending"))
        self.assertEqual([c["id"] for c in load_project_chunks(self.root)], ["C1", "C2", "C3"])
        self.assertIn("## Goal C3: Let people share a day's image", self.store.read_doc("spec"))
        log = self.store.read_doc("decisions")
        self.assertIn("· New feature", log)
        self.assertLess(log.index(IDEA), log.index("A share button copies the link."))


if __name__ == "__main__":
    unittest.main()
