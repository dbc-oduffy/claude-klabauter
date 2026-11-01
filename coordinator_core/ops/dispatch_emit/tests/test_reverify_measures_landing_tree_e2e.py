"""Prime falsifier: a fixture edited outside every declared write either lands with `scope_expanded` or
refuses the land, driven through the real producer (`measured_rev`) and the real `land_run` handler; the
export the verify leg runs in equals the record's tree, and the landed product equals the record."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit import measured_rev as mr
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import (  # noqa: F401
    RUN2,
    halted,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git, repo  # noqa: F401
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
DECLARED = [C3_PATH, C4_PATH, LANE]
FIXTURE = LANE_TEST


def _stage(root):
    resume = _composed(root)
    token = _stage_replanned_run(root, '  register_rows: ["R2"]\n')
    return resume, token


def _record(root):
    return json.loads(measured_rev_record_path(root, RUN2).read_text(encoding="utf-8"))


def _head(root):
    return _git(root, "rev-parse", "HEAD").strip()


def _ls_blob(root, rev, path):
    out = _git(root, "ls-tree", rev, "--", path).split()
    return out[2] if len(out) >= 3 else None


def test_fixture_the_record_omits_is_absent_from_the_export_and_refuses_the_land(halted):  # noqa: F811
    resume, token = _stage(halted)
    mr.measured_rev(RUN2, [C3_PATH, C4_PATH], repo_root=halted)
    record = _record(halted)
    head = _git(halted, "rev-parse", "HEAD")

    assert C3_PATH in record["blobs"]
    assert FIXTURE not in record["blobs"] and LANE not in record["blobs"]

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out["committed"] is False and out["refused"] == land_run.LANDING_TREE_DIFFERS, out
    assert FIXTURE in out["paths"]
    assert _git(halted, "rev-parse", "HEAD") == head


def test_fixture_the_record_carries_lands_disclosed_and_the_export_equals_the_record_tree(halted):  # noqa: F811
    resume, token = _stage(halted)
    mr.measured_rev(RUN2, [C3_PATH, C4_PATH, LANE, FIXTURE], repo_root=halted)
    record = _record(halted)
    assert FIXTURE in record["blobs"]
    assert mr.measured_rev_drift(measured_rev_record_path(halted, RUN2).parent, halted) == []
    parent = _head(halted)
    assert record["base_sha"] == parent

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out["committed"] is True, out
    assert out["scope_expanded"] == sorted([LANE, FIXTURE])
    sha = out["sha"]
    assert _git(halted, "rev-parse", f"{sha}^").strip() == parent
    for rel, blob in record["blobs"].items():
        assert _ls_blob(halted, sha, rel) == blob, rel
    landed = set(_git(halted, "diff", "--name-only", parent, sha).split())
    assert set(record["paths"]) <= landed
    assert not (halted / "scratch" / "measured-rev" / RUN2).exists()


def test_a_process_artifact_landing_alongside_does_not_trip_the_product_compare(halted):  # noqa: F811
    resume, token = _stage(halted)
    mr.measured_rev(RUN2, [C3_PATH, C4_PATH, LANE, FIXTURE], repo_root=halted)
    record = _record(halted)
    parent = _head(halted)

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out["committed"] is True, out
    landed = set(_git(halted, "diff", "--name-only", parent, out["sha"]).split())
    artifacts = {p for p in landed if p not in record["paths"]}
    assert artifacts, "the staged run lands no process artifact; the case would pin nothing"
    assert f"state/warp/{RUN2}/measured-rev.json" in artifacts
    assert set(record["paths"]) <= landed
