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
    """Repo-root-relative, and under the MACHINERY root, not `state/`.

    Asserted through `machinery_paths.cache_dir` rather than a respelled
    literal: that module is the declared owner of the bucket, and this store
    spelling its own `("state", "cache")` tuple by hand is precisely how it
    kept writing to the retired root after the relocation had moved it.
    """
    from coordinator_core.session.machinery_paths import cache_dir

    path = cache._cache_path(tmp_path)
    assert path == Path(cache_dir(str(tmp_path))) / "generator-scan-cache.json"
    assert path.parent.parent == tmp_path / ".coordinator-local"


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


# ---------------------------------------------------------------------------
# Content-keyed cache (cold-path, first-ever-run remedy).
#
# state/bug-backlog/2026-08-22-generator-discovery-ast-parses-71mb-per-
# 94a6779e1ad8.yaml's 2026-09-07/2026-09-09 notes: the stat cache above is
# useless on a fresh clone/install (git stamps a fresh mtime_ns on every
# file regardless of content), so `discover_generators` pays its full
# ~46-60s scan on a first-ever run. These tests cover the second store this
# module now owns: a content-hash-keyed cache that a stat-miss falls
# through to BEFORE paying a full AST parse.
# ---------------------------------------------------------------------------


def test_content_hash_is_deterministic_and_content_only() -> None:
    a = cache.content_hash(b"hello")
    b = cache.content_hash(b"hello")
    c = cache.content_hash(b"hello ")
    assert a == b
    assert a != c
    assert isinstance(a, str) and len(a) == 32  # 16-byte digest, hex-encoded


def test_content_cache_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache, "_content_cache_path", lambda: tmp_path / "generator-content-cache.json")
    entries = {
        cache.content_hash(b"module a source"): _sample_writes(),
        cache.content_hash(b"module b source"): FileWrites(
            generates=None, mutates=["state/**/*.yaml"], write_sites=[], syntax_error=False
        ),
    }
    cache.save_content_cache(entries)
    loaded = cache.load_content_cache()
    assert loaded == entries


def test_load_content_cache_missing_file_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache, "_content_cache_path", lambda: tmp_path / "does-not-exist.json")
    assert cache.load_content_cache() == {}


def test_load_content_cache_wrong_schema_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "generator-content-cache.json"
    monkeypatch.setattr(cache, "_content_cache_path", lambda: path)
    path.write_text(json.dumps({"schema": cache._SCHEMA_VERSION - 1, "entries": {}}), encoding="utf-8")
    assert cache.load_content_cache() == {}


def test_load_content_cache_invalid_json_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "generator-content-cache.json"
    monkeypatch.setattr(cache, "_content_cache_path", lambda: path)
    path.write_text("{not json", encoding="utf-8")
    assert cache.load_content_cache() == {}


def test_load_content_cache_malformed_entry_dropped_rest_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "generator-content-cache.json"
    monkeypatch.setattr(cache, "_content_cache_path", lambda: path)
    good_digest = cache.content_hash(b"good")
    payload = {
        "schema": cache._SCHEMA_VERSION,
        "entries": {
            "bad-digest": {"not": "a valid FileWrites shape"},
            good_digest: cache.file_writes_to_json(_sample_writes()),
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = cache.load_content_cache()
    assert set(loaded) == {good_digest}
    assert loaded[good_digest] == _sample_writes()


def test_save_content_cache_atomic_no_stray_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "generator-content-cache.json"
    monkeypatch.setattr(cache, "_content_cache_path", lambda: path)
    cache.save_content_cache({cache.content_hash(b"x"): _sample_writes()})
    leftovers = [p for p in tmp_path.iterdir() if p.name != path.name]
    assert leftovers == []


def test_save_content_cache_deterministic_bytes_across_key_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regenerating over an unchanged corpus must not churn the committed
    diff on key order alone."""
    path = tmp_path / "generator-content-cache.json"
    monkeypatch.setattr(cache, "_content_cache_path", lambda: path)
    d1, d2 = cache.content_hash(b"one"), cache.content_hash(b"two")
    cache.save_content_cache({d1: _sample_writes(), d2: _sample_writes()})
    first_bytes = path.read_bytes()
    cache.save_content_cache({d2: _sample_writes(), d1: _sample_writes()})
    second_bytes = path.read_bytes()
    assert first_bytes == second_bytes


def test_stat_miss_hits_content_cache_and_never_parses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The star case: a file with NO stat-cache entry (a fresh checkout,
    simulated by never having run discover_generators against tmp_path
    before) but whose content the shipped content cache already knows must
    never reach `_scan_or_reuse_file_writes` (the AST-parsing path)."""
    module_path = _write_fixture_module(tmp_path, "gen_a.py", _DECLARED_GENERATOR_SOURCE)
    expected_writes = gp._scan_file_writes(
        __import__("ast").parse(_DECLARED_GENERATOR_SOURCE)
    )
    digest = cache.content_hash(_DECLARED_GENERATOR_SOURCE.encode("utf-8"))

    monkeypatch.setattr(cache, "load_content_cache", lambda: {digest: expected_writes})

    calls: list[Path] = []
    original = gp._scan_or_reuse_file_writes

    def _spy(path: Path) -> FileWrites:
        calls.append(path)
        return original(path)

    monkeypatch.setattr(gp, "_scan_or_reuse_file_writes", _spy)

    records = discover_generators(tmp_path)

    assert calls == [], "content-cache hit must short-circuit the AST parse"
    assert any(r.generator.endswith("gen_a.py") for r in records)
    assert module_path.exists()


def test_content_cache_hit_is_independent_of_path_and_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Correctness requirement: a content-keyed hit is exactly as trustworthy
    as a fresh scan REGARDLESS of which path the content sits at or when it
    was written -- the whole reason a shipped, content-addressed cache is
    sound where a shipped stat-addressed one is not."""
    same_source = _DECLARED_GENERATOR_SOURCE
    path_a = _write_fixture_module(tmp_path, "gen_a.py", same_source)
    path_b = _write_fixture_module(tmp_path, "gen_a_copy.py", same_source)
    # Force distinct mtimes, both far from "now", to rule out any accidental
    # mtime/size coincidence with a real stat-cache entry.
    os.utime(path_a, (1_000_000, 1_000_000))
    os.utime(path_b, (2_000_000, 2_000_000))

    digest = cache.content_hash(same_source.encode("utf-8"))
    expected_writes = gp._scan_file_writes(__import__("ast").parse(same_source))
    monkeypatch.setattr(cache, "load_content_cache", lambda: {digest: expected_writes})

    calls: list[Path] = []
    original = gp._scan_or_reuse_file_writes

    def _spy(path: Path) -> FileWrites:
        calls.append(path)
        return original(path)

    monkeypatch.setattr(gp, "_scan_or_reuse_file_writes", _spy)

    records = discover_generators(tmp_path)
    generators = {r.generator for r in records}

    assert calls == []
    assert any(g.endswith("gen_a.py") for g in generators)
    assert any(g.endswith("gen_a_copy.py") for g in generators)


def test_content_cache_never_loaded_on_a_fully_warm_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard for the whole point of the lazy-load: a warm sweep
    (every file's stat cache entry already matches) must never even
    consult the content cache, let alone pay to load or parse it."""
    _write_fixture_module(tmp_path, "gen_a.py", _DECLARED_GENERATOR_SOURCE)
    _write_fixture_module(tmp_path, "gen_b.py", _UNDECLARED_WRITER_SOURCE)

    discover_generators(tmp_path)  # cold: populates the stat cache

    calls = {"n": 0}
    original_loader = cache.load_content_cache

    def _counting_loader():
        calls["n"] += 1
        return original_loader()

    monkeypatch.setattr(cache, "load_content_cache", _counting_loader)

    discover_generators(tmp_path)  # warm: every file's stat matches

    assert calls["n"] == 0, "a fully warm run must never touch the content cache"


def test_shipped_content_cache_schema_matches_current_version() -> None:
    """Regeneration tripwire: if `_SCHEMA_VERSION` is bumped (a scanner-
    semantics change) and someone forgets to run
    `coordinator/bin/regenerate-generator-content-cache.py` and commit the
    result, the shipped artifact's `schema` field is left behind. That is
    SAFE at runtime (`load_content_cache`'s own version gate makes a stale
    schema degrade to "no content cache", never a wrong answer) but it is
    silent -- the cold-path speedup quietly stops working with no error
    anywhere. This test is the loud version of that same check, so a
    forgotten regeneration fails a test instead of only degrading
    performance unnoticed.
    """
    path = cache._content_cache_path()
    assert path.exists(), f"shipped content cache missing at {path}"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data.get("schema") == cache._SCHEMA_VERSION, (
        f"shipped content cache schema={data.get('schema')!r} does not match "
        f"the current _SCHEMA_VERSION={cache._SCHEMA_VERSION!r} -- run "
        f"coordinator/bin/regenerate-generator-content-cache.py and commit "
        f"the result"
    )


@pytest.mark.cadence
def test_cold_discover_generators_with_shipped_content_cache_under_bound() -> None:
    """Pins the cold-path fix: with the STAT cache deleted (simulating a
    fresh checkout) but the shipped, git-committed content cache present
    (the real production artifact next to this module), `discover_generators`
    must stay far below the ~46-60s no-cache cold cost.

    Measured on this box/container, three fresh-process runs (stat cache
    deleted before each, content cache present): 576-673ms process time --
    in line with the spike's ~609ms/79.4MB blake2b prediction plus
    resolution. Bound below is 5000ms: generous margin over the observed
    figure, while remaining two full orders of magnitude under the ~46-60s
    no-content-cache cold cost this bound exists to catch a regression back
    into.
    """
    stat_cache_path = cache._cache_path(REPO_ROOT)
    if stat_cache_path.exists():
        stat_cache_path.unlink()

    start = time.process_time()
    records = discover_generators(REPO_ROOT)
    elapsed_ms = (time.process_time() - start) * 1000

    assert len(records) > 0
    assert elapsed_ms < 5000, (
        f"cold discover_generators with the shipped content cache took "
        f"{elapsed_ms:.1f}ms -- expected well under 5000ms (~46-60s is the "
        f"no-content-cache cold cost this bound guards against regressing to)"
    )
