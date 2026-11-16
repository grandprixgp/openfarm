"""openfarm's tests.

v0.7: no test may read or write the owner's real global harness library, so every
test process points `OPENFARM_GLOBAL_HARNESS` at a fresh empty folder unless a test
sets its own `[harness] global_dir`.
"""

import os
import tempfile

os.environ["OPENFARM_GLOBAL_HARNESS"] = os.path.join(
    tempfile.mkdtemp(prefix="openfarm-test-global-", dir=os.environ.get("TMPDIR")), "harness")

import atexit
import shutil

atexit.register(shutil.rmtree, os.path.dirname(os.environ["OPENFARM_GLOBAL_HARNESS"]), True)
