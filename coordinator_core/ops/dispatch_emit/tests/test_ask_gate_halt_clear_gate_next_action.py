"""A zero-row stage refusal on an uncleared external_gate names the clear-gate call before the refire."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_stage import _handler

PLAN_REL = "docs/plans/2026-10-03-gated-only.md"
_SIZING = "state/sizings/x.yaml"

_PLAN = """---
title: fixture
---
# Fixture

## Tasks

```yaml plan-tasks
- id: G1
  title: gated
  change_kind: code-edit
  surface: pkg/g.py
  writes:
    - pkg/g.py
  external_gate:
    - owner_repo: other-repo
      requires: landed-work
```
"""


def _refuse(tmp_path, plan: str) -> dict:
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(plan, encoding="utf-8", newline="\n")
    return _handler({"run_id": "r1", "plan_path": PLAN_REL}, repo_root=tmp_path)


def test_gated_only_refusal_carries_the_gated_rows_and_plan(tmp_path):
    reply = _refuse(tmp_path, _PLAN)
    assert "zero dispatchable rows" in reply["error"]
    assert reply["gated_rows"] == [{"id": "G1", "owner_repo": "other-repo"}]
    assert reply["owed_plan"] == PLAN_REL


def test_refusal_without_gated_rows_carries_none(tmp_path):
    plan = _PLAN.replace("  external_gate:\n    - owner_repo: other-repo\n      requires: landed-work\n", "  disposition: coded\n")
    reply = _refuse(tmp_path, plan)
    assert "error" in reply
    assert "gated_rows" not in reply


def test_halt_next_action_names_clear_gate_then_refire():
    expr = ask_compose._all_em_next_action_js(_SIZING)
    gated = ask_compose._gated_next_action_js(_SIZING)
    assert gated in expr
    assert "_manifest.gated_rows" in gated
    assert 'plan.tasks.mutate {"verb":"clear-gate"' in gated
    assert "g.owner_repo" in gated and "g.id" in gated and "_manifest.owed_plan" in gated
    assert gated.index("clear-gate") < gated.index("Then refire")
    assert ask_compose._refire_js(_SIZING) in gated
    assert ask_compose._no_manifest_next_action_js(_SIZING) in gated


def test_composed_script_folds_gated_rows_into_the_halt_manifest(monkeypatch):
    from coordinator_core.ops.dispatch_emit.tests.test_ask_owed_handback import _script

    script = _script(monkeypatch)
    assert "_stageGatedRows = { rows: r.gated_rows" in script
    assert "gated_rows: _stageGatedRows?.rows ?? []" in script
