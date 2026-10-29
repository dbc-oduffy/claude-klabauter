"""Every manifest row staged by dispatch.ask_stage carries route_source (closed set) and a rag_calls key."""

import json

from coordinator_core.hooks import block_unenumerated_agent_type as bua
from coordinator_core.ops.dispatch_emit.ask_contract import ROUTE_SOURCES
from coordinator_core.ops.dispatch_emit.ask_stage import stage

UE = "example-game-repo-control:ue-editor"
MCP = "mcp__example_game_repo-control__"
PLAN_REL = "docs/plans/2026-10-01-route-source.md"

PLAN = """---
deliverable_id: dlv-fixture-abc123
---
# Route source fixture

## Tasks

```yaml plan-tasks
- id: C1
  title: import meshes
  change_kind: code-edit
  surface: Content/A.uasset
  writes:
    - Content/A.uasset
- id: C2
  title: explicit agent
  change_kind: code-edit
  surface: pkg/b.py
  agent_type: coordinator:executor
  agent_model: sonnet
  writes:
    - pkg/b.py
- id: C3
  title: plain code
  change_kind: code-edit
  surface: pkg/c.py
  writes:
    - pkg/c.py
```
"""


def _agent_md(name, tools, model="sonnet"):
    return f"---\nname: {name}\nmodel: {model}\ntools: {tools}\n---\nbody\n"


def _staged(tmp_path, monkeypatch):
    content = tmp_path / "content"
    (content / "agents").mkdir(parents=True)
    (content / "agents" / "executor.md").write_text(_agent_md("executor", "Read, Edit, Write"), encoding="utf-8")
    home = tmp_path / "home"
    plug = home / ".claude" / "plugins" / "example-game-repo-control" / "agents"
    plug.mkdir(parents=True)
    (plug / "ue-editor.md").write_text(_agent_md("ue-editor", f"Read, {MCP}inspect"), encoding="utf-8")
    monkeypatch.setattr(bua, "read_content_root", lambda: str(content))
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "state" / "sizings").mkdir(parents=True)
    (root / ".claude").mkdir()
    table = {
        "schema_version": 1,
        "default_agent_type": "coordinator:executor",
        "work_classes": [
            {
                "id": "ue-editor",
                "match": {"paths": ["Content/*"]},
                "agent_type": UE,
                "required_tools": [MCP],
                "on_mismatch": "refuse",
            }
        ],
    }
    (root / ".claude" / "route-table.json").write_text(json.dumps(table), encoding="utf-8")
    (root / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    return stage(root, run_id="r1", plan_rel=PLAN_REL)


def test_every_staged_row_carries_both_fields(tmp_path, monkeypatch):
    rows = {r.id: r for r in _staged(tmp_path, monkeypatch).rows}
    assert {"C1", "C2", "C3"} <= set(rows)
    for r in rows.values():
        out = r.to_json()
        assert out["route_source"] in ROUTE_SOURCES
        assert "rag_calls" in out and out["rag_calls"] is None
    assert rows["C1"].route_source == "route-table"
    assert rows["C2"].route_source == "overlay"
    assert rows["C3"].route_source == "default"


def test_no_route_table_leaves_explicit_overlay_and_plain_default(tmp_path, monkeypatch):
    _staged(tmp_path, monkeypatch)
    root = tmp_path / "repo"
    (root / ".claude" / "route-table.json").unlink()
    plan = PLAN.replace("Content/A.uasset", "pkg/a.py")
    (root / PLAN_REL).write_text(plan, encoding="utf-8", newline="\n")
    rows = {r.id: r for r in stage(root, run_id="r2", plan_rel=PLAN_REL).rows}
    assert [rows[i].route_source for i in ("C1", "C2", "C3")] == ["default", "overlay", "default"]
