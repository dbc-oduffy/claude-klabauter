"""Pins route_rows' fallback to the installed example-game-repo-control route table in a UE repo."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from coordinator_core.hooks import block_unenumerated_agent_type as bua
from coordinator_core.ops.dispatch_emit import agent_route as ar
from coordinator_core.ops.dispatch_emit import ue_route_source
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

FIXTURE = Path(__file__).parent / "fixtures" / "example_game_repo_route_table.json"
SPECIALIST = "example-game-repo-control:ue-python-executor"
MCP = "mcp__example_game_repo-control__"


def _agent_md(name, tools, model="sonnet"):
    return f"---\nname: {name}\nmodel: {model}\ntools: {tools}\n---\nbody\n"


def _row(rid="C1", writes=("Source/Game/X.cpp",), title="t", body="", **kw):
    return WaveRow(rid, title, "s", tuple(writes), [], [], body=body, **kw)


def _clear():
    ue_route_source.clear_caches()
    ar._PLUGIN_TABLE_CACHE.clear()
    ar._TABLE_CACHE.clear()


@pytest.fixture
def world(tmp_path, monkeypatch):
    content = tmp_path / "content"
    (content / "agents").mkdir(parents=True)
    (content / "agents" / "executor.md").write_text(
        _agent_md("executor", "Read, Edit, Write, Bash"), encoding="utf-8"
    )
    home = tmp_path / "home"
    plugin = home / ".claude" / "plugins" / "example-game-repo-control"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "example-game-repo-control"}), encoding="utf-8")
    (plugin / "routing").mkdir()
    shutil.copyfile(FIXTURE, plugin / "routing" / "ue-job-shapes.json")
    (plugin / "agents").mkdir()
    (plugin / "agents" / "ue-python-executor.md").write_text(
        _agent_md("ue-python-executor", f"Read, {MCP}execute_python_code"), encoding="utf-8"
    )
    monkeypatch.setattr(bua, "read_content_root", lambda: str(content))
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.setenv("COORDINATOR_PLUGIN_ROOTS", str(plugin))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Game.uproject").write_text("{}", encoding="utf-8")
    _clear()
    yield repo, plugin
    _clear()


def test_manifest_job_shape_routes_to_specialist(world):
    repo, _ = world
    out, routes, notes = ar.route_rows([_row(body="run ExecutePythonScript on the level")], repo)
    assert out[0].agent_type == SPECIALIST
    assert routes["C1"].work_class == "ue-editor-python"
    assert routes["C1"].primary[0] == SPECIALIST
    assert notes == ["C1 -> example-game-repo-control:ue-python-executor (class ue-editor-python)"]


def test_consumer_cpp_path_is_not_routed_by_plugin_path_class(world):
    repo, _ = world
    rows = [_row(writes=("Source/Game/X.cpp",), body="refactor")]
    out, routes, notes = ar.route_rows(rows, repo)
    assert out[0].agent_type is None and routes == {} and notes == []


def test_non_ue_repo_returns_rows_unchanged(world):
    repo, _ = world
    (repo / "Game.uproject").unlink()
    _clear()
    rows = [_row(body="ExecutePythonScript")]
    out, routes, notes = ar.route_rows(rows, repo)
    assert out is rows and routes == {} and notes == []


def test_example_game_repo_not_installed_returns_rows_unchanged(world, monkeypatch):
    repo, _ = world
    monkeypatch.setenv("COORDINATOR_PLUGIN_ROOTS", "")
    _clear()
    rows = [_row(body="ExecutePythonScript")]
    out, routes, notes = ar.route_rows(rows, repo)
    assert out is rows and routes == {} and notes == []


def test_malformed_plugin_table_keeps_regular_with_drift_note(world):
    repo, plugin = world
    (plugin / "routing" / "ue-job-shapes.json").write_text("{not json", encoding="utf-8")
    _clear()
    rows = [_row(body="ExecutePythonScript")]
    out, routes, notes = ar.route_rows(rows, repo)
    assert out is rows and routes == {}
    assert len(notes) == 1 and notes[0].startswith("drift:")


def test_specialist_without_model_pin_keeps_regular_with_drift_note(world):
    repo, plugin = world
    (plugin / "agents" / "ue-python-executor.md").write_text(
        _agent_md("ue-python-executor", f"Read, {MCP}execute_python_code", model="inherit"), encoding="utf-8"
    )
    out, routes, notes = ar.route_rows([_row(body="ExecutePythonScript")], repo)
    assert out[0].agent_type is None and routes == {}
    assert len(notes) == 1 and notes[0].startswith("drift:") and "model pin" in notes[0]


def test_specialist_absent_from_roster_keeps_regular_with_drift_note(world, monkeypatch):
    repo, _ = world
    monkeypatch.setattr(ue_route_source, "example_game_repo_roster", lambda root: frozenset())
    out, routes, notes = ar.route_rows([_row(body="ExecutePythonScript")], repo)
    assert out[0].agent_type is None and routes == {}
    assert len(notes) == 1 and notes[0].startswith("drift:") and "roster" in notes[0]


def test_explicit_row_hitting_refuse_class_degrades_to_drift_note(world):
    repo, _ = world
    rows = [_row(body="tweak the unreal level", agent_type="coordinator:executor")]
    out, routes, notes = ar.route_rows(rows, repo)
    assert out[0].agent_type == "coordinator:executor" and routes == {}
    assert len(notes) == 1 and notes[0].startswith("drift:")


def test_one_drifted_row_does_not_unroute_siblings(world):
    repo, _ = world
    rows = [
        _row("C1", body="tweak the unreal level", agent_type="coordinator:executor"),
        _row("C2", body="ExecutePythonScript"),
    ]
    out, routes, notes = ar.route_rows(rows, repo)
    assert out[0].agent_type == "coordinator:executor"
    assert out[1].agent_type == SPECIALIST and "C2" in routes and "C1" not in routes
    assert any(n.startswith("drift: row C1") for n in notes)


def test_repo_owned_table_wins_and_still_raises(world):
    repo, _ = world
    (repo / ".claude").mkdir()
    table = {
        "schema_version": 1,
        "default_agent_type": "coordinator:executor",
        "work_classes": [
            {
                "id": "ue-any",
                "match": {"keywords": ["unreal"]},
                "agent_type": SPECIALIST,
                "required_tools": [MCP],
                "on_mismatch": "refuse",
            }
        ],
    }
    (repo / ".claude" / "route-table.json").write_text(json.dumps(table), encoding="utf-8")
    out, _, _ = ar.route_rows([_row(body="unreal thing")], repo)
    assert out[0].agent_type == SPECIALIST
    with pytest.raises(ar.RouteAdmissionError):
        ar.route_rows([_row(body="unreal thing", agent_type="coordinator:executor")], repo)
