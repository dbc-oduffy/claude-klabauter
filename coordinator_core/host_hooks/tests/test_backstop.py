"""Backstop: guard set pinned to the registry, deny/allow on push remotes, no warm/ops import."""
from __future__ import annotations

import subprocess
import sys

import pytest

from coordinator_core import deny_registry
from coordinator_core.bash_guards import block_push_pr_non_owned_remote as push_guard
from coordinator_core.host_hooks import backstop
from coordinator_core.host_hooks.contract import HostEvent


def _event(command, tool_name="Bash", cwd="/nonexistent-backstop-cwd"):
    return HostEvent(
        host="codex", event="PreToolUse", session_id="s1",
        tool_name=tool_name, tool_input={"command": command}, cwd=cwd,
    )


def test_guard_set_equals_irreversible_external_registry_entries():
    expected = {
        g for g, c in deny_registry.GUARD_DENY_CLASS.items()
        if c == deny_registry.IRREVERSIBLE_EXTERNAL
    }
    assert set(backstop.GUARD_MODULES) == expected


def test_push_to_non_owned_remote_denied(monkeypatch):
    monkeypatch.setattr(push_guard, "_configured_prefixes", lambda: ["github.com/stranger/"])
    verdict = backstop.match(_event("git push https://github.com/stranger/repo.git main"))
    assert verdict is not None
    assert verdict["decision"] == "deny"
    assert verdict["reason"]


def test_push_to_owned_remote_allowed(monkeypatch):
    monkeypatch.setattr(push_guard, "_configured_prefixes", lambda: ["github.com/stranger/"])
    assert backstop.match(_event("git push https://github.com/mine/repo.git main")) is None


def test_non_command_tool_input_is_ignored():
    event = HostEvent(host="agy", event="PreToolUse", session_id="s", tool_name="view_file",
                      tool_input={"path": "x"})
    assert backstop.match(event) is None


def test_agy_commandline_shape_is_checked(monkeypatch):
    monkeypatch.setattr(push_guard, "_configured_prefixes", lambda: ["github.com/stranger/"])
    event = HostEvent(host="agy", event="PreToolUse", session_id="s", tool_name="run_command",
                      tool_input={"CommandLine": "git push https://github.com/stranger/r.git main"})
    assert backstop.match(event)["decision"] == "deny"


def test_raising_guard_is_skipped(monkeypatch):
    def boom(_payload):
        raise RuntimeError("guard broke")

    monkeypatch.setattr(push_guard, "check", boom)
    assert backstop.match(_event("git push https://github.com/stranger/r.git main")) is None


@pytest.mark.parametrize("command", [
    "git push --force origin main",
    "git push -f origin main",
    "git push origin +main",
    "git push origin +HEAD:refs/heads/main",
    "git push --force-with-lease origin master",
    "git -C sub push --force origin main",
    "git add . && git push origin main --force",
    "git push --force --all",
])
def test_force_push_to_main_denied(command):
    verdict = backstop.match(_event(command))
    assert verdict is not None and verdict["decision"] == "deny"
    assert "force-pushes main" in verdict["reason"]


@pytest.mark.parametrize("command", [
    "git status",
    "git push origin main",
    "git push --force origin topic/x",
    "git push origin +topic/x",
    "git stash push -f -- file",
    "git commit -m 'never git push --force origin main'",
    "echo git push --force origin main",
])
def test_non_main_or_non_force_push_not_denied(command):
    assert backstop.match(_event(command)) is None


def _repo_on(tmp_path, branch):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/%s\n" % branch, encoding="utf-8")
    return str(tmp_path)


@pytest.mark.parametrize("command", ["git push --force", "git push -f origin HEAD"])
def test_bare_force_push_resolves_the_current_branch(tmp_path, command):
    assert backstop.match(_event(command, cwd=_repo_on(tmp_path, "main")))["decision"] == "deny"


@pytest.mark.parametrize("command", ["git push --force", "git push -f origin HEAD"])
def test_bare_force_push_on_a_topic_branch_passes(tmp_path, command):
    assert backstop.match(_event(command, cwd=_repo_on(tmp_path, "topic"))) is None


def test_agy_force_push_to_main_denied():
    event = HostEvent(host="agy", event="PreToolUse", session_id="s", tool_name="run_command",
                      tool_input={"CommandLine": "git push --force origin main"})
    assert backstop.match(event)["decision"] == "deny"


def test_backstop_loads_no_warm_or_ops_module():
    code = (
        "import sys\n"
        "from coordinator_core.host_hooks import backstop\n"
        "from coordinator_core.host_hooks.contract import HostEvent\n"
        "backstop.match(HostEvent('codex','PreToolUse','s',tool_name='Bash',"
        "tool_input={'command':'git push origin main'},cwd='/nonexistent-backstop-cwd'))\n"
        "bad = sorted(m for m in sys.modules if m.startswith(('coordinator_core.warm','coordinator_core.ops')))\n"
        "print('LOADED', bad)\n"
        "sys.exit(1 if bad else 0)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert proc.returncode == 0, proc.stdout + proc.stderr
