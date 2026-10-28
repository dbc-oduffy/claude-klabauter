"""Arm fix builds land HEAD plus the arm's own hunks while a peer's live hunks survive."""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit import emit, own_hunk_stage

pytestmark = pytest.mark.spawns_process

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
HEAD = "".join(f"line{i}\n" for i in range(1, 21))


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, creationflags=_NOWIN
    )


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "arm.py").write_bytes(HEAD.encode())
    _git(tmp_path, "add", "arm.py")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _live(peer: bool) -> str:
    lines = HEAD.splitlines(keepends=True)
    lines[2] = "own3\n"
    if peer:
        lines[15:15] = ["peer-a\n", "peer-b\n"]
    return "".join(lines)


def test_landed_blob_is_head_plus_own_hunks_and_peer_hunks_survive(tmp_path):
    root = _repo(tmp_path)
    live = root / "arm.py"
    live.write_bytes(_live(peer=True).encode())
    before = live.read_bytes()

    diff = own_hunk_stage.read_diff(root, ["arm.py"])["arm.py"]
    assert len(diff.hunks) == 2
    own = [h for h in diff.hunks if h.new_start == 3]
    sha = own_hunk_stage.own_blob(root, "arm.py", diff.head_bytes, own)

    got = git_objects.read_object(resolve_git_common_dir(root), sha)
    assert got[1].decode() == _live(peer=False)
    assert b"peer-a" not in got[1]
    assert live.read_bytes() == before


def test_a_blob_that_would_carry_the_peer_hunk_is_the_refused_side(tmp_path):
    root = _repo(tmp_path)
    (root / "arm.py").write_bytes(_live(peer=True).encode())
    diff = own_hunk_stage.read_diff(root, ["arm.py"])["arm.py"]
    sha = own_hunk_stage.own_blob(root, "arm.py", diff.head_bytes, diff.hunks)
    got = git_objects.read_object(resolve_git_common_dir(root), sha)
    assert got[1].decode() == _live(peer=True)
    assert own_hunk_stage.check_head_unchanged(root, {"arm.py": diff}) is None


def test_head_moved_is_detected_before_landing(tmp_path):
    root = _repo(tmp_path)
    (root / "arm.py").write_bytes(_live(peer=True).encode())
    diff = own_hunk_stage.read_diff(root, ["arm.py"])["arm.py"]
    _git(root, "commit", "-q", "--allow-empty", "-m", "noop")
    (root / "other.txt").write_text("x")
    _git(root, "add", "other.txt")
    _git(root, "commit", "-q", "-m", "other")
    assert own_hunk_stage.check_head_unchanged(root, {"arm.py": diff}) is None
    _git(root, "add", "arm.py")
    _git(root, "commit", "-q", "-m", "moved")
    assert own_hunk_stage.check_head_unchanged(root, {"arm.py": diff}) == "arm.py"


def test_every_arm_brief_points_at_the_export_builder():
    clause = emit._isolated_arm_build_clause()
    module = importlib.import_module(emit._LANDING_EXPORT_MODULE)
    assert emit._LANDING_EXPORT_MODULE in clause
    assert callable(module.build_landing_export)
