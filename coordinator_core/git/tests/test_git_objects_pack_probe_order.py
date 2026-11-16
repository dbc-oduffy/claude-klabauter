"""A listing rebuild drops the lookup source and last-hit pack; a few uncovered packs probe the last hit first."""

from __future__ import annotations

import contextlib
import hashlib
from pathlib import Path

import pytest

from coordinator_core.git import git_objects


def _pack(common: Path, n: int) -> str:
    payload = f"obj-{n}".encode()
    sha = hashlib.sha1(b"blob %d\x00" % len(payload) + payload).hexdigest()
    git_objects._write_pack(common, {sha: (b"blob", payload)})
    return sha


def _clear() -> None:
    for d in (
        git_objects._PACK_LISTING_CACHE,
        git_objects._PACK_INDEXES_BY_LISTING,
        git_objects._LOOKUP_SOURCES,
        git_objects._LAST_HIT_PACK,
        git_objects._MIDX_CACHE,
    ):
        d.clear()


@pytest.fixture(autouse=True)
def _bare_tmp_common_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fixtures' "common dir" is a bare tmp dir, not a repo; the writer lock resolves a repo.
    monkeypatch.setattr(git_objects.object_store_lock, "hold", lambda *a, **k: contextlib.nullcontext())


def test_second_lookup_in_same_pack_probes_it_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear()
    shas = [_pack(tmp_path, n) for n in range(4)]
    probes: list[Path] = []
    real = git_objects._pack_index_find

    def find(pidx, sha):
        probes.append(pidx.pack_path)
        return real(pidx, sha)

    monkeypatch.setattr(git_objects, "_pack_index_find", find)
    assert git_objects.packed_contains(tmp_path, shas[1])
    probes.clear()
    assert git_objects.packed_contains(tmp_path, shas[1])
    assert len(probes) == 1


def test_listing_rebuild_drops_lookup_source_and_last_hit(tmp_path: Path) -> None:
    _clear()
    sha = _pack(tmp_path, 0)
    assert git_objects.packed_contains(tmp_path, sha)
    key = str(tmp_path)
    assert key in git_objects._LOOKUP_SOURCES and key in git_objects._LAST_HIT_PACK
    git_objects._PACK_LISTING_CACHE.pop(key, None)
    git_objects._iter_pack_files(tmp_path)
    assert key not in git_objects._LOOKUP_SOURCES
    assert key not in git_objects._LAST_HIT_PACK


def test_new_pack_after_listing_change_is_found(tmp_path: Path) -> None:
    _clear()
    first = _pack(tmp_path, 0)
    assert git_objects.packed_contains(tmp_path, first)
    second = _pack(tmp_path, 1)
    assert git_objects.read_object(tmp_path, second) == ("blob", b"obj-1")
