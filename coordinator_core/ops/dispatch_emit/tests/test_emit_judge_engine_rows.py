"""The plan-route exit-criterion judge reads engine-plane rows at the engine root."""
from __future__ import annotations

import json
import re

from coordinator_core.ops.dispatch_emit import engine_owned_rows
from coordinator_core.ops.dispatch_emit.emit import _falsifier_schema_literal, compose_script
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.ops.review_mint.tests.test_execute_review import _STAGE_SCHEMAS, _v5_fragment

_ENGINE_ROOT = "/engine/root"

_HEAD = """---
plan_id: "p1"
---

## Tasks

```yaml plan-tasks
"""

_PLAN_ENGINE = _HEAD + """- id: T1
  title: "engine row"
  external_gate:
    - owner_repo: Project-Claude-Klabauter
      condition: delivered in the engine
      requires: commit-in-owner-repo
      cleared: true
- id: T2
  title: "cwd row"
  writes:
    - lib/widget.py
```
"""

_PLAN_CWD = _HEAD + """- id: T2
  title: "cwd row"
  writes:
    - lib/widget.py
```
"""

_PLAN_PATHS_ONLY = _HEAD + """- id: T1
  title: "writes the engine tree"
  writes:
    - coordinator_core/ops/sizing_intake.py
```
"""


def _fragment():
    fragment = _v5_fragment()
    fragment["execute_review"]["stages"].append(
        {
            "kind": "judge",
            "agents": [
                {"agentType": "coordinator:criterion-judge", "model": "opus", "effort": "low", "schema": "judge-result"}
            ],
        }
    )
    return fragment


def _script(tmp_path, plan_text):
    plan = tmp_path / "plan.md"
    plan.write_text(plan_text, encoding="utf-8")
    row = WaveRow(
        id="T1", title="t", surface="x", writes=["lib/widget.py"], reads=[], depends_on=[],
        agent_type=None, agent_model=None,
    )
    return compose_script(
        [[row]],
        name="wf",
        description="d",
        review_roster_fragment=_fragment(),
        review_stage_schemas={**_STAGE_SCHEMAS, "judge-result": {"type": "object"}},
        plan_path=str(plan),
        plan_text=plan_text,
        repo_root=tmp_path,
    )


def test_engine_row_names_engine_root_in_the_judge_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_owned_rows, "resolve_engine_root", lambda: (_ENGINE_ROOT, "resolved-engine"))
    script = _script(tmp_path, _PLAN_ENGINE)
    assert f"T1: engine-plane row, engine root {_ENGINE_ROOT} (resolved-engine)" in script
    assert "T2: engine-plane row" not in script


def test_dispatched_rows_writing_coordinator_core_are_not_engine_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_owned_rows, "resolve_engine_root", lambda: (_ENGINE_ROOT, "resolved-engine"))
    assert "Engine-plane rows" not in _script(tmp_path, _PLAN_PATHS_ONLY)


def test_plan_without_engine_rows_renders_no_clause(tmp_path, monkeypatch):
    def boom():
        raise AssertionError("resolver called for a plan with no engine row")

    monkeypatch.setattr(engine_owned_rows, "resolve_engine_root", boom)
    assert "Engine-plane rows" not in _script(tmp_path, _PLAN_CWD)


def test_unresolved_engine_renders_the_absent_text(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_owned_rows, "resolve_engine_root", lambda: ("", "unresolved"))
    script = _script(tmp_path, _PLAN_ENGINE)
    assert "T1: owner claude-klabauter is not on this machine" in script


def test_judge_and_falsifier_schemas_share_roots_read():
    schema = json.loads(_falsifier_schema_literal())
    item = schema["properties"]["roots_read"]["items"]
    assert set(item["required"]) == {"id", "root"}
    assert re.search(r'"roots_read"', _falsifier_schema_literal())
