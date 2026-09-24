"""``blocked`` is a valid, non-terminal PlanStatus that buckets liveness BLOCKED.

Mirrors ``test_plan_status_closed_partial.py``'s shape for the new member added by
docs/plans/2026-09-23-plan-blocked-state.md (P181-C2). ``blocked`` is emitted at the cockpit
layer (not quarantined to ``malformed_records.plans``), appears in ``typing.get_args(PlanStatus)``,
``plans._PLAN_STATUS_ENUM`` and the plan keys of ``LIVENESS_MAPPING``, and
``records_query.liveness`` buckets a blocked plan BLOCKED while a blocked roadmap stays LIVE
(``test_roadmap_blocked_status_is_live_not_blocked`` — untouched by this row).

Spec backlink: docs/plans/2026-09-23-plan-blocked-state.md (C2)
"""

from __future__ import annotations

import typing
from pathlib import Path
from unittest.mock import patch

from coordinator_core.contract.cockpit_schema.entities.plan_summary import PlanStatus
from coordinator_core.ops import records_query
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections import plans as plans_section
from coordinator_core.ops.emit_artifact_shape_contract import LIVENESS_MAPPING


def _make_ctx(tmp_path: Path) -> EmitContext:
    central = tmp_path / "state"
    central.mkdir(parents=True, exist_ok=True)
    return EmitContext(
        repo_root=tmp_path,
        coordinator_root=tmp_path,
        central_state_root=central,
        git_branch="test-branch",
        git_sha="0" * 40,
        git_sha_short="00000000",
        observed_at="2026-09-23T00:00:00Z",
        hostname="test-host",
        repo_name="test-org/test-repo",
    )


def _plan_rec(path: str, **overrides) -> dict:
    fm = {
        "title": "Test Plan",
        "created": "2026-09-01",
        "author": "test-em",
        "status": "draft",
    }
    fm.update(overrides)
    return {"path": path, "frontmatter": fm}


@patch("coordinator_core.ops.emit.sections.plans._query_plan_records")
def test_blocked_status_is_emitted_not_quarantined(mock_qr, tmp_path: Path) -> None:
    """A plan with ``status: blocked`` reaches ``records``, not ``malformed``."""
    ctx = _make_ctx(tmp_path)
    mock_qr.return_value = [_plan_rec("docs/plans/2026-09-01-foo.md", status="blocked")]

    records, malformed = plans_section.collect(ctx)

    assert malformed == []
    assert len(records) == 1
    assert records[0]["status"] == "blocked"


def test_blocked_in_plan_status_literal() -> None:
    assert "blocked" in typing.get_args(PlanStatus)


def test_blocked_in_plans_section_enum() -> None:
    assert "blocked" in plans_section._PLAN_STATUS_ENUM


def test_blocked_in_liveness_mapping_plan_keys() -> None:
    plan_mapping = LIVENESS_MAPPING["types"]["plan"]["mapping"]
    assert "blocked" in plan_mapping


def test_liveness_plan_blocked_is_blocked() -> None:
    assert records_query.liveness({"status": "blocked"}, "plan") == "BLOCKED"


def test_liveness_roadmap_blocked_stays_live() -> None:
    assert records_query.liveness({"status": "blocked"}, "roadmap") == "LIVE"
