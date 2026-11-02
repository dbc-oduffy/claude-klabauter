"""land_run lands exactly the tree a measured-rev record carries: an outside-footprint path the record
carries lands with `scope_expanded`, one it omits refuses `landing-tree-differs-from-measured`, and
the product changes a commit would introduce are compared with the record's blobs/removed."""

from __future__ import annotations

import json

import pytest

from coordinator_core.git import commit
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path
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


def _record(root, paths, removed=()):
    """Write the record where the producer does (`state/warp/<run>`)."""
    rec = measured_rev_record_path(root, RUN2)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(
        json.dumps({
            "sha": "0" * 40, "base_sha": "0" * 40, "paths": list(paths), "removed": list(removed), "tree": "0" * 40,
            "blobs": {p: commit.checkin_blob_sha(root, p) for p in paths},
        }),
        encoding="utf-8",
    )


def _stage(root):
    resume = _composed(root)
    token = _stage_replanned_run(root, '  register_rows: ["R2"]\n')
    return resume, token


def test_breach_path_the_record_omits_refuses_naming_it(halted):  # noqa: F811
    resume, token = _stage(halted)
    _record(halted, [C3_PATH])
    head = _git(halted, "rev-parse", "HEAD")

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out["committed"] is False and out["refused"] == land_run.LANDING_TREE_DIFFERS, out
    assert set(out["paths"]) == {LANE, LANE_TEST}
    assert _git(halted, "rev-parse", "HEAD") == head
    assert LANE in _git(halted, "status", "--porcelain", "--", LANE)
    assert land_run._measured_rev_call(RUN2) in out["error"]


def test_the_printed_remeasure_call_names_the_run_and_the_mismatch_remedy_carries_it(repo):  # noqa: F811
    call = land_run._measured_rev_call(RUN2)
    assert call == f"""coordinator-invoke dispatch.measured_rev '{{"run_id": "{RUN2}", "paths": []}}'"""
    _write(repo, "p.py", "p = 1\n")
    detail = land_run._mismatch_detail(repo, "p.py", {"p.py": "a" * 40}, {}, RUN2)
    assert call in detail and "re-run dispatch.measured_rev" in detail


def test_breach_path_the_record_carries_lands_and_is_disclosed(halted):  # noqa: F811
    resume, token = _stage(halted)
    _record(halted, [C3_PATH, C4_PATH, LANE, LANE_TEST])

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out["committed"] is True, out.get("paths")
    assert out["scope_expanded"] == sorted([LANE, LANE_TEST])
    landed = set(_git(halted, "show", "--name-only", "--format=", out["sha"]).split())
    assert {C3_PATH, LANE, LANE_TEST} <= landed


def test_landing_a_declared_path_the_record_omits_refuses(halted):  # noqa: F811
    resume, token = _stage(halted)
    _record(halted, [C3_PATH, LANE, LANE_TEST])
    head = _git(halted, "rev-parse", "HEAD")

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out["committed"] is False and out["refused"] == land_run.LANDING_TREE_DIFFERS, out
    assert out["paths"] == [C4_PATH]
    assert _git(halted, "rev-parse", "HEAD") == head


def test_no_record_keeps_the_silent_withhold(halted):  # noqa: F811
    resume, token = _stage(halted)
    measured_rev_record_path(halted, RUN2).parent.mkdir(parents=True, exist_ok=True)  # the dir the record would sit in

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out["committed"] is True, out
    assert "scope_expanded" not in out
    assert out["footprint_breach"]
    landed = set(_git(halted, "show", "--name-only", "--format=", out["sha"]).split())
    assert LANE_TEST not in landed


def test_export_dir_is_reaped_after_a_refusal(halted):  # noqa: F811
    resume, token = _stage(halted)
    _record(halted, [C3_PATH])
    export = halted / "scratch" / "measured-rev" / RUN2
    export.mkdir(parents=True)
    (export / "x.txt").write_text("x", encoding="utf-8")

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out["refused"] == land_run.LANDING_TREE_DIFFERS
    assert not export.exists()


def _blob(root, rel):
    return commit.checkin_blob_sha(root, rel)


def test_mismatch_pins_both_sides(repo):  # noqa: F811
    _write(repo, "p.py", "p = 1\n")
    _write(repo, "q.py", "q = 1\n")
    record = {"blobs": {"p.py": _blob(repo, "p.py")}, "removed": []}

    assert land_run._measured_mismatch(repo, record, ["p.py"], [], {}) == []
    assert land_run._measured_mismatch(repo, record, ["p.py", "q.py"], [], {}) == ["q.py"]
    assert land_run._measured_mismatch(repo, record, [], [], {}) == ["p.py"]
    _write(repo, "p.py", "p = 2\n")
    assert land_run._measured_mismatch(repo, record, ["p.py"], [], {}) == ["p.py"]


def test_mismatch_tolerates_a_recorded_path_already_at_head(repo):  # noqa: F811
    record = {"blobs": {"README.md": _blob(repo, "README.md")}, "removed": []}

    assert land_run._measured_mismatch(repo, record, [], [], {}) == []


def test_mismatch_compares_removals(repo):  # noqa: F811
    record = {"blobs": {}, "removed": ["README.md"]}

    assert land_run._measured_mismatch(repo, record, [], ["README.md"], {}) == []
    assert land_run._measured_mismatch(repo, record, [], [], {}) == ["README.md"]
    assert land_run._measured_mismatch(repo, {"blobs": {}, "removed": []}, [], ["README.md"], {}) == ["README.md"]


def test_settled_blob_overrides_the_worktree_blob(repo):  # noqa: F811
    _write(repo, "p.py", "p = 1\n")
    record = {"blobs": {"p.py": "a" * 40}, "removed": []}

    assert land_run._measured_mismatch(repo, record, ["p.py"], [], {"p.py": "a" * 40}) == []
