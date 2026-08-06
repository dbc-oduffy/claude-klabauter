"""Tests for coordinator_core.ops.audit_enabled_plugins.

Port of: audit-enabled-plugins.sh (example-doctrine-repo b5a4192c, 2026-07-20)
"""
from __future__ import annotations

import json
import os

import pytest

from coordinator_core.ops.audit_enabled_plugins import _is_justified, _parse_stack_tags, main


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def test_no_settings_json_is_silent_noop(tmp_path, capsys):
    rc = main([str(tmp_path)])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_settings_json_no_enabled_plugins_is_silent_noop(tmp_path, capsys):
    _write(str(tmp_path / ".claude/settings.json"), json.dumps({"enabledPlugins": {}}))
    rc = main([str(tmp_path)])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_all_justified_no_drift(tmp_path, capsys):
    _write(
        str(tmp_path / ".claude/settings.json"),
        json.dumps({"enabledPlugins": {"coordinator@marketplace": True, "web-dev@marketplace": True}}),
    )
    _write(
        str(tmp_path / "coordinator.local.md"),
        "---\nproject_type: web\nstack_tags:\n  - web\n  - typescript\n---\nBody.\n",
    )
    rc = main([str(tmp_path)])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_drift_flags_unjustified_plugins(tmp_path, capsys):
    _write(
        str(tmp_path / ".claude/settings.json"),
        json.dumps(
            {
                "enabledPlugins": {
                    "coordinator@marketplace": True,
                    "web-dev@marketplace": True,
                    "game-dev@marketplace": False,
                    "unknown-plugin@marketplace": True,
                }
            }
        ),
    )
    _write(
        str(tmp_path / "coordinator.local.md"),
        "---\nproject_type: game\nstack_tags: [unreal-engine, game]\n---\nBody content here.\n",
    )
    rc = main([str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "enabledPlugins drift advisory" in out
    assert "web-dev@marketplace: enabled, but not justified" in out
    assert "unknown-plugin@marketplace: enabled, but not justified" in out
    assert "coordinator@marketplace" not in out.split("\n", 1)[1]  # not flagged
    assert "game-dev@marketplace" not in out  # disabled (false), never considered
    assert "Full uninstall = " in out


def test_missing_frontmatter_treated_as_unset(tmp_path, capsys):
    _write(
        str(tmp_path / ".claude/settings.json"),
        json.dumps({"enabledPlugins": {"plugin-dev@marketplace": True}}),
    )
    rc = main([str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "project_type='unset' stack_tags='unset'" in out


def test_meta_short_circuit_always_justified(tmp_path, capsys):
    _write(
        str(tmp_path / ".claude/settings.json"),
        json.dumps({"enabledPlugins": {"totally-unknown-plugin@marketplace": True}}),
    )
    _write(str(tmp_path / "coordinator.local.md"), "---\nproject_type: meta\n---\nBody.\n")
    rc = main([str(tmp_path)])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_default_repo_root_is_cwd(tmp_path, capsys, monkeypatch):
    _write(str(tmp_path / ".claude/settings.json"), json.dumps({"enabledPlugins": {}}))
    monkeypatch.chdir(tmp_path)
    rc = main([])
    assert rc == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "plugin,pt,tags,expected",
    [
        ("coordinator", "web", "", True),
        ("coordinator-claude", "", "", True),
        ("deep-research", "", "", True),
        ("game-dev", "game", "", True),
        ("game-dev", "web", "unreal-engine", True),
        ("game-dev", "web", "other", False),
        ("web-dev", "web", "", True),
        ("web-dev", "game", "", False),
        ("data-science", "data-science", "", True),
        ("data-science", "game", "", False),
        ("mcp-server-dev", "", "mcp-server", True),
        ("mcp-server-dev", "", "mcp-plugin", True),
        ("mcp-server-dev", "", "other", False),
        ("plugin-dev", "meta", "", True),  # via caller's meta short-circuit, not this fn
        ("plugin-dev", "", "claude-plugin", True),
        ("plugin-dev", "", "", False),
        ("nonexistent-plugin", "web", "web", False),
    ],
)
def test_is_justified_matrix(plugin, pt, tags, expected):
    if plugin == "plugin-dev" and pt == "meta":
        # _is_justified itself does not special-case meta (main()'s caller-level
        # short-circuit is what handles it) — verify the raw predicate result here.
        assert _is_justified(plugin, pt, tags) is True
        return
    assert _is_justified(plugin, pt, tags) is expected


def test_parse_stack_tags_inline_list():
    fm = "project_type: game\nstack_tags: [unreal-engine, game]"
    assert _parse_stack_tags(fm).split() == ["unreal-engine", "game"]


def test_parse_stack_tags_block_list():
    # Faithful bash-oracle quirk: block-list form yields a LEADING space (the
    # "stack_tags:" line's empty `rest` still emits one awk `print` + newline
    # before the first tag) — verified against the live bash oracle during the
    # port's parity gate. See module docstring negative-spec.
    fm = "project_type: web\nstack_tags:\n  - web\n  - typescript\nother_key: x"
    assert _parse_stack_tags(fm) == " web typescript "
