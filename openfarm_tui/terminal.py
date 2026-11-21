"""Ask the terminal what its background colour is, so the theme can follow it."""

from __future__ import annotations

import os
import re
import select
import time

_REPLY = re.compile(r"\]11;rgba?:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})")


def background_is_light(reply: str) -> bool | None:
    """Read a terminal's answer to "what is your background?" (OSC 11). None when it did not say."""
    found = _REPLY.search(reply)
    if not found:
        return None
    red, green, blue = (int(part, 16) / (16 ** len(part) - 1) for part in found.groups())
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue > 0.5


def ask_background(fd: int | None = None, wait_s: float = 1.0) -> bool | None:
    """Ask the terminal on `fd` (the controlling one by default). None when there is none or it does not answer.

    The question is followed by one every terminal answers ("what are you?"), so a terminal that does not know
    its colour is not waited on, and no late answer is left to turn up as typed keys.
    """
    try:
        import termios
        import tty
    except ImportError:  # no terminal of this kind here
        return None
    opened = fd is None
    try:
        if opened:
            fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        before = termios.tcgetattr(fd)
    except (OSError, termios.error):
        if opened and fd is not None:
            os.close(fd)
        return None
    reply = ""
    try:
        tty.setcbreak(fd, termios.TCSANOW)
        os.write(fd, b"\033]11;?\033\\\033[c")
        until = time.monotonic() + wait_s
        while time.monotonic() < until and not re.search(r"\033\[\?[0-9;]*c", reply):
            if select.select([fd], [], [], max(0.0, until - time.monotonic()))[0]:
                reply += os.read(fd, 256).decode("latin-1")
    except OSError:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, before)
        if opened:
            os.close(fd)
    return background_is_light(reply)
