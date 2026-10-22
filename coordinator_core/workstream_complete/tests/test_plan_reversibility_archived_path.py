"""The plan-reversibility-eligibility directive names the plan's current path."""

from __future__ import annotations

from coordinator_core.workstream_complete.directives_completion import (
    build_plan_reversibility_eligibility_directive as build,
)

REL = "docs/plans/2026-10-22-x.md"


def _positional(directive):
    return directive["args"][0]


def test_archived_plan_path_is_resolved(tmp_path):
    arch = tmp_path / "archive" / "specs" / "2026-10"
    arch.mkdir(parents=True)
    (arch / "2026-10-22-x.md").write_text("x", encoding="utf-8")
    d = build(plan_path=REL, repo_root=tmp_path)
    assert _positional(d) == "archive/specs/2026-10/2026-10-22-x.md"


def test_absolute_plan_path_resolves_to_absolute_archived_path(tmp_path):
    arch = tmp_path / "archive" / "plans" / "2026-10"
    arch.mkdir(parents=True)
    (arch / "2026-10-22-x.md").write_text("x", encoding="utf-8")
    d = build(plan_path=str(tmp_path / REL), repo_root=tmp_path)
    assert _positional(d) == str(arch / "2026-10-22-x.md")


def test_live_plan_path_unchanged(tmp_path):
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / REL).write_text("x", encoding="utf-8")
    arch = tmp_path / "archive" / "specs" / "2026-10"
    arch.mkdir(parents=True)
    (arch / "2026-10-22-x.md").write_text("x", encoding="utf-8")
    assert _positional(build(plan_path=REL, repo_root=tmp_path)) == REL


def test_missing_everywhere_unchanged(tmp_path):
    assert _positional(build(plan_path=REL, repo_root=tmp_path)) == REL


def test_falsy_plan_path_returns_none(tmp_path):
    assert build(plan_path="", repo_root=tmp_path) is None
