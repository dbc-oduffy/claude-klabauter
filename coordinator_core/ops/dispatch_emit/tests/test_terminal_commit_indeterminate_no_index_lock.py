"""A dispatch.terminal_commit that exits mid-splice frees its own index.lock and never a peer's."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import index_write
from coordinator_core.ops.dispatch_emit import land_deadline
from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "new.txt").write_text("n\n", encoding="utf-8")
    return tmp_path


def _lock(repo: Path) -> Path:
    return repo / ".git" / "index.lock"


def _updates(repo: Path) -> dict:
    blob = _git(repo, "hash-object", "-w", "--", "new.txt").strip()
    return {"new.txt": (0o100644, blob)}


def test_deadline_exit_mid_splice_leaves_no_index_lock(repo, monkeypatch):
    keep = []

    def body(params, repo_root=None):
        keep.append(index_write.prepare_splice(repo, _updates(repo), stat_fallback=True))
        assert _lock(repo).exists()
        raise land_deadline.LandDeadlineExceeded("splice")

    monkeypatch.setattr(tc, "_handler_body", body)
    reply = tc._handler({}, repo)
    assert "land-deadline" in reply["error"]
    assert not _lock(repo).exists()
    assert not index_write._owner_path(_lock(repo)).exists()
    keep[0].abort()
    assert not _lock(repo).exists()


def test_exception_exit_mid_splice_leaves_no_index_lock(repo, monkeypatch):
    keep = []

    def body(params, repo_root=None):
        keep.append(index_write.prepare_splice(repo, _updates(repo), stat_fallback=True))
        raise RuntimeError("boom")

    monkeypatch.setattr(tc, "_handler_body", body)
    with pytest.raises(RuntimeError):
        tc._handler({}, repo)
    assert not _lock(repo).exists()


def test_a_peers_index_lock_survives_the_exit(repo, monkeypatch):
    _lock(repo).write_bytes(b"")
    index_write._owner_path(_lock(repo)).write_text(
        json.dumps({"pid": 1, "token": "1-2-3"}), encoding="utf-8"
    )

    def body(params, repo_root=None):
        raise land_deadline.LandDeadlineExceeded("splice")

    monkeypatch.setattr(tc, "_handler_body", body)
    tc._handler({}, repo)
    assert _lock(repo).exists()
    assert index_write._owner_path(_lock(repo)).exists()


def test_a_lock_held_by_another_thread_survives_the_exit(repo, monkeypatch):
    import threading

    held = []
    t = threading.Thread(
        target=lambda: held.append(index_write.prepare_splice(repo, _updates(repo), stat_fallback=True))
    )
    t.start()
    t.join()

    def body(params, repo_root=None):
        raise land_deadline.LandDeadlineExceeded("splice")

    monkeypatch.setattr(tc, "_handler_body", body)
    tc._handler({}, repo)
    assert _lock(repo).exists()
    held[0].abort()
