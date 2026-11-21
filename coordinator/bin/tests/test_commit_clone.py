"""commit_clone: the publisher-code closure of one commit written from the commit tree, warm hits
with zero spawns, a refusal naming a runnable retry when the clone cannot be built, and an entry
run from the clone rather than the caller's tree. All trees live under tmp_path."""

from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from types import ModuleType

pytestmark = [pytest.mark.complete]

_MODULE = Path(__file__).resolve().parents[1] / "lib" / "commit_clone.py"


@pytest.fixture()
def cc() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_commit_clone_under_test", _MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def _no_real_cache() -> Path:
        raise AssertionError("a test reached the real managed commit-clone cache; pass a tmp_path cache")

    module.default_cache_root = _no_real_cache
    return module


_ORPHAN: dict[str, str] = {}


def test_a_legacy_marker_still_names_the_source(cc, tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    (clone / ".git" / cc._LEGACY_MARKER).write_text(f'{{"sha": "{"a" * 40}", "source_root": "{(tmp_path / "src").as_posix()}"}}', encoding="utf-8")
    assert cc.is_commit_clone(clone)
    assert cc.source_repo_root(clone) == tmp_path / "src"


def test_objects_dir_follows_a_linked_worktree_to_the_common_store(cc, tmp_path: Path) -> None:
    common = tmp_path / "main" / ".git"
    (common / "objects").mkdir(parents=True)
    gitdir = common / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {gitdir.as_posix()}\n", encoding="utf-8")
    assert cc._objects_dir(wt) == (common / "objects").resolve()


def test_prune_keeps_dest_and_newest(cc, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    names = [f"{i:040x}" for i in range(1, 6)]
    for i, name in enumerate(names):
        d = cache / name
        d.mkdir()
        os.utime(d, (1000 + i, 1000 + i))
    (cache / "abc123-staging").mkdir()
    keep = cache / names[0]  # oldest, still kept
    cc._prune(cache, keep=keep)
    left = {p.name for p in cache.iterdir()}
    assert names[0] in left
    assert "abc123-staging" in left
    assert {names[4], names[3]} <= left
    assert names[1] not in left and names[2] not in left


def test_prune_spares_a_recently_used_clone_beyond_retention(cc, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    names = [f"{i:040x}" for i in range(1, 7)]
    now = time.time()
    for i, name in enumerate(names):
        d = cache / name
        d.mkdir()
        os.utime(d, (now - 10 + i, now - 10 + i))
    cc._prune(cache, keep=cache / names[5])
    assert {p.name for p in cache.iterdir()} == set(names)


def test_every_engine_file_the_publisher_loads_by_path_is_seeded(cc: ModuleType) -> None:
    """A by-path load bypasses the clone's import filler, so the file must be in the static seed."""
    import re

    bin_dir = _MODULE.parents[1]
    loaded = {
        f"coordinator_core/{name}"
        for entry in cc._SEED_FILES
        if entry.startswith("coordinator/bin/") and entry.endswith(".py")
        for name in re.findall(r'"coordinator_core" / "([A-Za-z_]+\.py)"', (bin_dir / entry.split("/")[-1]).read_text(encoding="utf-8"))
    }
    assert loaded, "the scan found no by-path load; the pattern no longer matches the publisher"
    assert sorted(p for p in loaded if not cc._is_wanted(p, frozenset())) == []
