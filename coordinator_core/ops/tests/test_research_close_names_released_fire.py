"""research.close names the submit call that starts each released build run."""

from __future__ import annotations

from coordinator_core.ops.tests import test_research_close_releases_build_sibling as base
from coordinator_core.ops.tests.test_research_close_releases_build_sibling import repo  # noqa: F401
from coordinator_core.warp_fire import fire_command

pytestmark = base.pytestmark


def test_release_names_the_fire_for_the_build_sizing(repo, tmp_path):
    base._split(repo)
    out = base._close(repo, tmp_path)
    assert out["released_runs"] == [base._BUILD]
    fires = out["released_fires"]
    assert fires == [{
        "run_id": base._BUILD,
        "sizing": base._BUILD_SIZING,
        "fire": fire_command(base._BUILD_SIZING),
    }]
    assert base._BUILD_SIZING in fires[0]["fire"]


def test_pipeline_run_close_names_the_fire(repo, tmp_path):
    base._split_ask(repo, with_status=False)
    out = base._close(repo, tmp_path, run_id="pipeline-research-1")
    assert [f["run_id"] for f in out["released_fires"]] == out["released_runs"] == [base._BUILD]
    assert out["released_fires"][0]["fire"] == fire_command(base._BUILD_SIZING)


def test_close_releasing_nothing_names_no_fire(repo, tmp_path):
    out = base._close(repo, tmp_path)
    assert out["exit_code"] == 0
    assert "released_fires" not in out and "released_runs" not in out
