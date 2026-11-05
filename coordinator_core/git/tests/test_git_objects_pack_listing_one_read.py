"""`_pack_listing` enumerates a pack dir once per generation per process, however often it is reached."""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from coordinator_core.git import git_objects

_STUB_PACKS = 1200
_COLD_BUDGET_S = 0.5


def _clear() -> None:
    for d in (
        git_objects._PACK_LISTING_CACHE,
        git_objects._PACK_INDEXES_BY_LISTING,
        git_objects._LOOKUP_SOURCES,
        git_objects._LAST_HIT_PACK,
        git_objects._MIDX_CACHE,
    ):
        d.clear()


def _real_pack(common: Path, n: int) -> str:
    payload = f"obj-{n}".encode()
    sha = hashlib.sha1(b"blob %d\x00" % len(payload) + payload).hexdigest()
    git_objects._write_pack(common, {sha: (b"blob", payload)})
    return sha


def _fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    """`alt` holds `_STUB_PACKS` unparseable pairs plus one real pack; `clone` reaches it by alternate."""
    alt = tmp_path / "alt"
    clone = tmp_path / "clone"
    pack_dir = alt / "objects" / "pack"
    pack_dir.mkdir(parents=True)
    for i in range(_STUB_PACKS):
        stem = f"pack-{i:040x}"
        (pack_dir / f"{stem}.idx").write_bytes(b"")
        (pack_dir / f"{stem}.pack").write_bytes(b"")
    sha = _real_pack(alt, 0)
    (clone / "objects" / "info").mkdir(parents=True)
    (clone / "objects" / "info" / "alternates").write_text(str(alt / "objects") + "\n", encoding="utf-8")
    return alt, clone, sha


def _count_scandir(monkeypatch) -> dict[str, int]:
    counts: dict[str, int] = {}
    real = os.scandir

    def counting(path="."):
        key = os.path.normcase(os.path.abspath(os.fspath(path)))
        counts[key] = counts.get(key, 0) + 1
        return real(path)

    monkeypatch.setattr(os, "scandir", counting)
    return counts


def _pack_scans(counts: dict[str, int], common: Path) -> int:
    return counts.get(os.path.normcase(os.path.abspath(common / "objects" / "pack")), 0)


def test_repeated_lookups_scan_each_pack_dir_once(tmp_path, monkeypatch):
    _clear()
    alt, clone, sha = _fixture(tmp_path)
    counts = _count_scandir(monkeypatch)

    started = time.process_time()
    assert git_objects.read_object(clone, sha) == ("blob", b"obj-0")
    cold = time.process_time() - started

    for _ in range(15):
        assert git_objects.read_object(clone, sha) == ("blob", b"obj-0")
        assert git_objects.packed_contains(alt, sha)
        assert len(git_objects._iter_pack_files(alt)) == _STUB_PACKS + 1

    assert _pack_scans(counts, alt) == 1
    assert _pack_scans(counts, clone) <= 1
    assert cold < _COLD_BUDGET_S, f"cold lookup over {_STUB_PACKS + 1} packs took {cold:.3f}s process time"


def test_no_revalidate_calls_never_touch_the_directory(tmp_path, monkeypatch):
    _clear()
    alt, _clone, _sha = _fixture(tmp_path)
    git_objects._pack_listing(alt)
    counts = _count_scandir(monkeypatch)
    stats: list[Path] = []
    real = git_objects._pack_dir_generation
    monkeypatch.setattr(git_objects, "_pack_dir_generation", lambda p: stats.append(p) or real(p))

    for _ in range(10):
        git_objects._pack_listing(alt, revalidate=False)

    assert _pack_scans(counts, alt) == 0
    assert stats == []


def test_revalidate_costs_a_stat_not_a_scan_while_generation_is_unchanged(tmp_path, monkeypatch):
    _clear()
    alt, _clone, _sha = _fixture(tmp_path)
    git_objects._pack_listing(alt)
    counts = _count_scandir(monkeypatch)

    for _ in range(10):
        git_objects._pack_listing(alt, revalidate=True)

    assert _pack_scans(counts, alt) == 0


def test_new_pack_is_visible_to_revalidating_callers_and_rescans_once(tmp_path, monkeypatch):
    _clear()
    alt, clone, _sha = _fixture(tmp_path)
    assert len(git_objects._pack_listing(alt)) == _STUB_PACKS + 1
    counts = _count_scandir(monkeypatch)

    fresh = _real_pack(alt, 1)
    # Force a distinct generation on filesystems whose directory mtime is coarse.
    pack_dir = alt / "objects" / "pack"
    st = pack_dir.stat()
    os.utime(pack_dir, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))

    assert git_objects.read_object(clone, fresh) == ("blob", b"obj-1")
    for _ in range(5):
        assert len(git_objects._pack_listing(alt)) == _STUB_PACKS + 2

    assert _pack_scans(counts, alt) == 1
