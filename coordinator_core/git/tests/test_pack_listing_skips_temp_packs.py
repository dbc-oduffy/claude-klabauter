"""Tests that `_iter_pack_files` never lists git's in-progress packs, and
that a pack vanishing between listing and open is survivable.

`index-pack`, `receive-pack` and `gc` write `.tmp-<pid>-pack-<sha>.{idx,pack}`
into `objects/pack/` and rename them into place on completion. `Path.glob`
matches leading-dot names -- unlike the shell and unlike `glob.glob` -- so a
bare `*.idx` pattern picks a temp pair up, and it passes an `is_file()` check
because at that instant it really is there. The failure lands later, at the
`open()` in `_read_pack_bytes`, once git has renamed it away.

Observed as 27 `FileNotFoundError: ...\\.tmp-35812-pack-<sha>.pack` failures in
one session on 2026-08-26 (`.git/push-failures.log`, via `auto_push.py`) -- the
shape of a busy box running concurrent fetch and gc against a shared tree.

These are unit tests over a synthesised pack directory: the bug is in which
names the listing admits, which needs no real pack bytes to demonstrate, and a
test that raced a real `git gc` would be the flake this suite exists to avoid.
"""

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
    """The listing cache is keyed by `common_dir` and lives for the process.
    Each tmp_path is a fresh key, but clearing keeps one test's rebuild from
    being served to another if a path is ever reused."""
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
    """The regression. A settled pack and a git temp pack sit side by side;
    only the settled one may reach a caller."""
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
    """Pinned separately so a future 'skip if others exist' shortcut cannot
    pass the test above while still handing a caller a doomed path."""
    pack_dir = _pack_dir(tmp_path)
    _write_pair(pack_dir, ".tmp-1-pack-" + "c" * 40)

    assert _iter_pack_files(tmp_path) == []


def test_settled_pack_whose_name_merely_contains_tmp_is_still_listed(tmp_path: Path):
    """The skip keys on the leading dot, which is what marks git's temp files
    -- not on the substring `tmp`, which would be a name-shape guess."""
    pack_dir = _pack_dir(tmp_path)
    stem = "pack-tmp" + "d" * 37
    _write_pair(pack_dir, stem)

    names = [idx.name for idx, _pack in _iter_pack_files(tmp_path)]
    assert names == [f"{stem}.idx"]


def test_pack_vanishing_between_listing_and_open_is_survived(tmp_path: Path, monkeypatch):
    """The residual race the dot-skip cannot close: a concurrent `gc` may
    unlink any pack after the listing is built, and the listing is cached
    across calls precisely so it is not re-stat'd per lookup.

    A vanished pack must be skipped, not raised through -- git only unlinks a
    pack whose objects it has already written elsewhere.
    """
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
