"""Tests for `coordinator_core.git.git_objects._parse_pack_index`'s
memoization, keyed `(path, st_mtime_ns, st_size)`.

The point of this suite is THE INVALIDATION, not the speedup: a memo that
never invalidates would still make "second call is faster" pass, which is
the dangerous version of this change (see C0's brief). Every test here
rewrites a real `.idx` file in place and asserts the memo tracks the new
parse rather than serving a stale one -- never a bare timing assertion.

Spec backlink: docs/plans/2026-08-26-the-archival-commit-helper-computes-its-own-tree.md, chunk C0
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.git.git_dir import resolve_git_dir
from coordinator_core.git.git_objects import (
    _PACK_INDEX_CACHE,
    _parse_pack_index,
)
from coordinator_core.win_portability import no_console_creationflags


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        **no_console_creationflags(),
    )


def _init_repo_with_pack(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "t@t", cwd=path)
    _git("config", "user.name", "t", cwd=path)
    _git("config", "commit.gpgsign", "false", cwd=path)
    (path / "seed.md").write_text("seed\n", encoding="utf-8")
    _git("add", "-A", cwd=path)
    _git("commit", "-qm", "seed", cwd=path)
    _git("repack", "-ad", "-q", cwd=path)

    gitdir = resolve_git_dir(path)
    pack_dir = gitdir / "objects" / "pack"
    idx_paths = sorted(pack_dir.glob("*.idx"))
    assert len(idx_paths) == 1, f"expected exactly one pack after repack -ad, got {idx_paths}"
    return idx_paths[0]


def _bump_mtime(path: Path) -> None:
    st = path.stat()
    new_ns = st.st_mtime_ns + 10_000_000_000
    os.utime(path, ns=(new_ns, new_ns))


@pytest.fixture(autouse=True)
def _clear_pack_index_cache():
    _PACK_INDEX_CACHE.clear()
    yield
    _PACK_INDEX_CACHE.clear()


def test_second_call_returns_memoized_object_not_a_fresh_parse(tmp_path: Path) -> None:
    idx_path = _init_repo_with_pack(tmp_path)

    first = _parse_pack_index(idx_path)
    assert first is not None
    key = (str(idx_path), idx_path.stat().st_mtime_ns, idx_path.stat().st_size)
    assert key in _PACK_INDEX_CACHE

    second = _parse_pack_index(idx_path)
    assert second is first


def test_rewritten_pack_index_invalidates_the_memo(tmp_path: Path) -> None:
    idx_path = _init_repo_with_pack(tmp_path)

    original = _parse_pack_index(idx_path)
    assert original is not None
    original_shas = original.shas

    # something derived once and cached forever) we overwrite the ORIGINAL
    (tmp_path / "second.md").write_text("second\n", encoding="utf-8")
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-qm", "second", cwd=tmp_path)
    _git("repack", "-ad", "-q", cwd=tmp_path)

    gitdir = resolve_git_dir(tmp_path)
    new_idx_paths = sorted((gitdir / "objects" / "pack").glob("*.idx"))
    assert len(new_idx_paths) == 1
    new_bytes = new_idx_paths[0].read_bytes()

    idx_path.write_bytes(new_bytes)
    _bump_mtime(idx_path)

    reparsed = _parse_pack_index(idx_path)
    assert reparsed is not None
    assert reparsed is not original
    assert reparsed.shas != original_shas
    assert reparsed.fanout[255] > original.fanout[255]


def test_corrupted_rewrite_at_same_path_is_not_served_stale(tmp_path: Path) -> None:
    idx_path = _init_repo_with_pack(tmp_path)

    original = _parse_pack_index(idx_path)
    assert original is not None

    idx_path.chmod(0o644)
    idx_path.write_bytes(b"\x00" * 32)
    _bump_mtime(idx_path)

    result = _parse_pack_index(idx_path)
    assert result is None, "memo served a stale valid parse for corrupted content"


def test_distinct_mtime_size_keys_do_not_collide(tmp_path: Path) -> None:
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    idx_a = _init_repo_with_pack(repo_a)

    repo_b.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo_b)
    _git("config", "user.email", "t@t", cwd=repo_b)
    _git("config", "user.name", "t", cwd=repo_b)
    _git("config", "commit.gpgsign", "false", cwd=repo_b)
    (repo_b / "x.md").write_text("x\n", encoding="utf-8")
    (repo_b / "y.md").write_text("y\n", encoding="utf-8")
    _git("add", "-A", cwd=repo_b)
    _git("commit", "-qm", "seed b", cwd=repo_b)
    _git("repack", "-ad", "-q", cwd=repo_b)
    idx_b = sorted((resolve_git_dir(repo_b) / "objects" / "pack").glob("*.idx"))[0]

    parsed_a = _parse_pack_index(idx_a)
    parsed_b = _parse_pack_index(idx_b)
    assert parsed_a is not None and parsed_b is not None
    assert parsed_a.pack_path != parsed_b.pack_path
    assert parsed_a.shas != parsed_b.shas

    assert _parse_pack_index(idx_a) is parsed_a
    assert _parse_pack_index(idx_b) is parsed_b
