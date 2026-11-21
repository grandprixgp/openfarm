"""R2.4: Try it runs the app and says what a person would actually see."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from openfarm_core.config import load_config
from openfarm_core.tryit import find_browser, fix_request, goal_words, how_to_start, try_it
from tests.test_store import _Project

SERVER = '''import http.server, os, pathlib
PAGE = pathlib.Path(__file__).with_name("page.html").read_bytes()
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(PAGE)
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
'''
PIXEL = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
# The test project's real fault: the photo is fixed over the whole screen and the text sits under it.
COVERED = f'''<!doctype html><title>t</title><style>img#art{{position:fixed;inset:0;width:100%;height:100%;z-index:0}}</style>
<img id="art" src="{PIXEL}"><h1 id="title">Glacier</h1><p id="story">A long story.</p><small id="credit">Ann, CC BY</small>'''
SHOWN = COVERED.replace("</style>", "#title,#story,#credit{position:relative;z-index:1;color:#fff;background:#000}</style>")
GOAL = {"id": "C1", "title": "Living image", "goal": "Show the photo full-bleed with its title, story and credit."}


class WordsTests(unittest.TestCase):
    def test_the_goal_text_says_what_to_look_for(self) -> None:
        words = goal_words(GOAL["goal"])
        for word in ("title", "story", "credit"):
            self.assertIn(word, words)
        self.assertNotIn("show", words)
        self.assertNotIn("with", words)

    def test_failed_checks_become_a_request_for_fix_tasks(self) -> None:
        text = fix_request({"checks": [{"name": "loads", "ok": True, "detail": "the page answered 200"},
                                       {"name": "seen", "ok": False, "detail": "title is on the page but can't be seen"}]})
        self.assertIn("- title is on the page but can't be seen", text)
        self.assertNotIn("answered 200", text)
        self.assertEqual(fix_request({"checks": [{"name": "loads", "ok": True, "detail": "x"}]}), "")


class TryItTests(_Project):
    def _configure(self, table: str) -> None:
        path = self.root / ".openfarm" / "config.toml"
        path.write_text(path.read_text() + "\n[try]\n" + table + "\n")
        self.cfg = load_config(self.root)

    def _serve(self, page: str) -> None:
        (self.root / "serve.py").write_text(SERVER)
        (self.root / "page.html").write_text(page)
        self._configure('start = "python3 serve.py"')

    def test_the_config_says_how_to_start_the_app(self) -> None:
        self.assertEqual(how_to_start(self.root, self.cfg), {})  # this pack can't tell for a library
        self._configure('start = "python3 serve.py"\npath = "/today"')
        plan = how_to_start(self.root, self.cfg)
        self.assertEqual((plan["kind"], plan["start"], plan["path"]), ("web", ["python3", "serve.py"], "/today"))

    def test_nothing_to_start_is_said_plainly_and_is_not_a_failure(self) -> None:
        result = try_it(self.root, self.cfg, GOAL)
        self.assertIsNone(result["ok"])
        self.assertIn("doesn't know how to start this app", result["checks"][0]["detail"])

    def test_a_command_line_tool_runs_its_sample(self) -> None:
        self._configure('''sample = "python3 -c 'print(6 * 7)'"''')
        result = try_it(self.root, self.cfg, GOAL)
        self.assertTrue(result["ok"])
        self.assertEqual(result["output"], "42")
        self.assertIn("python3 -c", result["command"])

    @unittest.skipUnless(find_browser(), "no headless browser on this machine")
    def test_content_under_a_full_screen_photo_is_present_but_not_visible(self) -> None:
        self._serve(COVERED)
        result = try_it(self.root, self.cfg, GOAL)
        checks = {c["name"]: c for c in result["checks"]}
        self.assertTrue(checks["loads"]["ok"])
        self.assertFalse(checks["seen"]["ok"])
        self.assertEqual(checks["seen"]["hidden"], ["title", "story", "credit"])
        self.assertIn("title is on the page but can't be seen (it is covered by img#art)", checks["seen"]["detail"])
        self.assertFalse(result["ok"])
        names = sorted(Path(p).name for p in result["screenshots"])
        self.assertEqual(names, ["desktop-1440x900.png", "phone-390x844.png"])
        for shot in result["screenshots"]:
            self.assertEqual(Path(shot).read_bytes()[:4], b"\x89PNG")
            self.assertTrue(shot.startswith(str(self.cfg.state_dir)))  # saved with the goal
        saved = json.loads((Path(self.cfg.state_dir) / "tryit" / "C1" / "result.json").read_text())
        self.assertEqual(saved["checks"], result["checks"])
        self.assertIn("covered by img#art", fix_request(result))

    @unittest.skipUnless(find_browser(), "no headless browser on this machine")
    def test_the_same_page_with_the_text_on_top_passes(self) -> None:
        self._serve(SHOWN)
        result = try_it(self.root, self.cfg, GOAL)
        self.assertTrue(result["ok"], result["checks"])
        self.assertEqual({c["name"]: c for c in result["checks"]}["seen"]["shown"], ["title", "story", "credit"])
        self.assertTrue({c["name"]: c for c in result["checks"]}["pictures"]["ok"])

    @unittest.skipUnless(find_browser(), "no headless browser on this machine")
    def test_a_picture_that_did_not_load_fails_even_when_the_words_show(self) -> None:
        # from the acceptance run: every check passed on a page whose picture never arrived
        self._serve(SHOWN.replace(PIXEL, "data:image/png;base64,AAAA"))
        result = try_it(self.root, self.cfg, GOAL)
        self.assertIn("1 did not load", {c["name"]: c for c in result["checks"]}["pictures"]["detail"])
        self.assertFalse(result["ok"])
        self.assertIn("did not load", fix_request(result))

    @unittest.skipUnless(find_browser(), "no headless browser on this machine")
    def test_words_below_the_first_screen_are_not_seen(self) -> None:
        # the acceptance run: title, story and credit sat under a full-screen picture, a scroll away, and the check passed
        self._serve(f'''<!doctype html><title>t</title><style>body{{margin:0}}img{{display:block;width:100vw;height:100vh}}</style>
<img id="art" src="{PIXEL}"><h1 id="title">Glacier</h1><p id="story">A long story.</p><small id="credit">Ann</small>''')
        result = try_it(self.root, self.cfg, GOAL)
        seen = {c["name"]: c for c in result["checks"]}["seen"]
        self.assertFalse(seen["ok"])
        self.assertEqual(seen["hidden"], ["title", "story", "credit"])
        self.assertIn("title is on the page but can't be seen (it is below the first screen, so you have to scroll to see it)",
                      seen["detail"])
        self.assertIn("below the first screen", fix_request(result))

    @unittest.skipUnless(find_browser(), "no headless browser on this machine")
    def test_each_screen_size_is_judged_on_its_own(self) -> None:
        # 860px down fits a 900px desktop screen and is a scroll away on an 844px phone
        self._serve('''<!doctype html><title>t</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{margin:0;height:3000px}
#title{position:absolute;top:10px}#credit{position:absolute;top:860px}</style>
<h1 id="title">Glacier</h1><small id="credit">Ann</small>''')
        seen = {c["name"]: c for c in try_it(self.root, self.cfg, GOAL)["checks"]}["seen"]
        self.assertFalse(seen["ok"])
        self.assertEqual((seen["hidden"], seen["shown"]), (["credit"], ["title"]))
        self.assertIn("credit is on the page but can't be seen (on a phone it is below the first screen", seen["detail"])
        self.assertNotIn("on a desktop screen", seen["detail"])

    @unittest.skipUnless(find_browser(), "no headless browser on this machine")
    def test_the_colours_of_the_first_screen_are_reported(self) -> None:
        self._serve(SHOWN.replace("</style>", "body{background:#102030}</style>"))
        result = try_it(self.root, self.cfg, GOAL)
        colours = {(c["hex"], c["use"]) for c in result["colours"]}
        self.assertIn(("#102030", "background"), colours)
        self.assertIn(("#000000", "background"), colours)  # the plate behind the words
        self.assertIn(("#ffffff", "text"), colours)
        saved = json.loads((Path(self.cfg.state_dir) / "tryit" / "C1" / "result.json").read_text())
        self.assertEqual(saved["colours"], result["colours"])

    @unittest.skipUnless(find_browser(), "no headless browser on this machine")
    def test_a_page_error_is_reported(self) -> None:
        self._serve(SHOWN + "<script>throw new Error('boom in the page')</script>")
        result = try_it(self.root, self.cfg, GOAL)
        self.assertIn("boom in the page", {c["name"]: c for c in result["checks"]}["console"]["detail"])
        self.assertFalse(result["ok"])

    def test_without_a_browser_the_checks_still_run_and_say_so(self) -> None:
        from unittest import mock

        self._serve(SHOWN)
        with mock.patch("openfarm_core.tryit.find_browser", return_value=None):
            result = try_it(self.root, self.cfg, GOAL)
        checks = {c["name"]: c for c in result["checks"]}
        self.assertTrue(checks["loads"]["ok"])
        self.assertIn("no headless browser was found", checks["seen"]["detail"])
        self.assertEqual(result["screenshots"], [])
        self.assertIsNone(result["ok"])


if __name__ == "__main__":
    unittest.main()
