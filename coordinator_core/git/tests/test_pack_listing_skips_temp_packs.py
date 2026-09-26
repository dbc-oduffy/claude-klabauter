
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.git import git_objects
from coordinator_core.git.git_objects import (
    _PACK_LISTING_CACHE,
    _iter_pack_files,
    _search_packs_for_sha,
)


@pytest.fixture(autouse=True)
def _clear_listing_cache():
    _PACK_LISTING_CACHE.clear()
    yield
    _PACK_LISTING_CACHE.clear()


def _pack_dir(common_dir: Path) -> Path:
    d = common_dir / "objects" / "pack"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_pair(pack_dir: Path, stem: str) -> tuple[Path, Path]:
    idx = pack_dir / f"{stem}.idx"
    pack = pack_dir / f"{stem}.pack"
    idx.write_bytes(b"\xfftOc\x00\x00\x00\x02")
    pack.write_bytes(b"PACK\x00\x00\x00\x02")
    return idx, pack


def test_temp_pack_pair_is_not_listed(tmp_path: Path):
    pack_dir = _pack_dir(tmp_path)
    _write_pair(pack_dir, "pack-" + "a" * 40)
    _write_pair(pack_dir, ".tmp-35812-pack-" + "b" * 40)

    listed = _iter_pack_files(tmp_path)

    names = sorted(idx.name for idx, _pack in listed)
    assert names == ["pack-" + "a" * 40 + ".idx"], (
        "git's in-progress .tmp-* pack was listed as a real pack; it will be "
        "renamed away before it can be opened"
    )


def test_temp_pack_is_skipped_even_when_it_is_the_only_pack(tmp_path: Path):
    pack_dir = _pack_dir(tmp_path)
    _write_pair(pack_dir, ".tmp-1-pack-" + "c" * 40)

    assert _iter_pack_files(tmp_path) == []


def test_settled_pack_whose_name_merely_contains_tmp_is_still_listed(tmp_path: Path):
    pack_dir = _pack_dir(tmp_path)
    stem = "pack-tmp" + "d" * 37
    _write_pair(pack_dir, stem)

    names = [idx.name for idx, _pack in _iter_pack_files(tmp_path)]
    assert names == [f"{stem}.idx"]


def test_pack_vanishing_between_listing_and_open_is_survived(tmp_path: Path, monkeypatch):
    pack_dir = _pack_dir(tmp_path)
    _write_pair(pack_dir, "pack-" + "e" * 40)

    sha = "f" * 40

    monkeypatch.setattr(
        git_objects,
        "_pack_indexes",
        lambda common_dir, *, revalidate=True: [
            (pack_dir / "gone.idx", pack_dir / "gone.pack", object())
        ],
    )
    monkeypatch.setattr(git_objects, "_pack_index_find", lambda _pidx, _sha: 12)

    def _vanished(_path):
        raise FileNotFoundError(2, "No such file or directory", str(_path))

    monkeypatch.setattr(git_objects, "_read_pack_bytes", _vanished)

    assert _search_packs_for_sha(tmp_path, sha, revalidate=False) is None, (
        "a pack unlinked by a concurrent gc must read as 'not in this pack set', "
        "not raise out of the object reader"
    )
