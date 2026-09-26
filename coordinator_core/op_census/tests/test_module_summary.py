
from __future__ import annotations

import time
from pathlib import Path

import pytest

from coordinator_core.op_census.module_summary import (
    ModuleSummary,
    load_index,
    save_index,
    summarize_paths,
)


SAMPLE_MODULE = '''\
import subprocess

def top_level_fn(x):
    return x + 1

async def another(y):
    subprocess.run(["ls"])
    return y

class C:
    def method(self):
        pass
'''


def test_summary_extracts_top_level_functions_only(tmp_path: Path):
    f = tmp_path / "mod.py"
    f.write_text(SAMPLE_MODULE, encoding="utf-8")

    result = summarize_paths([f])
    summary = result[str(f)]

    assert summary.function_names == ("top_level_fn", "another")
    assert "method" not in summary.function_names


def test_summary_counts_spawn_call_sites(tmp_path: Path):
    f = tmp_path / "mod.py"
    f.write_text(SAMPLE_MODULE, encoding="utf-8")

    summary = summarize_paths([f])[str(f)]

    assert summary.spawn_call_sites == 1


def test_summary_line_count_matches_splitlines(tmp_path: Path):
    f = tmp_path / "mod.py"
    f.write_text(SAMPLE_MODULE, encoding="utf-8")

    summary = summarize_paths([f])[str(f)]

    assert summary.line_count == len(SAMPLE_MODULE.splitlines())


def test_unparseable_file_does_not_crash(tmp_path: Path):
    f = tmp_path / "broken.py"
    f.write_text("def f(:\n    pass\n", encoding="utf-8")

    result = summarize_paths([f])
    summary = result[str(f)]

    assert summary.function_names == ()
    assert summary.spawn_call_sites == 0
    assert summary.parse_error is not None
    assert summary.line_count == 2


def test_index_hit_never_reparses(tmp_path: Path, monkeypatch):
    f = tmp_path / "mod.py"
    f.write_text(SAMPLE_MODULE, encoding="utf-8")

    index: dict = {}
    first = summarize_paths([f], index=index)[str(f)]
    assert (str(f), None) not in index
    assert str(f) in index

    calls = []
    import coordinator_core.op_census.module_summary as mod_summary_module

    real_compute = mod_summary_module._compute_module_summary

    def spy(path):
        calls.append(path)
        return real_compute(path)

    monkeypatch.setattr(mod_summary_module, "_compute_module_summary", spy)

    second = summarize_paths([f], index=index)[str(f)]

    assert calls == []
    assert second == first


def test_index_miss_on_body_change_recomputes(tmp_path: Path):
    f = tmp_path / "mod.py"
    f.write_text(SAMPLE_MODULE, encoding="utf-8")

    index: dict = {}
    first = summarize_paths([f], index=index)[str(f)]

    f.write_text(SAMPLE_MODULE + "\ndef extra():\n    pass\n", encoding="utf-8")

    second = summarize_paths([f], index=index)[str(f)]

    assert second.stamp != first.stamp
    assert "extra" in second.function_names


def test_disk_index_round_trip(tmp_path: Path):
    f = tmp_path / "mod.py"
    f.write_text(SAMPLE_MODULE, encoding="utf-8")

    index: dict = {}
    summarize_paths([f], index=index)

    disk_path = tmp_path / "index.json"
    save_index(index, disk_path)

    reloaded = load_index(disk_path)

    assert str(f) in reloaded
    stamp, summary = reloaded[str(f)]
    assert isinstance(summary, ModuleSummary)
    assert stamp == index[str(f)][0]
    assert summary == index[str(f)][1]


def test_load_index_missing_file_returns_empty(tmp_path: Path):
    assert load_index(tmp_path / "nope.json") == {}


def test_load_index_corrupt_file_returns_empty(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json{{{", encoding="utf-8")
    assert load_index(bad) == {}


def test_warm_revalidate_process_time_within_budget(tmp_path: Path):
    paths = []
    for i in range(40):
        f = tmp_path / f"mod_{i}.py"
        f.write_text(SAMPLE_MODULE, encoding="utf-8")
        paths.append(f)

    index: dict = {}
    summarize_paths(paths, index=index)

    start = time.process_time()
    summarize_paths(paths, index=index)
    elapsed_ms = (time.process_time() - start) * 1000

    assert elapsed_ms < 100, (
        f"warm revalidate over {len(paths)} files took {elapsed_ms:.1f}ms of "
        "process time, exceeding the tight synthetic-corpus budget"
    )
