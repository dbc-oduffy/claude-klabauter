"""The run-bound ask_gate benchmark builds the requested run-dir tree and reports both shapes.

Behaviour pinned: the fixture holds exactly the requested number of run dirs plus the two bound
runs, the call is run-bound (its reply names the bound sizing), and every leg and spawn figure the
report promises is present for the non-`-build` (incident) and `-build` shapes.
"""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import ask_gate_run_bound_process_budget as bench

pytestmark = pytest.mark.cadence

DIRS = 60


@pytest.fixture(scope="module")
def report():
    return bench.measure(dirs=DIRS, runs=1)


def test_fixture_holds_the_requested_dir_count_plus_the_two_bound_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    root = tmp_path / "repo"
    root.mkdir()
    sizings = bench.build_fixture(root, DIRS)
    warp = root / "state" / "warp"
    assert sum(1 for p in warp.iterdir() if p.is_dir()) == DIRS + len(bench.SHAPES)
    assert set(sizings) == set(bench.SHAPES)
    assert not sizings["non_build"].endswith("-build.yaml") and sizings["build"].endswith("-build.yaml")
    assert any(warp.glob("*/parent.json")) and any(warp.glob("ask-*/run.json")) and any(warp.glob("warp-*/run.json"))


def test_both_shapes_are_measured_run_bound_with_every_promised_figure(report):
    assert report["dirs"] == DIRS
    assert set(report["shapes"]) == set(bench.SHAPES)
    for shape, res in report["shapes"].items():
        assert res["bound_sizing_path"] == res["sizing_rel"], (shape, res)
        assert res["halt"] is None, (shape, res)
        assert isinstance(res["process_ms"], float) and res["process_ms"] >= 0.0
        assert isinstance(res["spawns"], int) and isinstance(res["spawn_argv"], list)
        assert {"credit_split_predecessors", "newest_run_binding", "bounded_orphan_lookup", "bind_run"} <= set(
            res["cold_legs_ms"]
        ), res["cold_legs_ms"]
        assert res["largest_leg"] in res["cold_legs_ms"]


def test_the_incident_shape_is_not_a_build_stem():
    assert not bench.SHAPES["non_build"][0].endswith("-build")
    assert bench.SHAPES["build"][0].endswith("-build")
