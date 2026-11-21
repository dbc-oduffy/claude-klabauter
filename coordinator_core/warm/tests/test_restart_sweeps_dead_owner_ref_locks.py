"""A restarted engine's first `record_served_repo` per repo finishes or moves aside dead-owner ref locks."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from coordinator_core.git import git_objects, ref_lock
from coordinator_core.warm import server

REF = "refs/heads/work/b"


class _VersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return False


def _ctx():
    return server._ServerContext(name="test", sid="sid", version_state=_VersionState())


def _commit(gitdir: Path, parent: str | None, msg: str) -> str:
    lines = ["tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904"]
    if parent:
        lines.append(f"parent {parent}")
    lines += ["author t <t@e> 1 +0000", "committer t <t@e> 1 +0000", "", msg, ""]
    return git_objects.write_object(gitdir, b"commit", "\n".join(lines).encode())


@pytest.fixture
def repo(tmp_path: Path):
    root = tmp_path / "repo"
    gd = root / ".git"
    (gd / "objects").mkdir(parents=True)
    (gd / "refs" / "heads" / "work").mkdir(parents=True)
    base = _commit(gd, None, "base")
    child = _commit(gd, base, "child")
    (gd / REF).write_bytes(base.encode() + b"\n")
    log = gd / "logs" / REF
    log.parent.mkdir(parents=True)
    log.write_text(f"{base} {child} t <t@e> 1 +0000\tcommit: child\n", encoding="utf-8")
    return root, gd, base, child


def _plant(gd: Path, content: str, pid: int) -> Path:
    lock = gd / (REF + ".lock")
    lock.write_bytes(content.encode("ascii"))
    old = time.time() - 5
    os.utime(lock, (old, old))
    owner = ref_lock._owner_path(gd, REF)
    owner.parent.mkdir(parents=True, exist_ok=True)
    owner.write_text(json.dumps({"pid": pid, "token": "t-1", "started": 1}), encoding="utf-8")
    return lock


def test_live_pid_lock_is_untouched(repo) -> None:
    root, gd, _, child = repo
    lock = _plant(gd, child + "\n", os.getpid() + 0)
    holder = ref_lock.register_holder(gd, REF)
    try:
        ref_lock._owner_path(gd, REF).write_text(
            json.dumps({"pid": os.getpid(), "token": holder.token, "started": 1}), encoding="utf-8"
        )
        _ctx().record_served_repo(root)
        assert lock.exists()
    finally:
        holder.close()


def test_second_call_does_not_rescan(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _, _, _ = repo
    calls: list[Path] = []
    monkeypatch.setattr(ref_lock, "sweep_dead_owner_locks", lambda gd: calls.append(gd) or [])
    ctx = _ctx()
    ctx.record_served_repo(root)
    ctx.record_served_repo(root)
    assert len(calls) == 1


def test_sweep_failure_is_swallowed(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _, _, _ = repo

    def _boom(gd):
        raise RuntimeError("boom")

    monkeypatch.setattr(ref_lock, "sweep_dead_owner_locks", _boom)
    ctx = _ctx()
    ctx.record_served_repo(root)
    assert ctx.served_repos() == [root]
