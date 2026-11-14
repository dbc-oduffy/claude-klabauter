"""PACK_DELETION_PAUSED: while true the engine deletes no pack file and launches no maintenance."""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest

from coordinator_core.git import git_objects as go
from coordinator_core.git import maintenance_tick, pack_deletion_pause, store_hygiene
from coordinator_core.ops import git_maintenance

_GIT_MADE = "a" * 40
_STALE = "b" * 40


def _blob_sha(payload: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(payload) + payload).hexdigest()


def _pending(n: int, tag: bytes) -> dict:
    out = {}
    for i in range(n):
        payload = b"%s-%d" % (tag, i)
        out[_blob_sha(payload)] = (b"blob", payload)
    return out


def _set_paused(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(pack_deletion_pause, "PACK_DELETION_PAUSED", value)


def _age(path: Path, seconds: float = 10 * 86400) -> None:
    when = time.time() - seconds
    os.utime(path, (when, when))


@pytest.fixture()
def common(tmp_path: Path) -> Path:
    c = tmp_path / "repo" / ".git"
    (c / "objects" / "pack").mkdir(parents=True)
    (c / "HEAD").write_text("ref: refs/heads/main\n")
    return c


@pytest.fixture()
def git_made_store(common: Path) -> tuple[Path, list[Path]]:
    """A pack dir whose tail marker names a git-made pack (with `.rev`) and a stale pack."""
    pack_dir = common / "objects" / "pack"
    files = []
    for name in (_GIT_MADE, _STALE):
        for suffix in (".pack", ".idx", ".rev"):
            f = pack_dir / f"pack-{name}{suffix}"
            f.write_bytes(b"git-made " + suffix.encode())
            files.append(f)
    (pack_dir / "claude-klabauter-tail").write_text(f"{_GIT_MADE}\n{_STALE}\n", encoding="ascii")
    return pack_dir, files


def test_shipped_default_is_paused():
    assert pack_deletion_pause.PACK_DELETION_PAUSED is True
    assert pack_deletion_pause.paused() is True


def test_flush_leaves_a_git_made_pack_and_its_rev_intact(common, git_made_store):
    pack_dir, files = git_made_store
    marker_before = (pack_dir / "claude-klabauter-tail").read_bytes()
    go._flush_pack(common, _pending(3, b"paused"))
    for f in files:
        assert f.exists(), f"{f.name} was deleted while paused"
    assert (pack_dir / "claude-klabauter-tail").read_bytes() == marker_before
    new_idx = [p for p in pack_dir.glob("pack-*.idx") if _GIT_MADE not in p.name and _STALE not in p.name]
    assert len(new_idx) == 1, "pending must become its own pack"


def test_unpaused_flush_still_folds_and_deletes(common, git_made_store, monkeypatch):
    _set_paused(monkeypatch, False)
    pack_dir, _ = git_made_store
    go._flush_pack(common, _pending(3, b"unpaused"))
    assert not (pack_dir / f"pack-{_STALE}.idx").exists()
    assert not (pack_dir / f"pack-{_STALE}.pack").exists()


def test_tick_spawns_nothing_and_stats_nothing(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    spawned = []
    monkeypatch.setattr(maintenance_tick, "_spawn", lambda r, t: spawned.append((r, t)) or True)
    monkeypatch.setattr(maintenance_tick, "_next_check", {})
    monkeypatch.setattr(
        maintenance_tick, "resolve_git_common_dir", lambda *_: pytest.fail("a paused tick touched the repo")
    )
    assert maintenance_tick.on_idle_tick([repo]) is False
    assert spawned == []
    assert maintenance_tick._next_check == {}


def test_unpaused_tick_still_launches(tmp_path, monkeypatch):
    _set_paused(monkeypatch, False)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    spawned = []
    monkeypatch.setattr(maintenance_tick, "_spawn", lambda r, t: spawned.append((r, t)) or True)
    monkeypatch.setattr(maintenance_tick, "_next_check", {})
    monkeypatch.setattr(maintenance_tick, "resolve_git_common_dir", lambda r: Path(r) / ".git")
    assert maintenance_tick.on_idle_tick([repo]) is True
    assert len(spawned) == 1


@pytest.mark.parametrize("tier", ["daily", "weekly"])
def test_run_tier_defers_without_a_git_spawn(tmp_path, monkeypatch, tier):
    def boom(*_a, **_k):
        raise AssertionError("a paused run_tier spawned git")

    monkeypatch.setattr(git_maintenance, "run_git", boom)
    monkeypatch.setattr(git_maintenance, "absolute_git_dir", boom)
    result = git_maintenance.run_tier(tmp_path, tier)
    assert result.deferred == git_maintenance.PAUSED_REASON
    assert result.ran is False
    assert result.rc == 0


def test_sweep_orphan_packs_defers_and_unlinks_nothing(common, monkeypatch):
    pack_dir = common / "objects" / "pack"
    orphan = pack_dir / "tmp-1-pack-xyz.pack"
    orphan.write_bytes(b"x")
    _age(orphan)
    swept = git_maintenance.sweep_orphan_packs(pack_dir, on_wait=lambda: None)
    assert swept.deferred == git_maintenance.PAUSED_REASON
    assert swept.reaped == []
    assert orphan.exists()

    _set_paused(monkeypatch, False)
    swept = git_maintenance.sweep_orphan_packs(pack_dir, on_wait=lambda: None)
    assert swept.deferred is None
    assert swept.reaped == [orphan]
    assert not orphan.exists()


def test_sweep_orphan_pack_files_unlinks_nothing(common, monkeypatch):
    pack_dir = common / "objects" / "pack"
    rev = pack_dir / f"pack-{_GIT_MADE}.rev"
    rev.write_bytes(b"rev")
    _age(rev)
    assert store_hygiene.sweep_orphan_pack_files(common) == 0
    assert rev.exists()

    _set_paused(monkeypatch, False)
    assert store_hygiene.sweep_orphan_pack_files(common) == 1
    assert not rev.exists()


def test_consolidate_pack_batch_spawns_nothing_and_unlinks_nothing(common, monkeypatch):
    pack_dir = common / "objects" / "pack"
    files = []
    for i in range(store_hygiene.CONSOLIDATE_MAX_PACKS + 5):
        name = f"{i:040x}"
        for suffix in (".pack", ".idx"):
            f = pack_dir / f"pack-{name}{suffix}"
            f.write_bytes(b"x" * (store_hygiene._MIN_PACK_BYTES + 1))
            files.append(f)

    def boom(*_a, **_k):
        raise AssertionError("a paused consolidate spawned git")

    monkeypatch.setattr("coordinator_core.git.run.run_git", boom)
    assert store_hygiene.consolidate_pack_batch(common) == 0
    assert all(f.exists() for f in files)
