"""
coordinator_core.ops.tests.test_session_artifact_owner — JSON-RPC veneer
tests for "session.artifact_owner".

Spec backlink: `state/handoffs/2026-08-13-live-peer-roster.md`
§ "What this covers" amendment (L52-62).
"""

from __future__ import annotations

from coordinator_core.ops.session_artifact_owner import _session_artifact_owner
from coordinator_core.session import reachability


def test_missing_param_degrades_without_raising():
    result = _session_artifact_owner({})
    assert result["owners"] == []
    assert result["file_error"] is not None


def test_owners_shape_passthrough(tmp_path, monkeypatch):
    f = tmp_path / "artifact.md"
    f.write_text("---\nclaimed_by: sid-a\n---\n\nbody\n", encoding="utf-8")

    monkeypatch.setattr(
        reachability,
        "resolve_address",
        lambda oid: reachability.ResolveResult(outcome="reachable", session_id="sid-a", address="peer-1"),
    )

    result = _session_artifact_owner({"artifact_path": str(f)})
    assert result["artifact_path"] == str(f)
    assert result["file_error"] is None
    assert len(result["owners"]) == 1
    row = result["owners"][0]
    assert row["session_id"] == "sid-a"
    assert row["source_field"] == "claimed_by"
    assert row["outcome"] == "reachable"
    assert row["address"] == "peer-1"
    assert row["candidates"] == []


def test_no_owner_field_returns_empty_owners_no_error(tmp_path):
    f = tmp_path / "artifact.md"
    f.write_text("---\ntitle: x\n---\n\nbody\n", encoding="utf-8")

    result = _session_artifact_owner({"artifact_path": str(f)})
    assert result["owners"] == []
    assert result["file_error"] is None


def test_claim_live_and_claim_stage_pass_through_for_claim_dir_owners(tmp_path, monkeypatch):
    """AC2: the two claim-dir-only fields must survive this JSON-RPC
    boundary, not just the internal dataclass (finding: previously dropped
    at `_owner_resolution_to_dict`)."""
    from coordinator_core.session import artifact_owner

    f = tmp_path / "artifact.md"
    f.write_text("---\ntitle: x\n---\n\nbody\n", encoding="utf-8")

    fake_owner = artifact_owner.OwnerRecord(
        session_id="sid-claim", source_field="claim_dir", claim_live=True, claim_stage="apply"
    )
    fake_result = artifact_owner.ArtifactOwnerResult(
        artifact_path=str(f),
        owners=[
            artifact_owner.OwnerResolution(
                owner=fake_owner,
                result=reachability.ResolveResult(outcome="reachable", session_id="sid-claim", address="peer-9"),
            )
        ],
        file_error=None,
    )
    monkeypatch.setattr(artifact_owner, "resolve_artifact_owner", lambda *a, **k: fake_result)

    result = _session_artifact_owner({"artifact_path": str(f)})

    row = result["owners"][0]
    assert row["claim_live"] is True
    assert row["claim_stage"] == "apply"


def test_non_claim_dir_owner_reports_null_claim_fields(tmp_path, monkeypatch):
    f = tmp_path / "artifact.md"
    f.write_text("---\nclaimed_by: sid-a\n---\n\nbody\n", encoding="utf-8")

    monkeypatch.setattr(
        reachability,
        "resolve_address",
        lambda oid: reachability.ResolveResult(outcome="reachable", session_id="sid-a", address="peer-1"),
    )

    result = _session_artifact_owner({"artifact_path": str(f)})
    row = result["owners"][0]
    assert row["claim_live"] is None
    assert row["claim_stage"] is None
