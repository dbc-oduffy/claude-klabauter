"""End-to-end falsifier for the archival index-resync self-heal.

An archival whose resync exhausts leaves the main index holding src and lacking
dst; a peer's next plain ``git add && git commit`` then un-archives the move.
Control leg reproduces that reversal with the drain off; treatment leg shows the
drain removes it; third leg shows a peer's deliberate staging at dst survives.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet import _common, _index_resync_drain
from coordinator_core.ops.fleet._common import Move, archive_and_commit
from coordinator_core.ops.fleet._index_resync_pending import list_pending, pending_dir
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_REAL_DRAIN = _index_resync_drain.drain_pending_resyncs
SRC_REL = "state/handoffs/baton.md"
DST_REL = "archive/handoffs/baton.md"


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        check=True, **no_console_creationflags(),
    )


class _FakeProc:
    def __init__(self, returncode: int, stderr: bytes) -> None:
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self):
        return b"", self._stderr


def _archive_with_exhausted_resync(tmp_path: Path, monkeypatch) -> Path:
    """Seed a repo, run archive_and_commit with `git restore` failing; drain is off."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "test@example.invalid"], root)
    _git(["config", "user.name", "test"], root)
    (root / "state" / "handoffs").mkdir(parents=True)
    (root / SRC_REL).write_text("baton\n", encoding="utf-8")
    (root / "peer_work.py").write_text("x = 1\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "peer_work.py").write_text("x = 2\n", encoding="utf-8")

    real_create = asyncio.create_subprocess_exec

    async def _exhaust_restore(*args, **kwargs):
        if len(args) >= 2 and args[0] == "git" and args[1] == "restore":
            return _FakeProc(128, b"fatal: Unable to create '.git/index.lock': File exists.")
        return await real_create(*args, **kwargs)

    async def _no_drain(*_a, **_k):
        return {"restored": [], "discharged": [], "kept": {}}

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exhaust_restore)
    monkeypatch.setattr(_index_resync_drain, "drain_pending_resyncs", _no_drain)
    monkeypatch.setattr(_common, "_INDEX_RETRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(_common, "_INDEX_RETRY_INITIAL_SLEEP_S", 0.001)
    monkeypatch.setattr(_common, "_INDEX_RETRY_BACKOFF_CAP_S", 0.001)

    move = Move(src=root / SRC_REL, dst=root / DST_REL, candidate_id=SRC_REL)
    acted, failed = asyncio.run(archive_and_commit(root, [move], "archive baton"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", real_create)

    assert failed == [] and len(acted) == 1
    assert acted[0].get("index_resync_failed")
    assert SRC_REL in _git(["ls-files"], root).stdout.split("\n")
    assert DST_REL not in _git(["ls-files"], root).stdout.split("\n")
    return root


def _drain(root: Path) -> dict:
    return asyncio.run(_REAL_DRAIN(root))


def _peer_commit(root: Path) -> list[str]:
    _git(["add", "--", "peer_work.py"], root)
    _git(["commit", "-q", "-m", "peer work"], root)
    return _git(["show", "--name-only", "--format=", "HEAD"], root).stdout.split()


def _head_files(root: Path) -> list[str]:
    return _git(["ls-tree", "-r", "--name-only", "HEAD"], root).stdout.split()


def test_control_without_drain_peer_commit_reverses_the_archival(tmp_path, monkeypatch):
    root = _archive_with_exhausted_resync(tmp_path, monkeypatch)
    assert len(list_pending(root)) == 1

    _peer_commit(root)

    assert SRC_REL in _head_files(root)
    assert DST_REL not in _head_files(root)


def test_treatment_with_drain_peer_commit_leaves_archival_intact(tmp_path, monkeypatch):
    root = _archive_with_exhausted_resync(tmp_path, monkeypatch)

    result = _drain(root)

    assert result["restored"] == [SRC_REL]
    touched = _peer_commit(root)
    assert touched == ["peer_work.py"]
    assert SRC_REL not in _head_files(root)
    assert DST_REL in _head_files(root)
    assert list_pending(root) == []
    pd = pending_dir(root)
    assert not pd.exists() or list(pd.iterdir()) == []


def test_peer_staged_dst_is_kept_contested_and_survives_the_drain(tmp_path, monkeypatch):
    root = _archive_with_exhausted_resync(tmp_path, monkeypatch)
    (root / DST_REL).write_text("peer rewrote the archived baton\n", encoding="utf-8")
    _git(["add", "--", DST_REL], root)
    staged_blob = _git(["rev-parse", ":" + DST_REL], root).stdout.strip()

    result = _drain(root)

    assert result["restored"] == []
    assert result["kept"] == {SRC_REL: "contested"}
    assert _git(["rev-parse", ":" + DST_REL], root).stdout.strip() == staged_blob
    assert len(list_pending(root)) == 1
