"""commit_clone: a shared sparse clone at a sha, warm hits with zero spawns, and an entry run
from the clone rather than the caller's tree. All trees live under tmp_path."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete]

_MODULE = Path(__file__).resolve().parents[1] / "lib" / "commit_clone.py"


@pytest.fixture()
def cc():
    spec = importlib.util.spec_from_file_location("_commit_clone_under_test", _MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "live"
    (root / "coordinator" / "bin").mkdir(parents=True)
    (root / "coordinator_core").mkdir()
    (root / "docs").mkdir()
    (root / "coordinator" / "bin" / "entry.py").write_text(
        "import sys\nprint('ENTRY=' + __file__)\nsys.exit(3)\n", encoding="utf-8"
    )
    (root / "coordinator_core" / "__init__.py").write_text("", encoding="utf-8")
    (root / "docs" / "note.md").write_text("payload\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "one")
    return root


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD")


class _SpawnCounter:
    def __init__(self, monkeypatch):
        self.calls: list[list[str]] = []
        real = subprocess.run

        def counting(cmd, *a, **kw):
            self.calls.append(list(cmd))
            return real(cmd, *a, **kw)

        monkeypatch.setattr(subprocess, "run", counting)


def test_cold_clone_is_at_sha_with_three_spawns(cc, repo, tmp_path, monkeypatch):
    sha = _head(repo)
    counter = _SpawnCounter(monkeypatch)
    dest = cc.materialise_commit_clone(repo, sha, tmp_path / "cache")
    assert dest == tmp_path / "cache" / sha
    assert (dest / ".git").is_dir()
    assert len(counter.calls) == 3
    assert _git(dest, "rev-parse", "HEAD") == sha
    assert (dest / "coordinator" / "bin" / "entry.py").is_file()
    assert not (dest / "docs").exists()


def test_warm_hit_spawns_nothing(cc, repo, tmp_path, monkeypatch):
    sha = _head(repo)
    first = cc.materialise_commit_clone(repo, sha, tmp_path / "cache")
    counter = _SpawnCounter(monkeypatch)
    assert cc.materialise_commit_clone(repo, sha, tmp_path / "cache") == first
    assert counter.calls == []


def test_clone_worktree_is_clean(cc, repo, tmp_path):
    dest = cc.materialise_commit_clone(repo, _head(repo), tmp_path / "cache")
    assert _git(dest, "status", "--porcelain") == ""


def test_sha_on_no_ref_checks_out(cc, repo, tmp_path):
    first = _head(repo)
    (repo / "coordinator" / "bin" / "entry.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    tree = _git(repo, "write-tree")
    orphan = _git(repo, "commit-tree", tree, "-m", "orphan")
    assert orphan != first
    dest = cc.materialise_commit_clone(repo, orphan, tmp_path / "cache")
    assert _git(dest, "rev-parse", "HEAD") == orphan


def test_unknown_sha_raises_and_leaves_no_staging(cc, repo, tmp_path):
    cache = tmp_path / "cache"
    with pytest.raises(RuntimeError):
        cc.materialise_commit_clone(repo, "0" * 40, cache)
    assert list(cache.iterdir()) == []


def test_preexisting_dest_is_returned_and_staging_discarded(cc, repo, tmp_path, monkeypatch):
    sha = _head(repo)
    cache = tmp_path / "cache"
    cc.materialise_commit_clone(repo, sha, cache)
    # A concurrent winner: force the cold path again while dest already holds a marker.
    real_read = cc._read_marker
    seen = {"n": 0}

    def miss_once(root):
        seen["n"] += 1
        return None if seen["n"] == 1 else real_read(root)

    monkeypatch.setattr(cc, "_read_marker", miss_once)
    assert cc.materialise_commit_clone(repo, sha, cache) == cache / sha
    assert [p.name for p in cache.iterdir()] == [sha]


def test_prune_keeps_dest_and_newest(cc, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    names = [f"{i:040x}" for i in range(1, 6)]
    for i, name in enumerate(names):
        d = cache / name
        d.mkdir()
        import os

        os.utime(d, (1000 + i, 1000 + i))
    (cache / "abc123-staging").mkdir()
    keep = cache / names[0]  # oldest, still kept
    cc._prune(cache, keep=keep)
    left = {p.name for p in cache.iterdir()}
    assert names[0] in left
    assert "abc123-staging" in left
    assert {names[4], names[3]} <= left
    assert names[1] not in left and names[2] not in left


def test_source_repo_root_round_trips(cc, repo, tmp_path):
    dest = cc.materialise_commit_clone(repo, _head(repo), tmp_path / "cache")
    assert cc.is_commit_clone(dest)
    assert cc.source_repo_root(dest) == repo
    assert not cc.is_commit_clone(repo)
    assert cc.source_repo_root(repo) == repo


def test_run_entry_from_clone_runs_clone_file_not_callers(cc, repo, tmp_path, monkeypatch, capsys):
    dest = cc.materialise_commit_clone(repo, _head(repo), tmp_path / "cache")
    # Live tree is dirty: the clone must still run the committed entry.
    (repo / "coordinator" / "bin" / "entry.py").write_text("raise SystemExit(9)\n", encoding="utf-8")
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(sys, "modules", dict(sys.modules))
    code = cc.run_entry_from_clone(dest, "coordinator/bin/entry.py", ["entry.py"])
    out = capsys.readouterr()
    assert code == 3
    assert f"ENTRY={dest / 'coordinator' / 'bin' / 'entry.py'}" in out.out
    assert f"running from commit clone {dest}" in out.err
    assert sys.path[0] == str(dest / "coordinator" / "bin")
