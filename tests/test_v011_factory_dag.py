"""v0.11.0 tests: Cards-as-the-DAG, `openfarm plan`, Deterministic Trial-Rebase,
and Policy-Driven Auto-Land onto local `openfarm/integration`."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from openfarm_core.adapters.files import FileCardSource
from openfarm_core.bootstrap import initialize_project
from openfarm_core.cards import Card
from openfarm_core.cli import main as cli_main
from openfarm_core.config import load_config
from openfarm_core.errors import Refused, StopCode
from openfarm_core.git import Git


class FactoryAutopilotV011Tests(unittest.TestCase):
    def test_dag_tiers_cycle_detection_and_parallel_batches(self) -> None:
        from openfarm_core.dag import build_card_dag, render_dag_ascii

        c1 = Card("W1-01", "Core model", touches=["src/model.ts"], fields={"wave": 1})
        c2 = Card("W1-02", "Formatter", touches=["src/fmt.ts"], fields={"wave": 1})
        c3 = Card("W1-03", "Model helper", touches=["src/model.ts"], fields={"wave": 1})
        c4 = Card("W2-01", "Renderer", touches=["src/render.ts"], deps=["W1-01", "W1-02"], fields={"wave": 2})

        dag = build_card_dag([c1, c2, c3, c4])
        self.assertEqual(dag["card_count"], 4)
        self.assertEqual(len(dag["tiers"]), 2)
        self.assertEqual(set(dag["tiers"][0]["cards"]), {"W1-01", "W1-02", "W1-03"})
        self.assertEqual(dag["tiers"][1]["cards"], ["W2-01"])
        # Within Tier 0, W1-01 and W1-03 collide on src/model.ts, so Tier 0 has 2 parallel batches
        self.assertEqual(len(dag["tiers"][0]["batches"]), 2)
        ascii_out = render_dag_ascii(dag)
        self.assertIn("Tier 1", ascii_out)
        self.assertIn("W2-01", ascii_out)

        # Cycle detection refuses cleanly
        cyc_a = Card("C1", "A", deps=["C2"])
        cyc_b = Card("C2", "B", deps=["C1"])
        with self.assertRaises(Refused) as caught:
            build_card_dag([cyc_a, cyc_b])
        self.assertEqual(caught.exception.code, "cyclic-dag")

    def test_deterministic_trial_rebase_clean_and_conflict_preserves_branch(self) -> None:
        from openfarm_core.factory import trial_rebase_branch

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "reb-repo"
            state_dir = Path(tmp) / "st"
            repo.mkdir()
            git = Git(repo)
            git.init(branch="main")
            (repo / "shared.txt").write_text("line1\n", encoding="utf-8")
            (repo / "other.txt").write_text("base\n", encoding="utf-8")
            git.commit_all("initial")

            # Create branch A editing other.txt (clean rebase) and branch B editing shared.txt (conflict)
            git.create_branch("openfarm/W1-01-clean", "main")
            git.checkout("openfarm/W1-01-clean")
            (repo / "other.txt").write_text("clean-change\n", encoding="utf-8")
            git.commit_all("W1-01 clean edit")

            git.checkout("main")
            git.create_branch("openfarm/W1-02-conflict", "main")
            git.checkout("openfarm/W1-02-conflict")
            (repo / "shared.txt").write_text("branch-b-version\n", encoding="utf-8")
            conflict_sha = git.commit_all("W1-02 conflicting edit")

            # Advance main on shared.txt
            git.checkout("main")
            (repo / "shared.txt").write_text("main-updated-version\n", encoding="utf-8")
            git.commit_all("main advance")

            # 1. Clean branch rebases cleanly onto main
            res_ok = trial_rebase_branch(repo, "openfarm/W1-01-clean", "main", state_dir)
            self.assertTrue(res_ok["ok"])
            self.assertTrue(res_ok["rebased"])

            # 2. Conflicting branch refuses cleanly with StopCode.REBASE_CONFLICT and preserves original commit SHA!
            res_conf = trial_rebase_branch(repo, "openfarm/W1-02-conflict", "main", state_dir)
            self.assertFalse(res_conf["ok"])
            self.assertEqual(res_conf["stop_code"], StopCode.REBASE_CONFLICT.value)
            self.assertEqual(git.branch_head("openfarm/W1-02-conflict"), conflict_sha)

    def test_policy_auto_land_onto_local_integration_and_promote_to_main(self) -> None:
        from openfarm_core.factory import auto_land_card, evaluate_auto_land, promote_integration_branch

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "factory-ts"
            state_dir = Path(tmp) / "st"
            initialize_project(
                "factory-ts",
                directory=repo,
                language="typescript",
                state_dir=state_dir,
                secrets_dir=Path(tmp) / "sec",
                commit=True,
            )
            # Enable opt-in [factory] auto_land in config.toml
            cfg_file = repo / ".openfarm" / "config.toml"
            cfg_file.write_text(
                cfg_file.read_text(encoding="utf-8")
                + '\n[factory]\nauto_land = true\nintegration_branch = "openfarm/integration"\nauto_land_profiles = ["lean"]\nrequire_zero_findings = true\n',
                encoding="utf-8",
            )
            git = Git(repo)
            git.commit_all("enable opt-in factory auto_land")
            cfg = load_config(repo)
            self.assertTrue(cfg.factory.auto_land)
            self.assertEqual(cfg.factory.integration_branch, "openfarm/integration")

            # Create card branch openfarm/W1-01-lean
            git.create_branch("openfarm/W1-01-lean", "main")
            git.checkout("openfarm/W1-01-lean")
            (repo / "src" / "helper.ts").write_text("export const H = 42;\n", encoding="utf-8")
            git.commit_all("implement W1-01")
            git.checkout("main")

            lean_card = Card(
                "W1-01",
                "Small helper",
                state="in_review",
                touches=["src/helper.ts"],
                fields={"category": "mechanical", "profile": "lean"},
            )
            clean_record = {
                "gates": [
                    {"gate": "fail-before", "result": "pass"},
                    {"gate": "pass-after", "result": "pass"},
                    {"gate": "revert-check", "result": "pass"},
                    {"gate": "checker-pass2", "result": "pass"},
                ]
            }

            # Blocked from auto-land when findings exist!
            ev_with_find = evaluate_auto_land(lean_card, cfg, record=clean_record, findings=[{"id": "FIND-1"}])
            self.assertFalse(ev_with_find["eligible"])

            # Eligible when 0 findings & all gates pass & profile == lean!
            ev_ok = evaluate_auto_land(lean_card, cfg, record=clean_record, findings=[])
            self.assertTrue(ev_ok["eligible"])

            # Land onto local openfarm/integration (main stays untouched!)
            src = FileCardSource(repo, "cards", worktree=state_dir / "cards-source", base_branch="main")
            src.prepare()
            src.add_card(lean_card)
            main_before = git.branch_head("main")

            landed = auto_land_card(repo, cfg, src, lean_card, branch="openfarm/W1-01-lean", record=clean_record, findings=[])
            self.assertTrue(landed["landed"])
            self.assertTrue(landed.get("marked_done"))
            self.assertEqual(src.read("W1-01")["card"]["state"], "done")
            self.assertEqual(git.branch_head("main"), main_before)  # main untouched!
            self.assertTrue(git.branch_exists("openfarm/integration"))

            # Promote openfarm/integration -> main locally
            promoted = promote_integration_branch(repo, cfg)
            self.assertTrue(promoted["promoted"])
            self.assertTrue((repo / "src" / "helper.ts").is_file())

            # CLI `openfarm plan --json` works cleanly
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli_main(["--repo", str(repo), "--json", "plan"])
            self.assertEqual(rc, 0)
            plan_json = json.loads(buf.getvalue())
            self.assertIn("tiers", plan_json)


if __name__ == "__main__":
    unittest.main()
