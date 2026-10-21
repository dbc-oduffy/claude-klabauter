"""The real-shape fixture reaches the corpus-scaled scans the attribution convicted.

Op-agnostic: it holds `build_real_shape` to the shape of
docs/research/2026-10-21-terminal-commit-real-shape-attribution.md (80 in-window stopped own
runs, 1,250 sizing records) so whichever op the real-shape gate measures pays those costs.
Neither scan reads the tracked tree, so it is monkeypatched small here.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from coordinator_core.benchmarks import terminal_commit_full_path as driver


@pytest.fixture(scope="module")
def real_shape_repo(tmp_path_factory):
    parent = tmp_path_factory.mktemp("real_shape_fidelity")
    saved_env = dict(os.environ)
    saved_tempdir = tempfile.tempdir
    saved_files = driver.REAL_TREE_FILES
    driver.REAL_TREE_FILES = 100
    try:
        repo, _script = driver.build_ask_fixture(parent)
        driver.build_real_shape(repo)
        yield repo
    finally:
        driver.REAL_TREE_FILES = saved_files
        os.environ.clear()
        os.environ.update(saved_env)
        tempfile.tempdir = saved_tempdir


def test_stopped_own_runs_returns_every_real_shape_run(real_shape_repo):
    from coordinator_core.ops.dispatch_emit import stopped_runs

    ended = stopped_runs.stopped_own_runs(real_shape_repo, driver.SESSION_ID)
    assert len(ended) == driver.REAL_STOPPED_RUNS, (
        f"stopped_own_runs returned {len(ended)} of {driver.REAL_STOPPED_RUNS} runs: the fixture's "
        "runs are not in-window, own and ended on evidence"
    )


def test_stopped_own_runs_pays_a_head_window_scan_per_run(real_shape_repo, monkeypatch):
    from coordinator_core import warp_live
    from coordinator_core.ops.dispatch_emit import stopped_runs

    scans: list[str] = []
    real = warp_live._earlier_lines

    def counted(path, needle):
        scans.append(needle)
        return real(path, needle)

    monkeypatch.setattr(warp_live, "_earlier_lines", counted)
    stopped_runs.stopped_own_runs(real_shape_repo, driver.SESSION_ID)
    assert len(scans) >= driver.REAL_STOPPED_RUNS, (
        f"{len(scans)} head-window scans for {driver.REAL_STOPPED_RUNS} runs: the evidence sits in the "
        "tail window, so the 16 to 34MB transcript read is never paid"
    )


def test_sizing_corpus_collector_reads_every_real_shape_record(real_shape_repo):
    from coordinator_core.ops import deliverable_cascade as dc

    metas: dict = {}
    dc._collect_live_candidates_for_kind(
        real_shape_repo, driver.ASK_DLV, dc._SIZING_KIND, metas_out=metas
    )
    assert len(metas) >= driver.REAL_SIZING_RECORDS, (
        f"the collector read {len(metas)} sizing records, under REAL_SIZING_RECORDS="
        f"{driver.REAL_SIZING_RECORDS}"
    )


def test_real_shape_fixture_mints_a_live_token_for_the_run(real_shape_repo):
    from coordinator_core.ops.dispatch_emit import run_token

    assert run_token.is_live(real_shape_repo, driver.ASK_RUN), (
        "the fixture run holds no live token: a field call carries run_id and run_token"
    )
