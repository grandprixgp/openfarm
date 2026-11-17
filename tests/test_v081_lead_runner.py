"""The runner's infra rule and cargo's lock-wait line."""
import unittest
from pathlib import Path

from openfarm_core.adapters.direct import FAIL, INFRA, PASS, DirectRunner

WAIT = "    Blocking waiting for file lock on package cache\n"
KILLED = (
    "   Compiling toy v0.1.0 (/x)\n"
    "error: could not compile `toy` (lib test)\n"
    "\n"
    "Caused by:\n"
    "  process didn't exit successfully: `/x/bin/rustc --crate-name toy --edition=2021 "
    "src/lib.rs --test` (signal: 9, SIGKILL: kill)\n"
    "warning: build failed, waiting for other jobs to finish...\n"
)


class LockWaitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = DirectRunner(Path("."))

    def test_a_failing_test_after_a_lock_wait_is_a_code_failure(self) -> None:
        # Seen live with six cargo runs at once: cargo waited, went on, the test failed.
        stderr = (WAIT + WAIT
                  + "   Compiling toy v0.1.0 (/x)\n"
                  + "    Finished `test` profile [unoptimized + debuginfo] target(s) in 0.50s\n"
                  + "     Running tests/t.rs (target/debug/deps/t-1)\n"
                  + "error: test failed, to rerun pass `--test t`\n")
        stdout = "test result: FAILED. 0 passed; 1 failed; 0 ignored\n"
        self.assertEqual(self.runner.classify(101, stdout, stderr, "test"), FAIL)

    def test_a_compile_error_after_a_lock_wait_is_a_code_failure(self) -> None:
        stderr = (WAIT + "    Checking toy v0.1.0 (/x)\n"
                  + "error[E0599]: no variant named `Nope`\n"
                  + "error: could not compile `toy` (lib) due to 1 previous error\n")
        self.assertEqual(self.runner.classify(101, "", stderr, "lint"), FAIL)

    def test_a_run_that_never_got_past_the_wait_is_infra(self) -> None:
        self.assertEqual(self.runner.classify(101, "", WAIT + WAIT + "\n", "test"), INFRA)

    def test_a_lock_wait_does_not_change_a_pass(self) -> None:
        self.assertEqual(self.runner.classify(0, "ok", WAIT + "    Finished\n", "test"), PASS)

    def test_a_real_machine_fault_after_a_lock_wait_is_still_infra(self) -> None:
        self.assertEqual(self.runner.classify(101, "", WAIT + KILLED, "test"), INFRA)


class KilledCompilerTests(unittest.TestCase):
    """cargo 1.100's real output when rustc is killed (captured with a wrapper that
    sends itself SIGKILL), trimmed of long paths."""

    def setUp(self) -> None:
        self.runner = DirectRunner(Path("."))

    def test_a_killed_compiler_is_infra_not_a_code_failure(self) -> None:
        for kind in ("test", "lint", "build"):
            self.assertEqual(self.runner.classify(101, "", KILLED, kind), INFRA, kind)

    def test_a_compile_error_is_still_a_code_failure(self) -> None:
        stderr = ("   Compiling toy v0.1.0 (/x)\n"
                  "error[E0308]: mismatched types\n"
                  "error: could not compile `toy` (lib) due to 1 previous error\n")
        self.assertEqual(self.runner.classify(101, "", stderr, "test"), FAIL)

    def test_a_test_binary_that_dies_on_a_signal_is_a_code_failure(self) -> None:
        stderr = ("     Running tests/t.rs (target/debug/deps/t-1)\n"
                  "error: test failed, to rerun pass `--test t`\n\n"
                  "Caused by:\n"
                  "  process didn't exit successfully: `/x/target/debug/deps/t-1` "
                  "(signal: 11, SIGSEGV: invalid memory reference)\n")
        self.assertEqual(self.runner.classify(101, "", stderr, "test"), FAIL)


if __name__ == "__main__":
    unittest.main()
