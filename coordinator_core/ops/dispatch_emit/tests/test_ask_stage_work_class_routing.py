"""ask_stage routes manifest rows through the repo's route table and refuses a mismatched row."""

import json

import pytest

from coordinator_core.hooks import block_unenumerated_agent_type as bua
from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, stage
from coordinator_core.ops.dispatch_emit.spine_read import read_spine
from coordinator_core.ops.dispatch_emit.wave_map import build_waves

UE = "example-game-repo-control:ue-editor"
UE_TRIAGE = "example-game-repo-control:ue-triage"
MCP = "mcp__example_game_repo-control__"
PLAN_REL = "docs/plans/2026-10-01-routing-fixture.md"


def _agent_md(name, tools, model="sonnet"):
    return f"---\nname: {name}\nmodel: {model}\ntools: {tools}\n---\nbody\n"


def _plan(second_extra=""):
    return f"""---
deliverable_id: dlv-fixture-abc123
---
# Routing fixture

## Problem

Two rows.

## Tasks

```yaml plan-tasks
- id: C1
  title: import meshes
  change_kind: code-edit
  surface: Content/A.uasset
  writes:
    - Content/A.uasset
- id: C2
  title: plain code
  change_kind: code-edit
  surface: pkg/b.py
  writes:
    - pkg/b.py
  depends_on:
    - chunk: C1
      gate_kind: output-consumption-runtime
{second_extra}```
"""


def _table(**cls_over):
    cls = {
        "id": "ue-editor",
        "match": {"paths": ["Content/*"]},
        "agent_type": UE,
        "triage_agent_type": UE_TRIAGE,
        "required_tools": [MCP],
        "on_mismatch": "refuse",
    }
    cls.update(cls_over)
    return {"schema_version": 1, "default_agent_type": "coordinator:executor", "work_classes": [cls]}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    content = tmp_path / "content"
    (content / "agents").mkdir(parents=True)
    (content / "agents" / "executor.md").write_text(_agent_md("executor", "Read, Edit, Write"), encoding="utf-8")
    home = tmp_path / "home"
    plug = home / ".claude" / "plugins" / "example-game-repo-control" / "agents"
    plug.mkdir(parents=True)
    (plug / "ue-editor.md").write_text(_agent_md("ue-editor", f"Read, {MCP}inspect"), encoding="utf-8")
    (plug / "ue-triage.md").write_text(_agent_md("ue-triage", "Read", model="haiku"), encoding="utf-8")
    monkeypatch.setattr(bua, "read_content_root", lambda: str(content))
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "state" / "sizings").mkdir(parents=True)
    (root / PLAN_REL).write_text(_plan(), encoding="utf-8", newline="\n")
    return root


def _write_table(root, table):
    (root / ".claude").mkdir(exist_ok=True)
    (root / ".claude" / "route-table.json").write_text(json.dumps(table), encoding="utf-8")


def _by_id(manifest):
    return {r.id: r for r in manifest.rows}


def test_routed_row_carries_class_agent_type_model_and_route(repo):
    _write_table(repo, _table())
    rows = _by_id(stage(repo, run_id="r1", plan_rel=PLAN_REL))
    c1, c2 = rows["C1"], rows["C2"]
    assert (c1.agent_type, c1.model) == (UE, "sonnet")
    assert c1.route["work_class"] == "ue-editor"
    assert c1.route["primary"] == {"agent_type": UE, "model": "sonnet"}
    assert c1.route["triage"] == {"agent_type": UE_TRIAGE, "model": "haiku"}
    assert c1.route["targets"][UE] == "sonnet"
    assert c2.route is None and c2.agent_type == "coordinator:executor"


def test_wave_shape_is_unchanged_by_routing(repo):
    plain = {r.id: (r.wave, r.deps) for r in stage(repo, run_id="r0", plan_rel=PLAN_REL).rows}
    _write_table(repo, _table())
    routed = {r.id: (r.wave, r.deps) for r in stage(repo, run_id="r1", plan_rel=PLAN_REL).rows}
    assert routed == plain
    shape = [[r.id for r in w] for w in build_waves(read_spine(repo / PLAN_REL))]
    assert shape == [["C1"], ["C2"]]


def test_no_table_manifest_is_identical_to_unrouted(repo):
    m = stage(repo, run_id="r1", plan_rel=PLAN_REL)
    assert all(r.route is None and "route" not in r.to_json() for r in m.rows)
    assert all(r.agent_type == "coordinator:executor" for r in m.rows)


def test_mismatched_explicit_row_is_refused_naming_row_and_class(repo):
    _write_table(repo, _table())
    plan = (repo / PLAN_REL).read_text(encoding="utf-8").replace(
        "  surface: Content/A.uasset\n", "  surface: Content/A.uasset\n  agent_type: coordinator:executor\n  agent_model: sonnet\n"
    )
    (repo / PLAN_REL).write_text(plan, encoding="utf-8", newline="\n")
    with pytest.raises(AskStageError) as exc:
        stage(repo, run_id="r1", plan_rel=PLAN_REL)
    msg = str(exc.value)
    assert msg.startswith("route: ") and "C1" in msg and "ue-editor" in msg


def test_mismatched_explicit_row_is_redirected_under_redirect(repo):
    _write_table(repo, _table(on_mismatch="redirect"))
    plan = (repo / PLAN_REL).read_text(encoding="utf-8").replace(
        "  surface: Content/A.uasset\n", "  surface: Content/A.uasset\n  agent_type: coordinator:executor\n  agent_model: sonnet\n"
    )
    (repo / PLAN_REL).write_text(plan, encoding="utf-8", newline="\n")
    c1 = _by_id(stage(repo, run_id="r1", plan_rel=PLAN_REL))["C1"]
    assert c1.agent_type == UE and c1.route["work_class"] == "ue-editor"


def test_malformed_table_is_an_ask_stage_refusal(repo):
    _write_table(repo, {"schema_version": 2})
    with pytest.raises(AskStageError, match="^route: .*schema_version"):
        stage(repo, run_id="r1", plan_rel=PLAN_REL)
