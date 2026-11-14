"""Pins that a plan row pinned to the default executor routes like an unpinned row under a sizing family."""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_stage
from coordinator_core.ops.dispatch_emit.ask_stage import stage
from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts

PLAN_REL = "docs/plans/2026-11-01-pin-fixture.md"
SIZING_REL = "state/sizings/2026-11-01-xs-pin.yaml"
RUNNER = "coordinator:family-stage-runner"
EXECUTOR = "coordinator:executor"
REVIEWER_AGENT_TYPE = "coordinator:test-runner"

_PLAN = """---
deliverable_id: dlv-family-abc123
---
# Pin fixture

## Problem

Rows with explicit and absent agent_type pins.

## Tasks

```yaml plan-tasks
- id: C1
  title: pinned default
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
  agent_type: coordinator:executor
- id: C2
  title: unpinned
  change_kind: code-edit
  surface: pkg/b.py
  writes:
    - pkg/b.py
- id: C3
  title: pinned other
  change_kind: code-edit
  surface: pkg/c.py
  writes:
    - pkg/c.py
  agent_type: {other}
```
"""


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    write_run_scripts(tmp_path, "r1", "x1run")
    return tmp_path


def _stage(repo, other=REVIEWER_AGENT_TYPE, **sizing_over):
    (repo / PLAN_REL).write_text(_PLAN.format(other=other), encoding="utf-8", newline="\n")
    sizing = {"intent": "Fix", "exit_criterion": {"statement": "ok"}, "deliverable_id": "dlv-xs-abc123"}
    sizing.update(sizing_over)
    (repo / SIZING_REL).write_text(yaml.safe_dump(sizing), encoding="utf-8", newline="\n")
    report: dict = {}
    m = stage(repo, run_id="r1", plan_rel=PLAN_REL, commit_sizing_rel=SIZING_REL, family_report=report)
    return {r.id: r for r in m.rows}, report


def test_default_executor_pin_takes_the_sizing_family_like_an_unpinned_row(repo):
    rows, report = _stage(repo, family="codex")
    assert rows["C1"].agent_type == RUNNER and rows["C2"].agent_type == RUNNER
    assert rows["C1"].route_source == "default"
    assert report["family_routes"] == {"C1": "codex", "C2": "codex"}
    assert "warnings" not in report


def test_sizing_family_row_carries_family_and_a_derived_claude_fallback(repo):
    rows, _ = _stage(repo, family="codex")
    for rid in ("C1", "C2"):
        row = rows[rid]
        assert row.family == "codex" and row.stage_role == "write"
        assert row.fallback_agent_type == EXECUTOR
        fallback = (repo / row.fallback_brief_path).read_text(encoding="utf-8")
        assert "family:" not in fallback.split("\n", 1)[0]
        assert (repo / row.brief_path).read_text(encoding="utf-8").startswith("family: codex")
    assert rows["C3"].family == "" and rows["C3"].fallback_brief_path == ""


def test_other_pin_keeps_its_pin_and_overlay_source(repo):
    rows, report = _stage(repo, family="codex")
    assert rows["C3"].agent_type == REVIEWER_AGENT_TYPE
    assert rows["C3"].route_source == "overlay"
    assert "C3" not in report["family_routes"]


def test_no_family_leaves_the_pin_an_overlay_on_claude(repo):
    rows, report = _stage(repo)
    assert rows["C1"].agent_type == EXECUTOR and rows["C1"].route_source == "overlay"
    assert report == {}


def test_family_reaching_no_row_warns_naming_the_family(repo):
    report = ask_stage._family_routing_report("grok", {"C1": "claude", "C3": "claude"})
    assert "family_routes" not in report
    assert len(report["warnings"]) == 1 and "'grok'" in report["warnings"][0]
    assert ask_stage._family_routing_report("grok", {"C1": "grok"}).get("warnings") is None
    assert ask_stage._family_routing_report("claude", {"C1": "claude"}) == {}


def test_receipt_names_the_family_the_stage_used(repo):
    from coordinator_core.ops.dispatch_emit import ask_stage_receipt

    _, report = _stage(repo, family="codex")
    ask_stage_receipt.write(repo, "d", {"path": "p", "sha256": "s", "bytes": 1}, family_report=report)
    body = json.loads((repo / ask_stage_receipt.RECEIPT_NAME).read_text())
    assert body["family_routes"] == {"C1": "codex", "C2": "codex"}


def test_every_row_pinned_elsewhere_stages_with_the_warning_not_a_refusal(repo):
    (repo / PLAN_REL).write_text(
        _PLAN.format(other=REVIEWER_AGENT_TYPE).replace("agent_type: coordinator:executor", "agent_type: " + REVIEWER_AGENT_TYPE)
        .replace("  title: unpinned", "  title: unpinned\n  agent_type: " + REVIEWER_AGENT_TYPE),
        encoding="utf-8",
        newline="\n",
    )
    sizing = {"intent": "Fix", "exit_criterion": {"statement": "ok"}, "deliverable_id": "dlv-xs-abc123", "family": "grok"}
    (repo / SIZING_REL).write_text(yaml.safe_dump(sizing), encoding="utf-8", newline="\n")
    report: dict = {}
    m = stage(repo, run_id="r1", plan_rel=PLAN_REL, commit_sizing_rel=SIZING_REL, family_report=report)
    assert {r.agent_type for r in m.rows} == {REVIEWER_AGENT_TYPE}
    assert "'grok'" in report["warnings"][0]
