"""A lookup over many packs parses each `.idx` at most once; a new pack adds one merged-table segment."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from coordinator_core.git import git_objects

_PACKS = 200


def _clear() -> None:
    for d in (
        git_objects._PACK_LISTING_CACHE,
        git_objects._PACK_INDEXES_BY_LISTING,
        git_objects._LOOKUP_SOURCES,
        git_objects._RETIRED_SOURCES,
        git_objects._LAST_HIT_PACK,
        git_objects._MIDX_CACHE,
        git_objects._PACK_INDEX_CACHE,
    ):
        d.clear()


def _sha(payload: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(payload) + payload).hexdigest()


def _pack(common: Path, *payloads: bytes) -> list[str]:
    objs = {_sha(p): (b"blob", p) for p in payloads}
    git_objects._write_pack(common, objs)
    return list(objs)


def _repo(tmp_path: Path, n: int = _PACKS) -> tuple[Path, list[str]]:
    _clear()
    shas = [_pack(tmp_path, f"obj-{i}".encode())[0] for i in range(n)]
    return tmp_path, shas


def _count_parses(monkeypatch) -> list[str]:
    parsed: list[str] = []
    real = git_objects._parse_pack_index_uncached

    def spy(idx_path):
        parsed.append(idx_path.name)
        return real(idx_path)

    monkeypatch.setattr(git_objects, "_parse_pack_index_uncached", spy)
    return parsed


def _merged(common: Path) -> dict:
    return git_objects._LOOKUP_SOURCES[str(common)]._merged_table()


def _owner(common: Path, sha: str) -> Path:
    indexes, pos, _ = git_objects._locate_packed(common, sha, revalidate=True)
    return indexes.pack_path(pos)


def test_many_lookups_parse_each_idx_at_most_once(tmp_path, monkeypatch):
    common, shas = _repo(tmp_path)
    parsed = _count_parses(monkeypatch)
    for i, sha in enumerate(shas):
        assert git_objects.read_object(common, sha) == ("blob", f"obj-{i}".encode())
    assert len(parsed) == len(set(parsed))
    assert len(parsed) <= _PACKS


def test_adding_a_pack_parses_only_that_idx_and_keeps_existing_segments(tmp_path, monkeypatch):
    common, shas = _repo(tmp_path)
    git_objects.read_object(common, shas[0])
    before = _merged(common)
    segments_before = dict(git_objects._LOOKUP_SOURCES[str(common)]._sids)
    parsed = _count_parses(monkeypatch)

    (new_sha,) = _pack(common, b"fresh")
    assert git_objects.read_object(common, new_sha) == ("blob", b"fresh")
    after = _merged(common)

    assert len(parsed) == 1
    assert after is before
    assert len(after) == _PACKS + 1
    sids_after = git_objects._LOOKUP_SOURCES[str(common)]._sids
    assert all(sids_after[n] == sid for n, sid in segments_before.items())
    assert len(sids_after) == len(segments_before) + 1
    for i in (0, 57, _PACKS - 1):
        assert git_objects.read_object(common, shas[i]) == ("blob", f"obj-{i}".encode())
    assert len(parsed) == 1


def test_newest_pack_wins_a_sha_held_by_several_after_carry(tmp_path):
    common, shas = _repo(tmp_path)
    git_objects.read_object(common, shas[0])
    _merged(common)
    shared = _sha(b"shared")
    pack_a = _pack(common, b"shared", b"pad-a")
    assert shared in pack_a
    assert git_objects.read_object(common, shared) == ("blob", b"shared")
    _merged(common)
    first = _owner(common, shared)
    _pack(common, b"pad-b", b"shared")
    newest = _owner(common, shared)
    assert newest != first
    assert git_objects.read_object(common, shared) == ("blob", b"shared")
    assert _merged(common) and _owner(common, shared) == newest
    mtimes = {p: p.with_suffix(".idx").stat().st_mtime_ns for p in (first, newest)}
    assert mtimes[newest] >= mtimes[first]


def test_older_added_pack_does_not_shadow_a_newer_one(tmp_path):
    common, _ = _repo(tmp_path)
    git_objects.read_object(common, _sha(b"obj-0"))
    _merged(common)
    shared = _sha(b"shared")
    _pack(common, b"shared", b"pad-a")
    git_objects.read_object(common, shared)
    _merged(common)
    newer = _owner(common, shared)
    (old_sha,) = _pack(common, b"shared")[:1]
    assert old_sha == shared
    pack_dir = common / "objects" / "pack"
    old_idx = max(pack_dir.glob("*.idx"), key=lambda p: p.stat().st_mtime_ns)
    assert old_idx.with_suffix(".pack") != newer
    os.utime(old_idx, ns=(1, 1))
    assert _owner(common, shared) == newer
    assert git_objects.read_object(common, shared) == ("blob", b"shared")


def test_removed_pack_is_dropped_and_the_rest_still_resolve(tmp_path):
    common, shas = _repo(tmp_path)
    git_objects.read_object(common, shas[0])
    _merged(common)
    pack_dir = common / "objects" / "pack"
    gone = _owner(common, shas[5])
    gone.unlink()
    gone.with_suffix(".idx").unlink()
    assert git_objects.read_object(common, shas[6]) == ("blob", b"obj-6")
    assert git_objects.read_object(common, shas[5]) is None
    table = _merged(common)
    assert len(table) == _PACKS - 1
    assert len(list(pack_dir.glob("*.idx"))) == _PACKS - 1
