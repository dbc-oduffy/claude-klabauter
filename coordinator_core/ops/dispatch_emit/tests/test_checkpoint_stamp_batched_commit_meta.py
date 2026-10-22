"""Pins that a first-parent walk of packed commits parses each pack index at most once and reads the
same metadata as per-commit ``commit_meta``, and that a loose commit never touches a pack index."""

from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.git import commit_walk, git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit import checkpoint_stamp as cs

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_NO_WINDOW =getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True,
        creationflags=_NO_WINDOW,
    ).stdout.strip()


def _packed_repo(root: Path, batches: int = 5, per_batch: int = 10) -> str:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    n = 0
    for _ in range(batches):
        for _ in range(per_batch):
            (root / "f.txt").write_text(str(n), encoding="utf-8")
            _git(root, "add", "-A")
            _git(root, "commit", "-qm", f"c{n}")
            n += 1
        _git(root, "repack", "-d", "-q")
    return _git(root, "rev-parse", "HEAD")


def _reset_caches() -> None:
    commit_walk._COMMIT_CACHE.clear()
    git_objects._OBJECT_CACHE.clear()
    git_objects._PACK_INDEX_CACHE.clear()
    git_objects._PACK_INDEXES_BY_LISTING.clear()
    git_objects._PACK_LISTING_CACHE.clear()
    git_objects._LAST_HIT_PACK.clear()


def _walk(common_dir: Path, head: str, fn) -> list:
    out, sha = [], head
    while sha is not None:
        meta = fn(common_dir, sha)
        assert meta is not None
        out.append((sha, meta["parents"], meta["message"]))
        sha = meta["parents"][0] if meta["parents"] else None
    return out


def test_packed_walk_parses_each_pack_index_at_most_once(tmp_path, monkeypatch):
    head = _packed_repo(tmp_path)
    common_dir = resolve_git_common_dir(tmp_path)
    packs = len(list((common_dir / "objects" / "pack").glob("*.idx")))
    assert packs >= 2

    _reset_caches()
    parsed: list = []
    real = git_objects._parse_pack_index_uncached
    monkeypatch.setattr(
        git_objects, "_parse_pack_index_uncached", lambda p: (parsed.append(p), real(p))[1]
    )
    walked = _walk(common_dir, head, cs._walk_commit_meta)

    assert len(walked) == 50
    assert len(parsed) <= packs
    assert len(set(parsed)) == len(parsed)

    _reset_caches()
    assert walked == _walk(common_dir, head, commit_walk.commit_meta)


def test_loose_walk_never_parses_a_pack_index(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    for n in range(5):
        (tmp_path / "f.txt").write_text(str(n), encoding="utf-8")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-qm", f"c{n}")
    head = _git(tmp_path, "rev-parse", "HEAD")
    common_dir = resolve_git_common_dir(tmp_path)

    _reset_caches()
    parsed: list = []
    monkeypatch.setattr(
        git_objects, "_parse_pack_index_uncached", lambda p: parsed.append(p)
    )
    assert len(_walk(common_dir, head, cs._walk_commit_meta)) == 5
    assert parsed == []


def test_two_walks_after_a_new_pack_see_it(tmp_path):
    head = _packed_repo(tmp_path, batches=2, per_batch=3)
    common_dir = resolve_git_common_dir(tmp_path)
    _reset_caches()
    assert len(_walk(common_dir, head, cs._walk_commit_meta)) == 6

    (tmp_path / "f.txt").write_text("new", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "later")
    _git(tmp_path, "repack", "-d", "-q")
    new_head = _git(tmp_path, "rev-parse", "HEAD")
    assert len(_walk(common_dir, new_head, cs._walk_commit_meta)) == 7
