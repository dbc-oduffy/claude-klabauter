from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.testing.git_write import (
    _BUMP_NS,
    write_bytes_seen_by_git,
    write_seen_by_git,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def _committed_repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "core.checkStat", "minimal")
    f = tmp_path / "a.txt"
    f.write_text("one\n")
    old = f.stat().st_mtime_ns - 10_000_000_000
    os.utime(f, ns=(old, old))
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "c")
    return f


def _porcelain(root: Path) -> str:
    return _git(root, "status", "--porcelain")


def test_same_size_rewrite_is_seen_only_through_the_helper(tmp_path):
    f = _committed_repo(tmp_path)
    before = f.stat()
    f.write_text("two\n")  # racy-stat-ok: deliberate same-size write demonstrating the race the helper fixes
    os.utime(f, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert _porcelain(tmp_path) == ""

    write_seen_by_git(f, "six\n")
    assert _porcelain(tmp_path) == " M a.txt\n"


def test_bytes_twin_same_size_rewrite_is_seen(tmp_path):
    f = _committed_repo(tmp_path)
    before = f.stat().st_mtime_ns
    write_bytes_seen_by_git(f, b"two\n")
    assert f.stat().st_mtime_ns >= before + _BUMP_NS
    assert _porcelain(tmp_path) == " M a.txt\n"


def test_size_changing_write_applies_no_bump(tmp_path):
    f = _committed_repo(tmp_path)
    write_seen_by_git(f, "a longer body\n")
    assert f.stat().st_mtime_ns <= time.time_ns() + 1_000_000_000
    assert _porcelain(tmp_path) == " M a.txt\n"


def test_new_file_is_written_unbumped(tmp_path):
    f = tmp_path / "new.txt"
    write_seen_by_git(f, "x\n", newline="\n")
    assert f.read_bytes() == b"x\n"
