"""
coordinator_core.benchmarks.tests.test_post_run_finish_bench

Purpose: the `post_run.finish` bench passes end to end on the real op for both pipelines, refuses
when the op is absent, fails a row that breaches a bar, records the base sha as op-not-registered,
and its hardcoded-root census is clean on the real modules yet catches a planted literal.
"""

from __future__ import annotations

import pytest

from coordinator_core import ipc
from coordinator_core.benchmarks import post_run_finish_bench as bench

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture(scope="module")
def report():
    return bench.run_bench(pages=4)


def test_both_pipelines_hold_every_bar(report):
    assert report["verdict"] == "pass", report.get("failed")
    assert [r["pipeline"] for r in report["rows"]] == list(bench.PIPELINES)
    for row in report["rows"]:
        assert row["committed"] and row["exit_code"] == 0
        assert row["residual_broken_links"] == 0
        assert row["noncanonical_h1"] == 0
        assert row["stale_index"] == 0
        assert row["run_scoped_porcelain"] == 0
        assert row["unrelated_dirty_preserved"] is True
        assert row["process_ms"] < bench.PROCESS_MS_BAR
        assert row["spawns"] == report["spawn_budget"]


def test_base_sha_is_recorded_as_op_not_registered():
    assert bench.classify_base(False) == "op-not-registered"
    assert bench.classify_base(True) == "registered"
    assert bench.classify_base(None) == "base-unavailable"


def test_an_unregistered_op_is_refused(monkeypatch):
    monkeypatch.setattr(ipc, "get_op_handler", lambda name, msg=None: None)
    assert bench.run_bench(pages=2)["verdict"] == "op-not-registered"
    assert bench.main(["--pages", "2", "--json"]) == 2


def _row(**over):
    row = {
        "exit_code": 0, "committed": True, "process_ms": 10.0, "spawns": 0,
        "residual_broken_links": 0, "noncanonical_h1": 0, "stale_index": 0,
        "run_scoped_porcelain": 0, "unrelated_dirty_preserved": True,
    }
    return {**row, **over}


def test_verdict_accepts_a_clean_row_and_refuses_each_breach():
    assert bench.verdict(_row(), 0) == []
    for key, bad in (
        ("residual_broken_links", 1), ("noncanonical_h1", 1), ("stale_index", 1),
        ("run_scoped_porcelain", 1), ("process_ms", bench.PROCESS_MS_BAR),
        ("spawns", 1), ("committed", False), ("unrelated_dirty_preserved", False),
    ):
        assert bench.verdict(_row(**{key: bad}), 0), key


def test_residual_checkers_see_what_the_op_would_fix(tmp_path):
    bench.build_fixture(tmp_path, "lessons", pages=3)
    touched = [f"docs/wiki/lessons-run/page-{i:03d}.md" for i in range(3)]
    assert bench.count_broken_links(tmp_path) == 6
    assert bench.count_noncanonical_h1(tmp_path, touched) == 3
    assert bench.count_stale_indexes(tmp_path) == 1
    assert len(bench.run_scoped_porcelain(tmp_path)) == 4


def test_census_is_clean_on_the_real_modules_and_catches_a_planted_literal(tmp_path):
    assert bench.hardcoded_root_census() == []
    mod = tmp_path / "coordinator_core" / "post_run"
    mod.mkdir(parents=True)
    drive = "C" + ":" + "\\\\" + "data"
    (mod / "scope.py").write_text(f'ROOT = "{drive}"\nOK = 1\n', encoding="utf-8")
    assert bench.hardcoded_root_census(tmp_path) == ["coordinator_core/post_run/scope.py:1"]
