"""
Tests for coordinator_core.ops.generator_scan_cache.

Negative-spec: these tests never touch the real `state/cache/` directory --
every case runs against `tmp_path`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from coordinator_core.ops.generator_provenance import FileWrites, WriteSite
from coordinator_core.ops import generator_scan_cache as cache


def _sample_writes() -> FileWrites:
    return FileWrites(
        generates=[{"artifact": "a.txt", "stamp_key": "k", "sources": ["s.py"]}],
        mutates=None,
        write_sites=[
            WriteSite(target_literal="a.txt", via_tmp_handle=False, excluded=False),
            WriteSite(target_literal=None, via_tmp_handle=True, excluded=False),
        ],
        syntax_error=False,
    )


def test_round_trip_save_then_load(tmp_path: Path) -> None:
    entries = {
        "coordinator_core/foo.py": {
            "mtime_ns": 123456789,
            "size": 42,
            "writes": _sample_writes(),
        }
    }
    cache.save(tmp_path, entries)
    loaded = cache.load(tmp_path)
    assert loaded == entries


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    assert cache.load(tmp_path) == {}


def test_load_os_error_on_read_returns_empty(tmp_path: Path) -> None:
    cache_path = cache._cache_path(tmp_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    # A directory in place of the expected file makes any read() raise
    # OSError (IsADirectoryError/PermissionError) cross-platform, without
    # relying on chmod semantics that differ on Windows.
    cache_path.mkdir()
    assert cache.load(tmp_path) == {}


def test_load_invalid_json_returns_empty(tmp_path: Path) -> None:
    cache_path = cache._cache_path(tmp_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("{not json", encoding="utf-8")
    assert cache.load(tmp_path) == {}


def test_load_truncated_body_returns_empty(tmp_path: Path) -> None:
    cache_path = cache._cache_path(tmp_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    full = json.dumps({"schema": 1, "entries": {"x": {"mtime_ns": 1, "size": 1, "writes": {}}}})
    cache_path.write_text(full[: len(full) // 2], encoding="utf-8")
    assert cache.load(tmp_path) == {}


def test_load_wrong_schema_returns_empty(tmp_path: Path) -> None:
    cache_path = cache._cache_path(tmp_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"schema": 2, "entries": {}}), encoding="utf-8")
    assert cache.load(tmp_path) == {}


def test_load_malformed_entry_returns_empty(tmp_path: Path) -> None:
    cache_path = cache._cache_path(tmp_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "entries": {"x.py": {"mtime_ns": "not-an-int", "size": 1, "writes": {}}},
    }
    cache_path.write_text(json.dumps(payload), encoding="utf-8")
    assert cache.load(tmp_path) == {}


def test_atomic_write_leaves_no_stray_temp_file(tmp_path: Path) -> None:
    entries = {
        "x.py": {"mtime_ns": 1, "size": 2, "writes": _sample_writes()},
    }
    cache.save(tmp_path, entries)
    cache_dir = cache._cache_path(tmp_path).parent
    leftovers = [p for p in cache_dir.iterdir() if p.name != cache._cache_path(tmp_path).name]
    assert leftovers == []


def test_save_into_unwritable_location_does_not_raise(tmp_path: Path) -> None:
    # A plain file sitting where a required directory segment must go makes
    # `mkdir(parents=True)` raise OSError cross-platform, without relying on
    # chmod semantics that differ on Windows.
    blocked_root = tmp_path / "blocked"
    blocked_root.write_text("not a directory", encoding="utf-8")
    cache.save(blocked_root, {"x.py": {"mtime_ns": 1, "size": 1, "writes": _sample_writes()}})


def test_cache_path_resolves_under_tmp_path_root(tmp_path: Path) -> None:
    path = cache._cache_path(tmp_path)
    assert path == tmp_path / "state" / "cache" / "generator-scan-cache.json"


def test_file_writes_round_trip_with_list_of_dicts_generates(tmp_path: Path) -> None:
    writes = FileWrites(
        generates=[
            {"artifact": "a.txt", "stamp_key": "k1", "sources": ["s1.py", "s2.py"]},
            {"artifact": "b.txt", "stamp_key": "k2", "sources": []},
        ],
        mutates=["state/**/*.yaml"],
        write_sites=[
            WriteSite(target_literal="a.txt", via_tmp_handle=False, excluded=False),
            WriteSite(target_literal=None, via_tmp_handle=False, excluded=True),
        ],
        syntax_error=False,
    )
    round_tripped = cache.file_writes_from_json(cache.file_writes_to_json(writes))
    assert round_tripped == writes
