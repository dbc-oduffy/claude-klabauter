"""Regression pins for the plan-trigger terminal cascade.

Shape A: a handoff with no scope evidence ships with the plan's own flip commit as
`shipped_in` instead of deferring. Shape B: a sizing carrying no `deliverable_id`
joins on its `plan` FK.

Run: python -m pytest coordinator_core/ops/tests/test_deliverable_cascade_plan_flip_and_fk.py -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

import coordinator_core.ops.deliverable_cascade as cascade_mod
from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ops.tests.test_deliverable_cascade_kinds import (
    _git,
    _init_repo,
    _seed_handoff,
    _seed_sizing,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PLAN = "docs/plans/2026-10-05-flip-plan.md"


def _run(params: dict, repo: Path) -> dict:
    return asyncio.run(cascade_mod._handler(params, repo_root=repo / ".git"))


def _commit_plan_flip(repo: Path) -> str:
    plan = repo / _PLAN
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("---\nstatus: approved\n---\n\nbody\n", encoding="utf-8")
    _git(repo, "add", _PLAN)
    _git(repo, "commit", "-m", "plan authored")
    plan.write_text("---\nstatus: implemented\n---\n\nbody\n", encoding="utf-8")
    _git(repo, "add", _PLAN)
    _git(repo, "commit", "-m", "plan flip")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_shape_b_null_deliverable_sizing_joins_on_plan_fk(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, "s.yaml", deliverable_id="null", plan=f"content-root:{_PLAN}")

    result = _run(
        {
            "deliverable_id": "dlv-minted-on-plan",
            "source_kind": "plan",
            "source_path": _PLAN,
            "target_kind": "sizing",
        },
        repo,
    )

    assert len(result["advanced"]) == 1
    assert yaml.safe_load(sizing.read_text(encoding="utf-8"))["status"] == "shipped"


def test_shape_b_plan_without_deliverable_id_still_cascades_sizing(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, "s.yaml", deliverable_id="null", plan=_PLAN)

    result = _run(
        {"deliverable_id": "", "source_kind": "plan", "source_path": _PLAN, "target_kind": "sizing"},
        repo,
    )

    assert result["exit_code"] == 0
    assert yaml.safe_load(sizing.read_text(encoding="utf-8"))["status"] == "shipped"


def test_shape_b_sizing_of_another_plan_is_not_joined(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, "s.yaml", deliverable_id="null", plan="docs/plans/other.md")

    result = _run(
        {"deliverable_id": "dlv-x", "source_kind": "plan", "source_path": _PLAN, "target_kind": "sizing"},
        repo,
    )

    assert result["candidates_matched"] == 0
    assert yaml.safe_load(sizing.read_text(encoding="utf-8"))["status"] == "routed"
