"""The repo-style plugin roster walk reads only the two documented agent depths, never a whole checkout."""

from __future__ import annotations

from coordinator_core.hooks import block_unenumerated_agent_type as mod


def _agent(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nname: x\n---\n", encoding="utf-8")


def test_both_documented_depths_are_read_and_deeper_trees_are_never_walked(tmp_path):
    top = tmp_path / "example-game-workbench-repo"
    _agent(top / "agents" / "flat.md")
    _agent(top / "game-dev" / "agents" / "staff-game-dev.md")
    _agent(top / "state" / "warp" / "run-1" / "agents" / "stray.md")
    _agent(top / "scratch" / "deep" / "agents" / "stray.md")

    keys = {key for key, _ in mod._repo_style_plugin_pairs(tmp_path)}

    assert keys == {"example-game-workbench-repo:flat", "game-dev:staff-game-dev"}
