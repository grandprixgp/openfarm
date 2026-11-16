from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.bootstrap import initialize_project
from openfarm_core.cli import EXIT_OK, EXIT_REFUSED, main
from openfarm_core.errors import Refused


def _write_git_object(repo: Path, kind: str, data: bytes) -> str:
    full = f"{kind} {len(data)}".encode("utf-8") + b"\0" + data
    digest = hashlib.sha1(full).hexdigest()
    obj_dir = repo / ".git" / "objects" / digest[:2]
    obj_dir.mkdir(parents=True, exist_ok=True)
    (obj_dir / digest[2:]).write_bytes(zlib.compress(full))
    return digest


def _tree_entry(name: str, sha: str, mode: str = "100644") -> bytes:
    return f"{mode} {name}".encode("utf-8") + b"\0" + bytes.fromhex(sha)


def _write_index(repo: Path, file_path: Path, rel_path: str, blob_sha: str) -> None:
    st = file_path.stat()
    ctime_s = st.st_ctime_ns // 1_000_000_000
    ctime_ns = st.st_ctime_ns % 1_000_000_000
    mtime_s = st.st_mtime_ns // 1_000_000_000
    mtime_ns = st.st_mtime_ns % 1_000_000_000
    entry = struct.pack(
        ">10I",
        ctime_s,
        ctime_ns,
        mtime_s,
        mtime_ns,
        st.st_dev & 0xFFFFFFFF,
        st.st_ino & 0xFFFFFFFF,
        0o100644,
        st.st_uid & 0xFFFFFFFF,
        st.st_gid & 0xFFFFFFFF,
        st.st_size & 0xFFFFFFFF,
    )
    entry += bytes.fromhex(blob_sha)
    entry += struct.pack(">H", len(rel_path))
    entry += rel_path.encode("utf-8")
    pad = (8 - (len(entry) % 8)) % 8
    if pad == 0:
        pad = 8
    entry += b"\0" * pad
    header = b"DIRC" + struct.pack(">II", 2, 1)
    body = header + entry
    checksum = hashlib.sha1(body).digest()
    (repo / ".git" / "index").write_bytes(body + checksum)


def _make_git_repo_with_pyproject(root: Path, pyproject_text: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    pyproject = root / "pyproject.toml"
    pyproject.write_text(pyproject_text)
    git_dir = root / ".git"
    (git_dir / "objects").mkdir(parents=True, exist_ok=True)
    (git_dir / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    (git_dir / "config").write_text(
        "[core]\n"
        "\trepositoryformatversion = 0\n"
        "\tfilemode = true\n"
        "\tbare = false\n"
        "\tlogallrefupdates = true\n"
    )
    blob_sha = _write_git_object(root, "blob", pyproject.read_bytes())
    tree_sha = _write_git_object(
        root, "tree", _tree_entry("pyproject.toml", blob_sha)
    )
    commit_data = (
        f"tree {tree_sha}\n"
        f"author Test <test@example.com> 1700000000 +0000\n"
        f"committer Test <test@example.com> 1700000000 +0000\n"
        f"\n"
        f"initial\n"
    ).encode("utf-8")
    commit_sha = _write_git_object(root, "commit", commit_data)
    (git_dir / "refs" / "heads" / "main").write_text(commit_sha + "\n")
    _write_index(root, pyproject, "pyproject.toml", blob_sha)


class AdoptCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_adopt_command_prints_json(self) -> None:
        root = self.tmp / "repo"
        _make_git_repo_with_pyproject(
            root,
            '[project]\nname = "demo"\nversion = "0.1.0"\n',
        )
        state_dir = self.tmp / "state"
        key_file = self.tmp / "key"
        state_dir.mkdir()
        key_file.write_text("test-key\n")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main([
                "--json",
                "adopt",
                "--dir",
                str(root),
                "--state-dir",
                str(state_dir),
                "--key-file",
                str(key_file),
            ])

        self.assertEqual(code, EXIT_OK)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["language"], "python")

    def test_adopt_command_reports_a_refusal(self) -> None:
        root = self.tmp / "plain"
        root.mkdir()
        state_dir = self.tmp / "state"
        key_file = self.tmp / "key"
        state_dir.mkdir()
        key_file.write_text("test-key\n")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main([
                "--json",
                "adopt",
                "--dir",
                str(root),
                "--state-dir",
                str(state_dir),
                "--key-file",
                str(key_file),
            ])

        self.assertEqual(code, EXIT_REFUSED)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["kind"], "refused")

    def test_init_on_a_full_folder_names_adopt(self) -> None:
        root = self.tmp / "full"
        root.mkdir()
        (root / "file.txt").write_text("hello\n")

        with self.assertRaises(Refused) as caught:
            initialize_project("x", directory=root)

        exc = caught.exception
        self.assertEqual(exc.code, "dir-not-empty")
        self.assertIn("openfarm adopt", exc.message)
        self.assertIn(f"openfarm adopt --dir {root.resolve()}", exc.message)


if __name__ == "__main__":
    unittest.main()