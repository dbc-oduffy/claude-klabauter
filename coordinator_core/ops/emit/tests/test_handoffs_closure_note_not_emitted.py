"""A `kind: closure-note` record is neither emitted nor malformed, and draws no write-time advisory."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coordinator_core.hooks import guard_handoff_summary_cap_on_write as guard
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections import handoffs as handoffs_section

_FM = {"title": "Closed", "created": "2026-10-16", "status": "closed", "kind": "closure-note"}


def _ctx(root: Path) -> EmitContext:
    (root / "state").mkdir(parents=True, exist_ok=True)
    return EmitContext(
        repo_root=root,
        coordinator_root=root,
        central_state_root=root / "state",
        git_branch="b",
        git_sha="0" * 40,
        git_sha_short="00000000",
        observed_at="2026-10-16T00:00:00Z",
        hostname="h",
        repo_name="o/r",
    )


def test_closure_note_without_deployment_state_is_skipped(tmp_path: Path):
    records = [{"path": "archive/handoffs/x.md", "frontmatter": dict(_FM)}]
    with patch.object(
        handoffs_section, "_query_records",
        side_effect=lambda ctx, t: records if t == "handoff" else [],
    ), patch.object(handoffs_section, "load_priority_ledger", return_value={}):
        emitted, malformed = handoffs_section.collect(_ctx(tmp_path))
    assert emitted == []
    assert malformed == []


def test_closure_note_through_guard_draws_no_missing_field_reason(tmp_path: Path):
    target = tmp_path / "archive" / "handoffs" / "x.md"
    target.parent.mkdir(parents=True)
    content = "---\n" + "\n".join(f"{k}: {v}" for k, v in _FM.items()) + "\n---\nbody\n"
    params = {"tool_name": "Write", "tool_input": {"file_path": str(target), "content": content}}
    assert guard._handler(params) == {}


def test_non_closure_note_without_deployment_state_still_flagged(tmp_path: Path):
    target = tmp_path / "archive" / "handoffs" / "y.md"
    target.parent.mkdir(parents=True)
    fm = {k: v for k, v in _FM.items() if k != "kind"}
    content = "---\n" + "\n".join(f"{k}: {v}" for k, v in fm.items()) + "\n---\nbody\n"
    params = {"tool_name": "Write", "tool_input": {"file_path": str(target), "content": content}}
    out = guard._handler(params)
    assert "deployment_state" in out["hookSpecificOutput"]["additionalContext"]
