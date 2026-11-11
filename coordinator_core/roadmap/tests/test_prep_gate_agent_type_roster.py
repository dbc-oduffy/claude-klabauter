"""The prep gate refuses a plugin-qualified agent_type absent from the installed roster."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.roadmap.prep_gate import _unroutable_rows

_STEM = "ue-metahuman-likeness-specialist"


@pytest.fixture
def installed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    agents = home / ".claude" / "plugins" / "example-game-repo-control" / "agents"
    agents.mkdir(parents=True)
    (agents / f"{_STEM}.md").write_text("---\nname: x\nmodel: sonnet\n---\nb\n", encoding="utf-8")
    from coordinator_core.hooks import block_unenumerated_agent_type as hook

    monkeypatch.setattr(hook, "_home_dir", lambda: str(home))


def _row(agent_type):
    return WaveRow("C1", "t", "s", ("x.py",), [], [], agent_type=agent_type)


def test_wrong_plugin_prefix_is_refused_naming_the_installed_type(installed):
    ids, first = _unroutable_rows([[_row(f"game-dev:{_STEM}")]])
    assert ids == ["C1"]
    assert isinstance(first, emit.UnresolvableQualifiedAgentTypeError)
    assert f"example-game-repo-control:{_STEM}" in str(first)


def test_uninstalled_plugin_prefix_is_refused_naming_the_installed_type(installed):
    ids, first = _unroutable_rows([[_row(f"example-game-repo:{_STEM}")]])
    assert ids == ["C1"]
    assert isinstance(first, emit.UnresolvableQualifiedAgentTypeError)
    assert f"example-game-repo-control:{_STEM}" in str(first)


def test_installed_qualified_type_passes(installed):
    assert _unroutable_rows([[_row(f"example-game-repo-control:{_STEM}")]]) == ([], None)


def test_unrelated_missing_type_is_still_refused_with_some_closest_type(installed):
    ids, first = _unroutable_rows([[_row("example-game-repo-control:nothing-like-it")]])
    assert ids == ["C1"] and "closest installed type" in str(first)


def test_models_table_type_passes_though_absent_from_the_installed_roster(installed):
    assert _unroutable_rows([[_row("coordinator:executor")]]) == ([], None)
