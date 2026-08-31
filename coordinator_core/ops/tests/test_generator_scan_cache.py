"""
Tests for coordinator_core.ops.generator_scan_cache.

Negative-spec: these tests never touch the real `state/cache/` directory --
every case runs against `tmp_path`.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from coordinator_core.ops import generator_provenance as gp
from coordinator_core.ops.generator_provenance import FileWrites, discover_generators
from coordinator_core.ops import generator_scan_cache as cache
from coordinator_core.ops.tests.test_generator_discovery_oracle import (
    REPO_ROOT,
    serialize_generator_records,
)


def _sample_writes() -> FileWrites:
    return FileWrites(
        generates=[{"artifact": "a.txt", "stamp_key": "k", "sources": ["s.py"]}],
        mutates=None,
        write_sites=["a.txt", None],
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


# Review: coordinatorcode-reviewer -- test_load_truncated_body_returns_empty
# removed here: it lands on the same except-JSONDecodeError branch as
# test_load_invalid_json_returns_empty above, and its own justification (a
# concurrent half-written read) is already structurally impossible given
# save()'s os.replace atomicity, covered from the writer side by
# test_atomic_write_leaves_no_stray_temp_file below.


def test_load_wrong_schema_returns_empty(tmp_path: Path) -> None:
    cache_path = cache._cache_path(tmp_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"schema": 3, "entries": {}}), encoding="utf-8")
    assert cache.load(tmp_path) == {}


def test_load_malformed_entry_returns_empty(tmp_path: Path) -> None:
    cache_path = cache._cache_path(tmp_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 2,
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
        write_sites=["a.txt", None],
        syntax_error=False,
    )
    round_tripped = cache.file_writes_from_json(cache.file_writes_to_json(writes))
    assert round_tripped == writes


def _write_fixture_module(root: Path, name: str, body: str) -> Path:
    sweep_dir = root / "coordinator_core"
    sweep_dir.mkdir(parents=True, exist_ok=True)
    module_path = sweep_dir / name
    module_path.write_text(body, encoding="utf-8")
    return module_path


_UNDECLARED_WRITER_SOURCE = """
from pathlib import Path


def write():
    Path("artifact.txt").write_text("x")
"""

_DECLARED_GENERATOR_SOURCE = """
GENERATES = [{"artifact": "out.txt", "stamp_key": "k", "sources": ["a.py"]}]

from pathlib import Path


def write():
    Path("out.txt").write_text("x")
"""


def test_cold_warm_byte_identity(tmp_path: Path) -> None:
    _write_fixture_module(tmp_path, "gen_a.py", _DECLARED_GENERATOR_SOURCE)
    _write_fixture_module(tmp_path, "gen_b.py", _UNDECLARED_WRITER_SOURCE)

    cold = discover_generators(tmp_path)
    warm = discover_generators(tmp_path)

    assert serialize_generator_records(cold) == serialize_generator_records(warm)
    assert serialize_generator_records(cold) != "[]"


def test_touching_one_file_rescans_only_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_fixture_module(tmp_path, "gen_a.py", _DECLARED_GENERATOR_SOURCE)
    stable_path = _write_fixture_module(tmp_path, "gen_b.py", _UNDECLARED_WRITER_SOURCE)

    discover_generators(tmp_path)

    calls: list[Path] = []
    original = gp._scan_or_reuse_file_writes

    def _spy(path: Path) -> FileWrites:
        calls.append(path)
        return original(path)

    monkeypatch.setattr(gp, "_scan_or_reuse_file_writes", _spy)

    new_mtime = time.time() + 10
    os.utime(stable_path, (new_mtime, new_mtime))
    stable_path.write_text(_UNDECLARED_WRITER_SOURCE + "\n# touched\n", encoding="utf-8")
    os.utime(stable_path, (new_mtime, new_mtime))

    discover_generators(tmp_path)

    assert calls == [stable_path]


def test_corrupt_cache_warm_run_returns_full_correct_set(tmp_path: Path) -> None:
    _write_fixture_module(tmp_path, "gen_a.py", _DECLARED_GENERATOR_SOURCE)
    _write_fixture_module(tmp_path, "gen_b.py", _UNDECLARED_WRITER_SOURCE)

    baseline = serialize_generator_records(discover_generators(tmp_path))

    cache_path = cache._cache_path(tmp_path)
    cache_path.write_text("{not json at all", encoding="utf-8")

    recovered = serialize_generator_records(discover_generators(tmp_path))
    assert recovered == baseline


# Review: coordinatorcode-reviewer -- symlink parity with the pre-C6 sweep
# (rglob + path.stat()/is_file(), both of which follow symlinks by default).
def test_symlinked_py_file_is_swept(tmp_path: Path) -> None:
    real_path = _write_fixture_module(tmp_path, "gen_a.py", _DECLARED_GENERATOR_SOURCE)
    sweep_dir = tmp_path / "coordinator_core"
    link_path = sweep_dir / "gen_a_link.py"
    try:
        link_path.symlink_to(real_path)
    except OSError as exc:
        pytest.skip(f"symlink creation requires elevated privilege on this host: {exc}")

    records = discover_generators(tmp_path)
    keys = {record.generator for record in records}
    assert any(key.endswith("gen_a_link.py") for key in keys)


def test_symlinked_directory_is_not_recursed_into(tmp_path: Path) -> None:
    sweep_dir = tmp_path / "coordinator_core"
    sweep_dir.mkdir(parents=True, exist_ok=True)
    real_dir = tmp_path / "real_generators"
    real_dir.mkdir()
    (real_dir / "gen_a.py").write_text(_DECLARED_GENERATOR_SOURCE, encoding="utf-8")
    link_dir = sweep_dir / "link_dir"
    try:
        link_dir.symlink_to(real_dir, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation requires elevated privilege on this host: {exc}")

    records = discover_generators(tmp_path)
    keys = {record.generator for record in records}
    assert not any("link_dir" in key for key in keys)


def test_resolution_reruns_against_changed_tracked_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_path = _write_fixture_module(tmp_path, "gen_writer.py", _UNDECLARED_WRITER_SOURCE)

    monkeypatch.setattr(gp, "_tracked_paths", lambda repo_root: frozenset())
    absent_records = discover_generators(tmp_path)
    assert all(record.generator != "coordinator_core/gen_writer.py" for record in absent_records)

    calls: list[Path] = []
    original = gp._scan_or_reuse_file_writes

    def _spy(path: Path) -> FileWrites:
        calls.append(path)
        return original(path)

    monkeypatch.setattr(gp, "_scan_or_reuse_file_writes", _spy)
    monkeypatch.setattr(gp, "_tracked_paths", lambda repo_root: frozenset({"artifact.txt"}))

    present_records = discover_generators(tmp_path)
    present = [r for r in present_records if r.generator == "coordinator_core/gen_writer.py"]

    assert calls == []
    assert len(present) == 1
    assert present[0].detail.startswith("coordinator_core/gen_writer.py writes tracked path 'artifact.txt'")
    assert module_path.exists()


def test_tracked_paths_memo_returns_equal_frozenset_second_call(tmp_path: Path) -> None:
    gp._TRACKED_PATHS_MEMO.clear()
    gp.subprocess.run(
        ["git", "init", "-q", str(tmp_path)],
        capture_output=True,
        creationflags=getattr(gp.subprocess, "CREATE_NO_WINDOW", 0),
    )
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    gp.subprocess.run(
        ["git", "-C", str(tmp_path), "add", "a.txt"],
        capture_output=True,
        creationflags=getattr(gp.subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert (tmp_path / ".git" / "index").exists()

    calls: list[Path] = []
    original_run = gp.subprocess.run

    def _spy_run(*args, **kwargs):
        calls.append(args)
        return original_run(*args, **kwargs)

    gp.subprocess.run = _spy_run
    try:
        first = gp._tracked_paths(tmp_path)
        second = gp._tracked_paths(tmp_path)
    finally:
        gp.subprocess.run = original_run

    assert first == second
    assert len(calls) == 1


def test_tracked_paths_memo_invalidates_on_index_signature_change(tmp_path: Path) -> None:
    gp._TRACKED_PATHS_MEMO.clear()
    gp.subprocess.run(
        ["git", "init", "-q", str(tmp_path)],
        capture_output=True,
        creationflags=getattr(gp.subprocess, "CREATE_NO_WINDOW", 0),
    )
    index_path = tmp_path / ".git" / "index"

    calls: list[Path] = []
    original_run = gp.subprocess.run

    def _spy_run(*args, **kwargs):
        calls.append(args)
        return original_run(*args, **kwargs)

    gp.subprocess.run = _spy_run
    try:
        gp._tracked_paths(tmp_path)
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        gp.subprocess.run = original_run
        gp.subprocess.run(
            ["git", "-C", str(tmp_path), "add", "a.txt"],
            capture_output=True,
            creationflags=getattr(gp.subprocess, "CREATE_NO_WINDOW", 0),
        )
        gp.subprocess.run = _spy_run
        assert index_path.exists()
        gp._tracked_paths(tmp_path)
        # Review: coordinatorcode-reviewer -- assert the bound directly
        # rather than resting on reading the overwrite-on-miss assignment.
        assert len(gp._TRACKED_PATHS_MEMO) == 1
    finally:
        gp.subprocess.run = original_run

    assert len(calls) == 2


def test_tracked_paths_missing_index_computes_fresh_and_does_not_raise(tmp_path: Path) -> None:
    gp._TRACKED_PATHS_MEMO.clear()
    # No `.git` directory at all under tmp_path -- `.git/index` is unstatable.
    result = gp._tracked_paths(tmp_path)
    assert result is None or isinstance(result, frozenset)


@pytest.mark.cadence
def test_warm_discover_generators_process_time_under_bar() -> None:
    """AC1: warm `discover_generators` clears the 500ms brightline bar.

    Measured on this box after C6's `os.scandir` sweep replaced
    `rglob`+`stat`+`relative_to`: min of 7 samples (`time.process_time()`,
    each preceded by one untimed warm call) was ~359ms, down from the
    pre-C6 warm figure of 531ms. Threshold below is intentionally left at
    500ms, not tightened to the observed figure -- see module docstring.
    """
    discover_generators(REPO_ROOT)

    start = time.process_time()
    discover_generators(REPO_ROOT)
    elapsed_ms = (time.process_time() - start) * 1000

    assert elapsed_ms < 500, f"warm discover_generators took {elapsed_ms:.1f}ms, over the 500ms bar"
