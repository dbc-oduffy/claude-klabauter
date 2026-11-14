"""`worktree_sync.sync_landed_paths` against a real git fixture under `tmp_path`, ceilinged so no git
call reaches an outer repo."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.own_delta_merge import MAX_MERGE_LINES
from coordinator_core.git.worktree_sync import sync_landed_paths

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(repo: Path, *args: str, data: bytes | None = None) -> str:
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(repo.parent))
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, input=data,
        env=env, creationflags=_NOWIN,
    ).stdout.decode().strip()


def _lines(*items: str) -> bytes:
    return "".join(f"{i}\n" for i in items).encode()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t.example")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "core.autocrlf", "false")
    return r


def _entry(repo: Path, data: bytes, mode: int = 0o100644):
    return (mode, _git(repo, "hash-object", "-w", "--stdin", data=data))


BASE = _lines(*[f"l{i}" for i in range(1, 11)])
THEIRS = BASE.replace(b"l2\n", b"L2-landed\n")
PEER = BASE.replace(b"l9\n", b"L9-peer\n")


def test_peer_dirt_and_landed_hunks_both_present(repo: Path) -> None:
    (repo / "a.txt").write_bytes(PEER)
    out = sync_landed_paths(repo, {"a.txt": (_entry(repo, BASE), _entry(repo, THEIRS))})
    assert out.rebased == ("a.txt",) and out.refused == () and out.head_equal == ()
    got = (repo / "a.txt").read_bytes()
    assert b"L2-landed" in got and b"L9-peer" in got


def test_worktree_at_base_takes_landed_blob(repo: Path) -> None:
    (repo / "a.txt").write_bytes(BASE)
    out = sync_landed_paths(repo, {"a.txt": (_entry(repo, BASE), _entry(repo, THEIRS))})
    assert out.head_equal == ("a.txt",)
    assert (repo / "a.txt").read_bytes() == THEIRS


def test_worktree_already_landed_is_head_equal_without_write(repo: Path) -> None:
    (repo / "a.txt").write_bytes(THEIRS)
    before = (repo / "a.txt").stat().st_mtime_ns
    out = sync_landed_paths(repo, {"a.txt": (_entry(repo, BASE), _entry(repo, THEIRS))})
    assert out.head_equal == ("a.txt",)
    assert (repo / "a.txt").stat().st_mtime_ns == before


def test_collision_is_refused_and_byte_intact(repo: Path) -> None:
    mine = BASE.replace(b"l2\n", b"L2-mine\n")
    (repo / "a.txt").write_bytes(mine)
    out = sync_landed_paths(repo, {"a.txt": (_entry(repo, BASE), _entry(repo, THEIRS))})
    assert out.refused == ("a.txt",) and out.rebased == () and out.head_equal == ()
    assert (repo / "a.txt").read_bytes() == mine


def test_over_merge_line_cap_is_refused(repo: Path) -> None:
    big = _lines(*[f"x{i}" for i in range(MAX_MERGE_LINES + 5)])
    landed = big.replace(b"x1\n", b"X1\n")
    mine = big.replace(b"x1000\n", b"peer\n")
    (repo / "a.txt").write_bytes(mine)
    out = sync_landed_paths(repo, {"a.txt": (_entry(repo, big), _entry(repo, landed))})
    assert out.refused == ("a.txt",)
    assert (repo / "a.txt").read_bytes() == mine


def test_symlink_worktree_is_refused(repo: Path) -> None:
    (repo / "real.txt").write_bytes(BASE)
    try:
        os.symlink("real.txt", repo / "a.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    out = sync_landed_paths(repo, {"a.txt": (_entry(repo, BASE), _entry(repo, THEIRS))})
    assert out.refused == ("a.txt",)
    assert (repo / "real.txt").read_bytes() == BASE


def test_symlink_mode_entry_is_refused(repo: Path) -> None:
    (repo / "a.txt").write_bytes(BASE)
    out = sync_landed_paths(
        repo, {"a.txt": (_entry(repo, BASE), _entry(repo, b"real.txt", 0o120000))}
    )
    assert out.refused == ("a.txt",)
    assert (repo / "a.txt").read_bytes() == BASE


def test_clean_filter_is_refused(repo: Path) -> None:
    (repo / ".gitattributes").write_bytes(b"*.txt filter=lfs\n")
    (repo / "a.txt").write_bytes(PEER)
    out = sync_landed_paths(repo, {"a.txt": (_entry(repo, BASE), _entry(repo, THEIRS))})
    assert out.refused == ("a.txt",)
    assert (repo / "a.txt").read_bytes() == PEER


def test_crlf_checkout_is_rebased_in_crlf(repo: Path) -> None:
    crlf_peer = PEER.replace(b"\n", b"\r\n")
    (repo / "a.txt").write_bytes(crlf_peer)
    _git(repo, "config", "core.autocrlf", "true")
    out = sync_landed_paths(repo, {"a.txt": (_entry(repo, BASE), _entry(repo, THEIRS))})
    assert out.rebased == ("a.txt",)
    got = (repo / "a.txt").read_bytes()
    assert got.count(b"\r\n") == got.count(b"\n") == 10
    assert b"L2-landed" in got and b"L9-peer" in got


def test_cr_mixed_worktree_is_refused(repo: Path) -> None:
    mixed = PEER.replace(b"l5\n", b"l5\r\n")
    (repo / "a.txt").write_bytes(mixed)
    _git(repo, "config", "core.autocrlf", "true")
    out = sync_landed_paths(repo, {"a.txt": (_entry(repo, BASE), _entry(repo, THEIRS))})
    assert out.refused == ("a.txt",)
    assert (repo / "a.txt").read_bytes() == mixed


def test_created_path_absent_in_worktree_is_written(repo: Path) -> None:
    out = sync_landed_paths(repo, {"n/new.txt": (None, _entry(repo, THEIRS))})
    assert out.head_equal == ("n/new.txt",)
    assert (repo / "n" / "new.txt").read_bytes() == THEIRS


def test_modified_path_deleted_in_worktree_is_refused(repo: Path) -> None:
    out = sync_landed_paths(repo, {"a.txt": (_entry(repo, BASE), _entry(repo, THEIRS))})
    assert out.refused == ("a.txt",) and not (repo / "a.txt").exists()


def test_landed_deletion_removes_unchanged_and_refuses_dirty(repo: Path) -> None:
    (repo / "gone.txt").write_bytes(BASE)
    (repo / "dirty.txt").write_bytes(PEER)
    parent = _entry(repo, BASE)
    out = sync_landed_paths(repo, {"gone.txt": (parent, None), "dirty.txt": (parent, None)})
    assert out.head_equal == ("gone.txt",) and out.refused == ("dirty.txt",)
    assert not (repo / "gone.txt").exists()
    assert (repo / "dirty.txt").read_bytes() == PEER


def test_unreadable_blob_is_refused(repo: Path) -> None:
    (repo / "a.txt").write_bytes(PEER)
    ghost = (0o100644, "0" * 40)
    out = sync_landed_paths(repo, {"a.txt": (_entry(repo, BASE), ghost)})
    assert out.refused == ("a.txt",)
    assert (repo / "a.txt").read_bytes() == PEER


def test_edit_between_read_and_replace_is_never_overwritten(repo: Path, monkeypatch) -> None:
    from coordinator_core.git import worktree_sync

    (repo / "a.txt").write_bytes(PEER)
    racing = PEER + b"late edit\n"
    real = worktree_sync.os.replace

    def replace_after_peer_write(src, dst):
        raise AssertionError("replace reached despite a changed worktree")

    orig_write = Path.write_bytes

    def write_then_race(self, data):
        n = orig_write(self, data)
        if self.name.endswith(".sync.tmp"):
            orig_write(repo / "a.txt", racing)
        return n

    monkeypatch.setattr(Path, "write_bytes", write_then_race)
    monkeypatch.setattr(worktree_sync.os, "replace", replace_after_peer_write)
    out = sync_landed_paths(repo, {"a.txt": (_entry(repo, BASE), _entry(repo, THEIRS))})
    monkeypatch.setattr(worktree_sync.os, "replace", real)
    assert out.refused == ("a.txt",)
    assert (repo / "a.txt").read_bytes() == racing
    assert not list(repo.glob(".*.sync.tmp"))


def test_zero_git_spawns(repo: Path, monkeypatch) -> None:
    (repo / "a.txt").write_bytes(PEER)
    plan = {"a.txt": (_entry(repo, BASE), _entry(repo, THEIRS))}
    spawned: list = []
    real = subprocess.Popen.__init__

    def counting(self, *a, **k):
        spawned.append(a)
        real(self, *a, **k)

    monkeypatch.setattr(subprocess.Popen, "__init__", counting)
    out = sync_landed_paths(repo, plan)
    assert out.rebased == ("a.txt",) and spawned == []
