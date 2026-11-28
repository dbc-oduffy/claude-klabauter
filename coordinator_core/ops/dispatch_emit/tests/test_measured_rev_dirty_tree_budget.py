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
    assert sum(opened.values()) <= 4 * RUNS, sum(opened.values())
    markers = sum(n for name, n in opened.items() if name.endswith("halted.json"))
    assert markers <= RUNS, markers  # a halted run's marker is answered from its claim row, not re-read per leg


def test_run_record_opens_do_not_grow_with_measured_paths(warmed, monkeypatch):
    _r1, narrow, _s1 = _counted_call(monkeypatch, warmed, "narrow", 6)
    _r2, wide, _s2 = _counted_call(monkeypatch, warmed, "wide2", 24)
    assert sum(wide.values()) <= 1.5 * sum(narrow.values()) + RUNS


def _halted_run(root, idx, halted_run, name: str, declared: list) -> object:
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    run_id = f"warp-{name}-20260101T000000-aaaaaa"
    rdir = record_dir(root, run_id)
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "manifest.json").write_text(
        '{"rows": [{"id": "P", "writes": %s}], "write_baseline": {}}' % str(declared).replace("'", '"'), encoding="utf-8",
    )
    halted_run.write_halt_marker(rdir, halted="bench_halt", sizing_rel=None)
    return rdir


def test_claim_row_is_written_with_the_marker_and_validates_by_stat(tmp_path):
    from coordinator_core.git import call_io_scope
    from coordinator_core.ops.dispatch_emit import halted_run
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx

    rdir = _halted_run(tmp_path, idx, halted_run, "a", ["src/a.py"])
    root = str(tmp_path)
    row = idx.claim_get(root, rdir.name)
    assert row is not None and row["h"] == 1 and row["p"] == ["src/a.py"]
    assert (tmp_path / idx._CLAIMS_FILE).exists()
    (rdir / "manifest.json").write_text('{"rows": [{"id": "P", "writes": ["src/b.py", "src/c.py"]}]}', encoding="utf-8")
    assert idx.claim_get(root, rdir.name) is None  # the manifest moved: the row is unused, never wrong
    assert idx.claim_footprint(root, rdir.name) is None  # outside a scope nothing is derived
    with call_io_scope.call_io_scope():
        again = idx.claim_footprint(root, rdir.name)
    assert again is not None and again["p"] == ["src/b.py", "src/c.py"]


def test_irrelevant_halted_run_is_decided_from_its_row_without_opening_its_files(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import halted_hold_release, halted_run
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx

    rdir = _halted_run(tmp_path, idx, halted_run, "b", ["src/only.py"])
    opened: list = []
    real_open = io.open

    def counting(file, *args, **kwargs):
        opened.append(os.fspath(file) if not isinstance(file, int) else "")
        return real_open(file, *args, **kwargs)

    marker = {"status": "halted"}
    with halted_hold_release.release_scope() as state:
        state["interest"] = frozenset({"src/other.py"})
        monkeypatch.setattr(io, "open", counting)
        monkeypatch.setattr(builtins, "open", counting)
        assert halted_hold_release.irrelevant(tmp_path, rdir, marker) is True
        assert [o for o in opened if "manifest.json" in o or "halted.json" in o] == []
    with halted_hold_release.release_scope() as state:
        state["interest"] = frozenset({"src/only.py"})
        assert halted_hold_release.irrelevant(tmp_path, rdir, marker) is False  # a declared path still claims


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


def test_a_valid_claim_row_stands_for_a_stale_index_entry_and_only_while_valid(tmp_path):
    from coordinator_core.ops.dispatch_emit import halted_run
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx

    rdir = _halted_run(tmp_path, idx, halted_run, "c", ["src/a.py"])
    halted_run.record_withheld(rdir, {"src/a.py": "withheld"})
    for entry_file in (tmp_path / idx.INDEX_DIR).glob("*.json"):
        entry_file.unlink()  # no file entry: only the row can answer it
    entry = idx.fresh_entry(tmp_path, rdir.name)
    assert entry is not None and entry.paths == ("src/a.py",) and "src/a.py" in entry.declared
    (rdir / "manifest.json").write_text('{"rows": [{"id": "P", "writes": ["src/z.py"]}]}', encoding="utf-8")
    idx.forget_fresh()
    assert idx.fresh_entry(tmp_path, rdir.name) is None  # the footprint moved: the row no longer proves anything


def test_a_row_without_a_withheld_path_is_no_entry(tmp_path):
    from coordinator_core.ops.dispatch_emit import halted_run
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx

    rdir = _halted_run(tmp_path, idx, halted_run, "d", ["src/a.py"])
    assert idx.claim_get(str(tmp_path), rdir.name)["w"] == []
    assert idx.fresh_entry(tmp_path, rdir.name) is None


def test_first_call_derives_no_claim_row_for_a_halted_run_whose_marker_wrote_one(warmed, monkeypatch):
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx

    fx = bench.build_fixture(warmed, "rows", dirty=DIRTY, runs=RUNS, paths=24)
    derived: list = []
    real = idx._derive_claim
    monkeypatch.setattr(idx, "_derive_claim", lambda root, run_id: derived.append(run_id) or real(root, run_id))
    assert bench._outcome(bench._handler_call(fx)()) == "ok"
    assert derived == [], derived[:3]  # a halted run's row came with its marker; a live run has none to derive


def test_rebuild_admits_a_superseded_marker_with_withheld_paths(tmp_path):
    from coordinator_core.ops.dispatch_emit import halted_run
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx

    rdir = _halted_run(tmp_path, idx, halted_run, "e", ["src/a.py"])
    halted_run.record_withheld(rdir, {"src/a.py": "withheld"})
    assert halted_run.mark_superseded_by_refire(rdir, prior_engine=None, engine=None, at="t") is True
    for entry_file in (tmp_path / idx.INDEX_DIR).glob("*.json"):
        entry_file.unlink()
    idx.rebuild(tmp_path)
    idx.forget_fresh()
    entry = idx.fresh_entry(tmp_path, rdir.name)
    assert entry is not None and entry.paths == ("src/a.py",)


def test_root_of_answers_a_run_dir_layout_and_refuses_any_other(tmp_path):
    from coordinator_core.ops.dispatch_emit import halted_run
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    assert halted_run._root_of(tmp_path / RECORD_DIR_ROOT / "warp-x-20260101T000000-aaaaaa") == tmp_path
    assert halted_run._root_of(str(tmp_path / RECORD_DIR_ROOT / "warp-x-20260101T000000-aaaaaa")) == tmp_path
    assert halted_run._root_of(tmp_path / "elsewhere" / "warp-x-20260101T000000-aaaaaa") is None
    assert halted_run._root_of(tmp_path / RECORD_DIR_ROOT / "a" / "b") is None
