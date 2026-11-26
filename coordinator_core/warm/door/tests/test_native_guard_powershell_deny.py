"""A PowerShell PreToolUse payload gets the door's native guard verdict with no warm engine.

`git worktree add ../x` carried as `tool_name: PowerShell` is denied by the door itself (the engine root has
no listener and no cold entrypoint, so nothing else can deny). Removing the PowerShell branch makes the
first test fail: the door would pass the payload loudly instead.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from coordinator_core.warm.tests.door_test_support import (
    DOOR_EXE,
    door_under_default_name,
    make_stub_engine_root,
)
from coordinator_core.warm.tests.test_door_read_deadline_posix import (
    _make_stub_engine_root as _make_posix_stub_engine_root,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.warm_tier]

_DOOR_DIR = Path(__file__).resolve().parents[1]
_OP = "hooks.preuse_bash_dispatch"


def _payload(tool: str, command: str, event: str = "PreToolUse") -> dict:
    return {
        "hook_event_name": event,
        "tool_name": tool,
        "tool_input": {"command": command},
        "session_id": "native-guard-powershell",
        "cwd": ".",
    }


@pytest.fixture(scope="module")
def door(tmp_path_factory) -> Path:
    if os.name == "nt":
        if not DOOR_EXE.exists():
            pytest.skip(f"no built door at {DOOR_EXE}")
        return DOOR_EXE
    compiler = next((shutil.which(c) for c in ("cc", "clang", "gcc") if shutil.which(c)), None)
    if compiler is None:
        pytest.skip("no C compiler")
    out = tmp_path_factory.mktemp("door") / "coordinator-invoke"
    subprocess.run(
        [compiler, "-O2", "-std=c11", '-DPYTHON_BIN="python3"', "-o", str(out),
         str(_DOOR_DIR / "door_posix.c"), str(_DOOR_DIR / "door_core.c")],
        check=True, capture_output=True, **no_console_creationflags(),
    )
    return out


@pytest.fixture(scope="module")
def engineless(door, tmp_path_factory):
    """Runs one hook payload through the built door against an engine root with no listener and no cold
    entrypoint; returns (denied, stdout)."""
    base = tmp_path_factory.mktemp("ps-deny")
    root = make_stub_engine_root(base) if os.name == "nt" else _make_posix_stub_engine_root(base)
    for script in (root / "coordinator" / "bin").glob("*.py"):
        script.unlink()
    exe = door_under_default_name(root) if os.name == "nt" else door
    short_runtime = Path(tempfile.mkdtemp(prefix="psd", dir="/tmp" if os.name != "nt" else None))
    env = dict(os.environ)
    env.update(
        COORDINATOR_DOOR_ENGINE_ROOT=str(root),
        COORDINATOR_WARM_RUNTIME_BASE=str(short_runtime),
        COORDINATOR_SETTINGS_HOME=str(base / "settings-home"),
        COORDINATOR_DOOR_STDIN_MODE="hook",
    )

    def run(payload: dict) -> tuple[bool, str]:
        proc = subprocess.run(
            [str(exe), _OP], input=json.dumps(payload).encode(), capture_output=True, env=env,
            cwd=str(root), timeout=60, **no_console_creationflags(),
        )
        assert proc.returncode == 0, proc.stderr
        text = proc.stdout.decode("utf-8", "replace")
        decision = json.loads(text).get("hookSpecificOutput", {}).get("permissionDecision", "")
        return decision == "deny", text

    try:
        yield run
    finally:
        shutil.rmtree(short_runtime, ignore_errors=True)


def test_powershell_git_worktree_add_is_denied_natively(engineless):
    denied, out = engineless(_payload("PowerShell", "git worktree add ../x"))
    assert denied, out
    assert "BLOCKED by block-worktree-creation" in out


@pytest.mark.parametrize(
    "command,guard",
    [
        ("git worktree add ../x", "block-worktree-creation"),
        ("Get-Location; git worktree add ../x", "block-worktree-creation"),
        ("git -C ../repo worktree add ../x", "block-worktree-creation"),
        ("git.exe worktree add ../x", "block-worktree-creation"),
        ("git push --delete origin x", "block-empty-source-push"),
        ("git push origin :refs/heads/x", "block-empty-source-push"),
        ("python -m pytest -p myplugin tests/", "guard-uncapped-pytest-plugin"),
        ("pytest -p=myplugin tests/", "guard-uncapped-pytest-plugin"),
    ],
)
def test_each_guards_deny_case_is_denied_natively_for_powershell(engineless, command, guard):
    denied, out = engineless(_payload("PowerShell", command))
    assert denied, out
    assert f"BLOCKED by {guard}" in out


@pytest.mark.parametrize(
    "command",
    [
        "git worktree list",
        "git worktree remove ../x",
        "git worktree prune",
        "git status",
        "git push origin feature-unheld",
        "git push origin main",
        "pytest -p no:cacheprovider tests/",
        "Get-ChildItem | Select-Object -First 3",
        "Write-Output 'git worktree add ../x'",
    ],
)
def test_powershell_allow_controls_are_not_denied_natively(engineless, command):
    denied, out = engineless(_payload("PowerShell", command))
    assert not denied, out


def test_a_non_command_tool_carrying_the_same_text_is_not_denied(engineless):
    denied, out = engineless(_payload("Read", "git worktree add ../x"))
    assert not denied, out


def test_a_non_pretooluse_powershell_payload_is_not_denied(engineless):
    denied, out = engineless(_payload("PowerShell", "git worktree add ../x", event="PostToolUse"))
    assert not denied, out


def test_bash_and_powershell_agree_on_the_worktree_deny(engineless):
    bash = engineless(_payload("Bash", "git worktree add ../x"))
    powershell = engineless(_payload("PowerShell", "git worktree add ../x"))
    assert bash == powershell
