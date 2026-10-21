"""Pins emit_script's work-class routing pre-pass: routed rows, admission refusal, no-table parity."""

from __future__ import annotations

import json
import re
import subprocess

import pytest

from coordinator_core.hooks import block_unenumerated_agent_type as bua
from coordinator_core.ops.dispatch_emit import agent_route
from coordinator_core.ops.dispatch_emit.agent_route import RouteAdmissionError
from coordinator_core.ops.dispatch_emit.emit import assert_zero_errors, emit_script

from .conftest import REVIEW_KW

UE = "example-game-repo-control:ue-editor"
UE_TRIAGE = "example-game-repo-control:ue-triage"
MCP = "mcp__example_game_repo-control__"


def _agent_md(name, tools=None, model="sonnet"):
    lines = ["---", f"name: {name}", f"model: {model}"]
    if tools is not None:
        lines.append(f"tools: {tools}")
    return "\n".join(lines + ["---", "body", ""])


@pytest.fixture
def repo(tmp_path, monkeypatch):
    content = tmp_path / "content"
    (content / "agents").mkdir(parents=True)
    (content / "agents" / "executor.md").write_text(
        _agent_md("executor", "Read, Edit, Write, Bash"), encoding="utf-8"
    )
    plug = tmp_path / "home" / ".claude" / "plugins" / "example-game-repo-control" / "agents"
    plug.mkdir(parents=True)
    (plug / "ue-editor.md").write_text(_agent_md("ue-editor", f"Read, {MCP}inspect"), encoding="utf-8")
    (plug / "ue-triage.md").write_text(_agent_md("ue-triage", "Read", model="haiku"), encoding="utf-8")
    monkeypatch.setattr(bua, "read_content_root", lambda: str(content))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    root = tmp_path / "repo"
    (root / ".claude").mkdir(parents=True)
    return root


def _table(on_mismatch="refuse", triage=None):
    cls = {
        "id": "ue-editor",
        "match": {"paths": ["Content/*"]},
        "agent_type": UE,
        "required_tools": [MCP],
        "on_mismatch": on_mismatch,
    }
    if triage:
        cls["triage_agent_type"] = triage
    return {"schema_version": 1, "default_agent_type": "coordinator:executor", "work_classes": [cls]}


def _write_table(root, table):
    (root / ".claude" / "route-table.json").write_text(json.dumps(table), encoding="utf-8")


def _plan(root, *, agent_type=None) -> object:
    override = f"  agent_type: {agent_type}\n" if agent_type else ""
    rows = (
        "- id: C1\n  title: import meshes\n  change_kind: code-edit\n  surface: Content/A.uasset\n"
        f"  writes:\n    - Content/A.uasset\n{override}  queue_scope: project\n  disposition: open\n"
        "  body: |\n    Do C1.\n"
        "- id: C2\n  title: plain\n  change_kind: code-edit\n  surface: src/b.py\n"
        "  writes:\n    - src/b.py\n  queue_scope: project\n  disposition: open\n  body: |\n    Do C2.\n"
    )
    path = root / "plan.md"
    path.write_text(
        '---\ntitle: "p"\nsizing_object: null\n---\n\n# p\n\n## Goal\n\ng\n\n'
        f"## Tasks\n\n```yaml plan-tasks\n{rows}```\n",
        encoding="utf-8",
    )
    return path


def _emit(root, **kw):
    return emit_script(_plan(root, **kw), repo_root=root, **REVIEW_KW)


def test_matching_row_is_dispatched_with_class_agent_and_frontmatter_model(repo):
    _write_table(repo, _table())
    script = _emit(repo)

    assert "async function _routedRun(" in script
    assert re.search(rf"t === '{UE}' \? agent\(.*?agentType: '{UE}', model: 'sonnet'", script, re.S)
    assert re.search(r"_routedRun\(\{ workClass: 'ue-editor', triage: null, primary: '" + UE, script)
    assert script.count("_routedRun(") == 2  # the declaration and the one routed row
    assert_zero_errors(script)


def test_unmatched_row_keeps_its_plain_executor_call(repo):
    _write_table(repo, _table())
    script = _emit(repo)

    assert re.search(r"_rows\['C2'\] = _runRow\('C2', \[\], [^\n]*agent\(", script)
    assert "_routedRun(" not in script.split("_rows['C2']")[1].split("_rows['C", 1)[0]


def test_triage_class_dispatches_triage_literal_first(repo):
    _write_table(repo, _table(triage=UE_TRIAGE))
    script = _emit(repo)

    assert f"triage: '{UE_TRIAGE}'" in script
    assert f"t === '{UE_TRIAGE}' ? agent(" in script
    assert f"t === '{UE}' ? agent(" in script
    assert "Routing: assess this work before proceeding" in script


def test_explicit_executor_row_on_class_refuses(repo):
    _write_table(repo, _table("refuse"))
    with pytest.raises(RouteAdmissionError, match=rf"row C1.*lacks {MCP}.*work class ue-editor"):
        _emit(repo, agent_type="coordinator:executor")


def test_explicit_executor_row_on_class_redirects_with_log_line(repo):
    _write_table(repo, _table("redirect"))
    script = _emit(repo, agent_type="coordinator:executor")

    assert f"t === '{UE}' ? agent(" in script
    assert re.search(r"log\('route: C1 redirected: [^']*ue-editor", script)


def _spawns(monkeypatch):
    calls = []
    real = subprocess.Popen

    class Spy(real):
        def __init__(self, *a, **k):
            calls.append(a)
            super().__init__(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", Spy)
    return calls


def test_no_table_script_equals_script_with_routing_bypassed(repo, monkeypatch):
    plain = _emit(repo)
    monkeypatch.setattr(agent_route, "route_rows", lambda rows, root: (rows, {}, []))
    bypassed = _emit(repo)

    assert plain == bypassed
    assert "_routedRun" not in plain


def test_routing_adds_no_subprocess_spawn(repo, monkeypatch):
    _write_table(repo, _table(triage=UE_TRIAGE))
    calls = _spawns(monkeypatch)
    _emit(repo)
    routed = len(calls)
    calls.clear()
    monkeypatch.setattr(agent_route, "route_rows", lambda rows, root: (rows, {}, []))
    _emit(repo)

    assert routed == len(calls)
