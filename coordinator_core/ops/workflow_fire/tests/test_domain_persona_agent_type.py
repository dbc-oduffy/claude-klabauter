"""A review's domain dispatch names a plugin persona (game-dev:staff-game-dev); both headless runners must load it
(ue-56 W4 fire 6411d58c: AgentFailed unsupported agentType at review)."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.codex_runner import workflow
from coordinator_core.hooks import block_unenumerated_agent_type as roster
from coordinator_core.ops.workflow_fire import headless_agents

RUNNERS = (headless_agents, workflow)


@pytest.mark.parametrize("mod", RUNNERS)
def test_installed_plugin_persona_loads(mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    found = tmp_path / "staff-game-dev.md"
    found.write_text("You are the Game Dev Reviewer.", encoding="utf-8")
    monkeypatch.setattr(roster, "plugin_agent_model", lambda t: (None, found) if t == "game-dev:staff-game-dev" else (None, None))
    assert mod._persona("game-dev:staff-game-dev") == "You are the Game Dev Reviewer."


@pytest.mark.parametrize("mod", RUNNERS)
def test_absent_plugin_persona_is_missing_not_unsupported(mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(roster, "plugin_agent_model", lambda t: (None, tmp_path / "absent.md"))
    with pytest.raises(mod.AgentFailed, match="agent definition missing"):
        mod._persona("game-dev:staff-game-dev")


@pytest.mark.parametrize("mod", RUNNERS)
@pytest.mark.parametrize("bad", ["../../escape", "game-dev:../x", "Game:Dev", "a:b:c"])
def test_path_shaped_types_stay_unsupported(mod, bad) -> None:
    with pytest.raises(mod.AgentFailed, match="unsupported agentType"):
        mod._persona(bad)
