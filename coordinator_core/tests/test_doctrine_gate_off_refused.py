"""An agent cannot turn the doctrine-edit approval gate off; a fresh PM
sentinel permits it, and turning it on is never refused."""

from __future__ import annotations

import os

import pytest

from coordinator_core import machine_profile as mp
from coordinator_core.bash_guards import block_approval_sentinel_creation as bash_guard
from coordinator_core.write_guards import guard_doctrine_surface_edits as write_guard

KEY = "coordinator.feature.doctrine_edit_gate"
SENTINEL = ".coordinator-doctrine-edit-approved"


@pytest.fixture()
def box(tmp_path, monkeypatch):
    reg = tmp_path / "reg"
    reg.mkdir()
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    for key in list(os.environ):
        if key.startswith("MACHINE_LOCAL_COORDINATOR_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "strict")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setattr(write_guard, "_git_root", lambda: str(repo))
    mp.reset_cache()
    yield reg, repo
    mp.reset_cache()


def _bash(cmd, repo, tool="Bash"):
    return {"tool_name": tool, "tool_input": {"command": cmd}, "cwd": str(repo)}


def _deny(result):
    return bool(result) and result["hookSpecificOutput"].get("permissionDecision") == "deny"


def _approve(repo):
    (repo / SENTINEL).write_text("", encoding="utf-8")


OFF_COMMANDS = [
    f"machine-local set {KEY} off",
    f"machine-local set {KEY} false",
    f"machine-local set {KEY} 0",
    f"cd /x && machine-local set {KEY} OFF",
    f"""python3 -c "from coordinator_core.machine_resolver import registry_set; registry_set('{KEY}','off')" """,
    f"""echo '"{KEY}" = "off"' >> registry.local.toml""",
]


@pytest.mark.parametrize("cmd", OFF_COMMANDS)
@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
def test_agent_set_off_refused_without_sentinel(box, cmd, tool):
    assert _deny(bash_guard.check(_bash(cmd, box[1], tool)))


def test_powershell_cmdlet_refused(box):
    cmd = f"""Set-Content registry.local.toml '"{KEY}" = "off"'"""
    assert _deny(bash_guard.check(_bash(cmd, box[1], "PowerShell")))


@pytest.mark.parametrize("cmd", OFF_COMMANDS)
def test_set_off_allowed_with_fresh_sentinel(box, cmd):
    _approve(box[1])
    assert bash_guard.check(_bash(cmd, box[1])) is None


def test_expired_sentinel_does_not_permit(box):
    _approve(box[1])
    old = os.path.getmtime(box[1] / SENTINEL) - 31 * 60
    os.utime(box[1] / SENTINEL, (old, old))
    assert _deny(bash_guard.check(_bash(f"machine-local set {KEY} off", box[1])))


@pytest.mark.parametrize(
    "cmd",
    [
        f"machine-local set {KEY} on",
        f"machine-local set {KEY} true",
        f"machine-local get {KEY}",
        f"grep {KEY} registry.local.toml",
        f"git commit -m 'refuse set {KEY} off'",
        "machine-local set coordinator.guard_level strict",
        "export MACHINE_LOCAL_COORDINATOR_FEATURE_PUBLISHING=off",
    ],
)
def test_on_and_reads_never_refused(box, cmd):
    assert bash_guard.check(_bash(cmd, box[1])) is None


def _write(path, content):
    return {"tool_name": "Write", "tool_input": {"file_path": str(path), "content": content}}


@pytest.mark.parametrize("name", ["registry.local.toml", "registry.toml"])
def test_registry_file_write_off_refused(box, name):
    path = box[0] / name
    assert _deny(write_guard.check(_write(path, f'"{KEY}" = \'off\'\n')))
    assert _deny(write_guard.check(_write(path, '[coordinator.feature]\ndoctrine_edit_gate = "off"\n')))


def test_registry_file_edit_off_refused(box):
    path = box[0] / "registry.local.toml"
    path.write_text(f'"{KEY}" = \'on\'\n', encoding="utf-8")
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(path), "old_string": "'on'", "new_string": "'off'"},
    }
    assert _deny(write_guard.check(payload))


def test_registry_file_write_off_allowed_with_sentinel(box):
    _approve(box[1])
    path = box[0] / "registry.local.toml"
    assert write_guard.check(_write(path, f'"{KEY}" = \'off\'\n')) is None


def test_registry_file_on_and_unrelated_writes_allowed(box):
    path = box[0] / "registry.local.toml"
    assert write_guard.check(_write(path, f'"{KEY}" = \'on\'\n')) is None
    assert write_guard.check(_write(path, '"repos.x" = \'/p\'\n')) is None


def test_already_off_file_unrelated_edit_allowed(box):
    path = box[0] / "registry.local.toml"
    path.write_text(f'"{KEY}" = \'off\'\n"a" = \'1\'\n', encoding="utf-8")
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(path), "old_string": "'1'", "new_string": "'2'"},
    }
    assert write_guard.check(payload) is None


LEVEL_COMMANDS = [
    "machine-local set coordinator.guard_level off",
    "machine-local set coordinator.guard_level warn",
    "machine-local set coordinator.guard_level.destructive-rm off",
    """python3 -c "from coordinator_core.machine_resolver import registry_set; registry_set('coordinator.guard_level','off')" """,
    """echo '"coordinator.guard_level" = "off"' >> registry.local.toml""",
    """tee registry.local.toml <<< 'guard_level = "warn"'""",
]
ENV_COMMANDS = [
    """jq '.env.MACHINE_LOCAL_COORDINATOR_FEATURE_PUBLISHING="off"' settings.json > t""",
    """jq '.env.MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL="off"' ~/.claude/settings.local.json > t""",
    """echo '{"env": {"MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL": "warn"}}' > .claude/settings.json""",
]


@pytest.mark.parametrize("cmd", LEVEL_COMMANDS + ENV_COMMANDS)
def test_level_lowering_and_env_off_refused(box, cmd):
    assert _deny(bash_guard.check(_bash(cmd, box[1])))
    _approve(box[1])
    assert bash_guard.check(_bash(cmd, box[1])) is None


@pytest.mark.parametrize(
    "cmd",
    [
        "machine-local set coordinator.guard_level.destructive-rm strict",
        "machine-local get coordinator.guard_level",
        """echo '{"env": {"MACHINE_LOCAL_COORDINATOR_FEATURE_PUBLISHING": "on"}}' > .claude/settings.json""",
        """echo '{"env": {"MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL": "strict"}}' > .claude/settings.json""",
        """echo '{"theme": "off"}' > .claude/settings.json""",
    ],
)
def test_raising_and_unrelated_never_refused(box, cmd):
    assert bash_guard.check(_bash(cmd, box[1])) is None


def test_warn_when_already_warn_is_not_lowering(box, monkeypatch):
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "warn")
    mp.reset_cache()
    assert bash_guard.check(_bash("machine-local set coordinator.guard_level warn", box[1])) is None


def test_sentinel_guard_is_floor():
    assert "block-approval-sentinel-creation" in mp.FLOOR_GUARDS


def test_registry_file_level_lowering_refused(box):
    path = box[0] / "registry.local.toml"
    path.write_text('"coordinator.guard_level" = "strict"\n', encoding="utf-8")
    assert _deny(write_guard.check(_write(path, '"coordinator.guard_level" = "off"\n')))
    edit = {"tool_name": "Edit", "tool_input": {"file_path": str(path), "old_string": "strict", "new_string": "warn"}}
    assert _deny(write_guard.check(edit))
    assert write_guard.check(_write(path, '"coordinator.guard_level" = "strict"\n')) is None
    _approve(box[1])
    assert write_guard.check(edit) is None


def _settings(box, where):
    d = box[1] / where if where == ".claude" else box[1].parent / "home" / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.mark.parametrize("where", [".claude", "home"])
@pytest.mark.parametrize("name", ["settings.json", "settings.local.json"])
@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
def test_settings_env_weakening_refused(box, where, name, tool):
    path = _settings(box, where) / name
    path.write_text('{"env": {"MACHINE_LOCAL_COORDINATOR_FEATURE_PUBLISHING": "on", "MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL": "strict"}}', encoding="utf-8")
    on, off = '"PUBLISHING": "on"', '"PUBLISHING": "off"'
    new_text = path.read_text().replace('FEATURE_PUBLISHING": "on"', 'FEATURE_PUBLISHING": "off"')
    lvl_text = path.read_text().replace('LEVEL": "strict"', 'LEVEL": "warn"')
    ok_text = '{"env": {"MACHINE_LOCAL_COORDINATOR_FEATURE_PUBLISHING": "on", "MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL": "strict"}, "theme": "x"}'

    def payload(text):
        if tool == "Write":
            return _write(path, text)
        edit = {"old_string": path.read_text(), "new_string": text}
        if tool == "Edit":
            return {"tool_name": "Edit", "tool_input": {"file_path": str(path), **edit}}
        return {"tool_name": "MultiEdit", "tool_input": {"file_path": str(path), "edits": [edit]}}

    assert _deny(write_guard.check(payload(new_text)))
    assert _deny(write_guard.check(payload(lvl_text)))
    assert write_guard.check(payload(ok_text)) is None
    _approve(box[1])
    assert write_guard.check(payload(new_text)) is None
