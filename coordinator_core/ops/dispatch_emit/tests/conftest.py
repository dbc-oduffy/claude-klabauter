import pytest


@pytest.fixture(autouse=True)
def _no_ambient_plugin_root(monkeypatch):
    """Fixture rosters name agent types no real plugin ships; an ambient
    CLAUDE_PLUGIN_ROOT from the invoking session would arm the emit-time
    agent-type resolution against them. Tests that exercise the check set it."""
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
