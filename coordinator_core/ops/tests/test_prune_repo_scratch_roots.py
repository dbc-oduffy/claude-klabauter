"""prune_claude_temp per-repo scratch roots: gitignore gate, tracked skip, link skip."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.hooks import sessionstart_async_dispatch as disp
from coordinator_core.ops import prune_claude_temp as pct

DAY = 86400


def _age(path, days):
    t = time.time() - days * DAY
    os.utime(path, (t, t))


def _repo(tmp_path, ignore="scratch/\ntasks/\n.coordinator-local/\n", index=b"DIRC"):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "index").write_bytes(index)
    (repo / ".gitignore").write_text(ignore)
    return repo


def _unit(space, name, days):
    d = space / name
    d.mkdir(parents=True)
    f = d / "f.txt"
    f.write_text("x")
    _age(f, days)
    _age(d, days)
    return d


def _prune(repo):
    return pct.prune(repo_root=str(repo), roots=[(r, d, n) for r, d, n in pct.default_roots(str(repo))][1:])


def test_stale_unit_removed_fresh_kept_all_three_spaces(tmp_path):
    repo = _repo(tmp_path)
    spaces = [repo / "scratch", repo / "tasks", repo / ".coordinator-local" / "subagent-share"]
    stale, fresh = [], []
    for s in spaces:
        stale.append(_unit(s, "old", 10))
        fresh.append(_unit(s, "new", 1))
    assert _prune(repo) == 3
    assert not any(p.exists() for p in stale)
    assert all(p.exists() for p in fresh)
    assert all(s.exists() for s in spaces)


def test_unit_with_fresh_nested_file_kept(tmp_path):
    repo = _repo(tmp_path)
    d = _unit(repo / "scratch", "u", 10)
    (d / "new.txt").write_text("x")
    assert _prune(repo) == 0 and d.exists()


def test_space_not_gitignored_is_skipped(tmp_path):
    repo = _repo(tmp_path, ignore="tasks/\n")
    d = _unit(repo / "scratch", "old", 10)
    roots = [r for r, _d, _n in pct.default_roots(str(repo))]
    assert repo / "scratch" not in roots
    assert d.exists()


def test_absent_space_not_listed(tmp_path):
    repo = _repo(tmp_path)
    # Machine-wide roots are listed regardless; no root inside the repo may be.
    assert not [r for r, _d, _n in pct.default_roots(str(repo)) if repo in Path(r).parents or Path(r) == repo]


def test_tracked_unit_skipped(tmp_path):
    repo = _repo(tmp_path, index=b"DIRC\0scratch/keep/f.txt\0")
    keep = _unit(repo / "scratch", "keep", 10)
    gone = _unit(repo / "scratch", "gone", 10)
    assert _prune(repo) == 1
    assert keep.exists() and not gone.exists()


def test_unreadable_index_prunes_nothing(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".git" / "index").unlink()
    d = _unit(repo / "scratch", "old", 10)
    assert _prune(repo) == 0 and d.exists()


def test_root_itself_never_deleted_and_stamp_kept(tmp_path):
    repo = _repo(tmp_path)
    _unit(repo / "scratch", "old", 10)
    _prune(repo)
    assert (repo / "scratch").is_dir()
    assert (repo / "scratch" / pct.STAMP_NAME).exists()
    assert pct.stamp_is_fresh(repo / "scratch")


def test_symlink_unit_not_followed(tmp_path):
    repo = _repo(tmp_path)
    target = _unit(tmp_path / "outside", "t", 10)
    (repo / "scratch").mkdir()
    link = repo / "scratch" / "lnk"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink privilege unavailable")
    _age(target, 10)
    _prune(repo)
    assert (target / "f.txt").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_junction_unit_not_followed(tmp_path):
    repo = _repo(tmp_path)
    target = _unit(tmp_path / "outside", "t", 10)
    (repo / "scratch").mkdir()
    link = repo / "scratch" / "jct"
    import _winapi

    try:
        _winapi.CreateJunction(str(target), str(link))
    except OSError:
        pytest.skip("junction creation failed")
    _prune(repo)
    assert (target / "f.txt").exists()


def test_symlinked_root_is_skipped(tmp_path):
    repo = _repo(tmp_path)
    target = _unit(tmp_path / "outside", "t", 10)
    try:
        os.symlink(target.parent, repo / "scratch", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink privilege unavailable")
    # Machine-wide roots are listed regardless; no root inside the repo may be.
    assert not [r for r, _d, _n in pct.default_roots(str(repo)) if repo in Path(r).parents or Path(r) == repo]


def test_find_repo_root_walks_up_and_none(tmp_path):
    repo = _repo(tmp_path)
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    assert pct.find_repo_root(str(sub)) == str(repo.resolve())
    assert pct.find_repo_root("") == ""


def test_leg_passes_repo_root_to_spawn(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / "scratch").mkdir()
    monkeypatch.setattr(pct, "claude_temp_root", lambda: tmp_path / "no-claude")
    spawned = []
    monkeypatch.setattr(disp, "spawn_detached", lambda *a, **k: spawned.append(a))
    disp._prune_claude_temp_leg({"payload": {"session_id": "s", "cwd": str(repo)}})
    assert len(spawned) == 1
    assert spawned[0][2] == ["s", str(repo.resolve())]
