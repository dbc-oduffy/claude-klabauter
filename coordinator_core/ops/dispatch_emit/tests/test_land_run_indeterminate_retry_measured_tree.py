"""An indeterminate (-32004) first dispatch.land_run followed by a retry at default params.

Example-game-repo-e6 retry shape: the run was measured (the measured-rev record holds the worktree blob), the
first land_run was cut after its integrity/settle work and before the commit landed, and the retry
refused `landing-tree-differs-from-measured` although the worktree still held the measured blob.

Both calls drive `land_run._handler` (the op's default entry) with the same params.

Reproduction status at b23e6cd3b1: NOT reproduced. Shapes tried, each green at base:
  - first call cut at the `land_run.commit` deadline leg (below), retry lands the measured blobs;
  - first call's `commit_paths` raising (refused `commit`), retry lands the measured blobs;
  - a second call arriving while the first is held inside `commit_paths` (below): answered
    `run-token-in-flight`, no second landing tree;
  - a baselined path carrying pre-run dirt plus an own hunk: lands narrowed with no mismatch.
The settle sources that can substitute a blob (`settled_stage` from `halted_clear`, `own_only_blobs`
over `manifest.write_baseline`) need state this fixture cannot attribute to the interrupted call, so
the cause stays unproven; these tests pin the proven-good retry contract for C2/C3 to keep green.
"""

from __future__ import annotations

import pytest

from coordinator_core.git import commit
from coordinator_core.ops.dispatch_emit import land_run, measured_rev
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import (  # noqa: F401
    RUN2,
    halted,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git, _write, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_land_run_met_over_footprint_breach_incident import (
    C3_PATH,
    C4_PATH,
    LANE,
    LANE_TEST,
    _stage_replanned_run,
)
from coordinator_core.ops.dispatch_emit.tests.test_ledger_remaining_leg_footprint_fence import _terminal_with
from coordinator_core.ops.dispatch_emit.tests.test_met_refire_lands_its_own_product_changes import _composed

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_VERDICT = {"items": [{"id": "R1", "status": "met", "wired": True, "met_by": ["C3"]}]}
_PATHS = [C3_PATH, C4_PATH, LANE, LANE_TEST]


def _stage(root):
    resume = _composed(root)
    token = _stage_replanned_run(root, '  register_rows: ["R2"]\n')
    return resume, token


def _blobs(root) -> dict:
    return {p: commit.checkin_blob_sha(root, p) for p in _PATHS}


def _cut_first_call(root, token, resume, monkeypatch) -> dict:
    """The first call passes integrity/settle/measured-tree work, then the commit leg's deadline cuts it."""
    real = land_run.land_deadline.check

    def _check(leg):
        if leg == "land_run.commit":
            raise land_run.land_deadline.LandDeadlineExceeded(leg)
        return real(leg)

    with monkeypatch.context() as ctx:
        ctx.setattr(land_run.land_deadline, "check", _check)
        out = _terminal_with(root, token, resume, _VERDICT)
    assert out["committed"] is False and out["refused"] == "land-deadline", out
    return out


def test_retry_after_an_interrupted_first_call_lands_the_measured_blobs(halted, monkeypatch):  # noqa: F811
    resume, token = _stage(halted)
    record = measured_rev.measured_rev(RUN2, _PATHS, halted)
    measured = _blobs(halted)
    assert set(record["paths"]) >= set(_PATHS), record
    head = _git(halted, "rev-parse", "HEAD")

    _cut_first_call(halted, token, resume, monkeypatch)
    assert _git(halted, "rev-parse", "HEAD") == head
    assert _blobs(halted) == measured

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out.get("refused") != land_run.LANDING_TREE_DIFFERS, out
    assert out["committed"] is True, out
    for p in _PATHS:
        assert _git(halted, "rev-parse", f"{out['sha']}:{p}").strip() == measured[p], p


def test_retry_still_refuses_a_genuine_difference_from_the_measured_blob(halted, monkeypatch):  # noqa: F811
    resume, token = _stage(halted)
    measured_rev.measured_rev(RUN2, _PATHS, halted)
    _cut_first_call(halted, token, resume, monkeypatch)
    _write(halted, C3_PATH, "a = 3  # edited after the measure\n")

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out["committed"] is False and out["refused"] == land_run.LANDING_TREE_DIFFERS, out
    assert C3_PATH in out["paths"], out


def test_second_call_while_the_first_is_held_mid_commit_gets_a_reconcile_answer(halted, monkeypatch):  # noqa: F811
    resume, token = _stage(halted)
    measured_rev.measured_rev(RUN2, _PATHS, halted)
    real = commit.commit_paths
    calls = []
    second = []

    def _held(*args, **kwargs):
        calls.append(1)
        if not second:
            second.append(_terminal_with(halted, token, resume, _VERDICT))
        return real(*args, **kwargs)

    monkeypatch.setattr(commit, "commit_paths", _held)
    first = _terminal_with(halted, token, resume, _VERDICT)

    assert first["committed"] is True, first
    assert second[0]["committed"] is False and second[0]["refused"] == "run-token-in-flight", second[0]
    assert len(calls) == 1
