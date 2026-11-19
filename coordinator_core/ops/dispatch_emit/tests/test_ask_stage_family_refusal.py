"""A sizing's own non-claude `family` that no row binds refuses at stage naming it; an inherited family warns."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_stage
from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, stage
from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts

PLAN_REL = "docs/plans/2026-11-19-family-refusal-fixture.md"
SIZING_REL = "state/sizings/2026-11-19-xs-family-refusal.yaml"
OTHER_AGENT = "coordinator:test-runner"

_PLAN = """---
deliverable_id: dlv-family-abc123
---
# Family refusal fixture

## Problem

One write row, pinned as the test sets it.

## Tasks

```yaml plan-tasks
- id: C1
  title: only row
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
{pin}```
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    write_run_scripts(tmp_path, "r1", "x1run")
    return tmp_path


def _stage(repo: Path, pinned_elsewhere: bool, family_report: dict | None, **sizing_over):
    pin = f"  agent_type: {OTHER_AGENT}\n" if pinned_elsewhere else ""
    (repo / PLAN_REL).write_text(_PLAN.format(pin=pin), encoding="utf-8", newline="\n")
    sizing = {"intent": "Fix", "exit_criterion": {"statement": "ok"}, "deliverable_id": "dlv-xs-abc123"}
    sizing.update(sizing_over)
    (repo / SIZING_REL).write_text(yaml.safe_dump(sizing), encoding="utf-8", newline="\n")
    return stage(repo, run_id="r1", plan_rel=PLAN_REL, commit_sizing_rel=SIZING_REL, family_report=family_report)


def test_own_family_bound_by_no_write_row_refuses_naming_it(repo) -> None:
    with pytest.raises(AskStageError, match="'grok'.*reached no row"):
        _stage(repo, True, {}, family="grok")


def test_refusal_holds_when_the_caller_passes_no_family_report(repo) -> None:
    with pytest.raises(AskStageError, match="'agy'"):
        _stage(repo, True, None, family="agy")


def test_own_family_bound_by_a_write_row_stages(repo) -> None:
    report: dict = {}
    manifest = _stage(repo, False, report, family="codex")
    assert report["family_routes"] == {"C1": "codex"} and manifest.rows


def test_claude_family_and_absent_family_never_refuse(repo) -> None:
    _stage(repo, True, {}, family="claude")
    _stage(repo, True, {})


def test_inherited_family_keeps_the_warning_not_a_refusal() -> None:
    assert ask_stage._unbound_family_refusal("s.yaml", "grok", {"C1": "claude"}, inherited=True) is None
    assert ask_stage._unbound_family_refusal("s.yaml", "grok", {"C1": "claude"}, inherited=False) is not None
    assert ask_stage._unbound_family_refusal("s.yaml", "grok", {"C1": "grok"}, inherited=False) is None
