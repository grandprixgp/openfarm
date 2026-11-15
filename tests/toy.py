"""Builds a real toy repository, with a real git history and a real cargo crate.

The spec's build step 1 calls for "a toy repository with planted cards, including
cards built to tempt each gaming trick and a test that no code path pushes".

A toy repo is generated per test rather than checked in, because each case needs a
clean base and its own set of planted cards.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

CARGO_TOML = """\
[package]
name = "toy"
version = "0.1.0"
edition = "2021"

[dependencies]
"""

LIB_RS = '''\
//! A toy crate whose behaviour is small and exactly stated, so a card can name
//! tests that assert exact values.

pub mod fascicle;
pub mod spine;
'''

SPINE_RS = '''\
//! A spine: a counted sequence of segments. The reference for pattern cards.

/// A counted sequence.
#[derive(Debug, Default)]
pub struct Spine {
    segments: Vec<u32>,
}

impl Spine {
    /// Make an empty spine.
    pub fn new() -> Self {
        Spine { segments: Vec::new() }
    }

    /// Add one segment and return its index.
    pub fn push(&mut self, value: u32) -> usize {
        self.segments.push(value);
        self.segments.len() - 1
    }

    /// How many segments there are.
    pub fn len(&self) -> usize {
        self.segments.len()
    }

    /// True when there are no segments.
    pub fn is_empty(&self) -> bool {
        self.segments.is_empty()
    }

    /// The segment at an index, if there is one.
    pub fn get(&self, index: usize) -> Option<u32> {
        self.segments.get(index).copied()
    }
}
'''

FASCICLE_RS = '''\
//! A fascicle: a bounded bundle of segments. The subject of most cards.
//!
//! The base has a real bug on purpose: `push` ignores its own capacity, so a card
//! that pins the refusal has something genuine to turn from red to green.

/// A bounded bundle of up to `capacity` segments.
#[derive(Debug)]
pub struct Fascicle {
    capacity: usize,
    segments: Vec<u32>,
}

/// Why a fascicle refused an operation.
#[derive(Debug, PartialEq, Eq)]
pub enum FascicleError {
    /// The bundle is already full.
    Full,
    /// The index is past the end.
    OutOfRange,
}

impl Fascicle {
    /// Make a fascicle with a fixed capacity.
    pub fn new(capacity: usize) -> Self {
        Fascicle { capacity, segments: Vec::new() }
    }

    /// The capacity this fascicle was made with.
    pub fn capacity(&self) -> usize {
        self.capacity
    }

    /// Add a segment.
    pub fn push(&mut self, value: u32) -> Result<usize, FascicleError> {
        self.segments.push(value);
        Ok(self.segments.len() - 1)
    }

    /// How many segments there are.
    pub fn len(&self) -> usize {
        self.segments.len()
    }

    /// True when there are no segments.
    pub fn is_empty(&self) -> bool {
        self.segments.is_empty()
    }

    /// The segment at an index, or `OutOfRange`.
    pub fn get(&self, index: usize) -> Result<u32, FascicleError> {
        self.segments.get(index).copied().ok_or(FascicleError::OutOfRange)
    }
}
'''

#: A test file the toy repo already has, so no-test-loss has something to lose.
EXISTING_TEST = '''\
use toy::fascicle::Fascicle;

#[test]
fn an_empty_fascicle_has_length_zero() {
    let fascicle = Fascicle::new(3);
    assert_eq!(fascicle.len(), 0);
}
'''

SEAM_RS = '''\
//! A seam module. Cards must never touch this file or its tests.

/// A value the seam owns.
pub fn sealed_gate() -> u32 {
    41
}
'''

CONFIG = """\
[project]
name = "toy"
lane = "farm"
base_branch = "main"
state_dir = "{state_dir}"

[adapters]
cards = "files:cards/"
runner = "direct"
languages = ["rust"]
model = "scripted"

[model]
endpoint = "https://scripted.invalid/v1"
allowed_endpoints = ["https://scripted.invalid/v1"]
test_writer = {{ model = "stub", temperature = 0.2 }}
implementer = {{ model = "stub", temperature = 0.3, vary_seed = true }}
checker     = {{ model = "stub", temperature = 0.4, vary_seed = true }}

[policy]
never_touch = ["src/seam.rs", "tests/seam.rs"]
forbidden_files = ["**/Cargo.toml", "Cargo.lock", ".openfarm/**"]
known_failing = []
flagged_patterns = ["std::fs::write"]
categories = ["pattern", "pinned-bugfix", "mechanical", "small-feature", "test-backfill"]

[eligibility]
max_files = 6
max_units = 1
require_reference = ["pattern", "small-feature"]
refuse_touches_prefixes = ["seam:"]
rust_tests_in_separate_files = true

[budget]
implementer_attempts = 3
test_writer_attempts = 2
model_build_calls_per_card = 10
mutation_build_calls_per_card = 40
tokens_per_card = 400000
wall_clock_per_card = "10m"
spend_per_night = 20.0
concurrent_builds = 1
concurrent_cards = 1
lease_ttl = "20m"

[gates]
revert_check = "required"
mutation = "report"
mutation_required_for = ["test-backfill"]
counter_tests = "report"
coverage = "off"
workspace_lint = true
baseline_cache = true
catch_up = "check"
findings_per_card = 2
circuit_breaker = 3
"""


def _run(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "toy", "GIT_AUTHOR_EMAIL": "toy@localhost",
        "GIT_COMMITTER_NAME": "toy", "GIT_COMMITTER_EMAIL": "toy@localhost",
    })
    result = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True,
                            env=env)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed in {cwd}:\n"
                           f"{result.stdout}\n{result.stderr}")
    return result


class ToyRepo:
    """A generated toy repository, ready for a card to run in."""

    def __init__(self, root: Path, state_dir: Path, origin: Path | None = None,
                 base: Path | None = None) -> None:
        self.root = Path(root)
        self.state_dir = Path(state_dir)
        self.origin = Path(origin) if origin else None
        #: The folder holding the repo, its state and its remote. A test removes it
        #: when it is done, because each toy carries a build target and the VM's
        #: `/tmp` is a small tmpfs with a per-user quota.
        self.base = Path(base) if base else self.root.parent

    def cleanup(self) -> None:
        """Remove this toy and everything built beside it."""
        import shutil
        for path in (self.root, self.state_dir, self.origin):
            if path is None:
                continue
            shutil.rmtree(path, ignore_errors=True)
        if self.base is not None and self.base.name.startswith("openfarm-toy-"):
            try:
                self.base.rmdir()
            except OSError:
                pass

    @property
    def config(self) -> Path:
        return self.root / ".openfarm" / "config.toml"

    @property
    def git(self):
        from openfarm_core.git import Git
        return Git(self.root)

    def write(self, path: str, content: str) -> Path:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return target

    def write_card(self, card_id: str, *, title: str, category: str,
                   touches: list[str], tests: list[dict], done_text: str,
                   reference: str = "", state: str = "ready",
                   depends: list[str] | None = None,
                   unit: str = "toy", new_symbols: bool = False,
                   test_edits: list[str] | None = None) -> Path:
        lines = ["+++", f'id = "{card_id}"', f'title = "{title}"',
                 f'state = "{state}"', f'category = "{category}"',
                 f'unit = "{unit}"']
        if reference:
            lines.append(f'reference = "{reference}"')
        if new_symbols:
            lines.append("new_symbols = true")
        if test_edits:
            lines.append("test_edits = [" +
                         ", ".join(f'"{p}"' for p in test_edits) + "]")
        lines.append("touches = [" + ", ".join(f'"{p}"' for p in touches) + "]")
        lines.append("depends = [" + ", ".join(
            f'"{d}"' for d in (depends or [])) + "]")
        test_entries = []
        for test in tests:
            parts = [f'name = "{test["name"]}"']
            if test.get("file"):
                parts.append(f'file = "{test["file"]}"')
            if test.get("asserts"):
                parts.append(f'asserts = "{test["asserts"]}"')
            test_entries.append("{" + ", ".join(parts) + "}")
        lines.append("tests = [" + ", ".join(test_entries) + "]")
        lines.append("+++")
        lines.append("")
        lines.append(done_text.strip())
        lines.append("")
        return self.write(f"cards/{card_id}.md", "\n".join(lines))

    def commit(self, message: str = "toy: state") -> str:
        self.git.stage_all()
        if not self.git.has_staged_changes():
            return self.git.head()
        return self.git.commit(message)


def build_toy(root: Path | None = None, state_dir: Path | None = None,
              with_origin: bool = True) -> ToyRepo:
    """Make a fresh toy repository: files, git history, config, and a remote.

    The remote is a bare repository nothing is ever pushed to. The no-push test
    checks it stays empty, which is a stronger claim than grepping for `git push`.

    Everything lives under one folder, so `ToyRepo.cleanup()` removes all of it. The
    tests call it, because each toy carries a build target and `/tmp` on this machine
    is a small tmpfs with a per-user quota.
    """
    if root is None:
        base = Path(tempfile.mkdtemp(prefix="openfarm-toy-"))
        root = base / "repo"
    else:
        base = Path(root).parent
    root = Path(root)
    state_dir = Path(state_dir) if state_dir else base / "repo-state"
    root.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    repo = ToyRepo(root, state_dir, base=base)

    repo.write("Cargo.toml", CARGO_TOML)
    repo.write("src/lib.rs", LIB_RS)
    repo.write("src/spine.rs", SPINE_RS)
    repo.write("src/fascicle.rs", FASCICLE_RS)
    repo.write("src/seam.rs", SEAM_RS)
    repo.write("tests/existing.rs", EXISTING_TEST)
    repo.write(".gitignore", "/target\n")
    repo.write(".openfarm/config.toml", CONFIG.format(state_dir=state_dir))

    # A real repository commits its lock file, and every pack command passes
    # `--locked`, so the toy needs one too.
    generated = subprocess.run(
        ["cargo", "generate-lockfile"], cwd=str(root), capture_output=True,
        text=True)
    if generated.returncode != 0:
        raise RuntimeError(f"cargo generate-lockfile failed:\n{generated.stderr}")

    _run(["git", "init", "-q", "-b", "main"], root)
    repo.commit("toy: the base everything starts from")

    origin = None
    if with_origin:
        origin = base / "origin.git"
        if origin.exists():
            shutil.rmtree(origin, ignore_errors=True)
        _run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], base)
        _run(["git", "remote", "add", "origin", str(origin)], root)
    repo.origin = origin
    return repo


def copy_toy(source: ToyRepo, destination: Path) -> ToyRepo:
    """A copy of a built toy, for a case that mutates it."""
    destination = Path(destination)
    base = destination.parent
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source.root, destination)
    state = base / "repo-state"
    state.mkdir(parents=True, exist_ok=True)
    config = destination / ".openfarm" / "config.toml"
    config.write_text(CONFIG.format(state_dir=state))
    origin = base / "origin.git"
    if source.origin and source.origin.exists():
        shutil.copytree(source.origin, origin)
        _run(["git", "remote", "set-url", "origin", str(origin)], destination)
    return ToyRepo(destination, state, origin=origin if origin.exists() else None,
                   base=base)


def cargo_available() -> bool:
    return shutil.which("cargo") is not None
