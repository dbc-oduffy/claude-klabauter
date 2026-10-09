"""needs-restart probe: each verdict path and fail-open."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

_COORDINATOR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_COORDINATOR / "lib"))
import session_surface_snapshot as snap  # noqa: E402

pytestmark = [pytest.mark.spawns_process]

PROBE = _COORDINATOR / "bin" / "needs-restart.py"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    cfg = tmp_path / "claude"
    plugin = tmp_path / "plugin"
    (cfg / "agents").mkdir(parents=True)
    (plugin / "hooks").mkdir(parents=True)
    (plugin / ".claude-plugin").mkdir()
    home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "sh"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (cfg / "settings.json").write_text(json.dumps({"env": {"A": "1"}, "hooks": {}}))
    (plugin / "hooks" / "hooks.json").write_text("{}")
    (plugin / ".claude-plugin" / "plugin.json").write_text('{"version": "1.0"}')
    snap.write_snapshot("sid1", "startup", str(tmp_path))
    return cfg, plugin, tmp_path


def run(sid="sid1", cwd=None):
    return snap.verdict(sid, cwd)


def test_unchanged_is_none(env):
    assert run(cwd=str(env[2])) == "none"


def test_hooks_json_change_is_reload(env):
    (env[1] / "hooks" / "hooks.json").write_text('{"x": 1}')
    assert run(cwd=str(env[2])) == "/reload-plugins"


def test_agent_added_is_reload(env):
    (env[0] / "agents" / "new.md").write_text("hi")
    assert run(cwd=str(env[2])) == "/reload-plugins"


def test_version_bump_is_reload(env):
    (env[1] / ".claude-plugin" / "plugin.json").write_text('{"version": "2.0"}')
    assert run(cwd=str(env[2])) == "/reload-plugins"


def test_settings_env_change_is_restart(env):
    (env[0] / "settings.json").write_text(json.dumps({"env": {"A": "2"}, "hooks": {}}))
    assert run(cwd=str(env[2])) == "restart"


def test_mcp_change_is_restart(env):
    (env[2] / ".mcp.json").write_text('{"mcpServers": {}}')
    assert run(cwd=str(env[2])) == "restart"


def test_restart_outranks_reload(env):
    (env[1] / "hooks" / "hooks.json").write_text('{"x": 1}')
    (env[0] / "settings.json").write_text(json.dumps({"env": {"A": "2"}, "hooks": {}}))
    assert run(cwd=str(env[2])) == "restart"


def test_most_recent_snapshot_fallback(env):
    (env[1] / "hooks" / "hooks.json").write_text('{"x": 1}')
    assert snap.verdict("", str(env[2])) == "/reload-plugins"


def test_compact_does_not_overwrite_snapshot(env):
    (env[0] / "settings.json").write_text(json.dumps({"env": {"A": "2"}, "hooks": {}}))
    snap.write_snapshot("sid1", "compact", str(env[2]))
    assert run(cwd=str(env[2])) == "restart"


def test_no_snapshot_is_none(env, tmp_path):
    for p in snap.cache_dir().glob("*.json"):
        p.unlink()
    assert run() == "none"


def test_corrupt_snapshot_fails_open(env):
    snap._snapshot_path("sid1").write_text("{not json")
    assert run(cwd=str(env[2])) == "none"


def test_write_snapshot_unwritable_is_silent(env, monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(blocker))
    snap.write_snapshot("sid2", "startup")


@pytest.mark.cadence
def test_cli_prints_one_verdict_and_exits_zero(env):
    (env[1] / "hooks" / "hooks.json").write_text('{"x": 1}')
    r = subprocess.run([sys.executable, str(PROBE), "--session-id", "sid1"],
                       capture_output=True, text=True, cwd=str(env[2]),
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert r.returncode == 0
    assert r.stdout.strip() in ("none", "/reload-plugins", "restart")
    assert len(r.stdout.strip().splitlines()) == 1


@pytest.mark.cadence
def test_cli_broken_environment_exits_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "nothing"))
    r = subprocess.run([sys.executable, str(PROBE)], capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert r.returncode == 0 and r.stdout.strip() == "none"
