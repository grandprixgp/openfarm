"""v0.9.0 Go language pack tests."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from openfarm_core import adopt, config_template, mutate
from openfarm_core.errors import Refused
from openfarm_core.git import Git
from openfarm_core.packs.base import pack_for


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)


class GoPackUnitTests(unittest.TestCase):
    def test_pack_for_go(self) -> None:
        pack = pack_for("go")
        self.assertEqual(pack.name, "go")
        self.assertEqual(pack.extension, ".go")
        summary = pack.tooling_summary()
        self.assertEqual(summary["pack"], "go")
        self.assertEqual(summary["runner"], "go test")
        self.assertEqual(summary["lint"], "go vet")

    def test_commands_shapes(self) -> None:
        pack = pack_for("go")
        self.assertEqual(pack.commands("build")["argv"], ["go", "build", "./..."])
        self.assertEqual(pack.commands("typecheck")["argv"], ["go", "build", "./..."])
        self.assertEqual(pack.commands("list")["argv"], ["go", "test", "-list", ".", "./..."])
        self.assertEqual(pack.commands("lint")["argv"], ["go", "vet", "./..."])
        self.assertEqual(pack.commands("ws-lint")["argv"], ["go", "vet", "./..."])
        self.assertEqual(pack.commands("test")["argv"], ["go", "test", "-json", "-count=1", "./..."])
        filtered = pack.commands("test", tests=["TestAdd", "TestSub"])["argv"]
        self.assertIn("-run", filtered)
        run_arg = filtered[filtered.index("-run") + 1]
        self.assertIn("TestAdd", run_arg)
        self.assertIn("TestSub", run_arg)
        pack.has_gofmt = True
        fmt = pack.commands("format", files=["pkg/a.go"])["argv"]
        self.assertEqual(fmt, ["gofmt", "-w", "pkg/a.go"])
        fmt_check = pack.commands("fmt-check", files=["pkg/a.go"])["argv"]
        self.assertIn("pkg/a.go", fmt_check)
        pack.has_gofmt = False
        self.assertEqual(pack.commands("format", files=["pkg/a.go"])["argv"][-1], "pass")

    def test_parse_tests_ndjson_pass_fail_skip(self) -> None:
        pack = pack_for("go")
        events = [
            {"Action": "run", "Package": "example.com/calc", "Test": "TestAdd"},
            {"Action": "pass", "Package": "example.com/calc", "Test": "TestAdd", "Elapsed": 0.01},
            {"Action": "run", "Package": "example.com/calc", "Test": "TestDiv"},
            {"Action": "fail", "Package": "example.com/calc", "Test": "TestDiv", "Elapsed": 0.01},
            {"Action": "skip", "Package": "example.com/calc", "Test": "TestSlow", "Elapsed": 0.0},
            {"Action": "fail", "Package": "example.com/calc", "Elapsed": 0.02},
        ]
        stdout = "\n".join(json.dumps(e) for e in events) + "\n"
        parsed = pack.parse_tests(stdout)
        self.assertEqual(parsed["missing_footers"], [])
        self.assertEqual(parsed["compile_errors"], [])
        by_name = {r["name"]: r for r in parsed["results"]}
        self.assertEqual(by_name["TestAdd"]["status"], "ok")
        self.assertEqual(by_name["TestDiv"]["status"], "FAILED")
        self.assertEqual(by_name["TestSlow"]["status"], "ignored")
        self.assertEqual(len(parsed["footers"]), 1)
        footer = parsed["footers"][0]
        self.assertEqual(footer["status"], "FAILED")
        self.assertEqual(footer["passed"], 1)
        self.assertEqual(footer["failed"], 1)
        self.assertEqual(footer["ignored"], 1)

    def test_parse_tests_build_fail_and_missing_footer(self) -> None:
        pack = pack_for("go")
        events = [
            {"ImportPath": "example.com/calc", "Action": "build-output", "Output": "# example.com/calc\n"},
            {"ImportPath": "example.com/calc", "Action": "build-fail"},
            {"Action": "fail", "Package": "example.com/calc", "FailedBuild": "example.com/calc"},
        ]
        stdout = "\n".join(json.dumps(e) for e in events) + "\n"
        parsed = pack.parse_tests(stdout)
        self.assertTrue(parsed["compile_errors"])
        self.assertIn("example.com/calc", parsed["missing_footers"])

        truncated = pack.parse_tests(json.dumps({"Action": "run", "Package": "p", "Test": "TestX"}) + "\n")
        self.assertTrue(truncated["missing_footers"])

    def test_parse_list(self) -> None:
        pack = pack_for("go")
        out = "\n".join([
            "TestAdd",
            "TestSub",
            "ok  \texample.com/calc\t0.002s",
            "?   \texample.com/empty\t[no test files]",
        ])
        parsed = pack.parse_list(out)
        self.assertEqual(
            parsed["tests"],
            [{"name": "TestAdd", "binary": "example.com/calc"},
             {"name": "TestSub", "binary": "example.com/calc"}],
        )

    def test_classify_paths(self) -> None:
        pack = pack_for("go")
        got = pack.classify_paths(["calc.go", "calc_test.go", "internal/a_test.go", "go.mod"])
        self.assertEqual(got, {
            "calc.go": "production",
            "calc_test.go": "test",
            "internal/a_test.go": "test",
            "go.mod": "other",
        })

    def test_patterns_banned(self) -> None:
        pack = pack_for("go")
        lines = [
            {"path": "calc.go", "line": 3, "text": 'import "os/exec"'},
            {"path": "calc.go", "line": 4, "text": 'import "unsafe"'},
            {"path": "calc.go", "line": 5, "text": 'x := 1 //nolint:errcheck'},
            {"path": "calc.go", "line": 6, "text": 'panic("not implemented")'},
            {"path": "calc_test.go", "line": 10, "text": 't.Skip("flaky")'},
            {"path": "calc_test.go", "line": 11, "text": 'panic("ok in test")'},
        ]
        res = pack.patterns(lines)
        labels = {(b["file"], b["pattern"]) for b in res["banned"]}
        self.assertIn(("calc.go", "process-call"), labels)
        self.assertIn(("calc.go", "unsafe-code"), labels)
        self.assertIn(("calc.go", "suppression-nolint"), labels)
        self.assertIn(("calc.go", "leftover-stub"), labels)
        self.assertIn(("calc_test.go", "test-skipped"), labels)
        self.assertNotIn(("calc_test.go", "leftover-stub"), labels)

    def test_signatures_and_append_stub(self) -> None:
        pack = pack_for("go")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "calc.go"
            p.write_text("package calc\n\ntype Adder struct{}\n\nfunc Add(a, b int) int {\n\treturn a + b\n}\n")
            sigs = pack.signatures(p)["signatures"]
            self.assertTrue(any("package calc" in s for s in sigs))
            self.assertTrue(any("func Add(a, b int) int" in s for s in sigs))
            self.assertFalse(any("return a + b" in s for s in sigs))

            with self.assertRaises(Refused):
                pack.append_stub(p, "TestSub", "func Sub(a, b int) int { return 0 }\n")
            info = pack.append_stub(p, "TestSub", 'func Sub(a, b int) int { panic("todo") }\n')
            self.assertGreater(info["line"], 1)
            self.assertIn("func Sub", p.read_text())


class GoMutationAndAdoptTests(unittest.TestCase):
    def test_go_mutants_and_runner(self) -> None:
        src = "package calc\n\nfunc IsPos(n int) bool {\n\tif n > 0 {\n\t\treturn true\n\t}\n\treturn false\n}\n"
        mutants = mutate.go_mutants(src, {4, 5})
        self.assertTrue(any(">" in m["what"] for m in mutants))
        self.assertTrue(any("true -> false" in m["what"] for m in mutants))

        # Scripted E2E of mutate.main --lang go using a python stand-in test command.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "calc.go"
            target.write_text(src)
            out_dir = root / "out"
            checker = [
                sys.executable, "-c",
                "from pathlib import Path; import sys; "
                "t = Path('calc.go').read_text(); "
                "sys.exit(0 if ('if n > 0' in t and 'return true' in t) else 1)",
            ]
            old_cwd = Path.cwd()
            try:
                import os
                os.chdir(root)
                rc = mutate.main([
                    "--lang", "go",
                    "--out", str(out_dir),
                    "--test-argv", json.dumps(checker),
                    "--lines", json.dumps({"calc.go": [4, 5]}),
                    "calc.go",
                ])
            finally:
                os.chdir(old_cwd)
            self.assertEqual(rc, mutate.EXIT_ALL_CAUGHT)
            outcomes = json.loads((out_dir / "outcomes.json").read_text())
            self.assertGreaterEqual(outcomes["caught"], 2)
            self.assertEqual(outcomes["missed"], 0)
            self.assertEqual(target.read_text(), src)

    def test_adopt_go_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "goproj"
            root.mkdir()
            _git_init(root)
            (root / "go.mod").write_text("module example.com/goproj\n\ngo 1.22\n")
            (root / "calc.go").write_text("package calc\n\nfunc Add(a, b int) int { return a + b }\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            self.assertEqual(adopt.detect_language(root), "go")
            state_dir = Path(tmp) / "state"
            key_file = Path(tmp) / "key"
            res = adopt.adopt_project(root, state_dir=state_dir, key_file=key_file, run_suite=False)
            self.assertTrue(res["ok"])
            self.assertEqual(res["language"], "go")
            cfg_text = (root / ".openfarm" / "config.toml").read_text()
            self.assertIn('languages = ["go"]', cfg_text)
            self.assertIn("go.mod", cfg_text)
            self.assertIn("go.sum", cfg_text)

    @unittest.skipUnless(shutil.which("go"), "go binary not installed")
    def test_real_go_test_smoke(self) -> None:
        pack = pack_for("go")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "go.mod").write_text("module example.com/smoke\n\ngo 1.22\n")
            (root / "calc.go").write_text("package smoke\n\nfunc Add(a, b int) int { return a + b }\n")
            (root / "calc_test.go").write_text(
                'package smoke\n\nimport "testing"\n\nfunc TestAdd(t *testing.T) {\n'
                '\tif Add(2, 3) != 5 { t.Fatalf("want 5") }\n}\n'
            )
            pack.configure(root)
            done = subprocess.run(pack.commands("test")["argv"], cwd=root, capture_output=True, text=True)
            parsed = pack.parse_tests(done.stdout, done.stderr)
            self.assertEqual(parsed["missing_footers"], [])
            self.assertEqual([r["name"] for r in parsed["results"]], ["TestAdd"])
            self.assertEqual(parsed["results"][0]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
