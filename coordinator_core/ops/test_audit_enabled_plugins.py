"""Unit tests for `coordinator_core.ops.audit_enabled_plugins`: the enabled-but-unjustified-plugin drift advisory."""

from __future__ import annotations

import json
import os

import pytest

from coordinator_core.ops.audit_enabled_plugins import _is_justified, _parse_stack_tags, main


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


@pytest.fixture(autouse=True)
def _isolated_claude_home(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path_factory.mktemp("cfg")))


def _flagged_repo(tmp_path):
    _write(
        str(tmp_path / ".claude/settings.json"),
        json.dumps({"enabledPlugins": {"web-dev@mkt": True}}),
    )


def test_bound_session_lines_precede_step3(tmp_path, capsys, monkeypatch):
    from coordinator_core.install import plugin_cache_sessions as pcs

    _flagged_repo(tmp_path)
    sess = pcs.BoundSession("sid-1", "peer", 4242, 1.0, "/x", "bound")
    seen = []
    monkeypatch.setattr(pcs, "sessions_bound_to", lambda p, **k: seen.append(p) or [sess])
    assert main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert seen and seen[0].parts[-3:] == ("cache", "mkt", "web-dev")
    assert "peer pid 4242 sid-1" in out
    assert out.index("relaunch these sessions, then re-run step (3)") < out.index("(3) rm -rf")
    assert "only once no session above remains" in out


def test_no_bound_sessions_output_unchanged(tmp_path, capsys, monkeypatch):
    from coordinator_core.install import plugin_cache_sessions as pcs

    _flagged_repo(tmp_path)
    monkeypatch.setattr(pcs, "sessions_bound_to", lambda p, **k: [])
    main([str(tmp_path)])
    out = capsys.readouterr().out
    assert "relaunch" not in out
    assert out.endswith("/cache/<marketplace>/<plugin>/.\n")


def test_no_findings_never_calls_detector(tmp_path, capsys, monkeypatch):
    from coordinator_core.install import plugin_cache_sessions as pcs

    def boom(*a, **k):
        raise AssertionError("detector called")

    monkeypatch.setattr(pcs, "sessions_bound_to", boom)
    _write(
        str(tmp_path / ".claude/settings.json"),
        json.dumps({"enabledPlugins": {"coordinator@mkt": True}}),
    )
    assert main([str(tmp_path)]) == 0
    assert capsys.readouterr().out == ""


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
    assert "coordinator@marketplace" not in out.split("\n", 1)[1]
    assert "game-dev@marketplace" not in out
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
        ("plugin-dev", "meta", "", True),
        ("plugin-dev", "", "claude-plugin", True),
        ("plugin-dev", "", "", False),
        ("nonexistent-plugin", "web", "web", False),
    ],
)
def test_is_justified_matrix(plugin, pt, tags, expected):
    if plugin == "plugin-dev" and pt == "meta":
        assert _is_justified(plugin, pt, tags) is True
        return
    assert _is_justified(plugin, pt, tags) is expected


def test_parse_stack_tags_inline_list():
    fm = "project_type: game\nstack_tags: [unreal-engine, game]"
    assert _parse_stack_tags(fm).split() == ["unreal-engine", "game"]


def test_parse_stack_tags_block_list():
    fm = "project_type: web\nstack_tags:\n  - web\n  - typescript\nother_key: x"
    assert _parse_stack_tags(fm) == " web typescript "
