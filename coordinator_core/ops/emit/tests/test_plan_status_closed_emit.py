"""``closed`` is a valid PlanStatus at emit time, not a malformed-quarantine trigger."""

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
        observed_at="2026-10-22T00:00:00Z",
        hostname="test-host",
        repo_name="test-org/test-repo",
    )


def _plan_rec(path: str, **overrides) -> dict:
    fm = {
        "title": "Test Plan",
        "created": "2026-10-01",
        "author": "test-em",
        "status": "draft",
    }
    fm.update(overrides)
    return {"path": path, "frontmatter": fm}


@patch("coordinator_core.ops.emit.sections.plans._query_plan_records")
def test_closed_status_is_emitted_not_quarantined(mock_qr, tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    mock_qr.return_value = [_plan_rec("docs/plans/2026-10-01-foo.md", status="closed")]

    records, malformed = plans_section.collect(ctx)

    assert malformed == []
    assert len(records) == 1
    assert records[0]["status"] == "closed"
