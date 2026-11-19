"""Tests for Openfarm v0.5 packaging, bundled skills, `install-skill`, and `init`."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import openfarm_core  # noqa: E402
from openfarm_core.bootstrap import (  # noqa: E402
    initialize_project,
    install_skill_bundle,
    skill_assets_root,
    validate_project_name,
)
from openfarm_core.cli import EXIT_REFUSED, EXIT_USAGE, main as cli_main  # noqa: E402
from openfarm_core.config import load_config  # noqa: E402
from openfarm_core.errors import ConfigError, Refused, UsageError  # noqa: E402
from openfarm_core.git import Git  # noqa: E402

ENABLED = os.environ.get("OPENFARM_E2E") == "1"
OPENFARM = ROOT / "openfarm"

EXPECTED_SKILL_ASSETS = frozenset({
    "commands/openfarm-grill-me.md",
    "commands/openfarm-initialize.md",
    "plugin/plugin.json",
    "plugin/rules/AGENTS.md",
    "skills/openfarm-grill-me/SKILL.md",
    "skills/openfarm-grill-me/prompts/grill_protocol.md",
    "skills/openfarm-initialize/SKILL.md",
    "skills/openfarm-initialize/prompts/architect_system.md",
    "skills/openfarm-initialize/prompts/card_decomposer.md",
    "skills/openfarm/SKILL.md",
    "skills/openfarm/prompts/wave_operator.md",
    "skills/openfarm/references/cheat_sheet.md",
})

CALORIE_INVARIANTS_RS = """\
//! Domain invariants protected by `[policy] never_touch`.
//!
//! Openfarm keeps this file out of lesser-model briefs and edits.

pub const SCHEMA_VERSION: u32 = 1;
pub const MAX_KCAL_PER_DAY: u32 = 25_000;

/// Errors returned by validated calorie domain operations.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DomainError {
    EmptyLabel,
    Overflow,
    OutOfRange,
    DuplicateId,
}

/// Validated non-negative calorie count bounded by `MAX_KCAL_PER_DAY`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct Kcal(u32);

impl Kcal {
    /// Zero calories constant.
    pub const ZERO: Self = Self(0);

    /// Construct a bounded `Kcal` value.
    pub fn new(raw: u32) -> Result<Self, DomainError> {
        if raw > MAX_KCAL_PER_DAY {
            return Err(DomainError::OutOfRange);
        }
        Ok(Self(raw))
    }

    /// Return the underlying integer calorie count.
    pub fn value(self) -> u32 {
        self.0
    }

    /// Checked addition respecting `MAX_KCAL_PER_DAY`.
    pub fn checked_add(self, other: Self) -> Result<Self, DomainError> {
        let sum = self.0.checked_add(other.0).ok_or(DomainError::Overflow)?;
        Self::new(sum)
    }
}

/// Validated macronutrient breakdown in whole grams.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MacroGrams {
    pub protein: u32,
    pub carbs: u32,
    pub fat: u32,
}

impl MacroGrams {
    /// Compute energy via checked Atwater multipliers (4 kcal/g protein & carbs, 9 kcal/g fat).
    pub fn to_kcal(self) -> Result<Kcal, DomainError> {
        let p = self.protein.checked_mul(4).ok_or(DomainError::Overflow)?;
        let c = self.carbs.checked_mul(4).ok_or(DomainError::Overflow)?;
        let f = self.fat.checked_mul(9).ok_or(DomainError::Overflow)?;
        let total = p
            .checked_add(c)
            .and_then(|pc| pc.checked_add(f))
            .ok_or(DomainError::Overflow)?;
        Kcal::new(total)
    }
}

/// Normalize and require a non-empty trimmed label.
pub fn ensure_non_empty_label(label: &str) -> Result<String, DomainError> {
    let trimmed = label.trim();
    if trimmed.is_empty() {
        return Err(DomainError::EmptyLabel);
    }
    Ok(trimmed.to_string())
}
"""

CALORIE_GOALS_RS = """\
//! Golden reference vertical slice: daily calorie targets and budget evaluation.

use crate::invariants::{DomainError, Kcal, MAX_KCAL_PER_DAY};

/// User's daily calorie and macronutrient target.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DailyTarget {
    pub kcal_limit: Kcal,
    pub protein_min_g: u32,
    pub carbs_target_g: u32,
    pub fat_limit_g: u32,
}

/// Comparison result for consumed energy against a `DailyTarget`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CalorieBudgetStatus {
    pub consumed: Kcal,
    pub limit: Kcal,
    pub remaining: Kcal,
    pub exceeded_by: Option<Kcal>,
}

impl DailyTarget {
    /// Create a validated daily target with a strictly positive calorie limit.
    pub fn new(
        kcal_limit: u32,
        protein_min_g: u32,
        carbs_target_g: u32,
        fat_limit_g: u32,
    ) -> Result<Self, DomainError> {
        if kcal_limit == 0 || kcal_limit > MAX_KCAL_PER_DAY {
            return Err(DomainError::OutOfRange);
        }
        Ok(Self {
            kcal_limit: Kcal::new(kcal_limit)?,
            protein_min_g,
            carbs_target_g,
            fat_limit_g,
        })
    }

    /// Evaluate how `consumed` compares against `self.kcal_limit`.
    pub fn evaluate(&self, consumed: Kcal) -> CalorieBudgetStatus {
        let limit_val = self.kcal_limit.value();
        let consumed_val = consumed.value();
        if consumed_val <= limit_val {
            let remaining = match Kcal::new(limit_val - consumed_val) {
                Ok(k) => k,
                Err(_) => Kcal::ZERO,
            };
            CalorieBudgetStatus {
                consumed,
                limit: self.kcal_limit,
                remaining,
                exceeded_by: None,
            }
        } else {
            let over = match Kcal::new(consumed_val - limit_val) {
                Ok(k) => k,
                Err(_) => self.kcal_limit,
            };
            CalorieBudgetStatus {
                consumed,
                limit: self.kcal_limit,
                remaining: Kcal::ZERO,
                exceeded_by: Some(over),
            }
        }
    }
}
"""

CALORIE_ENTRY_STUB_RS = "//! Food entry records (implemented in Wave 1).\n"
CALORIE_LOGBOOK_STUB_RS = "//! Daily calorie logbook aggregation (implemented in Wave 1-2).\n"

CALORIE_LIB_RS = """\
pub mod entry;
pub mod goals;
pub mod invariants;
pub mod logbook;

pub use goals::{CalorieBudgetStatus, DailyTarget};
pub use invariants::{
    ensure_non_empty_label, DomainError, Kcal, MacroGrams, MAX_KCAL_PER_DAY, SCHEMA_VERSION,
};

pub fn project_name() -> &'static str {
    "calorie-log"
}
"""

CALORIE_BASELINE_TESTS_RS = """\
use calorie_log::{
    ensure_non_empty_label, project_name, DailyTarget, DomainError, Kcal, MacroGrams,
    SCHEMA_VERSION,
};

#[test]
fn baseline_invariants_and_schema_are_valid() {
    assert_eq!(SCHEMA_VERSION, 1);
    assert_eq!(project_name(), "calorie-log");
    assert_eq!(ensure_non_empty_label("  Oats  "), Ok("Oats".to_string()));
    assert_eq!(ensure_non_empty_label("   "), Err(DomainError::EmptyLabel));
}

#[test]
fn baseline_macros_compute_checked_kcal() {
    let macros = MacroGrams {
        protein: 20,
        carbs: 30,
        fat: 10,
    };
    let kcal = macros.to_kcal();
    assert_eq!(kcal, Kcal::new(290));
    assert_eq!(
        MacroGrams {
            protein: u32::MAX,
            carbs: 0,
            fat: 0,
        }
        .to_kcal(),
        Err(DomainError::Overflow)
    );
}

#[test]
fn baseline_golden_slice_evaluates_daily_target() {
    assert_eq!(
        DailyTarget::new(0, 100, 200, 60),
        Err(DomainError::OutOfRange)
    );
    let target = DailyTarget::new(2_000, 140, 200, 65).ok();
    assert!(target.is_some());
    if let (Some(t), Ok(consumed), Ok(expected_rem)) = (target, Kcal::new(1_450), Kcal::new(550)) {
        let status = t.evaluate(consumed);
        assert_eq!(status.remaining, expected_rem);
        assert_eq!(status.exceeded_by, None);
    }
}
"""

CALORIE_CARD_001 = """\
+++
id = "CARD-001"
title = "Implement validated FoodEntry and MealSlot"
state = "ready"
touches = ["src/entry.rs"]
depends = []
category = "small-feature"
reference = "src/goals.rs"
test_edits = ["tests/entry_tests.rs"]
frontier_estimate = 2.0
new_symbols = true
unit = "calorie_log"
type_changes = true

[[tests]]
name = "valid_food_entry_computes_atwater_kcal"
file = "tests/entry_tests.rs"
asserts = "FoodEntry::new(\\"Oats\\", MealSlot::Breakfast, MacroGrams { protein: 4, carbs: 27, fat: 5 }) returns Ok(e) with e.kcal().value() == 169."

[[tests]]
name = "food_entry_rejects_blank_name_and_macro_overflow"
file = "tests/entry_tests.rs"
asserts = "FoodEntry::new(\\"   \\", MealSlot::Snack, ...) returns Err(DomainError::EmptyLabel) and u32::MAX protein returns Err(DomainError::Overflow)."
+++

## Done

- Export `pub enum MealSlot { Breakfast, Lunch, Dinner, Snack }` and `pub struct FoodEntry` in `src/entry.rs` (`calorie_log::entry::{FoodEntry, MealSlot}`, since `src/lib.rs` already exports `pub mod entry;`).
- `pub fn FoodEntry::new(name: &str, meal: MealSlot, macros: MacroGrams) -> Result<Self, DomainError>` validates `name` via `ensure_non_empty_label` and computes energy via `macros.to_kcal()`.
- Export accessors `pub fn name(&self) -> &str`, `pub fn meal(&self) -> MealSlot`, `pub fn macros(&self) -> MacroGrams`, and `pub fn kcal(&self) -> Kcal`.
"""

CALORIE_CARD_900 = """\
+++
id = "CARD-900"
title = "Seam violation control test"
state = "ready"
touches = ["src/invariants.rs"]
depends = []
category = "mechanical"
test_edits = []
frontier_estimate = 1.0
new_symbols = false
+++

## Done

- Modify `MAX_KCAL_PER_DAY` in `src/invariants.rs` (expected to be refused by `never_touch`).
"""

CARD_001_ENTRY_TESTS = """\
use calorie_log::entry::{FoodEntry, MealSlot};
use calorie_log::{DomainError, MacroGrams};

#[test]
fn valid_food_entry_computes_atwater_kcal() {
    let macros = MacroGrams {
        protein: 4,
        carbs: 27,
        fat: 5,
    };
    let entry = FoodEntry::new("  Oats  ", MealSlot::Breakfast, macros);
    assert!(entry.is_ok());
    if let Ok(item) = entry {
        assert_eq!(item.name(), "Oats");
        assert_eq!(item.meal(), MealSlot::Breakfast);
        assert_eq!(item.macros(), macros);
        assert_eq!(item.kcal().value(), 169);
    }
}

#[test]
fn food_entry_rejects_blank_name_and_macro_overflow() {
    let macros = MacroGrams {
        protein: 4,
        carbs: 27,
        fat: 5,
    };
    assert_eq!(
        FoodEntry::new("   ", MealSlot::Snack, macros),
        Err(DomainError::EmptyLabel)
    );
    let overflow = MacroGrams {
        protein: u32::MAX,
        carbs: 0,
        fat: 0,
    };
    assert_eq!(
        FoodEntry::new("Whey", MealSlot::Lunch, overflow),
        Err(DomainError::Overflow)
    );
}
"""

CARD_001_ENTRY_STUB = """\
use crate::invariants::{DomainError, Kcal, MacroGrams};

/// Meal category for a food log entry.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MealSlot {
    Breakfast,
    Lunch,
    Dinner,
    Snack,
}

/// Validated food log entry.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FoodEntry {
    name: String,
    meal: MealSlot,
    macros: MacroGrams,
    kcal: Kcal,
}

impl FoodEntry {
    /// Construct a validated food entry.
    pub fn new(_name: &str, _meal: MealSlot, _macros: MacroGrams) -> Result<Self, DomainError> {
        todo!()
    }

    /// Entry label.
    pub fn name(&self) -> &str {
        let _ = (&self.name, self.meal, self.macros, self.kcal);
        todo!()
    }

    /// Assigned meal slot.
    pub fn meal(&self) -> MealSlot {
        todo!()
    }

    /// Entry macronutrients.
    pub fn macros(&self) -> MacroGrams {
        todo!()
    }

    /// Computed calories.
    pub fn kcal(&self) -> Kcal {
        todo!()
    }
}
"""

CARD_001_ENTRY_IMPL = """\
//! Validated food entry records.

use crate::invariants::{ensure_non_empty_label, DomainError, Kcal, MacroGrams};

/// Meal category for a food log entry.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MealSlot {
    Breakfast,
    Lunch,
    Dinner,
    Snack,
}

/// Validated food log entry.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FoodEntry {
    name: String,
    meal: MealSlot,
    macros: MacroGrams,
    kcal: Kcal,
}

impl FoodEntry {
    /// Construct a validated food entry with normalized label and checked calories.
    pub fn new(name: &str, meal: MealSlot, macros: MacroGrams) -> Result<Self, DomainError> {
        let normalized = ensure_non_empty_label(name)?;
        let kcal = macros.to_kcal()?;
        Ok(Self {
            name: normalized,
            meal,
            macros,
            kcal,
        })
    }

    /// Return the entry label.
    pub fn name(&self) -> &str {
        &self.name
    }

    /// Return the assigned meal slot.
    pub fn meal(&self) -> MealSlot {
        self.meal
    }

    /// Return the entry macronutrients.
    pub fn macros(&self) -> MacroGrams {
        self.macros
    }

    /// Return the computed calorie energy.
    pub fn kcal(&self) -> Kcal {
        self.kcal
    }
}
"""


def _run_openfarm_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "openfarm_core.cli", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )


def _parse_frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    assert len(lines) >= 3 and lines[0].strip() == "---", "missing YAML frontmatter opening"
    end = lines.index("---", 1)
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" in line and not line.startswith(" "):
            key, val = line.split(":", 1)
            fields[key.strip()] = val.strip()
    return fields


class PackagingAndWheelTests(unittest.TestCase):
    def test_pyproject_manifest_and_version(self) -> None:
        pyproject_path = ROOT / "pyproject.toml"
        self.assertTrue(pyproject_path.is_file())
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        project = data["project"]
        self.assertEqual(project["name"], "openfarm")
        self.assertEqual(project["version"], "0.9.2")
        self.assertEqual(project["dependencies"], [])
        self.assertEqual(project["scripts"]["openfarm"], "openfarm_core.cli:main")
        self.assertEqual(openfarm_core.__version__, "0.9.2")

    def test_offline_wheel_contains_all_skill_assets(self) -> None:
        try:
            import setuptools.build_meta as build_meta
        except ImportError:
            self.skipTest("setuptools is not available")

        tmp = Path(tempfile.mkdtemp(prefix="of-wheel-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        src_dir = tmp / "src"
        dist_dir = tmp / "dist"
        src_dir.mkdir()
        dist_dir.mkdir()

        shutil.copy2(ROOT / "pyproject.toml", src_dir / "pyproject.toml")
        shutil.copy2(ROOT / "README.md", src_dir / "README.md")
        shutil.copytree(ROOT / "openfarm_core", src_dir / "openfarm_core")

        old_cwd = Path.cwd()
        try:
            os.chdir(src_dir)
            wheel_name = build_meta.build_wheel(str(dist_dir))
        finally:
            os.chdir(old_cwd)

        wheel_path = dist_dir / wheel_name
        self.assertTrue(wheel_path.is_file(), f"missing wheel {wheel_path}")
        with zipfile.ZipFile(wheel_path) as zf:
            members = set(zf.namelist())
        for rel in EXPECTED_SKILL_ASSETS:
            self.assertIn(f"openfarm_core/skill_assets/{rel}", members)
        self.assertIn("openfarm_core/bootstrap.py", members)
        self.assertFalse((ROOT / "build").exists())


class SkillBundleParityTests(unittest.TestCase):
    def test_repo_skills_mirror_packaged_skill_assets_byte_for_byte(self) -> None:
        assets_root = skill_assets_root()
        actual_assets = {
            p.relative_to(assets_root).as_posix()
            for p in assets_root.rglob("*")
            if p.is_file()
        }
        self.assertEqual(actual_assets, EXPECTED_SKILL_ASSETS)

        packaged_skills = assets_root / "skills"
        repo_skills = ROOT / "skills"
        pkg_rels = {
            p.relative_to(packaged_skills).as_posix()
            for p in packaged_skills.rglob("*")
            if p.is_file()
        }
        repo_rels = {
            p.relative_to(repo_skills).as_posix()
            for p in repo_skills.rglob("*")
            if p.is_file()
        }
        self.assertEqual(pkg_rels, repo_rels)
        self.assertEqual(len(pkg_rels), 8)
        for rel in sorted(pkg_rels):
            self.assertEqual(
                (repo_skills / rel).read_bytes(),
                (packaged_skills / rel).read_bytes(),
                f"skill drift on {rel}",
            )

    def test_skill_frontmatter_command_and_plugin_schema(self) -> None:
        assets_root = skill_assets_root()
        root_fm = _parse_frontmatter((ROOT / "SKILL.md").read_text(encoding="utf-8"))
        self.assertEqual(root_fm.get("name"), "openfarm-installer")
        self.assertTrue(root_fm.get("description"))

        for skill_name in ("openfarm-grill-me", "openfarm-initialize", "openfarm"):
            fm = _parse_frontmatter(
                (assets_root / "skills" / skill_name / "SKILL.md").read_text(encoding="utf-8")
            )
            self.assertEqual(fm.get("name"), skill_name)
            self.assertTrue(fm.get("description"))

        for cmd_name in ("openfarm-initialize.md", "openfarm-grill-me.md"):
            cmd_text = (assets_root / "commands" / cmd_name).read_text(
                encoding="utf-8"
            )
            self.assertIn("$ARGUMENTS", cmd_text)

        plugin = json.loads(
            (assets_root / "plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(plugin.get("name"), "openfarm")
        rules_text = (assets_root / "plugin" / "rules" / "AGENTS.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("openfarm", rules_text.lower())


class InstallSkillTests(unittest.TestCase):
    def test_install_skill_dry_run_and_real_all_with_bin_shim(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="of-install-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        fake_home = tmp / "home"
        bin_dir = tmp / "bin"

        dry = _run_openfarm_cli(
            "install-skill",
            "--target",
            "all",
            "--dest",
            str(fake_home),
            "--install-bin",
            str(bin_dir),
            "--dry-run",
            "--json",
        )
        self.assertEqual(dry.returncode, 0, dry.stderr)
        dry_data = json.loads(dry.stdout)
        self.assertTrue(dry_data["ok"])
        self.assertTrue(dry_data["dry_run"])
        self.assertFalse((fake_home / ".claude").exists())
        self.assertFalse((bin_dir / "openfarm").exists())

        real = _run_openfarm_cli(
            "install-skill",
            "--target",
            "all",
            "--dest",
            str(fake_home),
            "--install-bin",
            str(bin_dir),
            "--json",
        )
        self.assertEqual(real.returncode, 0, real.stderr)
        real_data = json.loads(real.stdout)
        self.assertTrue(real_data["ok"])
        self.assertFalse(real_data["dry_run"])

        expected_paths = (
            fake_home / ".claude" / "skills" / "openfarm-grill-me" / "SKILL.md",
            fake_home / ".claude" / "skills" / "openfarm-initialize" / "SKILL.md",
            fake_home / ".claude" / "skills" / "openfarm" / "SKILL.md",
            fake_home / ".claude" / "commands" / "openfarm-grill-me.md",
            fake_home / ".claude" / "commands" / "openfarm-initialize.md",
            fake_home / ".gemini" / "config" / "skills" / "openfarm-grill-me" / "SKILL.md",
            fake_home / ".gemini" / "config" / "skills" / "openfarm-initialize" / "SKILL.md",
            fake_home / ".gemini" / "config" / "plugins" / "openfarm" / "plugin.json",
            fake_home / ".gemini" / "config" / "plugins" / "openfarm" / "rules" / "AGENTS.md",
            fake_home / ".agents" / "skills" / "openfarm-grill-me" / "SKILL.md",
            fake_home / ".agents" / "skills" / "openfarm-initialize" / "SKILL.md",
        )
        for path in expected_paths:
            self.assertTrue(path.is_file(), f"missing installed asset: {path}")

        shim = bin_dir / "openfarm"
        self.assertTrue(shim.is_file())
        self.assertTrue(os.access(shim, os.X_OK))
        help_res = subprocess.run([str(shim), "--help"], capture_output=True, text=True)
        self.assertEqual(help_res.returncode, 0, help_res.stderr)
        self.assertIn("install-skill", help_res.stdout)
        self.assertIn("init", help_res.stdout)
        self.assertIn("acp", help_res.stdout)

    def test_install_skill_workspace_and_invalid_target(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="of-ws-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        ws = tmp / "project"
        ws.mkdir()

        res = _run_openfarm_cli(
            "install-skill", "--target", "workspace", "--dest", str(ws), "--json"
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(
            (ws / ".agents" / "skills" / "openfarm-grill-me" / "SKILL.md").is_file()
        )
        self.assertTrue(
            (ws / ".agents" / "skills" / "openfarm-initialize" / "SKILL.md").is_file()
        )
        self.assertTrue(
            (ws / ".claude" / "commands" / "openfarm-grill-me.md").is_file()
        )
        self.assertTrue(
            (ws / ".claude" / "commands" / "openfarm-initialize.md").is_file()
        )

        with self.assertRaises(UsageError):
            install_skill_bundle(target="invalid", dest=ws)
        bad = _run_openfarm_cli("install-skill", "--target", "invalid")
        self.assertEqual(bad.returncode, EXIT_USAGE)


class InitBootstrapTests(unittest.TestCase):
    def test_init_calorie_log_validates_config_perms_lockfile_and_git(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="of-init-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        repo = tmp / "calorie-log"
        state = tmp / "state"
        secrets = tmp / "secrets"

        res = _run_openfarm_cli(
            "init",
            "calorie-log",
            "--dir",
            str(repo),
            "--state-dir",
            str(state),
            "--secrets-dir",
            str(secrets),
            "--mutation",
            "off",
            "--idea",
            "An app to track my calories",
            "--commit",
            "--json",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        self.assertTrue(data["ok"])
        self.assertTrue(data["config_valid"])
        self.assertEqual(data["project"], "calorie-log")
        self.assertEqual(data["crate_name"], "calorie_log")
        self.assertEqual(data["base_branch"], "main")
        self.assertEqual(data["profile"], "active")
        self.assertEqual(data["micromanager_adapter"], "claude-acp")
        self.assertEqual(data["micromanager_mode"], "active")
        self.assertEqual(data["micromanager_auth"], "oauth")
        self.assertEqual(data["micromanager_binary"], "auto")

        for rel in (
            "Cargo.toml",
            "Cargo.lock",
            ".gitignore",
            "src/invariants.rs",
            "src/lib.rs",
            "tests/baseline_tests.rs",
            ".openfarm/config.toml",
            ".openfarm/PROJECT_SPEC.md",
            ".openfarm/test-conventions.md",
            "AGENTS.md",
            "cards/.gitkeep",
        ):
            self.assertTrue((repo / rel).is_file(), f"missing scaffold file {rel}")

        cfg = load_config(repo)
        self.assertEqual(cfg.name, "calorie-log")
        self.assertIn("src/invariants.rs", cfg.never_touch)
        self.assertTrue(any(".openfarm/**" == g for g in cfg.forbidden_files))
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(secrets.stat().st_mode), 0o700)
        self.assertTrue((repo / ".git").is_dir())
        self.assertTrue(Git(repo).head())

    def test_init_refuses_non_empty_dir_unless_forced(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="of-nonempty-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        repo = tmp / "app"
        state = tmp / "state"
        secrets = tmp / "secrets"
        repo.mkdir()
        (repo / "README.txt").write_text("existing\n", encoding="utf-8")

        rc = cli_main([
            "init",
            "calorie-log",
            "--dir",
            str(repo),
            "--state-dir",
            str(state),
            "--secrets-dir",
            str(secrets),
            "--no-git",
            "--json",
        ])
        self.assertEqual(rc, EXIT_REFUSED)
        with self.assertRaises(Refused):
            initialize_project(
                "calorie-log",
                directory=repo,
                state_dir=state,
                secrets_dir=secrets,
                no_git=True,
            )

        rc_forced = cli_main([
            "init",
            "calorie-log",
            "--dir",
            str(repo),
            "--state-dir",
            str(state),
            "--secrets-dir",
            str(secrets),
            "--no-git",
            "--force",
            "--json",
        ])
        self.assertEqual(rc_forced, 0)

    def test_init_rejects_in_repo_state_or_key_and_invalid_names(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="of-badinit-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        repo = tmp / "app"
        outside_state = tmp / "state"
        outside_sec = tmp / "secrets"

        rc_state = cli_main([
            "init",
            "calorie-log",
            "--dir",
            str(repo),
            "--state-dir",
            str(repo / "state"),
            "--secrets-dir",
            str(outside_sec),
            "--no-git",
        ])
        self.assertEqual(rc_state, EXIT_USAGE)

        with self.assertRaises(ConfigError):
            initialize_project(
                "calorie-log",
                directory=repo,
                state_dir=outside_state,
                key_file=repo / "leak.key",
                no_git=True,
            )
        rc_key = cli_main([
            "init",
            "calorie-log",
            "--dir",
            str(repo),
            "--state-dir",
            str(outside_state),
            "--key-file",
            str(repo / "leak.key"),
            "--no-git",
        ])
        self.assertEqual(rc_key, EXIT_USAGE)

        for bad in ("../bad", "fn", "9bad", ""):
            with self.assertRaises(UsageError):
                validate_project_name(bad)
        bad_cli = cli_main(["init", "fn", "--dir", str(repo), "--no-git"])
        self.assertEqual(bad_cli, EXIT_USAGE)


@unittest.skipUnless(ENABLED, "set OPENFARM_E2E=1 to run cargo-backed v0.5 tests")
class StarterE2ETests(unittest.TestCase):
    def _cargo_env(self, target_dir: Path) -> dict[str, str]:
        env = dict(os.environ)
        env["CARGO_TARGET_DIR"] = str(target_dir)
        return env

    def _run_cargo_checks(self, repo: Path, target_dir: Path, rust_files: list[str]) -> None:
        env = self._cargo_env(target_dir)
        test_res = subprocess.run(
            ["cargo", "test", "--locked"],
            cwd=str(repo),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(test_res.returncode, 0, test_res.stdout + test_res.stderr)

        clippy_res = subprocess.run(
            ["cargo", "clippy", "--locked", "--all-targets", "--", "-D", "warnings"],
            cwd=str(repo),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(clippy_res.returncode, 0, clippy_res.stdout + clippy_res.stderr)

        fmt_res = subprocess.run(
            ["rustfmt", "--edition", "2021", "--check", *rust_files],
            cwd=str(repo),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(fmt_res.returncode, 0, fmt_res.stdout + fmt_res.stderr)

    def test_init_skeleton_passes_cargo_clippy_and_fmt(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="of-init-e2e-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        repo = tmp / "calorie-log"
        res = _run_openfarm_cli(
            "init",
            "calorie-log",
            "--dir",
            str(repo),
            "--state-dir",
            str(tmp / "state"),
            "--secrets-dir",
            str(tmp / "secrets"),
            "--profile",
            "solo",
            "--mutation",
            "off",
            "--commit",
            "--json",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self._run_cargo_checks(
            repo,
            tmp / "target",
            ["src/invariants.rs", "src/lib.rs", "tests/baseline_tests.rs"],
        )

    def test_calorie_log_starter_guide_reaches_in_review(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="calorie-log-e2e-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        repo = tmp / "calorie-log"
        init_res = _run_openfarm_cli(
            "init",
            "calorie-log",
            "--dir",
            str(repo),
            "--state-dir",
            str(tmp / "state"),
            "--secrets-dir",
            str(tmp / "secrets"),
            "--profile",
            "solo",
            "--mutation",
            "off",
            "--idea",
            "An app to track my calories",
            "--json",
        )
        self.assertEqual(init_res.returncode, 0, init_res.stderr)

        (repo / "src" / "invariants.rs").write_text(CALORIE_INVARIANTS_RS, encoding="utf-8")
        (repo / "src" / "goals.rs").write_text(CALORIE_GOALS_RS, encoding="utf-8")
        (repo / "src" / "entry.rs").write_text(CALORIE_ENTRY_STUB_RS, encoding="utf-8")
        (repo / "src" / "logbook.rs").write_text(CALORIE_LOGBOOK_STUB_RS, encoding="utf-8")
        (repo / "src" / "lib.rs").write_text(CALORIE_LIB_RS, encoding="utf-8")
        (repo / "tests" / "baseline_tests.rs").write_text(
            CALORIE_BASELINE_TESTS_RS, encoding="utf-8"
        )

        self._run_cargo_checks(
            repo,
            tmp / "target",
            [
                "src/invariants.rs",
                "src/goals.rs",
                "src/entry.rs",
                "src/logbook.rs",
                "src/lib.rs",
                "tests/baseline_tests.rs",
            ],
        )

        (repo / "cards" / "CARD-001.md").write_text(CALORIE_CARD_001, encoding="utf-8")
        (repo / "cards" / "CARD-900.md").write_text(CALORIE_CARD_900, encoding="utf-8")
        Git(repo).commit_all("chore: calorie-log baseline and cards")

        check_ok = subprocess.run(
            [sys.executable, str(OPENFARM), "--repo", str(repo), "check", "CARD-001", "--json"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(check_ok.returncode, 0, check_ok.stdout + check_ok.stderr)
        ok_data = json.loads(check_ok.stdout)
        self.assertTrue(ok_data["eligible"], ok_data)

        check_seam = subprocess.run(
            [sys.executable, str(OPENFARM), "--repo", str(repo), "check", "CARD-900", "--json"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(check_seam.returncode, EXIT_REFUSED, check_seam.stdout)
        seam_data = json.loads(check_seam.stdout)
        self.assertFalse(seam_data["eligible"])
        self.assertIn("never-touch", json.dumps(seam_data))

        (repo / "cards" / "CARD-900.md").unlink()
        Git(repo).commit_all("chore: drop negative control card")

        dry_run = subprocess.run(
            [
                sys.executable,
                str(OPENFARM),
                "--repo",
                str(repo),
                "run",
                "--dry-run",
                "--card",
                "CARD-001",
                "--json",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(dry_run.returncode, 0, dry_run.stdout + dry_run.stderr)

        script_path = tmp / "script.json"
        script_path.write_text(
            json.dumps({
                "CARD-001/test_writer": (
                    "=== whole_file: tests/entry_tests.rs\n"
                    + CARD_001_ENTRY_TESTS
                    + "\n=== append_stub: src/entry.rs\n"
                    + "valid_food_entry_computes_atwater_kcal\n"
                    + CARD_001_ENTRY_STUB
                ),
                "CARD-001/implementer": (
                    "=== whole_file: src/entry.rs\n" + CARD_001_ENTRY_IMPL
                ),
                "CARD-001/checker_pass1": (
                    "RESTATEMENT: FoodEntry validates non-empty labels and computes checked Atwater kcal."
                ),
                "CARD-001/checker_pass2": json.dumps({
                    "traceability": [
                        {
                            "clause": "FoodEntry::new validates label and macro kcal",
                            "code": "src/entry.rs:25",
                            "test": "tests/entry_tests.rs:5",
                        }
                    ],
                    "edge_cases": ["whitespace-only name", "u32::MAX macro overflow"],
                    "counter_tests": [],
                }),
            }),
            encoding="utf-8",
        )

        run_res = subprocess.run(
            [
                sys.executable,
                str(OPENFARM),
                "--repo",
                str(repo),
                "run",
                "--card",
                "CARD-001",
                "--script",
                str(script_path),
                "--cards",
                "1",
                "--json",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(run_res.returncode, 0, run_res.stdout + run_res.stderr)
        payload = json.loads(run_res.stdout)
        outcome = payload["outcomes"][0]
        self.assertEqual(
            outcome["final_state"],
            "in-review",
            f"{outcome.get('stop_code')}: {outcome.get('detail')}",
        )
        branches = subprocess.run(
            ["git", "branch", "--list", "openfarm/*"],
            cwd=str(repo),
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("openfarm/CARD-001", branches, f"missing branch: {branches!r}")


if __name__ == "__main__":
    unittest.main()
