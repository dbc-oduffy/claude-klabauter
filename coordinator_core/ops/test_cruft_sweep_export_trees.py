"""sweep_scratch reaps stale full-repo export trees keyed on shape, owner liveness and age."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from coordinator_core.ops import cruft_sweep
from coordinator_core.testing import baseline_cache

_DEFAULT_AGE_DAYS = 7
_OLD = 30 * 86400
_SHA = "a" * 40
_LIVE_PID = 4242


@pytest.fixture(autouse=True)
def _pid_table(monkeypatch):
    """Only _LIVE_PID is alive; every other pid reads as dead."""
    monkeypatch.setattr(baseline_cache, "_pid_alive", lambda pid: pid == _LIVE_PID)


def _age(path: Path, secs: int) -> None:
    old = time.time() - secs
    os.utime(path, (old, old))


def _make_tree(parent: Path, name: str, *, age: int = _OLD, core: bool = True, pyproject: bool = True) -> Path:
    tree = parent / name
    tree.mkdir(parents=True)
    if core:
        (tree / "coordinator_core").mkdir()
        _age(tree / "coordinator_core", age)
    if pyproject:
        (tree / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        _age(tree / "pyproject.toml", age)
    _age(tree, age)
    return tree


def _sweep(repo: Path, *, apply: bool, json_mode: bool = False, rows: list | None = None):
    return cruft_sweep.sweep_scratch(
        repo, _DEFAULT_AGE_DAYS, apply=apply, json_mode=json_mode, quiet=True,
        emit_fn=(rows.append if rows is not None else None),
    )


@pytest.fixture
def scratch(tmp_path):
    root = tmp_path / "repo" / "scratch"
    root.mkdir(parents=True)
    return root


def test_apply_reaps_aged_ownerless_adhoc_named_tree_and_keeps_the_rest(scratch):
    stale = _make_tree(scratch, "verify-feature-x")
    young = _make_tree(scratch, "young-export", age=3600)
    owned = _make_tree(scratch, f"test-base-deadbeef-{_LIVE_PID}")
    dead_owner = _make_tree(scratch, "test-base-deadbeef-999999")
    no_core = _make_tree(scratch, "no-core", core=False)
    no_pyproject = _make_tree(scratch, "no-pyproject", pyproject=False)
    unit4 = _make_tree(scratch, "unit4")
    warp = _make_tree(scratch, "warp")

    _, items = _sweep(scratch.parent, apply=True)

    assert not stale.exists()
    assert not dead_owner.exists()
    assert items == 2
    for kept in (young, owned, no_core, no_pyproject, unit4, warp):
        assert kept.exists(), kept.name


def test_dry_run_reports_candidate_and_deletes_nothing(scratch):
    stale = _make_tree(scratch, "bis-old")
    rows: list = []

    _, items = _sweep(scratch.parent, apply=False, json_mode=True, rows=rows)

    assert stale.exists()
    assert items == 1
    assert any(r["path"] == str(stale) and r["disposition"] == "auto-prune" for r in rows)


def test_tree_with_git_dir_is_left_to_the_clone_proof(scratch):
    clone = _make_tree(scratch, "clone-old")
    (clone / ".git").mkdir()

    _sweep(scratch.parent, apply=True)

    assert clone.exists()


def test_cache_entry_with_live_lease_is_kept_and_unleased_stale_entry_is_reaped(scratch):
    cache = scratch / baseline_cache.CACHE_DIRNAME
    leased_sha, free_sha = "b" * 40, "c" * 40
    leased = _make_tree(cache, leased_sha)
    free = _make_tree(cache, free_sha)
    (cache / f"{leased_sha}.lease-{_LIVE_PID}").touch()
    (cache / f"{free_sha}.used").touch()
    _age(cache / f"{free_sha}.used", _OLD)

    _sweep(scratch.parent, apply=True)

    assert leased.exists()
    assert not free.exists()
    assert not (cache / f"{free_sha}.used").exists()


def test_young_cache_entry_is_kept(scratch):
    entry = _make_tree(scratch / baseline_cache.CACHE_DIRNAME, _SHA, age=3600)

    _sweep(scratch.parent, apply=True)

    assert entry.exists()


def test_cache_dir_itself_and_nested_trees_are_not_reaped_by_shape(scratch):
    cache = scratch / baseline_cache.CACHE_DIRNAME
    cache.mkdir()
    _age(cache, _OLD)
    holder = scratch / "holder"
    holder.mkdir()
    nested = _make_tree(holder, "inner")
    _age(holder, _OLD)

    _sweep(scratch.parent, apply=True)

    assert cache.exists()
    assert nested.exists()
