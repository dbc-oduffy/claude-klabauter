"""Tests for coordinator_core.ops.baton_close_evidence_plans."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.baton_close_evidence_plans import (
    PLAN_IMPLEMENTED,
    PLAN_SPINE_DONE,
    plan_evidence,
)

BATON = "state/handoffs/2026-10-21-x.md"
OTHER = "state/handoffs/2026-10-21-y.md"


def _spine(*dispositions: str) -> str:
    rows = "".join(
        f"- id: C{i}\n  title: t\n  disposition: {d}\n" for i, d in enumerate(dispositions)
    )
    return f"## Tasks\n\n```yaml plan-tasks\n{rows}```\n"


def _plan(root: Path, name: str, header: str, body: str = "") -> None:
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / name).write_text(f"---\n{header}\n---\n\n{body}", encoding="utf-8")


def _kinds(result: dict, rel: str = BATON) -> list[str]:
    return [e["kind"] for e in result.get(rel, [])]


def test_implemented_via_pm_brief_inline_ref(tmp_path):
    _plan(tmp_path, "a.md", f'status: implemented\npm_brief: {{source: x, ref: "{BATON}"}}')
    result = plan_evidence(tmp_path, {BATON: {}, OTHER: {}})
    assert _kinds(result) == [PLAN_IMPLEMENTED]
    assert OTHER not in result
    ev = result[BATON][0]
    assert ev["reads"] == "terminal" and ev["strength"] == "strong"
    assert ev["proposes"] == "shipped" and ev["source"] == "docs/plans/a.md"


def test_implemented_via_pm_brief_block_ref(tmp_path):
    _plan(tmp_path, "a.md", f"status: implemented\npm_brief:\n  source: x\n  ref: {BATON}")
    assert _kinds(plan_evidence(tmp_path, {BATON: {}})) == [PLAN_IMPLEMENTED]


def test_join_by_sizing_object_with_backslash_normalised(tmp_path):
    _plan(tmp_path, "a.md", "status: implemented\nsizing_object: state/sizings/s.yaml")
    batons = {BATON: {"sizing_object": "state\\sizings\\s.yaml"}}
    assert _kinds(plan_evidence(tmp_path, batons)) == [PLAN_IMPLEMENTED]


def test_join_by_deliverable_id(tmp_path):
    _plan(tmp_path, "a.md", 'status: implemented\ndeliverable_id: "dlv-1"')
    batons = {BATON: {"deliverable_id": "dlv-1"}, OTHER: {"deliverable_id": "dlv-2"}}
    result = plan_evidence(tmp_path, batons)
    assert _kinds(result) == [PLAN_IMPLEMENTED]
    assert OTHER not in result


def test_null_deliverable_id_does_not_join(tmp_path):
    _plan(tmp_path, "a.md", "status: implemented\ndeliverable_id: null")
    assert plan_evidence(tmp_path, {BATON: {"deliverable_id": ""}}) == {}


def test_spine_done_weak(tmp_path):
    _plan(
        tmp_path,
        "a.md",
        f'status: reviewed\npm_brief: {{ref: "{BATON}"}}',
        _spine("coded", "delivered_elsewhere"),
    )
    result = plan_evidence(tmp_path, {BATON: {}})
    assert _kinds(result) == [PLAN_SPINE_DONE]
    assert result[BATON][0]["strength"] == "weak"


def test_one_open_row_yields_nothing(tmp_path):
    _plan(
        tmp_path,
        "a.md",
        f'status: reviewed\npm_brief: {{ref: "{BATON}"}}',
        _spine("coded", "open"),
    )
    assert plan_evidence(tmp_path, {BATON: {}}) == {}


def test_no_spine_yields_nothing(tmp_path):
    _plan(tmp_path, "a.md", f'status: reviewed\npm_brief: {{ref: "{BATON}"}}', "no tasks here\n")
    assert plan_evidence(tmp_path, {BATON: {}}) == {}


def test_unmatched_plan_contributes_nothing(tmp_path):
    _plan(tmp_path, "a.md", f'status: implemented\npm_brief: {{ref: "{OTHER}"}}')
    assert plan_evidence(tmp_path, {BATON: {}}) == {}


def test_review_sidecar_ignored(tmp_path):
    _plan(tmp_path, "a.review.md", f'status: implemented\npm_brief: {{ref: "{BATON}"}}')
    assert plan_evidence(tmp_path, {BATON: {}}) == {}


def test_missing_plans_dir(tmp_path):
    assert plan_evidence(tmp_path, {BATON: {}}) == {}


def test_multiple_join_edges_one_evidence_per_plan(tmp_path):
    _plan(
        tmp_path,
        "a.md",
        f'status: implemented\ndeliverable_id: "dlv-1"\npm_brief: {{ref: "{BATON}"}}',
    )
    result = plan_evidence(tmp_path, {BATON: {"deliverable_id": "dlv-1"}})
    assert len(result[BATON]) == 1
