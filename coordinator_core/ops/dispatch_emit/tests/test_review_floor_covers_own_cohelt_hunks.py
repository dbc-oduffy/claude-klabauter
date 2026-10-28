"""The review slice must cover every path a run lands, including its own hunks in co-held files."""

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit import review_floor as rf
from coordinator_core.ops.dispatch_emit.tests import test_land_run_review_floor as lf
from coordinator_core.ops.dispatch_emit.tests.test_land_run_review_floor import repo  # noqa: F401  (fixture)

LANDING = (
    "coordinator_core/ops/dispatch_emit/run_close_parts.py",
    "coordinator_core/ops/dispatch_emit/tests/test_run_close_parts.py",
    "coordinator_core/ops/dispatch_emit/land_run.py",
    "coordinator_core/ops/dispatch_emit/terminal_commit_closeout.py",
)


def test_slice_omitting_own_cohelt_hunks_has_a_gap_and_a_refusal():
    sl = LANDING[:2]
    gap = rf.slice_gap_paths(LANDING, sl)
    assert gap == [LANDING[2], LANDING[3]]
    assert "slice omits 2 landing path(s)" in rf.slice_gap_reason(gap)


def test_full_slice_passes():
    gap = rf.slice_gap_paths(LANDING, [f"./{p}" for p in LANDING])
    assert gap == []
    assert rf.slice_gap_reason(gap) is None


def test_bookkeeping_paths_are_not_owed():
    assert rf.slice_gap_paths(["state/x.json", "scratch/y.py"], []) == []


@pytest.mark.spawns_process
def test_land_run_lands_with_a_note_when_the_review_slice_omits_a_landed_file(repo, monkeypatch):  # noqa: F811
    orig = land_run._handler

    def omit_slice(params, **kw):
        params["inline_review"]["prep"]["slice_files"] = ["coordinator_core/other.py"]
        return orig(params, **kw)

    monkeypatch.setattr(land_run, "_handler", omit_slice)
    token = lf._stage(repo, lf.CODE)

    out = lf._land(repo, token, lf.CODE, verdicts=1)

    assert out.get("refused") != "review-floor", out
    assert rf.slice_gap_reason(["coordinator_core/mod.py"]).startswith("review: slice omits 1 landing path(s)")
