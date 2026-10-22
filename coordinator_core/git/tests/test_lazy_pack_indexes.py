"""The pack set is probed newest-first and parses an `.idx` only when a probe reaches it."""

from __future__ import annotations

import os
from pathlib import Path

from coordinator_core.git import git_objects


def _pair(pack_dir: Path, stem: str, mtime: int) -> None:
    idx = pack_dir / f"{stem}.idx"
    idx.write_bytes(b"\xfftOc\x00\x00\x00\x02")
    (pack_dir / f"{stem}.pack").write_bytes(b"PACK")
    os.utime(idx, ns=(mtime, mtime))


def _fresh(monkeypatch, tmp_path: Path):
    pack_dir = tmp_path / "objects" / "pack"
    pack_dir.mkdir(parents=True)
    parsed: list[str] = []
    monkeypatch.setattr(git_objects, "_parse_pack_index", lambda p: parsed.append(p.name) or None)
    git_objects._PACK_LISTING_CACHE.pop(str(tmp_path), None)
    git_objects._PACK_INDEXES_BY_LISTING.pop(str(tmp_path), None)
    return pack_dir, parsed


def test_newest_idx_is_first_and_unprobed_idx_is_never_parsed(monkeypatch, tmp_path):
    pack_dir, parsed = _fresh(monkeypatch, tmp_path)
    _pair(pack_dir, "pack-a", 3_000_000_000)
    _pair(pack_dir, "pack-b", 1_000_000_000)
    _pair(pack_dir, "pack-c", 2_000_000_000)

    indexes = git_objects._pack_indexes(tmp_path)

    assert parsed == []
    assert [indexes[i][0].name for i in range(3)] == ["pack-a.idx", "pack-c.idx", "pack-b.idx"]
    assert parsed == ["pack-a.idx", "pack-c.idx", "pack-b.idx"]


def test_a_lookup_stops_parsing_at_the_hit(monkeypatch, tmp_path):
    pack_dir, parsed = _fresh(monkeypatch, tmp_path)
    for n, stem in enumerate(("pack-a", "pack-b", "pack-c")):
        _pair(pack_dir, stem, 1_000_000_000 + n * 1_000_000_000)
    found = git_objects._PackIndex(Path("x.pack"), (0,) * 256, b"", [])
    monkeypatch.setattr(
        git_objects, "_parse_pack_index", lambda p: parsed.append(p.name) or found
    )
    monkeypatch.setattr(git_objects, "_pack_index_find", lambda pidx, sha: 0)
    git_objects._LAST_HIT_PACK.pop(str(tmp_path), None)

    assert git_objects.packed_contains(tmp_path, "0" * 40)
    assert parsed == ["pack-c.idx"]


def test_iteration_skips_an_unparseable_idx_and_a_miss_still_probes_the_rest(monkeypatch, tmp_path):
    pack_dir, parsed = _fresh(monkeypatch, tmp_path)
    _pair(pack_dir, "pack-a", 2_000_000_000)
    _pair(pack_dir, "pack-b", 1_000_000_000)

    assert list(git_objects._pack_indexes(tmp_path)) == []
    assert not git_objects.packed_contains(tmp_path, "0" * 40)
    assert sorted(parsed) == ["pack-a.idx", "pack-b.idx"]
