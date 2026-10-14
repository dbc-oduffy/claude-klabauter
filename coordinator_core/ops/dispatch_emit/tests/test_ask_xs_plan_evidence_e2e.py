"""An XS ask naming a plan body for evidence stages through ask_stage.stage: the mixed row routes to the sidecar."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit.ask_stage import stage
from coordinator_core.ops.dispatch_emit.emit import MixedAgentTypeRowError

SIZING_REL = "state/sizings/2026-10-14-xs-evidence.yaml"
PLAN_BODY = "docs/plans/2026-10-14-p.md"
PLAN_SIDECAR = "docs/plans/2026-10-14-p.evidence.yaml"
PROBLEM_BODY = "docs/problems/2026-10-14-q.md"


@pytest.fixture
def repo(tmp_path):
    for d in ("docs/plans", "docs/problems", "state/sizings", "src"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / PLAN_BODY).write_text("# P\n", encoding="utf-8", newline="\n")
    (tmp_path / PROBLEM_BODY).write_text("# Q\n", encoding="utf-8", newline="\n")
    doc = {
        "intent": "Fix x and record evidence on the plan",
        "exit_criterion": {"statement": "x works"},
        "deliverable_id": "dlv-xs-evidence-abc123",
    }
    (tmp_path / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    return tmp_path


def test_plan_body_beside_code_routes_to_the_evidence_sidecar(repo):
    m = stage(repo, run_id="xe1", sizing_rel=SIZING_REL, writes=["src/x.py", PLAN_BODY])
    (row,) = m.rows
    assert row.id == "X1"
    assert row.agent_type == "coordinator:executor"
    assert "src/x.py" in row.writes
    assert PLAN_SIDECAR in row.writes
    assert PLAN_BODY not in row.writes
    brief = (repo / row.brief_path).read_text(encoding="utf-8")
    assert "evidence-append" in brief
    assert PLAN_SIDECAR in brief


def test_problems_body_beside_code_still_refuses(repo):
    with pytest.raises(MixedAgentTypeRowError):
        stage(repo, run_id="xe2", sizing_rel=SIZING_REL, writes=["src/x.py", PROBLEM_BODY])


def test_plan_body_alone_is_left_as_declared(repo):
    m = stage(repo, run_id="xe3", sizing_rel=SIZING_REL, writes=[PLAN_BODY])
    (row,) = m.rows
    assert list(row.writes) == [PLAN_BODY]
