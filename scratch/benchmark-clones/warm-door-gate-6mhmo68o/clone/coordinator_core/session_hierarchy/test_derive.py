"""Tests for coordinator_core.session_hierarchy.derive.

Covers the session_id coalescing pass added to close the duplicate-primary-key
divergence between downstream consumers (example-cockpit-repo, example-retrieval-repo) — see
the module negative-spec in ``derive.py`` for merge semantics.
"""
from __future__ import annotations

from coordinator_core.session_hierarchy.derive import derive


def _handoff(path: str, claimed_by: str, workstream: str, **extra):
    fm = {"claimed_by": claimed_by, "workstream": workstream, **extra}
    return {"path": path, "frontmatter": fm}


def test_two_handoffs_same_session_two_workstreams_coalesce_to_one_record():
    sid = "5524e905-f2e0-4ccf-97f8-953f358ab615"
    earlier = _handoff("state/handoffs/2026-06-24_075121_alpha.md", sid, "workstream-a")
    later = _handoff("state/handoffs/2026-07-01_090000_beta.md", sid, "workstream-b")

    result = derive([earlier, later], [])

    session_records = [r for r in result if r["session_type"] != "workstream"]
    matching = [r for r in session_records if r["session_id"] == sid]
    assert len(matching) == 1

    merged = matching[0]
    assert merged["linked_handoffs"] == sorted(
        [
            "state/handoffs/2026-06-24_075121_alpha.md",
            "state/handoffs/2026-07-01_090000_beta.md",
        ]
    )
    assert merged["workstream"] == "workstream-b"


def test_single_handoff_session_record_unchanged_in_shape():
    sid = "aaaa0000-0000-0000-0000-000000000001"
    handoff = _handoff(
        "state/handoffs/2026-06-24_075121_solo.md", sid, "workstream-a", branch="work/foo/2026-06-24"
    )

    result = derive([handoff], [])
    session_records = [r for r in result if r["session_type"] != "workstream"]
    assert len(session_records) == 1

    record = session_records[0]
    assert record == {
        "session_id": sid,
        "session_type": "session",
        "workstream": "workstream-a",
        "parent_session_id": None,
        "linked_handoffs": ["state/handoffs/2026-06-24_075121_solo.md"],
        "branch": "work/foo/2026-06-24",
        "system": record["system"],
    }


def test_winner_null_parent_falls_back_to_earlier_real_parent():
    sid = "bbbb0000-0000-0000-0000-000000000002"
    parent_sid = "bbbb0000-0000-0000-0000-000000000001"

    # Predecessor lookup keyed by basename of the predecessor handoff's path.
    predecessor_handoff = {
        "path": "state/handoffs/2026-06-01_000000_pred.md",
        "frontmatter": {"claimed_by": parent_sid, "workstream": "workstream-a"},
    }
    earlier = {
        "path": "state/handoffs/2026-06-24_075121_earlier.md",
        "frontmatter": {
            "claimed_by": sid,
            "workstream": "workstream-a",
            "predecessor": "2026-06-01_000000_pred.md",
        },
    }
    later_no_parent = {
        "path": "state/handoffs/2026-07-01_090000_later.md",
        "frontmatter": {"claimed_by": sid, "workstream": "workstream-b"},
    }

    result = derive([predecessor_handoff, earlier, later_no_parent], [])
    session_records = [r for r in result if r["session_type"] != "workstream"]
    merged = next(r for r in session_records if r["session_id"] == sid)

    assert merged["parent_session_id"] == parent_sid
    assert merged["workstream"] == "workstream-b"


def test_no_branch_key_when_winner_has_no_branch():
    sid = "cccc0000-0000-0000-0000-000000000001"
    earlier = _handoff(
        "state/handoffs/2026-06-24_075121_earlier.md", sid, "workstream-a", branch="work/foo/2026-06-24"
    )
    later_no_branch = _handoff("state/handoffs/2026-07-01_090000_later.md", sid, "workstream-b")

    result = derive([earlier, later_no_branch], [])
    session_records = [r for r in result if r["session_type"] != "workstream"]
    merged = next(r for r in session_records if r["session_id"] == sid)

    assert "branch" not in merged


def test_output_ordering_session_records_precede_workstream_nodes():
    sid = "dddd0000-0000-0000-0000-000000000001"
    handoff = _handoff("state/handoffs/2026-06-24_075121_solo.md", sid, "workstream-a")

    result = derive([handoff], [])

    session_type_positions = [r["session_type"] for r in result]
    assert session_type_positions == ["session", "workstream"]


def test_dual_tolerant_read_falls_back_to_retired_consumed_by():
    # Exercises derive._claimed_by's dual-tolerant fallback for not-yet-migrated
    # frontmatter (DR-084 transitional tolerance, restored 2026-07-23) —
    # deliberately keeps the old vocabulary as input.
    sid = "ffff0000-0000-0000-0000-000000000001"
    handoff = {
        "path": "state/handoffs/2026-06-24_075121_solo.md",
        "frontmatter": {"consumed_by": sid, "workstream": "workstream-a"},
    }

    result = derive([handoff], [])
    session_records = [r for r in result if r["session_type"] != "workstream"]
    assert len(session_records) == 1
    assert session_records[0]["session_id"] == sid


def test_claimed_by_alone_bridges_to_a_session_record():
    sid = "ffff0000-0000-0000-0000-000000000002"
    handoff = {
        "path": "state/handoffs/2026-06-24_075121_solo.md",
        "frontmatter": {"claimed_by": sid, "workstream": "workstream-a"},
    }

    result = derive([handoff], [])
    session_records = [r for r in result if r["session_type"] != "workstream"]
    assert len(session_records) == 1
    assert session_records[0]["session_id"] == sid


def test_workstream_node_derivation_unaffected_by_merge():
    sid = "eeee0000-0000-0000-0000-000000000001"
    earlier = _handoff("state/handoffs/2026-06-24_075121_earlier.md", sid, "workstream-a")
    later = _handoff("state/handoffs/2026-07-01_090000_later.md", sid, "workstream-b")

    result = derive([earlier, later], [])
    workstream_nodes = [r for r in result if r["session_type"] == "workstream"]

    assert {n["workstream"] for n in workstream_nodes} == {"workstream-a", "workstream-b"}
    assert {n["session_id"] for n in workstream_nodes} == {
        "workstream:workstream-a",
        "workstream:workstream-b",
    }
