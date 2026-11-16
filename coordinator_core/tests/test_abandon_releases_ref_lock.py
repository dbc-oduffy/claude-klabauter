"""
coordinator_core.tests.test_abandon_releases_ref_lock

Purpose: when the dispatcher abandons a mutating handler that is inside `git_objects._cas_ref`'s
lock window, it frees the `<ref>.lock` (all three `_abandoned.set()` sites) and the hung thread
can no longer rename over, or unlink, a lock a peer took after the release.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
from pathlib import Path

import pytest

import coordinator_core.ipc as ipc
from coordinator_core.git import git_objects, ref_lock
from coordinator_core.git.git_dir import resolve_git_dir
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REF = "refs/heads/main"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
        stdin=subprocess.DEVNULL, **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "seed.md").write_text("seed\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    return tmp_path


@pytest.fixture
def hung_replace(monkeypatch: pytest.MonkeyPatch):
    """`_replace_with_retry` blocks inside the lock window until `release` is set, then honours
    `still_valid` exactly as the real retry loop does after a sleep."""
    entered = threading.Event()
    release = threading.Event()
    outcome: dict = {}
    real = git_objects._replace_with_retry

    def _blocking(src, dst, **kw):
        entered.set()
        release.wait(10)
        valid = kw["still_valid"]()
        outcome["still_valid"] = valid
        return real(src, dst, **kw) if valid else False

    monkeypatch.setattr(git_objects, "_replace_with_retry", _blocking)
    yield entered, release, outcome
    release.set()


def _mutating_handler(gitdir: Path, new: str):
    sha = _git(gitdir.parent, "rev-parse", _REF)

    def _handler(params, repo_root=None):
        outcome = git_objects.cas_ref(gitdir, _REF, sha, new)
        return {"cas": outcome}

    return _handler


def _dispatch(method: str) -> dict:
    return asyncio.run(
        ipc.dispatch_message({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}})
    )


def _lock(gitdir: Path) -> Path:
    return gitdir / (_REF + ".lock")


def _join_handler_threads() -> None:
    for t in threading.enumerate():
        if t.name.startswith(("asyncio_", "ThreadPoolExecutor")):
            t.join(timeout=5)


@pytest.mark.parametrize("cold", [False, True], ids=["sync-timeout", "await-ceiling"])
def test_abandoned_sync_handler_leaves_no_lock_and_the_ref_unmoved(
    monkeypatch: pytest.MonkeyPatch, repo: Path, hung_replace, cold: bool
) -> None:
    entered, release, outcome = hung_replace
    gitdir = resolve_git_dir(repo)
    start = _git(repo, "rev-parse", _REF)
    monkeypatch.setitem(ipc._REGISTRY, "test.ref_lock_hang", _mutating_handler(gitdir, "2" * 40))
    monkeypatch.setattr(ipc, "_timeout_for", lambda method, msg=None: 0.3)
    monkeypatch.setattr(ipc, "_cold_mutating_op", lambda method: cold)
    monkeypatch.setattr(ipc, "_COLD_AWAIT_CEILING_FACTOR", 2.0)

    reply = _dispatch("test.ref_lock_hang")

    assert "error" in reply and entered.is_set()
    assert not _lock(gitdir).exists()
    assert _git(repo, "rev-parse", _REF) == start
    assert not ref_lock._HOLDERS or all(h.released for h in ref_lock._HOLDERS.values())

    peer = _lock(gitdir)
    peer.write_bytes(b"peer\n")
    release.set()
    _join_handler_threads()
    assert outcome["still_valid"] is False
    assert peer.read_bytes() == b"peer\n"
    assert _git(repo, "rev-parse", _REF) == start
    peer.unlink()


def test_abandoned_async_handler_releases_its_ref_lock(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    gitdir = resolve_git_dir(repo)
    sha = _git(repo, "rev-parse", _REF)
    taken = threading.Event()

    async def _handler(params, repo_root=None):
        ref_lock.register_holder(gitdir, _REF)
        _lock(gitdir).write_bytes(b"x\n")
        taken.set()
        await asyncio.sleep(10)

    monkeypatch.setitem(ipc._REGISTRY, "test.ref_lock_hang_async", _handler)
    monkeypatch.setattr(ipc, "_timeout_for", lambda method, msg=None: 0.2)

    reply = _dispatch("test.ref_lock_hang_async")

    assert "error" in reply and taken.is_set()
    assert not _lock(gitdir).exists()
    assert _git(repo, "rev-parse", _REF) == sha
    ref_lock._HOLDERS.clear()


def test_release_failure_does_not_turn_a_timeout_into_a_crash(
    monkeypatch: pytest.MonkeyPatch, repo: Path, hung_replace
) -> None:
    def _boom(event):
        raise RuntimeError("release exploded")

    gitdir = resolve_git_dir(repo)
    monkeypatch.setattr(ref_lock, "release_abandoned", _boom)
    monkeypatch.setitem(ipc._REGISTRY, "test.ref_lock_hang_boom", _mutating_handler(gitdir, "2" * 40))
    monkeypatch.setattr(ipc, "_timeout_for", lambda method, msg=None: 0.2)
    monkeypatch.setattr(ipc, "_cold_mutating_op", lambda method: False)

    reply = _dispatch("test.ref_lock_hang_boom")

    assert "error" in reply
    hung_replace[1].set()
    _join_handler_threads()
    _lock(gitdir).unlink(missing_ok=True)
    assert os.path.exists(gitdir)
