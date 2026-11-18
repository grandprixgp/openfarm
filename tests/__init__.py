"""openfarm's tests.

v0.7: no test may read or write the owner's real global harness library, so every
test process points `OPENFARM_GLOBAL_HARNESS` at a fresh empty folder unless a test
sets its own `[harness] global_dir`.

v0.8.1: a test process leaves nothing in the temp folder. Every temp file of the
process, and of every program it starts (the CLI, git, node, cargo), goes in one
folder of its own, `openfarm-tests-*` under `$TMPDIR`, which is removed when the
process exits. A test still removes what it makes as soon as it is done with it;
this catches what one forgets, so the machine's file slots are not used up run
after run. Set `OPENFARM_KEEP_TEST_TMP=1` to keep the folder and look at what was
left in it (its path is printed at exit).
"""

import atexit
import os
import shutil
import stat
import sys
import tempfile

TEST_TMP_ROOT = tempfile.mkdtemp(prefix="openfarm-tests-", dir=os.environ.get("TMPDIR"))
os.environ["TMPDIR"] = TEST_TMP_ROOT
tempfile.tempdir = TEST_TMP_ROOT

# v0.8.5: a provider CLI that cannot start is tried again for about 15 s; the
# many tests that start a missing one on purpose would each wait that long.
os.environ.setdefault("OPENFARM_SPAWN_RETRY_DELAYS", "")

os.environ["OPENFARM_GLOBAL_HARNESS"] = os.path.join(
    tempfile.mkdtemp(prefix="openfarm-test-global-"), "harness")


def _writable_and_retry(func, path, _exc) -> None:
    """A test may leave a read-only folder; make it writable and try once more."""
    try:
        parent = os.path.dirname(path)
        os.chmod(parent, os.stat(parent).st_mode | stat.S_IRWXU)
        if os.path.isdir(path) and not os.path.islink(path):
            os.chmod(path, os.stat(path).st_mode | stat.S_IRWXU)
        func(path)
    except OSError:
        pass


def remove_tree(path) -> None:
    """Remove a folder and everything in it, read-only parts included."""
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_writable_and_retry)
    else:
        shutil.rmtree(path, onerror=_writable_and_retry)


def _remove_test_tmp_root() -> None:
    if os.environ.get("OPENFARM_KEEP_TEST_TMP"):
        print(f"openfarm tests: kept the temp folder {TEST_TMP_ROOT}", file=sys.stderr)
        return
    if os.path.isdir(TEST_TMP_ROOT):
        remove_tree(TEST_TMP_ROOT)


atexit.register(_remove_test_tmp_root)
