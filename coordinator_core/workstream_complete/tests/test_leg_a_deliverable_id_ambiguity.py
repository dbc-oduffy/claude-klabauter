"""Regression for leg A's zero-candidate vs multi-candidate `deliverable_id`
join: both cases previously collapsed into the identical "does not resolve
to exactly one" `not-applicable` verdict, silently treating a real
collision the same as nothing-to-look-at.

Spec backlink: cross-repo/archive/2026-08-08-doe-claude-em-leg-a-correction-
our-premise-was-wrong-keep-the-verdict-fix.md (ask 1).

Negative-spec: a `deliverable_id` absent entirely from frontmatter is the
genuine nothing-to-look-at case and MUST still resolve `not-applicable` --
this test pins that this member's fix does not widen the `indeterminate`
verdict to that case too.
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.workstream_complete import (
    _evaluate_session_handoff_leg_a,
    _resolve_session_handoff_plan_by_deliverable_id,
)


def _write_plan(plans_dir: Path, name: str, deliverable_id: str, status: str = "open") -> None:
    plans_dir.mkdir(parents=True, exist_ok=True)
    (plans_dir / name).write_text(
        f"---\ndeliverable_id: {deliverable_id}\nstatus: {status}\n---\n\nbody\n",
        encoding="utf-8",
    )


def test_zero_candidates_is_indeterminate(tmp_path: Path) -> None:
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    result = _evaluate_session_handoff_leg_a(tmp_path, {"deliverable_id": "dlv-nowhere"})
    assert result["verdict"] == "indeterminate"
    assert "zero candidates" in result["detail"]


def test_multi_candidate_is_indeterminate_and_names_both(tmp_path: Path) -> None:
    plans_dir = tmp_path / "docs" / "plans"
    _write_plan(plans_dir, "a.md", "dlv-dup")
    _write_plan(plans_dir, "b.md", "dlv-dup")
    result = _evaluate_session_handoff_leg_a(tmp_path, {"deliverable_id": "dlv-dup"})
    assert result["verdict"] == "indeterminate"
    assert "a.md" in result["detail"]
    assert "b.md" in result["detail"]


def test_no_deliverable_id_at_all_stays_not_applicable(tmp_path: Path) -> None:
    result = _evaluate_session_handoff_leg_a(tmp_path, {})
    assert result["verdict"] == "not-applicable"


def test_single_match_still_resolves_open_verdict(tmp_path: Path) -> None:
    plans_dir = tmp_path / "docs" / "plans"
    _write_plan(plans_dir, "a.md", "dlv-solo", status="reviewed")
    result = _evaluate_session_handoff_leg_a(tmp_path, {"deliverable_id": "dlv-solo"})
    assert result["verdict"] == "open"


def test_resolver_returns_list_of_matches(tmp_path: Path) -> None:
    plans_dir = tmp_path / "docs" / "plans"
    _write_plan(plans_dir, "a.md", "dlv-dup")
    _write_plan(plans_dir, "b.md", "dlv-dup")
    matches = _resolve_session_handoff_plan_by_deliverable_id(tmp_path, "dlv-dup")
    assert len(matches) == 2
