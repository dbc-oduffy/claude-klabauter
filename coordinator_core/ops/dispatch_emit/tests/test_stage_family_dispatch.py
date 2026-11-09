"""Pins per-stage family dispatch: a family row hands to the runner, an unset one stays claude."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import stage_family
from coordinator_core.ops.dispatch_emit.ask_stage import stage
from coordinator_core.ops.dispatch_emit.ask_stage_blocks import AskStageError
from coordinator_core.ops.dispatch_emit.spine_read import InvalidFieldTypeError, read_spine
from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts

PLAN_REL = "docs/plans/2026-11-01-family-fixture.md"
SIZING_REL = "state/sizings/2026-11-01-xs-family.yaml"

_PLAN = """---
deliverable_id: dlv-family-abc123
---
# Family fixture

## Problem

Rows with and without a family.

## Tasks

```yaml plan-tasks
- id: C1
  title: first
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
{c1_extra}- id: C2
  title: second
  change_kind: code-edit
  surface: pkg/b.py
  writes:
    - pkg/b.py
```
"""


def _plan(c1_extra: str = "") -> str:
    return _PLAN.format(c1_extra=c1_extra)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    write_run_scripts(tmp_path, "r1", "x1run")
    return tmp_path


def _write_plan(repo, c1_extra=""):
    path = repo / PLAN_REL
    path.write_text(_plan(c1_extra), encoding="utf-8", newline="\n")
    return path


def test_family_row_dispatches_to_runner_with_family_in_brief(repo):
    path = _write_plan(repo, "  family: codex\n")
    rows = {r.id: r for r in read_spine(path, schema_preflight=False)}
    assert rows["C1"].agent_type == stage_family.RUNNER_AGENT
    assert "family: codex" in rows["C1"].body and "  - pkg/a.py" in rows["C1"].body
    assert rows["C2"].agent_type is None and "family:" not in rows["C2"].body


def test_staged_manifest_swaps_only_the_family_row(repo):
    _write_plan(repo, "  family: codex\n")
    m = stage(repo, run_id="r1", plan_rel=PLAN_REL)
    by_id = {r.id: r for r in m.rows}
    assert by_id["C1"].agent_type == "coordinator:family-stage-runner"
    assert by_id["C2"].agent_type == "coordinator:executor"
    assert "family: codex" in (repo / by_id["C1"].brief_path).read_text(encoding="utf-8")
    assert "family:" not in (repo / by_id["C2"].brief_path).read_text(encoding="utf-8")


def test_absent_and_explicit_claude_family_stay_claude(repo):
    _write_plan(repo, "  family: claude\n")
    m = stage(repo, run_id="r1", plan_rel=PLAN_REL)
    assert {r.agent_type for r in m.rows} == {"coordinator:executor"}


def test_unknown_family_is_a_parse_refusal(repo):
    path = _write_plan(repo, "  family: gpt\n")
    with pytest.raises(InvalidFieldTypeError, match=r"claude\|codex\|grok\|agy"):
        read_spine(path, schema_preflight=False)


def test_family_beside_explicit_agent_type_is_refused(repo):
    path = _write_plan(repo, "  family: codex\n  agent_type: coordinator:executor\n")
    with pytest.raises(InvalidFieldTypeError, match="keep one"):
        read_spine(path, schema_preflight=False)


def _sizing(**over):
    doc = {
        "intent": "Fix the thing",
        "exit_criterion": {"statement": "it works"},
        "deliverable_id": "dlv-xs-abc123",
    }
    doc.update(over)
    return doc


def test_sizing_family_routes_its_rows_to_the_runner(repo):
    (repo / SIZING_REL).write_text(yaml.safe_dump(_sizing(family="agy")), encoding="utf-8", newline="\n")
    m = stage(repo, run_id="x1run", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    assert [r.agent_type for r in m.rows] == ["coordinator:family-stage-runner"]
    assert "family: agy" in (repo / m.rows[0].brief_path).read_text(encoding="utf-8")


def test_sizing_without_family_stays_executor(repo):
    (repo / SIZING_REL).write_text(yaml.safe_dump(_sizing()), encoding="utf-8", newline="\n")
    m = stage(repo, run_id="x1run", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    assert [r.agent_type for r in m.rows] == ["coordinator:executor"]


def test_sizing_unknown_family_is_refused(repo):
    (repo / SIZING_REL).write_text(yaml.safe_dump(_sizing(family="gpt")), encoding="utf-8", newline="\n")
    with pytest.raises(AskStageError, match="claude"):
        stage(repo, run_id="x1run", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
