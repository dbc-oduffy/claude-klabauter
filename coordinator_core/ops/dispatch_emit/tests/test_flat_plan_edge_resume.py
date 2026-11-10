"""A month-directory `plan:` edge resumes the flat plan, and the 763b4b sizing's edge names an existing plan file."""

from __future__ import annotations

from pathlib import Path

import yaml

from coordinator_core.ops.dispatch_emit.sizing_fire import flat_plan_edge, resumable_plan
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import _plan

FLAT = "docs/plans/2026-11-10-hnd-unimatrix-name-route-misdelive-92ee48.md"
MONTH = "docs/plans/2026-11/2026-11-10-hnd-unimatrix-name-route-misdelive-92ee48.md"
SIZING_763B4B = "state/sizings/2026-11/2026-11-10-unimatrix-name-route-misdelivery-763b4b.yaml"


def test_flat_plan_edge_drops_only_the_month_directory():
    assert flat_plan_edge(MONTH) == FLAT
    assert flat_plan_edge(FLAT) == FLAT


def test_month_edge_resumes_the_flat_plan(repo):  # noqa: F811
    _plan(repo, rel=FLAT)
    plan = resumable_plan({"plan": MONTH}, REL, repo, "m_plus")
    assert plan is not None and plan.plan_rel == FLAT


def test_month_edge_naming_a_real_file_is_kept(repo):  # noqa: F811
    _plan(repo, rel=FLAT)
    _plan(repo, rel=MONTH)
    plan = resumable_plan({"plan": MONTH}, REL, repo, "m_plus")
    assert plan is not None and plan.plan_rel == MONTH


def test_month_edge_with_no_flat_plan_stays_unresolved(repo):  # noqa: F811
    assert resumable_plan({"plan": MONTH}, REL, repo, "m_plus") is None


def test_763b4b_sizing_plan_edge_names_an_existing_plan_file():
    root = Path(__file__).resolve().parents[4]
    edge = yaml.safe_load((root / SIZING_763B4B).read_text(encoding="utf-8"))["plan"]
    assert edge == FLAT
    assert (root / edge).is_file()
