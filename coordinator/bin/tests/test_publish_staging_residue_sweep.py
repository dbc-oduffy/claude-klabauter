"""Staging directories `publish.py` mints are discarded on success and on failure.

Run: python -m pytest coordinator/bin/tests/test_publish_staging_residue_sweep.py -q
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location("publish_staging_residue_sweep_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


@pytest.fixture(autouse=True)
def _clean_minted():
    publish._MINTED_STAGING_DIRS.clear()
    yield
    publish._MINTED_STAGING_DIRS.clear()


def _dest(tmp_path: Path) -> Path:
    dest = tmp_path / "mirror" / "coordinator_core"
    dest.mkdir(parents=True)
    return dest


def _staging_dirs(dest: Path) -> "list[Path]":
    return sorted(dest.parent.glob(f".{dest.name}.publish-staging-*"))


def _fill_readonly(staging: Path) -> None:
    nested = staging / "sub"
    nested.mkdir()
    victim = nested / "_engine_stamp"
    victim.write_text("stamp\n", encoding="utf-8")
    os.chmod(victim, stat.S_IREAD)


def test_undiscarded_minted_dirs_are_removed_including_readonly_files(tmp_path):
    dest = _dest(tmp_path)
    first = publish._create_publish_staging_dir(dest)
    second = publish._create_publish_staging_dir(dest)
    _fill_readonly(first)
    bystander = dest.parent / ".other.publish-staging-keepme"
    bystander.mkdir()

    publish._remove_minted_staging_dirs()

    assert not first.exists() and not second.exists()
    assert _staging_dirs(dest) == []
    assert bystander.is_dir(), "a directory this process did not mint must survive"
    assert publish._MINTED_STAGING_DIRS == set()


def test_discard_retries_a_partial_removal(tmp_path, monkeypatch):
    dest = _dest(tmp_path)
    staging = publish._create_publish_staging_dir(dest)
    (staging / "f").write_text("x", encoding="utf-8")
    from coordinator_core import owner_safe_rmtree

    real_rmtree = owner_safe_rmtree.fast_rmtree
    calls = []

    def flaky_rmtree(path):
        calls.append(path)
        if len(calls) == 1:
            return False
        return real_rmtree(path)

    monkeypatch.setattr(owner_safe_rmtree, "fast_rmtree", flaky_rmtree)
    publish._discard_publish_staging_dir(staging)

    assert len(calls) == 2
    assert not staging.exists()


def test_main_leaves_no_staging_after_a_successful_run(tmp_path, monkeypatch):
    dest = _dest(tmp_path)
    monkeypatch.setattr(publish, "lower_process_priority_self", lambda: None)

    def fake_main(_argv):
        publish._create_publish_staging_dir(dest)
        return 0

    monkeypatch.setattr(publish, "_main", fake_main)

    assert publish.main([]) == 0
    assert _staging_dirs(dest) == []


def test_main_leaves_no_staging_after_a_failed_run(tmp_path, monkeypatch):
    dest = _dest(tmp_path)
    monkeypatch.setattr(publish, "lower_process_priority_self", lambda: None)

    def fake_main(_argv):
        _fill_readonly(publish._create_publish_staging_dir(dest))
        raise RuntimeError("row aborted after minting")

    monkeypatch.setattr(publish, "_main", fake_main)

    with pytest.raises(RuntimeError):
        publish.main([])
    assert _staging_dirs(dest) == []


def test_stale_sweep_still_spares_a_fresh_unminted_dir(tmp_path):
    dest = _dest(tmp_path)
    fresh = dest.parent / f".{dest.name}.publish-staging-abcd1234"
    fresh.mkdir()

    publish._sweep_stale_publish_staging_dirs(dest, publish.RunTotals())

    assert fresh.is_dir()


def _owned_dir(dest: Path, pid: int, *, age_seconds: float = 0.0) -> Path:
    path = dest.parent / f".{dest.name}.publish-staging-p{pid}-abcd1234"
    path.mkdir()
    if age_seconds:
        old = path.stat().st_mtime - age_seconds
        os.utime(path, (old, old))
    return path


def test_minted_dir_name_records_the_owner_pid(tmp_path):
    dest = _dest(tmp_path)
    staging = publish._create_publish_staging_dir(dest)

    assert f".publish-staging-p{os.getpid()}-" in staging.name


def test_live_owner_old_dir_is_kept(tmp_path):
    dest = _dest(tmp_path)
    live = _owned_dir(dest, os.getppid(), age_seconds=7 * 3600)

    publish._sweep_stale_publish_staging_dirs(dest, publish.RunTotals())

    assert live.is_dir()


def test_ownerless_dir_follows_the_age_rule(tmp_path):
    dest = _dest(tmp_path)
    old = dest.parent / f".{dest.name}.publish-staging-abcd1234"
    old.mkdir()
    stamp = old.stat().st_mtime - 7200
    os.utime(old, (stamp, stamp))

    publish._sweep_stale_publish_staging_dirs(dest, publish.RunTotals())

    assert not old.exists()
