"""guard_ue_specialist_advisory: advisory-only naming of the example-game-repo specialist on a direct dispatch."""

from __future__ import annotations

import json

import pytest

from coordinator_core.hooks import guard_ue_specialist_advisory as guard
from coordinator_core.ops.dispatch_emit import ue_route_source

SPECIALIST = "example-game-repo-control:ue-metahuman"
GHOST = "example-game-repo-control:ue-ghost"
EXECUTOR = "coordinator:executor"


def _table() -> dict:
    return {
        "schema_version": 1,
        "default_agent_type": EXECUTOR,
        "work_classes": [
            {
                "id": "ue-ghost",
                "match": {"keywords": ["niagara"]},
                "agent_type": GHOST,
                "on_mismatch": "redirect",
            },
            {
                "id": "ue-metahuman-asset",
                "match": {"keywords": ["metahuman"]},
                "agent_type": SPECIALIST,
                "on_mismatch": "redirect",
            },
        ],
    }


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A UE repo plus an installed example-game-repo plugin dir with a manifest and a one-agent roster."""
    plugin = tmp_path / "plugins" / "example-game-repo-control"
    (plugin / "routing").mkdir(parents=True)
    (plugin / "agents").mkdir()
    (plugin / "agents" / "ue-metahuman.md").write_text("x", encoding="utf-8")
    (plugin / "routing" / "ue-job-shapes.json").write_text(json.dumps(_table()), encoding="utf-8")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "Game.uproject").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_PLUGIN_ROOTS", str(plugin))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    ue_route_source.clear_caches()
    yield {"repo": repo, "plugin": plugin, "tmp": tmp_path}
    ue_route_source.clear_caches()


def _payload(cwd, prompt="fix the metahuman helmet", subagent_type="general-purpose", tool_name="Agent"):
    tool_input = {"prompt": prompt}
    if subagent_type is not None:
        tool_input["subagent_type"] = subagent_type
    return {"tool_name": tool_name, "cwd": str(cwd), "tool_input": tool_input}


def _text(out: dict) -> str:
    return out["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("subagent_type", ["general-purpose", EXECUTOR, None])
def test_non_example_game_repo_dispatch_gets_advisory_naming_specialist(world, subagent_type):
    out = guard.evaluate(_payload(world["repo"], subagent_type=subagent_type))
    assert f"route to {SPECIALIST}" in _text(out)
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


def test_never_a_deny_or_rewrite(world):
    hso = guard.evaluate(_payload(world["repo"]))["hookSpecificOutput"]
    assert "permissionDecision" not in hso
    assert "updatedInput" not in hso


@pytest.mark.parametrize("subagent_type", [SPECIALIST, "example-game-repo-control:anything", "game-dev:ue-gameplay"])
def test_example_game_repo_and_game_dev_agents_get_none(world, subagent_type):
    assert guard.evaluate(_payload(world["repo"], subagent_type=subagent_type)) == {}


def test_non_ue_repo_gets_none(world):
    plain = world["tmp"] / "plain"
    plain.mkdir()
    assert guard.evaluate(_payload(plain)) == {}


def test_example_game_repo_not_installed_gets_none(world, monkeypatch):
    monkeypatch.setenv("COORDINATOR_PLUGIN_ROOTS", str(world["tmp"] / "nowhere"))
    ue_route_source.clear_caches()
    assert guard.evaluate(_payload(world["repo"])) == {}


def test_specialist_absent_from_roster_is_never_named(world):
    out = guard.evaluate(_payload(world["repo"], prompt="fix the niagara system"))
    assert out == {}


def test_non_ue_brief_gets_none(world):
    assert guard.evaluate(_payload(world["repo"], prompt="refactor the python build script")) == {}


def test_non_agent_tool_gets_none(world):
    assert guard.evaluate(_payload(world["repo"], tool_name="Bash")) == {}


def test_malformed_manifest_fails_open(world):
    (world["plugin"] / "routing" / "ue-job-shapes.json").write_text("{not json", encoding="utf-8")
    ue_route_source.clear_caches()
    assert guard.evaluate(_payload(world["repo"])) == {}


def test_internal_error_fails_open(world, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(guard, "_message", boom)
    assert guard.evaluate(_payload(world["repo"])) == {}


def test_explicit_repo_root_overrides_cwd(world):
    out = guard.evaluate(_payload("/nonexistent-cwd"), repo_root=world["repo"])
    assert f"route to {SPECIALIST}" in _text(out)


def test_registered_op_dispatches(world):
    assert f"route to {SPECIALIST}" in _text(guard._handler(_payload(world["repo"])))
