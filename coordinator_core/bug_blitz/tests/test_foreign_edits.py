"""foreign_edits: foreign dirty paths, holder naming, and the cached HEAD tree with own-edit overlay."""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from coordinator_core.bug_blitz import foreign_edits
from coordinator_core.bug_blitz.foreign_edits import UNATTRIBUTED, foreign_dirty, head_tree, holders_of
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        **no_console_creationflags(),
    )


@pytest.fixture(autouse=True)
def _tmp_cache(tmp_path, monkeypatch):
    cache = tmp_path / "gettemp"
    cache.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(cache))
    return cache


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "own.py").write_text("OWN = 'head'\n")
    (root / "pkg" / "other.py").write_text("OTHER = 'head'\n")
    (root / "pkg" / "gone.py").write_text("GONE = 1\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def test_foreign_dirty_excludes_own_and_counts_untracked(repo):
    (repo / "pkg" / "own.py").write_text("OWN = 'edited'\n")
    (repo / "pkg" / "other.py").write_text("OTHER = 'edited'\n")
    (repo / "pkg" / "new.py").write_text("NEW = 1\n")
    assert sorted(foreign_dirty(repo, ["pkg/own.py"])) == ["pkg/new.py", "pkg/other.py"]
    assert foreign_dirty(repo, [str(repo / "pkg" / "own.py")]) != foreign_dirty(repo, [])
    assert "pkg/own.py" not in foreign_dirty(repo, [str(repo / "pkg" / "own.py")])


def test_foreign_dirty_clean_tree_is_empty(repo):
    assert foreign_dirty(repo, []) == []


def test_foreign_dirty_rename_source_is_not_a_path(repo):
    _git(repo, "mv", "pkg/other.py", "pkg/moved.py")
    assert sorted(foreign_dirty(repo, [])) == ["pkg/moved.py"]


def test_holders_of_maps_contested_and_unattributed(repo, monkeypatch):
    import coordinator_core.session.scope as scope

    monkeypatch.setattr(
        scope, "contested_by_live_peers", lambda paths, sid, cwd=None: {"pkg/a.py": ["peer-1", "peer-2"]}
    )
    got = holders_of(repo, ["pkg/a.py", "pkg/b.py"], "me")
    assert got == {"pkg/a.py": ["peer-1", "peer-2"], "pkg/b.py": [UNATTRIBUTED]}


def test_holders_of_none_is_unattributed_everywhere(repo, monkeypatch):
    import coordinator_core.session.scope as scope

    monkeypatch.setattr(scope, "contested_by_live_peers", lambda paths, sid, cwd=None: None)
    assert holders_of(repo, ["pkg/a.py", "pkg/b.py"], "") == {
        "pkg/a.py": [UNATTRIBUTED],
        "pkg/b.py": [UNATTRIBUTED],
    }


def test_head_tree_is_head_plus_own_overlay(repo):
    (repo / "pkg" / "own.py").write_text("OWN = 'edited'\n")
    (repo / "pkg" / "other.py").write_text("OTHER = 'foreign'\n")
    (repo / "pkg" / "fresh.py").write_text("FRESH = 1\n")
    _git(repo, "rm", "-q", "--cached", "pkg/gone.py")
    (repo / "pkg" / "gone.py").unlink()
    row = head_tree(repo, ["pkg/own.py", "pkg/fresh.py", "pkg/gone.py"])
    assert (row / "pkg" / "own.py").read_text() == "OWN = 'edited'\n"
    assert (row / "pkg" / "fresh.py").read_text() == "FRESH = 1\n"
    assert (row / "pkg" / "other.py").read_text() == "OTHER = 'head'\n"
    assert not (row / "pkg" / "gone.py").exists()
    assert not row.is_relative_to(repo)


def test_head_tree_with_no_own_overlays_nothing(repo):
    (repo / "pkg" / "own.py").write_text("OWN = 'edited'\n")
    (repo / "pkg" / "fresh.py").write_text("FRESH = 1\n")
    row = head_tree(repo, [])
    assert (row / "pkg" / "own.py").read_text() == "OWN = 'head'\n"
    assert not (row / "pkg" / "fresh.py").exists()


def test_overlay_leaves_shared_base_at_head(repo):
    (repo / "pkg" / "own.py").write_text("OWN = 'edited'\n")
    row1 = head_tree(repo, ["pkg/own.py"])
    base = row1.parent / foreign_edits.run_git(["rev-parse", "HEAD"], cwd=str(repo)).stdout.strip()
    assert (base / "pkg" / "own.py").read_text() == "OWN = 'head'\n"
    row2 = head_tree(repo, [])
    assert row2 != row1
    assert (row2 / "pkg" / "own.py").read_text() == "OWN = 'head'\n"
    assert (row1 / "pkg" / "own.py").read_text() == "OWN = 'edited'\n"


def test_base_is_built_once_per_head_sha_and_old_shas_pruned(repo):
    row = head_tree(repo, [])
    parent = row.parent
    base_dirs = [p for p in parent.iterdir() if foreign_edits._SHA_DIR_RE.match(p.name)]
    assert len(base_dirs) == 1
    stamp = base_dirs[0].stat().st_mtime_ns
    head_tree(repo, [])
    assert base_dirs[0].stat().st_mtime_ns == stamp

    (repo / "pkg" / "own.py").write_text("OWN = 'v2'\n")
    _git(repo, "commit", "-q", "-am", "second")
    head_tree(repo, [])
    after = [p for p in parent.iterdir() if foreign_edits._SHA_DIR_RE.match(p.name)]
    assert len(after) == 1 and after[0] != base_dirs[0]
    assert not [p for p in parent.iterdir() if p.name.startswith((".tmp-", ".index-"))]


def test_concurrent_builder_wins_race(repo, monkeypatch):
    real_replace = os.replace

    def lose_race(src, dst):
        real_replace(src, dst)  # the "peer" lands first
        raise PermissionError("target exists")

    monkeypatch.setattr(foreign_edits.os, "replace", lose_race)
    row = head_tree(repo, [])
    assert (row / "pkg" / "own.py").read_text() == "OWN = 'head'\n"


def test_link_failure_falls_back_to_copy(repo, monkeypatch):
    def no_link(*a, **k):
        raise OSError("no hardlinks")

    monkeypatch.setattr(foreign_edits.os, "link", no_link)
    row = head_tree(repo, [])
    assert (row / "pkg" / "other.py").read_text() == "OTHER = 'head'\n"
