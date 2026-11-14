"""ask_stage redirects a sizing_path whose sizing carries a plan edge to an existing plan; it stages that plan with the sizing as commit sizing."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, _handler, stage
from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan

SIZING_REL = "state/sizings/2026-10-16-fixture.yaml"


def _sizing(repo, **extra):
    doc = {
        "intent": "Fix the thing",
        "exit_criterion": {"statement": "it works"},
        "estimate": {"tshirt": "S"},
        "plan": PLAN_REL,
        **extra,
    }
    doc = {k: v for k, v in doc.items() if v is not None}
    (repo / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (repo / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


@pytest.mark.parametrize("tshirt", ["S", "XS"])
def test_sizing_path_with_plan_edge_to_existing_plan_stages_the_plan(tmp_path, tshirt):
    _sizing(tmp_path, estimate={"tshirt": tshirt})
    _plan(tmp_path)
    write_run_scripts(tmp_path, "r1")
    m = stage(tmp_path, run_id="r1", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    assert [r.id for r in m.rows] == ["A1"]


def test_redirect_matches_the_explicit_plan_path_stage(tmp_path):
    _sizing(tmp_path)
    _plan(tmp_path)
    write_run_scripts(tmp_path, "r1", "r2")
    via_sizing = stage(tmp_path, run_id="r1", sizing_rel=SIZING_REL)
    via_plan = stage(tmp_path, run_id="r2", plan_rel=PLAN_REL, commit_sizing_rel=SIZING_REL)
    assert [r.id for r in via_sizing.rows] == [r.id for r in via_plan.rows]


def test_handler_replies_a_manifest_handle_for_a_planned_sizing(tmp_path):
    _sizing(tmp_path)
    _plan(tmp_path)
    write_run_scripts(tmp_path, "r1")
    (tmp_path / ".git").mkdir()
    reply = _handler({"run_id": "r1", "sizing_path": SIZING_REL, "writes": []}, repo_root=tmp_path)
    assert "error" not in reply and reply["manifest_handle"]


def test_plan_edge_to_a_missing_plan_is_refused_as_dangling_naming_the_file(tmp_path):
    _sizing(tmp_path)
    with pytest.raises(AskStageError, match="^PLAN-EDGE-DANGLING: .*docs/plans/2026-10-01-gate.md"):
        stage(tmp_path, run_id="r1", sizing_rel=SIZING_REL, writes=["pkg/a.py"])


def _move_plan(repo, dest):
    src = repo / PLAN_REL
    (repo / dest).parent.mkdir(parents=True, exist_ok=True)
    src.rename(repo / dest)


def test_sharded_edge_with_a_flat_plan_stages_the_plan(tmp_path):
    _sizing(tmp_path, plan="docs/plans/2026-10/2026-10-01-gate.md")
    _plan(tmp_path)
    write_run_scripts(tmp_path, "r1")
    m = stage(tmp_path, run_id="r1", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    assert [r.id for r in m.rows] == ["A1"]


def test_flat_edge_with_a_sharded_plan_stages_the_plan(tmp_path):
    _sizing(tmp_path)
    _plan(tmp_path)
    _move_plan(tmp_path, "docs/plans/2026-10/2026-10-01-gate.md")
    write_run_scripts(tmp_path, "r1")
    m = stage(tmp_path, run_id="r1", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    assert [r.id for r in m.rows] == ["A1"]


def test_sharded_edge_with_a_sharded_plan_stages_the_plan(tmp_path):
    _sizing(tmp_path, plan="docs/plans/2026-10/2026-10-01-gate.md")
    _plan(tmp_path)
    _move_plan(tmp_path, "docs/plans/2026-10/2026-10-01-gate.md")
    write_run_scripts(tmp_path, "r1")
    m = stage(tmp_path, run_id="r1", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    assert [r.id for r in m.rows] == ["A1"]


def test_plan_edge_escaping_the_repo_is_still_refused(tmp_path):
    _sizing(tmp_path, plan="../outside.md")
    (tmp_path.parent / "outside.md").write_text("x", encoding="utf-8")
    with pytest.raises(AskStageError, match="^XS-MINT-MISROUTED: "):
        stage(tmp_path, run_id="r1", sizing_rel=SIZING_REL, writes=["pkg/a.py"])


def test_plan_less_xs_sizing_still_mints_x1(tmp_path):
    _sizing(tmp_path, plan=None, estimate={"tshirt": "XS"})
    write_run_scripts(tmp_path, "r1")
    m = stage(tmp_path, run_id="r1", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    assert [r.id for r in m.rows] == ["X1"]


def test_plan_less_s_sizing_is_still_refused(tmp_path):
    _sizing(tmp_path, plan=None)
    with pytest.raises(AskStageError, match="^XS-MINT-MISROUTED: "):
        stage(tmp_path, run_id="r1", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
