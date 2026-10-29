"""
coordinator_core.ops.tests.test_deliverable_cascade_plan_linked -- `cascade_run`
admits a handoff of another deliverable that links the run's plan, only when
every plan it links is implemented.

Run (from repo root):
    python -m pytest coordinator_core/ops/tests/test_deliverable_cascade_plan_linked.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinator_core.ops.deliverable_cascade as cascade_mod
from coordinator_core.ops.tests.test_deliverable_cascade_kinds import _init_repo, _seed_handoff

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_DID = "dlv-run-plan-linked-0"
_PLAN = "docs/plans/2026-01-01-p.md"
_PLAN_Q = "docs/plans/2026-01-01-q.md"


def _plan(repo: Path, rel: str, plan_id: str, status: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\nplan_id: {plan_id}\nstatus: {status}\n---\n\n# Plan\n", encoding="utf-8")


def _link(path: Path, **extra) -> None:
    text = path.read_text(encoding="utf-8")
    add = "".join(f"{k}: {v}\n" for k, v in extra.items())
    path.write_text(text.replace("\n---\n\n# Handoff", f"\n{add}---\n\n# Handoff", 1), encoding="utf-8")


def _state(path: Path) -> str:
    return yaml.safe_load(path.read_text(encoding="utf-8").split("---")[1])["deployment_state"]


def _run(repo: Path, rel: str) -> dict:
    return cascade_mod.cascade_run(repo, _DID, [rel], "abc1234", repo_root=repo / ".git", plan_path=_PLAN)


def test_foreign_handoff_linked_to_shipped_plan_ships(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _plan(repo, _PLAN, "plan-p", "implemented")
    h = _seed_handoff(repo, "h.md", deliverable_id="dlv-other-000000")
    _link(h, governing_plan=_PLAN)
    result = _run(repo, "state/handoffs/h.md")
    assert [e["path"] for e in result["advanced"]] == [str(h)], result
    assert _state(h) == "shipped"


def test_foreign_handoff_linked_to_unimplemented_second_plan_is_skipped(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _plan(repo, _PLAN, "plan-p", "implemented")
    _plan(repo, _PLAN_Q, "plan-q", "in_progress")
    h = _seed_handoff(repo, "h.md", deliverable_id="dlv-other-000000")
    _link(h, plan_ids="[plan-p, plan-q]")
    result = _run(repo, "state/handoffs/h.md")
    assert result["candidates_matched"] == 0 and not result["advanced"], result
    assert _state(h) == "ready_to_fire"


def test_unlinked_foreign_handoff_is_skipped(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _plan(repo, _PLAN, "plan-p", "implemented")
    h = _seed_handoff(repo, "h.md", deliverable_id="dlv-other-000000")
    result = _run(repo, "state/handoffs/h.md")
    assert result["candidates_matched"] == 0 and not result["advanced"], result
    assert _state(h) == "ready_to_fire"
