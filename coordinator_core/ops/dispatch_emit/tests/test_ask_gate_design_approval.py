"""dispatch.ask_gate record kind design_approval: halt, record, refire stages the planned rows."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import design_route
from coordinator_core.ops.dispatch_emit.ask_gate import _handler
from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, stage
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import repo as _bare_repo

BOARD_URL = "https://claude.ai/artifact/fixture-canvas-001"
PLAN_REL = "docs/plans/2026-10-16-canvas-fixture.md"
HARVEST = "docs/design/canvas-fixture/harvest/harvest.json"
PM_RULING = "Approved. Ship the canvas."
RULING_REF = "chat 2026-10-16 23:40"
RECORD = {"kind": "design_approval", "board_url": BOARD_URL, "pm_ruling": PM_RULING, "ruling_ref": RULING_REF}

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

BLOCK = {"route": "canvas", "authority": "PM", "board_url": None, "approval": None}


@pytest.fixture
def repo(_bare_repo):
    (_bare_repo / ".git").mkdir()
    return _bare_repo


def _planned(repo, block=BLOCK, harvested=True):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch", name="canvas-fixture", design_route=block, plan=PLAN_REL)
    (repo / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (repo / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    if harvested:
        (repo / HARVEST).parent.mkdir(parents=True, exist_ok=True)
        (repo / HARVEST).write_text("{}", encoding="utf-8")


def _call(repo, record=None, **extra):  # noqa: F811
    params = {"sizing_path": REL, "writes": ["pkg/a.py"], **extra}
    if record is not None:
        params["record"] = record
    return _handler(params, repo)


def _block(repo):  # noqa: F811
    return yaml.safe_load((repo / REL).read_text(encoding="utf-8"))["design_route"]


def test_halt_record_refire_stages_the_planned_rows(repo):  # noqa: F811
    _planned(repo)
    with pytest.raises(AskStageError) as err:
        stage(repo, run_id="canvas-a", plan_rel=PLAN_REL)
    assert str(err.value).startswith(design_route.STAGE_REFUSAL_PREFIX)

    reply = _call(repo, record=RECORD)
    assert reply["recorded"] == {"ok": True, "pm_ruling": PM_RULING, "ruling_ref": RULING_REF}
    block = _block(repo)
    assert block["board_url"] == BOARD_URL
    assert block["approval"]["by"] == "pm" and block["approval"]["at"].endswith("Z")
    assert PM_RULING not in yaml.safe_dump(block) and "pm_ruling" not in block

    refire = _call(repo, run_id="canvas-b")
    assert refire["halt"] is None and refire["resume_plan"] == PLAN_REL
    assert refire["resume_rows"] == ["C1"]
    assert [r.id for r in stage(repo, run_id="canvas-b", plan_rel=PLAN_REL).rows] == ["C1"]


def test_a_bare_string_block_is_upgraded_to_the_full_block(repo):  # noqa: F811
    _planned(repo, block="canvas")
    assert _call(repo, record=RECORD)["recorded"]["ok"] is True
    block = _block(repo)
    assert set(block) == {"route", "authority", "board_url", "approval"} and block["route"] == "canvas"


def test_next_action_names_the_record_shape_the_gate_accepts(repo):  # noqa: F811
    text = design_route.next_action(REL) + design_route.approval_touchpoint(REL)
    assert f"kind: {design_route.RECORD_KIND}" in text
    for name in ("board_url", "pm_ruling", "ruling_ref"):
        assert name in text
    from coordinator_core.ops.dispatch_emit.ask_gate import _record_refusal

    assert _record_refusal(RECORD) is None
    assert RECORD["kind"] == design_route.RECORD_KIND


@pytest.mark.parametrize("name", ["pm_ruling", "ruling_ref", "board_url"])
@pytest.mark.parametrize("empty", [True, False])
def test_missing_or_empty_testimony_is_refused_and_writes_nothing(repo, name, empty):  # noqa: F811
    _planned(repo)
    before = (repo / REL).read_bytes()
    record = {**RECORD, name: " "} if empty else {k: v for k, v in RECORD.items() if k != name}
    assert name in _call(repo, record=record)["error"]
    assert (repo / REL).read_bytes() == before


def test_a_record_carrying_apm_ruling_is_refused(repo):  # noqa: F811
    _planned(repo)
    before = (repo / REL).read_bytes()
    assert "apm_ruling" in _call(repo, record={**RECORD, "apm_ruling": "approved"})["error"]
    assert (repo / REL).read_bytes() == before


def _refused(repo, record=RECORD):  # noqa: F811
    before = (repo / REL).read_bytes()
    reply = _call(repo, record=record)
    assert reply["recorded"]["ok"] is False and reply["recorded"]["error"]
    assert "pm_ruling" not in reply["recorded"]
    assert (repo / REL).read_bytes() == before
    return reply["recorded"]["error"]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/artifact/x",
        f"see {BOARD_URL} for the board",
        f"{BOARD_URL} trailing",
        "claude.ai/artifact/fixture-canvas-001",
    ],
)
def test_a_non_artifact_url_is_refused(repo, url):  # noqa: F811
    _planned(repo)
    assert "artifact" in _refused(repo, {**RECORD, "board_url": url})


@pytest.mark.parametrize("route", ["reviewer", "none"])
def test_a_non_canvas_route_is_refused(repo, route):  # noqa: F811
    _planned(repo, block={**BLOCK, "route": route})
    assert "canvas" in _refused(repo)


def test_an_absent_block_is_refused(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch", name="canvas-fixture")
    assert "design_route" in _refused(repo)


def test_an_already_approved_canvas_is_refused(repo):  # noqa: F811
    _planned(repo)
    assert _call(repo, record=RECORD)["recorded"]["ok"] is True
    assert "already approved" in _refused(repo)


def test_a_missing_harvest_is_refused(repo):  # noqa: F811
    _planned(repo, harvested=False)
    assert "harvest.json" in _refused(repo)
