"""A refresh whose `merge --ff-only` is killed mid-checkout leaves the prior tree, no index.lock."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.git.run import GitResult  # noqa: E402
from percolate import dest_refresh  # noqa: E402

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True, **_NO_CONSOLE
    )


def _out(root: Path, *args: str) -> str:
    return _git(root, *args).stdout.strip()


def _commit(root: Path, files: dict, message: str, remove: tuple = ()) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for rel in remove:
        (root / rel).unlink()
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message)


@pytest.fixture()
def behind_clone(tmp_path: Path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    _git(origin, "config", "user.email", "t@example.invalid")
    _git(origin, "config", "user.name", "t")
    _commit(origin, {"a.txt": "a0\n", "gone.txt": "g0\n", "keep.txt": "k0\n"}, "base")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(clone))
    _git(clone, "config", "user.email", "t@example.invalid")
    _git(clone, "config", "user.name", "t")
    prior = _out(clone, "rev-parse", "HEAD")
    _commit(origin, {"a.txt": "a1\n", "sub/new.txt": "n1\n"}, "upstream", remove=("gone.txt",))
    _git(clone, "fetch", "origin")
    return clone, prior, _out(clone, "rev-parse", "origin/main")


def _killed_merge(clone: Path, tip: str, *, hold_lock: bool = True):
    """Patch target: a ff-only merge that writes part of the tree, strands the lock, times out."""
    real = dest_refresh._git

    def fake(repo_root, args, *, remote=False):
        if "merge" in args and "--ff-only" in args:
            (clone / "a.txt").write_text("a1\n", encoding="utf-8")
            (clone / "sub").mkdir(exist_ok=True)
            (clone / "sub" / "new.txt").write_text("n1\n", encoding="utf-8")
            (clone / "gone.txt").unlink()
            if hold_lock:
                (clone / ".git" / "index.lock").write_bytes(b"")
            return GitResult(returncode=-1, stdout="", stderr="", timed_out=True)
        return real(repo_root, args, remote=remote)

    return fake


def _assert_prior_tree(clone: Path, prior: str) -> None:
    assert _out(clone, "rev-parse", "HEAD") == prior
    assert not (clone / ".git" / "index.lock").exists()
    assert _out(clone, "status", "--porcelain", "--untracked-files=all") == ""
    assert (clone / "a.txt").read_text(encoding="utf-8") == "a0\n"
    assert (clone / "gone.txt").read_text(encoding="utf-8") == "g0\n"
    assert not (clone / "sub" / "new.txt").exists()
    assert not (clone / "sub").exists()


def test_fast_forward_dest_killed_mid_checkout_restores_prior_tree(behind_clone, monkeypatch):
    clone, prior, tip = behind_clone
    monkeypatch.setattr(dest_refresh, "_git", _killed_merge(clone, tip))
    fetched = dest_refresh.fetch_dest_upstream(clone, out=io.StringIO(), err=io.StringIO())
    result = dest_refresh.fast_forward_dest(fetched, out=io.StringIO(), err=io.StringIO())
    assert result.ok is False
    assert "could not be fast-forwarded" in result.reason
    _assert_prior_tree(clone, prior)


def test_fast_forward_consumer_killed_mid_checkout_restores_prior_tree(behind_clone, monkeypatch):
    clone, prior, tip = behind_clone
    monkeypatch.setattr(dest_refresh, "_git", _killed_merge(clone, tip))
    result = dest_refresh.fast_forward_consumer(clone)
    assert result.ok is False
    _assert_prior_tree(clone, prior)


def test_foreign_index_lock_is_never_removed(behind_clone, monkeypatch):
    """A lock that predates the merge belongs to a live peer's git."""
    clone, prior, tip = behind_clone
    lock = clone / ".git" / "index.lock"
    lock.write_bytes(b"")
    os.utime(lock, (1_000_000_000, 1_000_000_000))
    fake = _killed_merge(clone, tip, hold_lock=False)
    monkeypatch.setattr(dest_refresh, "_git", fake)
    result = dest_refresh.fast_forward_consumer(clone)
    assert result.ok is False
    assert lock.exists()


def test_uninterrupted_fast_forward_lands_at_upstream_tip(behind_clone):
    clone, _prior, tip = behind_clone
    fetched = dest_refresh.fetch_dest_upstream(clone, out=io.StringIO(), err=io.StringIO())
    result = dest_refresh.fast_forward_dest(fetched, out=io.StringIO(), err=io.StringIO())
    assert result.ok is True
    assert _out(clone, "rev-parse", "HEAD") == tip
    assert _out(clone, "status", "--porcelain") == ""
