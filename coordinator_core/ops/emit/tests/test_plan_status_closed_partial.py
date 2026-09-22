"""``closed_partial`` is a valid PlanStatus at emit time, not a malformed-quarantine trigger.

Why this file exists: ``closed_partial`` was added to
``coordinator_core/frontmatter/schemas/plan.schema.json`` (authoring-time validation) and to
``lifecycle_constants.PLAN_ARCHIVABLE_STATUS`` / ``PLAN_ORPHAN_TERMINAL_STATUS`` (terminal/
archivable classification), but ``sections/plans.py::_PLAN_STATUS_ENUM`` — the set ``_valid()``
gates cockpit emission on — was never updated to match. A plan that legitimately authors
``status: closed_partial`` (schema-conformant, per plan.schema.json) would silently quarantine
to ``malformed_records.plans`` and vanish from the cockpit snapshot instead of being emitted.

Spec backlink: state/bug-backlog/2026-09-20-closed-partial-is-half-landed-across-the-mirror-set.yaml
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections import plans as plans_section


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
        observed_at="2026-09-21T00:00:00Z",
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
def test_closed_partial_status_is_emitted_not_quarantined(mock_qr, tmp_path: Path) -> None:
    """A plan with ``status: closed_partial`` reaches ``records``, not ``malformed``."""
    ctx = _make_ctx(tmp_path)
    mock_qr.return_value = [
        _plan_rec("docs/plans/2026-09-01-foo.md", status="closed_partial")
    ]

    records, malformed = plans_section.collect(ctx)

    assert malformed == []
    assert len(records) == 1
    assert records[0]["status"] == "closed_partial"
