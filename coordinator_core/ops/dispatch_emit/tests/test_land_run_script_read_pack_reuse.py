"""One land_run leg opens each pack index and each pack file at most once, and a cold pack lookup
set is built from one directory scan and one merged table, not rebuilt per object."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from coordinator_core.git import call_io_scope
from coordinator_core.git import git_objects as go

PACKS = go._UNCOVERED_LAZY_MAX + 12


def _reset() -> None:
    for d in (
        go._PACK_LISTING_CACHE,
        go._PACK_INDEXES_BY_LISTING,
        go._LOOKUP_SOURCES,
        go._LAST_HIT_PACK,
        go._MIDX_CACHE,
        go._PACK_INDEX_CACHE,
        go._OBJECT_CACHE,
    ):
        d.clear()


@pytest.fixture(autouse=True)
def _clean():
    _reset()
    yield
    _reset()


def _blob(payload: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(payload) + payload).hexdigest()


def _packs(common: Path, count: int) -> dict[str, bytes]:
    want = {}
    for n in range(count):
        payload = f"blob-{n}".encode()
        sha = _blob(payload)
        go._write_pack(common, {sha: (b"blob", payload)})
        want[sha] = payload
    return want


def _count(monkeypatch, name: str) -> dict[str, int]:
    seen: dict[str, int] = {}
    real = getattr(go, name)

    def counting(path, *a, **kw):
        seen[str(path)] = seen.get(str(path), 0) + 1
        return real(path, *a, **kw)

    monkeypatch.setattr(go, name, counting)
    return seen


def test_a_leg_opens_each_pack_index_and_pack_file_at_most_once(tmp_path, monkeypatch):
    want = _packs(tmp_path, PACKS)
    _reset()
    idx_opens = _count(monkeypatch, "_parse_pack_index_uncached")
    pack_opens = _count(monkeypatch, "_read_pack_bytes")
    with call_io_scope.call_io_scope():
        for _ in range(3):
            go._OBJECT_CACHE.clear()
            for sha, payload in want.items():
                assert go.read_object(tmp_path, sha) == ("blob", payload)
    assert len(idx_opens) == PACKS
    assert max(idx_opens.values()) == 1
    assert len(pack_opens) == PACKS
    assert max(pack_opens.values()) == 1


def test_a_cold_lookup_set_comes_from_one_directory_scan(tmp_path, monkeypatch):
    want = _packs(tmp_path, PACKS)
    _reset()
    pack_dir = str(tmp_path / "objects" / "pack")
    scans: list[str] = []
    real = os.scandir

    def counting(path=".", *a, **kw):
        if str(path) == pack_dir:
            scans.append(str(path))
        return real(path, *a, **kw)

    monkeypatch.setattr(os, "scandir", counting)
    for sha in want:
        assert go.packed_contains(tmp_path, sha)
    assert len(scans) == 1


def test_newest_first_order_uses_the_listing_scan_mtimes(tmp_path):
    _packs(tmp_path, 3)
    _reset()
    pack_dir = tmp_path / "objects" / "pack"
    idxs = sorted(pack_dir.glob("*.idx"))
    for rank, idx in enumerate(idxs):
        os.utime(idx, ns=(10**18 + rank * 10**9, 10**18 + rank * 10**9))
    _reset()
    order = [e[0].name for e in go._pack_indexes(tmp_path)]
    assert order == [p.name for p in reversed(idxs)]


def test_a_sha_in_several_uncovered_packs_resolves_to_the_newest(tmp_path):
    dup = b"held twice"
    dup_sha = _blob(dup)
    go._write_pack(tmp_path, {dup_sha: (b"blob", dup)})
    _packs(tmp_path, PACKS)
    go._write_pack(tmp_path, {dup_sha: (b"blob", dup), _blob(b"pad"): (b"blob", b"pad")})
    _reset()
    indexes = go._pack_indexes(tmp_path)
    src = go._LookupSource(tmp_path, indexes)
    assert len(src.uncovered) > go._UNCOVERED_LAZY_MAX
    holders = [i for i in src.uncovered if go._pack_index_find(indexes[i][2], dup_sha) is not None]
    assert len(holders) == 2
    pos, offset = src.find(str(tmp_path), dup_sha, bytes.fromhex(dup_sha))
    assert pos == min(holders)
    assert offset == go._pack_index_find(indexes[pos][2], dup_sha)
    assert src.find(str(tmp_path), "ab" * 20, bytes.fromhex("ab" * 20)) is None


def test_a_few_recent_lookups_do_not_parse_every_uncovered_index(tmp_path, monkeypatch):
    want = _packs(tmp_path, go._PROBE_NEWEST + 20)
    pack_dir = tmp_path / "objects" / "pack"
    idxs = sorted(pack_dir.glob("*.idx"))
    for rank, idx in enumerate(idxs):
        os.utime(idx, ns=(10**18 + rank * 10**9, 10**18 + rank * 10**9))
    newest = go._parse_pack_index_uncached(idxs[-1])
    newest_sha = newest.shas[:20].hex()
    _reset()
    idx_opens = _count(monkeypatch, "_parse_pack_index_uncached")
    assert go.packed_contains(tmp_path, newest_sha)
    assert len(idx_opens) == 1
    assert go._LOOKUP_SOURCES[str(tmp_path)]._merged is None
    assert not go.packed_contains(tmp_path, "ab" * 20)
    assert len(idx_opens) == len(want)
    assert go._LOOKUP_SOURCES[str(tmp_path)]._merged is not None
    for sha, payload in want.items():
        assert go.read_object(tmp_path, sha) == ("blob", payload)


def test_a_lookup_builds_paths_only_for_the_packs_it_touches(tmp_path):
    want = _packs(tmp_path, go._PROBE_NEWEST + 20)
    idxs = sorted((tmp_path / "objects" / "pack").glob("*.idx"))
    for rank, idx in enumerate(idxs):
        os.utime(idx, ns=(10**18 + rank * 10**9, 10**18 + rank * 10**9))
    newest = go._parse_pack_index_uncached(idxs[-1])
    sha = newest.shas[:20].hex()
    _reset()
    assert go.packed_contains(tmp_path, sha)
    listing = go._PACK_LISTING_CACHE[str(tmp_path)][1]
    assert len(listing) == len(want)
    assert len(listing._pairs) < len(want)
    assert len(go._iter_pack_files(tmp_path)) == len(want)
