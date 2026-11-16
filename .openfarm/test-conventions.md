# openfarm test conventions (for the test writer)

openfarm is standard-library Python 3.11+ and tests with `unittest`. There is no pytest.

- One file per card: `tests/test_<subject>.py`, named in the card's `tests` field.
- Start every file with:

  ```python
  from __future__ import annotations

  import sys
  import unittest
  from pathlib import Path

  sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
  ```

  then import what you test from `openfarm_core` by its public name. Never import or
  call a name that starts with `_`: if a card needs it, the card is wrong.
- Classes subclass `unittest.TestCase`; test methods are named `test_<behaviour>` and
  match the names the card lists exactly.
- Assert exact values with `assertEqual`, `assertIn`, `assertRaises(SpecificError)`.
  Never `assertTrue(x)` where an exact value exists.
- Files on disk go under `tempfile.mkdtemp(prefix="openfarm-")` and are removed in
  `tearDown` or with `addCleanup(shutil.rmtree, path, True)`.
- No network, no `subprocess`, no `time.sleep`, no reading the real `~/.openfarm` or
  `~/.config`. The banned-pattern gate refuses `subprocess`, sockets, `eval`/`exec`,
  skips, `# noqa` and `# type: ignore` in new code.
- No `unittest.skip` or `expectedFailure`: a skipped test is not proof.
