"""land_run's dirty scan settles stat-clean literals from the index sha with no byte read and hashes
only stat-dirty ones; staged changes, same-size rewrites, untracked, deleted and filter-refused
literals keep their changed/unchanged verdicts."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import git_index
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process]


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    for i in range(6):
        _write(root, f"src/f{i}.txt", f"content {i}\n")
    _git(["add", "-A"], root)
    _git(["commit", "-qm", "base"], root)
    ident = git_index.parse_index_identity(root, wanted=["src/f0.txt"])["src/f0.txt"]
    if not ident.mtime_nsec:
        pytest.skip("this git records no nanosecond mtime in the index")
    return root


@pytest.fixture
def hashed(monkeypatch):
    calls: list = []
    real = land_run._hashed_blob

    def spy(root, rel):
        calls.append(rel)
        return real(root, rel)

    monkeypatch.setattr(land_run, "_hashed_blob", spy)
    return calls


def _literals():
    return [f"src/f{i}.txt" for i in range(6)]


def test_stat_clean_literals_read_no_bytes(repo, hashed):
    changed, _ignored, head = land_run._dirty_among(repo, _literals())
    assert changed == set()
    assert hashed == []
    assert set(head) == set(_literals())


def test_touched_unchanged_is_hashed_and_still_clean(repo, hashed):
    path = repo / "src/f1.txt"
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    changed, _i, _h = land_run._dirty_among(repo, _literals())
    assert changed == set()
    assert hashed == ["src/f1.txt"]


def test_content_changed_is_hashed_and_changed(repo, hashed):
    _write(repo, "src/f2.txt", "different and longer content\n")
    changed, _i, _h = land_run._dirty_among(repo, _literals())
    assert changed == {"src/f2.txt"}
    assert hashed == ["src/f2.txt"]


def test_staged_change_reads_changed_without_a_read(repo, hashed):
    _write(repo, "src/f3.txt", "staged edit\n")
    _git(["add", "src/f3.txt"], repo)
    hashed.clear()
    changed, _i, _h = land_run._dirty_among(repo, _literals())
    assert changed == {"src/f3.txt"}
    assert hashed == []


def test_same_size_rewrite_with_other_nanoseconds_is_hashed(repo, hashed):
    path = repo / "src/f4.txt"
    ident = git_index.parse_index_identity(repo, wanted=["src/f4.txt"])["src/f4.txt"]
    original = path.stat()
    path.write_text("content X\n", encoding="utf-8", newline="\n")
    assert path.stat().st_size == original.st_size
    stored_ns = ident.mtime * 1_000_000_000 + ident.mtime_nsec
    os.utime(path, ns=(original.st_atime_ns, stored_ns + 100))
    assert path.stat().st_mtime_ns // 1_000_000_000 == ident.mtime
    changed, _i, _h = land_run._dirty_among(repo, _literals())
    assert changed == {"src/f4.txt"}
    assert hashed == ["src/f4.txt"]


def test_untracked_and_deleted_keep_their_verdicts(repo, hashed):
    _write(repo, "src/new.txt", "fresh\n")
    (repo / "src/f5.txt").unlink()
    changed, _i, _h = land_run._dirty_among(repo, [*_literals(), "src/new.txt", "src/ghost.txt"])
    assert changed == {"src/new.txt", "src/f5.txt"}
    assert "src/f5.txt" not in hashed
    assert "src/new.txt" in hashed


def test_filter_refusal_counts_as_changed_not_absent(repo):
    _write(repo, ".gitattributes", "src/f0.txt filter=lfs\n")
    _write(repo, "src/f0.txt", "bytes behind a clean filter\n")
    assert land_run._hashed_blob(repo, "src/f0.txt") == land_run._FILTERED
    changed, _i, _h = land_run._dirty_among(repo, ["src/f0.txt"])
    assert changed == {"src/f0.txt"}
    assert land_run._hashed_blob(repo, "src/missing.txt") is None


def test_index_parse_failure_falls_back_to_hashing_every_literal(repo, hashed, monkeypatch):
    def boom(*_a, **_k):
        raise git_index.IndexParseError("bad")

    monkeypatch.setattr(git_index, "parse_index_identity", boom)
    changed, _i, _h = land_run._dirty_among(repo, _literals())
    assert changed == set()
    assert sorted(hashed) == _literals()


def test_withheld_pending_settles_stat_clean_without_a_read(repo, hashed):
    assert land_run._withheld_pending(repo, ["src/f0.txt"], {"src/f0.txt": 1}) is False
    assert hashed == []
    _write(repo, "src/f0.txt", "changed now\n")
    assert land_run._withheld_pending(repo, ["src/f0.txt"], {"src/f0.txt": 1}) is True
    assert hashed == ["src/f0.txt"]


def test_shadowed_worktree_blob_def_is_gone():
    src = Path(land_run.__file__).read_text(encoding="utf-8")
    assert src.count("\ndef _worktree_blob(") == 1
