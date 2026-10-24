"""The real-shape fixture reaches the corpus-scaled scans the attribution convicted.

Op-agnostic: it holds `build_real_shape` to the shape of
docs/research/2026-10-21-terminal-commit-real-shape-attribution.md (1,250 sizing records) so
whichever op the real-shape gate measures pays that cost. The scan does not read the tracked tree,
so it is monkeypatched small here.
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
