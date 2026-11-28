"""While pack deletion is paused, a flush past PACK_COUNT_FORCE leaves objects loose, not a new pack."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from coordinator_core.git import git_objects as go
from coordinator_core.git import pack_deletion_pause, store_hygiene

_FORCE = 3


def _blob_sha(payload: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(payload) + payload).hexdigest()


def _pending(n: int, tag: bytes) -> dict:
    out = {}
    for i in range(n):
        payload = b"%s-%d" % (tag, i)
        out[_blob_sha(payload)] = (b"blob", payload)
    return out


@pytest.fixture()
def common(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    c = tmp_path / "repo" / ".git"
    (c / "objects" / "pack").mkdir(parents=True)
    (c / "HEAD").write_text("ref: refs/heads/main\n")
    monkeypatch.setattr(store_hygiene, "PACK_COUNT_FORCE", _FORCE)
    return c


def _seed_packs(common: Path, count: int) -> None:
    pack_dir = common / "objects" / "pack"
    for i in range(count):
        pack, idx, name = go._build_pack(_pending(2, b"seed-%d" % i))
        go._place_pack(pack_dir, pack, idx, name)


def _packs(common: Path) -> set:
    return {p.name for p in (common / "objects" / "pack").glob("pack-*.pack")}


def test_paused_flush_past_force_mints_no_pack_and_objects_read_back(common: Path) -> None:
    assert pack_deletion_pause.paused() is True
    _seed_packs(common, _FORCE + 1)
    before = _packs(common)
    pending = _pending(12, b"past-force")
    go._flush_pack(common, pending)
    assert _packs(common) == before
    for sha, (_, payload) in pending.items():
        assert (common / "objects" / sha[:2] / sha[2:]).is_file()
        assert go.read_object(common, sha) == ("blob", payload)


def test_paused_flush_at_or_below_force_still_mints_a_pack(common: Path) -> None:
    _seed_packs(common, _FORCE)
    before = _packs(common)
    pending = _pending(12, b"below-force")
    go._flush_pack(common, pending)
    assert len(_packs(common) - before) == 1
    for sha, (_, payload) in pending.items():
        assert not (common / "objects" / sha[:2] / sha[2:]).exists()
        assert go.read_object(common, sha) == ("blob", payload)


def test_paused_flush_past_force_deletes_nothing(common: Path) -> None:
    _seed_packs(common, _FORCE + 1)
    pack_dir = common / "objects" / "pack"
    files_before = {p.name for p in pack_dir.iterdir()}
    go._flush_pack(common, _pending(12, b"no-delete"))
    assert files_before <= {p.name for p in pack_dir.iterdir()}


def test_unpaused_flush_past_force_still_folds_into_a_pack(common: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pack_deletion_pause, "PACK_DELETION_PAUSED", False)
    _seed_packs(common, _FORCE + 1)
    before = _packs(common)
    pending = _pending(12, b"unpaused")
    go._flush_pack(common, pending)
    assert len(_packs(common) - before) == 1
    for sha in pending:
        assert not (common / "objects" / sha[:2] / sha[2:]).exists()
