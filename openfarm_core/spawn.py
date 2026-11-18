"""Starting a provider CLI that may be mid-update (v0.8.5).

Claude Code's npm auto-update removes its `claude` for a moment, and a card that
started a child then stopped as `infra` ("Failed to spawn"), twice in one bench.
A child that cannot start because its program is missing or busy is tried again a
few times, a little later each time; any other failure to start is not retried.
"""

from __future__ import annotations

import errno
import os
import subprocess
import time

#: Seconds to wait before each further try: about 15 s in all.
SPAWN_RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)
#: The errors a program being replaced gives: gone, or still being written.
_TRANSIENT = {errno.ENOENT, errno.ETXTBSY, errno.EACCES}


def retry_delays() -> tuple[float, ...]:
    """`SPAWN_RETRY_DELAYS`, or `OPENFARM_SPAWN_RETRY_DELAYS` (comma-separated
    seconds, empty for none) where it is set: the test suite turns retries off."""
    raw = os.environ.get("OPENFARM_SPAWN_RETRY_DELAYS")
    if raw is None:
        return SPAWN_RETRY_DELAYS
    out = []
    for part in raw.split(","):
        try:
            value = float(part)
        except ValueError:
            continue
        if value >= 0:
            out.append(value)
    return tuple(out)


def popen_with_retry(argv, *, delays=None, popen=None, sleep=time.sleep,
                     on_retry=None, **kwargs):
    """`subprocess.Popen(argv, **kwargs)`, tried again while the program is missing."""
    start = popen or subprocess.Popen
    waits = list(retry_delays() if delays is None else delays)
    while True:
        try:
            return start(argv, **kwargs)
        except OSError as exc:
            if exc.errno not in _TRANSIENT or not waits:
                raise
            wait = waits.pop(0)
            if on_retry is not None:
                on_retry(exc, wait)
            sleep(wait)
