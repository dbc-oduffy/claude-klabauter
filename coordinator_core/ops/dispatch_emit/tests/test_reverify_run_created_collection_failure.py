"""Reverify carries a staged-new run-created test into the mirror, and types a collection failure on a run-owned file apart from test_infra_failure."""
from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git, _write
from coordinator_core.ops.dispatch_emit.tests.test_land_run_clean_overlay_verify import _isolated_system_temp, _repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "runCreatedCollect"
GOOD = "t/test_run_created_good.py"
BROKEN = "t/test_run_created_broken.py"
PEER_BROKEN = "t/test_peer_broken.py"
_GOOD_SRC = "def test_runs():\n    assert 1 + 1 == 2\n"
_BROKEN_SRC = "def test_never(:\n    pass\n"


def _world(tmp_path, *, declared, stage=True):
    root, head = _repo(tmp_path)
    _write(root, PEER_BROKEN, _BROKEN_SRC)
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "peer broken")
    head = _git(root, "rev-parse", "HEAD").strip()
    _write(root, GOOD, _GOOD_SRC)
    _write(root, BROKEN, _BROKEN_SRC)
    if stage:
        _git(root, "add", "--", GOOD, BROKEN)
    manifest = cov.measured_rev_record_path(root, RUN).with_name("manifest.json")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"review_declared_paths": list(declared)}), encoding="utf-8")
    return root, head


@pytest.mark.parametrize("stage", [True, False], ids=["staged-new", "untracked"])
def test_a_run_created_test_absent_at_the_candidate_reaches_the_mirror_and_runs(tmp_path, stage):
    root, head = _world(tmp_path, declared=[GOOD], stage=stage)

    assert cov.own_untracked_writes(root, RUN, head) == [GOOD]
    out = cov.verify(root, RUN, head, [GOOD], touched=[GOOD])

    assert out.verdict == "green", out
    assert "1 passed" in out.detail and GOOD in out.detail


def test_a_path_head_holds_is_never_named_even_when_in_the_write_set(tmp_path):
    root, head = _world(tmp_path, declared=[GOOD, PEER_BROKEN])

    assert cov.own_untracked_writes(root, RUN, head) == [GOOD]


@pytest.mark.parametrize("stage", [True, False], ids=["staged-new", "untracked"])
def test_a_collection_failure_on_a_run_owned_file_is_typed_run_owned_and_names_the_path(tmp_path, stage):
    root, head = _world(tmp_path, declared=[BROKEN], stage=stage)

    out = cov.verify(root, RUN, head, [BROKEN], touched=[BROKEN])

    assert out.verdict == "red", out
    assert out.cause == cov.ZERO_RUN_OWNED_COLLECTION != cov.ZERO_RUN_TEST_INFRA
    assert f"run-owned collection failure: {BROKEN}" in out.detail


def test_a_collection_failure_on_a_file_outside_the_write_set_is_not_run_owned(tmp_path):
    root, head = _world(tmp_path, declared=[GOOD])

    out = cov.verify(root, RUN, head, [PEER_BROKEN], touched=[PEER_BROKEN])

    assert out.verdict == "red", out
    assert out.cause != cov.ZERO_RUN_OWNED_COLLECTION
    assert "run-owned collection failure" not in out.detail


def test_run_owned_collection_path_matches_posix_and_windows_separators_inside_the_set_only():
    out = "____ ERROR collecting t\\test_a.py ____\nE   ImportError\n____ ERROR collecting t/test_b.py ____\n"

    assert cov.run_owned_collection_path(out, {"t/test_a.py"}) == "t/test_a.py"
    assert cov.run_owned_collection_path(out, {"t/test_b.py"}) == "t/test_b.py"
    assert cov.run_owned_collection_path(out, {"t/test_c.py"}) == ""
    assert cov.run_owned_collection_path("no collection error here", {"t/test_a.py"}) == ""
