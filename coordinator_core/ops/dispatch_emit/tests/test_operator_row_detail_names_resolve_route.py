"""An operator row's exclusion detail names the release route that works (verb resolve), not em-discharge."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.spine_read import read_spine
from coordinator_core.ops.plan_tasks_mutate import _em_discharge

_BODY = (
    "- id: O1\n  title: t\n  surface: s\n  performer: em\n"
    "  execution_mode: operator\n  writes:\n    - a.py\n"
)


def _plan(tmp_path):
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    path = plans / "2026-10-16-own.md"
    path.write_text(
        "# p\n\n## Tasks\n\n```yaml plan-tasks\n" + _BODY + "```\n", encoding="utf-8"
    )
    return path


def test_operator_exclusion_detail_names_resolve_coded_route(tmp_path):
    exclusions: list = []
    read_spine(_plan(tmp_path), exclusions)
    (entry,) = [e for e in exclusions if e["id"] == "O1"]
    assert entry["reason"] == "operator"
    detail = entry["detail"]
    assert "verb resolve" in detail
    assert "disposition coded" in detail
    assert "disposition_ref" in detail
    assert "em-discharge" not in detail
    assert "\n" not in detail


def test_em_discharge_still_refuses_the_operator_row(tmp_path):
    path = _plan(tmp_path)
    out = _em_discharge(str(path), "O1", "done", tmp_path, tmp_path)
    assert "execution_mode: operator" in str(out)
