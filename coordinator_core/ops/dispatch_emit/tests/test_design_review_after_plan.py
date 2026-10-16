"""The design-review halt sits after plan and before stage: the gate arms, ask_stage refuses."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, stage
from coordinator_core.ops.dispatch_emit.op import _gate_sizing_at_emit
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401

BOARD_URL = "https://claude.ai/artifact/fixture-canvas-001"
PLAN_REL = "docs/plans/2026-10-16-canvas-fixture.md"
PREFIX = "design-review:"
APPROVAL = {"by": "pm", "at": "2026-10-16T23:00:00Z"}

PLAN = f"""---
deliverable_id: dlv-fixture-abc123
sizing_object: {REL}
---
# Canvas fixture plan

## Tasks

```yaml plan-tasks
- id: C1
  title: first
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
```
"""


def _route(route, approval=None):
    return {
        "route": route,
        "authority": "PM authorizes the Claude Design canvas",
        "board_url": BOARD_URL if approval is not None else None,
        "approval": approval,
    }


def _planned(repo, tshirt="S", **over):  # noqa: F811
    """A sizing plus a filled plan whose frontmatter names it."""
    _put(repo, tshirt=tshirt, route="spec-dispatch", name="canvas-fixture", **over)
    (repo / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (repo / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")


def _files(repo):  # noqa: F811
    return sorted(
        p.relative_to(repo).as_posix()
        for root in (RUN_DIR_ROOT, RECORD_DIR_ROOT)
        if (repo / root).is_dir()
        for p in (repo / root).rglob("*")
    )


def test_unapproved_canvas_gates_to_an_arm_carrying_the_payload(repo):  # noqa: F811
    _planned(repo, design_route=_route("canvas"))
    verdict = gate(repo, REL, writes=["pkg/a.py"])
    assert verdict.halt is None and verdict.arm is not None
    payload = verdict.to_json()["design_route"]
    assert payload["approved"] is False
    assert payload["board"]["url"] is None


def test_emit_time_gate_does_not_refuse_an_unapproved_canvas(repo):  # noqa: F811
    _planned(repo, design_route=_route("canvas"))
    _gate_sizing_at_emit(repo, REL, ["pkg/a.py"])


def test_stage_refuses_an_unapproved_canvas_plan_and_writes_nothing(repo):  # noqa: F811
    _planned(repo, design_route=_route("canvas"))
    before = _files(repo)
    with pytest.raises(AskStageError) as err:
        stage(repo, run_id="canvas1", plan_rel=PLAN_REL)
    assert str(err.value).startswith(PREFIX)
    assert _files(repo) == before


def test_stage_refuses_an_unapproved_xs_canvas_sizing_and_writes_nothing(repo):  # noqa: F811
    _put(repo, tshirt="XS", route="dispatch", name="canvas-fixture", design_route=_route("canvas"))
    before = _files(repo)
    with pytest.raises(AskStageError) as err:
        stage(repo, run_id="canvas2", sizing_rel=REL, writes=["pkg/a.py"])
    assert str(err.value).startswith(PREFIX)
    assert _files(repo) == before


def test_stage_stages_an_approved_canvas_plan(repo):  # noqa: F811
    _planned(repo, design_route=_route("canvas", APPROVAL))
    manifest = stage(repo, run_id="canvas3", plan_rel=PLAN_REL)
    assert [r.id for r in manifest.rows] == ["C1"]


@pytest.mark.parametrize("route", ["reviewer", "none"])
def test_reviewer_and_none_gate_and_stage_without_a_design_refusal(repo, route):  # noqa: F811
    _planned(repo, design_route=_route(route))
    verdict = gate(repo, REL, writes=["pkg/a.py"])
    assert verdict.halt is None and verdict.arm is not None
    assert verdict.to_json()["design_route"]["route"] == route
    _gate_sizing_at_emit(repo, REL, ["pkg/a.py"])
    assert stage(repo, run_id=f"{route}1", plan_rel=PLAN_REL).rows


def test_an_absent_block_emits_no_key_and_stages(repo):  # noqa: F811
    _planned(repo)
    assert "design_route" not in yaml.safe_load((repo / REL).read_text(encoding="utf-8"))
    verdict = gate(repo, REL, writes=["pkg/a.py"])
    assert verdict.halt is None and verdict.arm is not None
    assert "design_route" not in verdict.to_json()
    _gate_sizing_at_emit(repo, REL, ["pkg/a.py"])
    assert stage(repo, run_id="absent1", plan_rel=PLAN_REL).rows
