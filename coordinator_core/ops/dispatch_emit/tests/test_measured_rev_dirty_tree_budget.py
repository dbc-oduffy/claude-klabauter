"""`dispatch.measured_rev` on a many-dirty-path, many-run-record tree: zero spawns, and the run-record scan
is bounded per call (one open per record file per call at most a small constant, independent of how many
paths are measured). Process time against the 500ms bar is enforced by the benchmark's `--enforce-bar`."""

from __future__ import annotations

import builtins
import collections
import io
import os

import pytest

from coordinator_core.benchmarks import measured_rev_dirty_tree_budget as bench

RUNS = 60
DIRTY = 160


def _counted_call(monkeypatch, parent, name: str, paths: int):
    fx = bench.build_fixture(parent, name, dirty=DIRTY, runs=RUNS, paths=paths)
    call = bench._handler_call(fx)
    opened: collections.Counter = collections.Counter()
    real_open = io.open

    def counting(file, *args, **kwargs):
        text = file if isinstance(file, str) else (os.fspath(file) if not isinstance(file, int) else "")
        posix = text.replace(os.sep, "/")
        if "bench-peer" in posix or "halt-withheld" in posix:  # peer run records and the withheld index; not the focal run's own
            opened[text] += 1
        return real_open(file, *args, **kwargs)

    spawns: list = []
    with monkeypatch.context() as m:
        m.setattr(io, "open", counting)
        m.setattr(builtins, "open", counting)
        with bench._spawn_counter(spawns.append):
            result = call()
    return result, opened, spawns


@pytest.fixture(scope="module")
def warmed(tmp_path_factory):
    bench.declare_benchmark_origin()
    warm = bench.build_fixture(tmp_path_factory.mktemp("warm"), "warmup", dirty=8, runs=4, paths=4)
    bench._handler_call(warm)()
    return tmp_path_factory.mktemp("fx")


def test_measured_rev_run_record_scan_is_bounded_and_spawn_free(warmed, monkeypatch):
    result, opened, spawns = _counted_call(monkeypatch, warmed, "wide", 24)
    assert bench._outcome(result) == "ok"
    assert spawns == []
    assert max(opened.values()) <= 5, opened.most_common(3)
    assert sum(opened.values()) <= 5 * RUNS, sum(opened.values())


def test_run_record_opens_do_not_grow_with_measured_paths(warmed, monkeypatch):
    _r1, narrow, _s1 = _counted_call(monkeypatch, warmed, "narrow", 6)
    _r2, wide, _s2 = _counted_call(monkeypatch, warmed, "wide2", 24)
    assert sum(wide.values()) <= 1.5 * sum(narrow.values()) + RUNS


def _cold(idx) -> None:
    idx._STAT_CACHE.clear()
    idx._SNAPSHOT.clear()
    idx._LOADED.clear()


def test_cold_process_reads_withheld_index_from_persisted_cache(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    (tmp_path / RECORD_DIR_ROOT).mkdir(parents=True)
    ids = [f"warp-r{i}-20260101T000000-aaaaa{i % 10}" for i in range(40)]
    for i, run_id in enumerate(ids):
        idx.add_run(tmp_path, run_id, [f"src/m{i}.py"], at_ns=1)
    idx.rebuild(tmp_path)
    expected = idx.entries(tmp_path)
    assert len(expected) == 40
    parsed: list = []
    real = idx._parse_file
    monkeypatch.setattr(idx, "_parse_file", lambda f: parsed.append(f) or real(f))

    _cold(idx)
    assert idx.entries(tmp_path) == expected
    assert parsed == []  # one cache read, no per-run parse

    idx.add_run(tmp_path, ids[3], ["src/changed.py"], at_ns=2)
    _cold(idx)
    got = idx.entries(tmp_path)
    assert got[ids[3]].paths == ("src/changed.py",)
    assert len(parsed) == 1  # only the file whose stat moved

    parsed.clear()
    (tmp_path / idx._CACHE_FILE).write_bytes(b"{not json")
    _cold(idx)
    assert idx.entries(tmp_path) == got
    assert len(parsed) == 40  # a malformed cache is never trusted
