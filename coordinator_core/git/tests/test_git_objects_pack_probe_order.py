"""Pack lookup probes the last-hit pack first and still probes every pack once on a miss."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.git import git_objects

N = 5


def _install(monkeypatch, tmp_path: Path, holders: dict[str, int]):
    common = tmp_path / ".git"
    indexes = [(Path(f"{i}.idx"), Path(f"{i}.pack"), i) for i in range(N)]
    probes: list[int] = []

    def find(pidx, sha):
        probes.append(pidx)
        return 0 if holders.get(sha) == pidx else None

    monkeypatch.setattr(git_objects, "_pack_indexes", lambda cd, revalidate=True: indexes)
    monkeypatch.setattr(git_objects, "_pack_index_find", find)
    git_objects._LAST_HIT_PACK.clear()
    return common, probes, indexes


def test_second_lookup_in_same_pack_probes_it_first(monkeypatch, tmp_path):
    common, probes, _ = _install(monkeypatch, tmp_path, {"a": 3, "b": 3})
    assert git_objects.packed_contains(common, "a")
    assert probes == [0, 1, 2, 3]
    probes.clear()
    assert git_objects.packed_contains(common, "b")
    assert probes == [3]


def test_miss_probes_every_pack_exactly_once(monkeypatch, tmp_path):
    common, probes, _ = _install(monkeypatch, tmp_path, {"a": 2})
    assert git_objects.packed_contains(common, "a")
    probes.clear()
    assert not git_objects.packed_contains(common, "zzz")
    assert sorted(probes) == list(range(N))
    assert probes[0] == 2


def test_search_records_hit_and_listing_rebuild_drops_it(monkeypatch, tmp_path):
    common, probes, _ = _install(monkeypatch, tmp_path, {"a": 4})
    monkeypatch.setattr(git_objects, "_read_pack_bytes", lambda p: b"")
    monkeypatch.setattr(git_objects, "_read_pack_object_at", lambda *a: (1, b"x"))
    assert git_objects._search_packs_for_sha(common, "a", revalidate=False) == (("commit", b"x"),)
    assert git_objects._LAST_HIT_PACK[str(common)] == 4
    (common / "objects" / "pack").mkdir(parents=True)
    git_objects._PACK_LISTING_CACHE.pop(str(common), None)
    git_objects._iter_pack_files(common)
    assert str(common) not in git_objects._LAST_HIT_PACK
