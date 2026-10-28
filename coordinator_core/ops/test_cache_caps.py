"""Tests for coordinator_core.ops.cache_caps."""

from __future__ import annotations

import os
import time

from coordinator_core.ops import cache_caps
from coordinator_core.ops.cache_caps import CacheClass, apply_caps, cap_root

MB = 1024 * 1024
NO_CONFIG = lambda key: None  # noqa: E731


def _file(path, size, age_hours=0.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.truncate(size)
    if age_hours:
        stamp = time.time() - age_hours * 3600
        os.utime(path, (stamp, stamp))
    return path


def test_over_cap_cache_reduced_oldest_first(tmp_path):
    home = tmp_path / "home"
    oldest = _file(home / "debug" / "a.log", 100 * MB, age_hours=3)
    middle = _file(home / "debug" / "b.log", 100 * MB, age_hours=2)
    newest = _file(home / "debug" / "c.log", 100 * MB, age_hours=1)
    cls = (CacheClass("debug", "claude_home", "debug", 150, None),)
    rows = apply_caps(home, None, get=NO_CONFIG, classes=cls)
    assert not oldest.exists() and not middle.exists()
    assert newest.exists()
    assert rows[0]["bytes_freed"] == 200 * MB


def test_age_cap_deletes_expired_and_keeps_fresh(tmp_path):
    home = tmp_path / "home"
    old = _file(home / "todos" / "old.json", 10, age_hours=200)
    fresh = _file(home / "todos" / "new.json", 10, age_hours=1)
    cls = (CacheClass("todos", "claude_home", "todos", None, 168),)
    apply_caps(home, None, get=NO_CONFIG, classes=cls)
    assert not old.exists() and fresh.exists()


def test_non_listed_directory_untouched(tmp_path):
    home = tmp_path / "home"
    listed = _file(home / "debug" / "x.log", 10 * MB, age_hours=500)
    unlisted = _file(home / "file-history" / "x.bin", 10 * MB, age_hours=500)
    rag = _file(tmp_path / "settings" / "project-rag" / "idx.db", 10 * MB, age_hours=500)
    apply_caps(home, tmp_path / "settings", get=NO_CONFIG)
    assert not listed.exists()
    assert unlisted.exists() and rag.exists()


def test_example_retrieval_repo_row_is_never_applied(tmp_path):
    settings = tmp_path / "settings"
    rag = _file(settings / "project-rag" / "idx.db", 10 * MB, age_hours=500)
    cls = (CacheClass("rag", "settings_home", "project-rag", None, 1),)
    apply_caps(tmp_path / "home", settings, get=NO_CONFIG, classes=cls)
    assert rag.exists()


def test_machine_local_override_tightens_cap(tmp_path):
    home = tmp_path / "home"
    a = _file(home / "debug" / "a.log", 10 * MB, age_hours=2)
    b = _file(home / "debug" / "b.log", 10 * MB, age_hours=1)
    cls = (CacheClass("debug", "claude_home", "debug", 200, None),)
    get = {"cache_caps.debug.max_mb": "12"}.get
    apply_caps(home, None, get=get, classes=cls)
    assert not a.exists() and b.exists()


def test_bad_override_falls_back_to_default(tmp_path):
    cls = CacheClass("debug", "claude_home", "debug", 200, 72)
    assert cache_caps.resolve_caps(cls, {"cache_caps.debug.max_mb": "junk"}.get) == (200 * MB, 72 * 3600)


def test_dry_run_deletes_nothing(tmp_path):
    f = _file(tmp_path / "r" / "a", 5 * MB, age_hours=900)
    row = cap_root(tmp_path / "r", 1, 3600, apply=False)
    assert f.exists() and row["files_deleted"] == 1 and row["applied"] is False


def test_warm_stderr_roots_are_capped(tmp_path):
    warm = tmp_path / "warm" / "abc" / "pool-worker-stderr"
    old = _file(warm / "w.err", 10, age_hours=100)
    cls = (CacheClass("pool-worker-stderr", "warm", "pool-worker-stderr", 50, 72),)
    apply_caps(tmp_path / "home", None, get=NO_CONFIG, warm_roots=[warm], classes=cls)
    assert not old.exists()
