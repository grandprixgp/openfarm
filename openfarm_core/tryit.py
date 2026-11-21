"""Try it: run the finished app and look at it before a goal is called done.

Web apps are started on a spare port and opened in a headless Chromium, which
takes a desktop and a phone screenshot and answers three questions: did the page
load, can the content the goal names actually be seen, and did the page log any
errors. Command-line tools run one sample command and show what it printed.

The browser is driven over its own debugging pipe with the standard library, so
nothing has to be installed beyond the browser itself.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

VIEWPORTS = (("desktop", 1440, 900), ("phone", 390, 844))
PICTURE_WAIT = 15.0  # seconds a page's pictures get to finish loading before it is judged and photographed

_STOP_WORDS = frozenset("""
    about above after again also always app application below browser build builds built button click each every
    from have into just local make makes more must only open opens over page pages project same server show shows
    site some that their them then there these they this those under user users using very view want website when
    where which while with without your image photo picture pictures images photos today daily
""".split())

# Runs in the page. For each word the goal names, find the element that carries it
# (by id or class) and say whether a person could see it: it has size, is not
# hidden, its text differs from what is behind it, and nothing else covers it.
_LOOK = r"""
(words) => {
  const rgb = (c) => { const m = (c || '').match(/[\d.]+/g) || []; return m.map(Number); };
  const behind = (el) => {
    for (let n = el; n; n = n.parentElement) {
      const c = rgb(getComputedStyle(n).backgroundColor);
      if (c.length >= 3 && (c.length < 4 || c[3] > 0)) return c.slice(0, 3);
    }
    return [255, 255, 255];
  };
  const seen = (el) => {
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    if (s.display === 'none' || s.visibility === 'hidden' || Number(s.opacity) === 0) return 'it is hidden';
    if (r.width < 1 || r.height < 1) return el.textContent.trim() ? 'it has no size' : 'it is empty';
    const isPicture = ['IMG', 'VIDEO', 'CANVAS', 'SVG'].includes(el.tagName.toUpperCase());
    if (!isPicture) {
      if (!el.textContent.trim()) return 'it is empty';
      const ink = rgb(s.color).slice(0, 3), paper = behind(el);
      if (ink.every((v, i) => Math.abs(v - paper[i]) < 12)) return 'its text is the same colour as what is behind it';
    }
    const x = Math.min(Math.max(r.left + r.width / 2, 0), innerWidth - 1);
    const y = Math.min(Math.max(r.top + r.height / 2, 0), innerHeight - 1);
    if (r.bottom < 0 || r.right < 0) return 'it is off the screen';
    if (r.top < innerHeight && r.left < innerWidth) {
      const top = document.elementFromPoint(x, y);
      if (top && top !== el && !el.contains(top) && !top.contains(el)) {
        return 'it is covered by ' + top.tagName.toLowerCase() + (top.id ? '#' + top.id : '');
      }
    }
    return '';
  };
  const named = [];
  for (const word of words) {
    const el = document.getElementById(word) || document.querySelector('.' + CSS.escape(word))
      || document.querySelector('[data-role="' + word + '"]');
    if (el) named.push({ word, why: seen(el) });
  }
  const readable = [...document.querySelectorAll('h1,h2,h3,p,li,span,small,a,button,td,label,figcaption')]
    .filter((el) => el.textContent.trim()).some((el) => !seen(el));
  const imgs = [...document.querySelectorAll('img')].filter((i) => i.getAttribute('src'));
  const pictures = { total: imgs.length, pending: imgs.filter((i) => !i.complete).length,
                     broken: imgs.filter((i) => i.complete && i.naturalWidth === 0).length };
  return JSON.stringify({ named, readable, pictures, title: document.title, text: document.body ? document.body.innerText.length : 0 });
}
"""


def find_browser() -> Path | None:
    """A headless Chromium, wherever one is: `OPENFARM_BROWSER`, Playwright's cache, or the PATH."""
    given = os.environ.get("OPENFARM_BROWSER", "").strip()
    if given:
        return Path(given) if Path(given).is_file() else None
    cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
    for pattern in ("chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
                    "chromium-*/chrome-linux*/chrome"):
        found = sorted(cache.glob(pattern))
        if found:
            return found[-1]
    for name in ("chrome-headless-shell", "chromium", "chromium-browser", "google-chrome"):
        path = shutil.which(name)
        if path:
            return Path(path)
    return None


def goal_words(text: str) -> list[str]:
    """The things a goal's text names, as lower-case words worth looking for on the page."""
    out: list[str] = []
    for word in re.findall(r"[A-Za-z][A-Za-z-]{2,}", text or ""):
        word = word.lower().strip("-")
        if word in _STOP_WORDS or word in out:
            continue
        out.append(word)
        if word.endswith("s") and len(word) > 4 and word[:-1] not in out:
            out.append(word[:-1])
    return out[:400]


class _Browser:
    """One headless Chromium, spoken to over its debugging pipe."""

    def __init__(self, binary: Path) -> None:
        self._dir = tempfile.TemporaryDirectory(prefix="openfarm-browser-")
        to_child_r, self._w = os.pipe()
        self._r, from_child_w = os.pipe()
        # The browser expects its pipe on descriptors 3 and 4; the shell puts it there.
        # `--password-store=basic`: otherwise the first request waits on a desktop keyring a server lacks.
        self._proc = subprocess.Popen(
            ["bash", "-c", 'exec "$0" "$@" 3<&$OF_IN 4>&$OF_OUT', str(binary), "--headless", "--no-sandbox",
             "--disable-gpu", "--hide-scrollbars", "--password-store=basic", "--remote-debugging-pipe", f"--user-data-dir={self._dir.name}",
             "about:blank"],
            pass_fds=(to_child_r, from_child_w), env={**os.environ, "OF_IN": str(to_child_r), "OF_OUT": str(from_child_w)},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(to_child_r)
        os.close(from_child_w)
        os.set_blocking(self._r, False)
        self._buffer = b""
        self._next = 0
        self.events: list[dict] = []

    def _read(self, deadline: float) -> dict | None:
        while True:
            if b"\0" in self._buffer:
                raw, self._buffer = self._buffer.split(b"\0", 1)
                return json.loads(raw)
            if time.monotonic() > deadline:
                return None
            try:
                chunk = os.read(self._r, 1 << 16)
            except BlockingIOError:
                time.sleep(0.02)
                continue
            if not chunk:
                return None
            self._buffer += chunk

    def call(self, method: str, params: dict | None = None, session: str = "", timeout: float = 20) -> dict:
        self._next += 1
        message = {"id": self._next, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        os.write(self._w, json.dumps(message).encode() + b"\0")
        deadline = time.monotonic() + timeout
        while (reply := self._read(deadline)) is not None:
            if reply.get("id") == self._next:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error'].get('message')}")
                return reply.get("result") or {}
            self.events.append(reply)
        raise RuntimeError(f"the browser did not answer {method}")

    def wait(self, method: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if any(e.get("method") == method for e in self.events):
                return True
            event = self._read(deadline)
            if event is None:
                return False
            self.events.append(event)

    def settle(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while (event := self._read(deadline)) is not None:
            self.events.append(event)

    def close(self) -> None:
        for fd in (self._w, self._r):
            try:
                os.close(fd)
            except OSError:
                pass
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._dir.cleanup()


def look_at(url: str, words: list[str], out_dir: Path, browser: Path | None = None) -> dict:
    """Open `url`, take both screenshots into `out_dir`, and run the three checks.

    Returns `{"browser", "screenshots", "checks": [{"name", "ok", "detail"}]}`.
    Without a browser the load check still runs, over plain HTTP, and the result
    says the page was not looked at.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    browser = browser or find_browser()
    if browser is None:
        import urllib.request

        try:
            with urllib.request.urlopen(url, timeout=10) as reply:  # noqa: S310 - a local URL we just started
                status = reply.status
            loads = {"name": "loads", "ok": 200 <= status < 400, "detail": f"the page answered {status}"}
        except OSError as exc:
            loads = {"name": "loads", "ok": False, "detail": f"the page did not load: {exc}"}
        return {"browser": "", "screenshots": [], "checks": [
            loads, {"name": "seen", "ok": None,
                    "detail": "no headless browser was found, so nobody looked at the page or took screenshots"}]}

    chrome = _Browser(browser)
    shots: list[str] = []
    try:
        target = chrome.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = chrome.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        for domain in ("Page", "Runtime", "Log", "Network"):
            chrome.call(f"{domain}.enable", session=session)
        looked: dict = {}
        status = 0
        for name, width, height in VIEWPORTS:
            chrome.call("Emulation.setDeviceMetricsOverride",
                        {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": name == "phone"}, session)
            chrome.events = [e for e in chrome.events if e.get("method") != "Page.loadEventFired"]
            chrome.call("Page.navigate", {"url": url}, session)
            chrome.wait("Page.loadEventFired", 20)
            chrome.settle(1.5)  # scripts that fill the page in after it loads
            waited = time.monotonic() + PICTURE_WAIT  # a picture set by script loads after the page's own load event
            while time.monotonic() < waited and chrome.call("Runtime.evaluate", {
                    "expression": "[...document.images].some((i) => i.getAttribute('src') && !i.complete)",
                    "returnByValue": True}, session)["result"].get("value"):
                chrome.settle(0.5)
            if not looked:
                for event in chrome.events:
                    reply = (event.get("params") or {}).get("response") or {}
                    if event.get("method") == "Network.responseReceived" and (event["params"].get("type") == "Document"):
                        status = int(reply.get("status") or 0)
                        break
                value = chrome.call("Runtime.evaluate", {"expression": f"({_LOOK})({json.dumps(words)})",
                                                         "returnByValue": True}, session)
                looked = json.loads(value["result"]["value"])
            png = chrome.call("Page.captureScreenshot", {"format": "png"}, session)["data"]
            path = out_dir / f"{name}-{width}x{height}.png"
            path.write_bytes(base64.b64decode(png))
            shots.append(str(path))
        errors: list[str] = []
        for event in chrome.events:
            params = event.get("params") or {}
            if event.get("method") == "Runtime.exceptionThrown":
                details = params.get("exceptionDetails") or {}
                errors.append(str((details.get("exception") or {}).get("description") or details.get("text") or "error"))
            elif event.get("method") == "Runtime.consoleAPICalled" and params.get("type") == "error":
                errors.append(" ".join(str(a.get("value") or a.get("description") or "") for a in params.get("args") or []))
            elif event.get("method") == "Log.entryAdded" and (params.get("entry") or {}).get("level") == "error":
                entry = params["entry"]
                errors.append(f"{entry.get('text', '')} {entry.get('url', '')}".strip())
        errors = list(dict.fromkeys(e.splitlines()[0][:200] for e in errors if e.strip()))
    except (RuntimeError, OSError, KeyError, ValueError) as exc:
        return {"browser": str(browser), "screenshots": shots,
                "checks": [{"name": "loads", "ok": False, "detail": f"the browser could not open the page: {exc}"}]}
    finally:
        chrome.close()

    hidden = [n for n in looked.get("named", []) if n["why"]]
    shown = [n["word"] for n in looked.get("named", []) if not n["why"]]
    if hidden:
        detail = ("; ".join(f"{n['word']} is on the page but can't be seen ({n['why']})" for n in hidden)
                  + (f". Visible: {', '.join(shown)}." if shown else ""))
    elif shown:
        detail = f"{', '.join(shown)} can be seen"
    elif looked.get("readable"):
        detail = "the page shows readable text (nothing on it carries the goal's own words, so those were not checked)"
    else:
        detail = "the page shows no text a person could read"
    pictures = looked.get("pictures") or {}
    bad = [f"{pictures[key]} {words}" for key, words in (
        ("broken", "did not load"), ("pending", f"still loading after {int(PICTURE_WAIT)} seconds (too large or too slow)"))
        if pictures.get(key)]
    picture_checks = [{"name": "pictures", "ok": not bad,
                       "detail": ("of the page's pictures: " + ", ".join(bad)) if bad else "the page's pictures loaded"}
                      ] if pictures.get("total") else []
    return {"browser": str(browser), "screenshots": shots, "checks": [
        {"name": "loads", "ok": 200 <= status < 400, "detail": f"the page answered {status or 'nothing'}"},
        {"name": "seen", "ok": not hidden and (bool(shown) or bool(looked.get("readable"))), "detail": detail,
         "hidden": [n["word"] for n in hidden], "shown": shown},
        {"name": "console", "ok": not errors,
         "detail": "no errors in the page's console" if not errors else "the page logged: " + " | ".join(errors[:5])},
        *picture_checks,
    ]}


def how_to_start(root: Path, cfg) -> dict:
    """What "try it" means here: the config's `[try]` table, else the language pack's answer."""
    from .packs.base import pack_for

    own = dict(getattr(cfg, "try_it", {}) or {})
    lang = cfg.languages[0] if cfg.languages else ""
    try:
        pack = pack_for(lang, cfg.cargo_flags, spec=cfg.pack_adapter, cwd=root, python=cfg.runner_python)
        plan = dict(pack.try_it(root) or {})
    except Exception:  # noqa: BLE001 - a pack that can't answer is the same as no answer
        plan = {}
    if own.get("start"):
        plan = {"kind": "web", "start": shlex.split(own["start"]), "port_env": "PORT"}
    elif own.get("sample"):
        plan = {"kind": "cli", "sample": shlex.split(own["sample"])}
    if plan:
        plan.update(port=int(own.get("port") or 0), path=str(own.get("path") or "/"))
    return plan


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int, proc: subprocess.Popen, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and proc.poll() is None:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def try_it(root: Path, cfg, goal: dict | None = None, *, out_dir: Path | None = None,
           browser: Path | None = None) -> dict:
    """Run the app and report what a person would find.

    Returns `{"kind", "command", "url", "checks", "screenshots", "output", "ok",
    "words", "goal"}` and writes the same to `result.json` beside the screenshots.
    `ok` is False when any check failed and None when nothing could be tried.
    """
    root = Path(root)
    goal = goal or {}
    goal_id = str(goal.get("id") or "app")
    out_dir = Path(out_dir or Path(cfg.state_dir) / "tryit" / goal_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    plan = how_to_start(root, cfg)
    result = {"kind": plan.get("kind", ""), "goal": goal_id, "command": "", "url": "", "checks": [],
              "screenshots": [], "output": "", "words": []}
    if not plan:
        result["checks"] = [{"name": "start", "ok": None,
                             "detail": "openfarm doesn't know how to start this app; set [try] start or sample in the config"}]
    elif plan["kind"] == "cli":
        result["command"] = shlex.join(plan["sample"])
        try:
            done = subprocess.run(plan["sample"], cwd=root, capture_output=True, text=True, timeout=120, check=False)
            result["output"] = (done.stdout + done.stderr).strip()[-6000:]
            result["checks"] = [{"name": "runs", "ok": done.returncode == 0,
                                 "detail": f"the sample command finished with code {done.returncode}"}]
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["checks"] = [{"name": "runs", "ok": False, "detail": f"the sample command did not run: {exc}"}]
    else:
        port = plan.get("port") or _free_port()
        result["command"] = shlex.join(plan["start"])
        result["url"] = f"http://127.0.0.1:{port}{plan.get('path') or '/'}"
        result["words"] = goal_words(f"{goal.get('title', '')} {goal.get('goal', '')}")
        log = out_dir / "server.log"
        with log.open("w", encoding="utf-8") as sink:
            try:
                server = subprocess.Popen(plan["start"], cwd=root, stdout=sink, stderr=subprocess.STDOUT,
                                          env={**os.environ, plan.get("port_env") or "PORT": str(port)},
                                          start_new_session=True)
            except OSError as exc:
                server = None
                result["checks"] = [{"name": "start", "ok": False, "detail": f"the app could not be started: {exc}"}]
            if server is not None:
                try:
                    if _wait_for_port(port, server, 30):
                        looked = look_at(result["url"], result["words"], out_dir, browser)
                        result.update(checks=looked["checks"], screenshots=looked["screenshots"])
                    else:
                        result["checks"] = [{"name": "start", "ok": False,
                                             "detail": f"the app did not start listening on port {port}"}]
                finally:
                    try:
                        os.killpg(server.pid, 15)
                    except OSError:
                        pass
                    try:
                        server.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(server.pid, 9)
        result["output"] = log.read_text(encoding="utf-8", errors="replace").strip()[-3000:]
    oks = [c["ok"] for c in result["checks"]]
    result["ok"] = False if False in oks else (True if oks and None not in oks else None)
    result["head"] = code_version(root)  # which code was tried, so a later change asks for a new try
    (out_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def code_version(root: Path) -> str:
    """The commit the project's code is at, or "" outside a repository."""
    from .git import Git

    done = Git(root)._run(["rev-parse", "HEAD"], check=False)
    return done.stdout.strip() if done.returncode == 0 else ""


def last_result(root: Path, cfg, goal_id: str, any_code: bool = False) -> dict | None:
    """The saved result of trying this goal, if the code has not changed since (`any_code`: whatever the code is now)."""
    try:
        result = json.loads((Path(cfg.state_dir) / "tryit" / goal_id / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return result if any_code or result.get("head") == code_version(root) else None


def looks_right(cfg, goal_id: str, why: str = "") -> None:
    """Record that a person looked and is satisfied, whatever the checks said."""
    path = Path(cfg.state_dir) / "tryit" / goal_id / "result.json"
    result = json.loads(path.read_text(encoding="utf-8"))
    result.update(looks_right=True, looks_right_why=why)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def fix_request(result: dict) -> str:
    """The failed checks as a request the Architect can draft fix tasks from."""
    failed = [c["detail"] for c in result.get("checks", []) if c["ok"] is False]
    if not failed:
        return ""
    return ("Trying the app showed problems a person would see. Fix them without changing what already works:\n"
            + "\n".join(f"- {detail}" for detail in failed))
