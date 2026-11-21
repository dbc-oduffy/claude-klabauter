"""A restarted engine's first `record_served_repo` per repo finishes or moves aside dead-owner ref locks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.git import git_objects, ref_lock
from coordinator_core.warm import server
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

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


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"], **no_console_creationflags())
    proc.wait()
    return proc.pid


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


def test_first_record_completes_a_matching_dead_owner_lock(repo) -> None:
    root, gd, base, child = repo
    lock = _plant(gd, child + "\n", _dead_pid())
    _ctx().record_served_repo(root)
    assert (gd / REF).read_bytes() == child.encode() + b"\n"
    assert not lock.exists()


def test_first_record_moves_aside_a_non_matching_dead_owner_lock(repo) -> None:
    root, gd, base, _ = repo
    stranger = _commit(gd, None, "stranger")
    lock = _plant(gd, stranger + "\n", _dead_pid())
    _ctx().record_served_repo(root)
    assert (gd / REF).read_bytes() == base.encode() + b"\n"
    assert not lock.exists()
    assert (gd / ref_lock.RECOVERY_DIR / ref_lock.RECOVERY_LOG).exists()
