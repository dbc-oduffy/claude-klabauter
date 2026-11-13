"""Parity: the installed codex/agy host guard (default invocation, cold engine) returns the verdict the Claude
path returns for the same command, per `deny_registry`. An EM-class caller gets EM powers (a subagent-only deny
stays allowed); a codex subagent (payload carries `agent_id`) gets the subagent limits `SUBAGENT_DENY_CLASS` names.

The Claude reference is `bash_guards.dispatch.evaluate_payload_json` on a Claude-shaped Bash payload, narrowed the
way `warm_guard.evaluate` narrows it. agy payloads carry no caller identity, so agy is asserted on the EM leg only.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core import deny_registry
from coordinator_core.bash_guards.dispatch import evaluate_payload_json

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process]

_ENGINE_ROOT = Path(__file__).resolve().parents[3]
_BIN = _ENGINE_ROOT / "coordinator" / "bin"
_SUBAGENT_ID = "a7889dad168f41aa5"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_ATTEMPTS = 4

_spec = importlib.util.spec_from_file_location("unimatrix_install_for_parity", _BIN / "unimatrix-install.py")
_ui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ui)

# (id, repo kind, command, EM deny guard or None, subagent deny guard or None). A guard name is the registry key
# of the guard that refuses; None means the Claude path allows. The subagent column is the verdict for a
# dispatched caller, so a name there that the EM column lacks must sit in SUBAGENT_DENY_CLASS.
_CASES = [
    ("owned-push", "owned", "git push origin main", None, None),
    ("non-owned-push", "foreign", "git push origin main",
     "block-push-pr-non-owned-remote", "block-push-pr-non-owned-remote"),
    ("force-push-main", "owned", "git push --force origin main", "force-push-main", "force-push-main"),
    ("destructive-rm", "owned", "rm -rf f.txt", "destructive-rm", "destructive-rm"),
    ("git-clean", "owned", "git clean -fdx", None, "block-subagent-destructive-action"),
    ("stash-drop", "owned", "git stash drop", "block-stash-destruction", "block-stash-destruction"),
    ("blanket-add", "owned", "git add -A", None, None),
    ("benign-status", "owned", "git status", None, None),
]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b.c", "-c", "user.name=n",
                    "-c", "commit.gpgsign=false", *args],
                   check=True, capture_output=True, creationflags=_NO_WINDOW)


def _real_repo(path: Path, url: str) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "remote", "add", "origin", url)
    (path / "f.txt").write_text("x", encoding="utf-8")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-qm", "init")
    (path / "untracked.txt").write_text("u", encoding="utf-8")
    (path / "f.txt").write_text("changed", encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    base = tmp_path_factory.mktemp("parity")
    registry = base / "registry"
    registry.mkdir()
    (registry / "registry.local.toml").write_text(
        '[policy]\nnon_owned_remotes = ["github.com/some-stranger/"]\n', encoding="utf-8")
    env = {"COORDINATOR_SETTINGS_HOME": str(base / "settings-home"), "MACHINE_LOCAL_REGISTRY_DIR": str(registry),
           "COORDINATOR_WARM_RUNTIME_BASE": str(base / "warm")}
    installed = base / "home" / "unimatrix"
    _ui.install_scripts(_ui._BIN_DIR, installed)
    repos = {"owned": _real_repo(base / "owned", "https://github.com/me/thing.git"),
             "foreign": _real_repo(base / "foreign", "https://github.com/some-stranger/thing.git")}
    return installed, env, repos


def _claude_denies(monkeypatch, env: dict, cwd: Path, command: str, subagent: bool) -> bool:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "parity-s", "cwd": str(cwd),
               "tool_input": {"command": command}}
    if subagent:
        payload["agent_id"] = _SUBAGENT_ID
    out = evaluate_payload_json(json.dumps(payload))
    hso = out.get("hookSpecificOutput") if isinstance(out, dict) else None
    return isinstance(hso, dict) and hso.get("permissionDecision") == "deny"


def _host_payload(host: str, cwd: Path, command: str, subagent: bool) -> dict:
    if host == "codex":
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "parity-s", "cwd": str(cwd),
                   "tool_input": {"command": command}}
        if subagent:
            payload["agent_id"] = _SUBAGENT_ID
        return payload
    return {"hookEventName": "PreToolUse", "conversationId": "parity-s", "workspacePaths": [str(cwd)],
            "toolCall": {"name": "run_command", "args": {"CommandLine": command}}}


def _host_denies(world, host: str, cwd: Path, command: str, subagent: bool) -> bool:
    installed, env, _ = world
    # The guard fails open when its 0.5s wall elapses on a loaded box; a persistent degrade (import error)
    # survives every attempt and fails the assertion.
    for _ in range(_ATTEMPTS):
        proc = subprocess.run(
            [sys.executable, str(installed / "unimatrix-host-guard.py"), "--host", host,
             "--engine-root", str(_ENGINE_ROOT)],
            input=json.dumps(_host_payload(host, cwd, command, subagent)), capture_output=True, text=True,
            env={**os.environ, **env}, timeout=60, creationflags=_NO_WINDOW)
        assert proc.returncode == 0, proc.stderr
        state = proc.stderr.strip().splitlines()[-1]
        if state != "degraded":
            break
    assert state != "degraded", (host, command, proc.stdout, proc.stderr)
    answer = json.loads(proc.stdout)
    if host == "codex":
        return answer.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    return answer.get("decision") == "deny"


def _registered(guard: str, registry: dict) -> bool:
    return guard in registry


@pytest.mark.parametrize("host", ["codex", "agy"])
@pytest.mark.parametrize("case", _CASES, ids=[c[0] for c in _CASES])
def test_em_verdict_matches_the_claude_em_path(world, monkeypatch, host, case):
    _, env, repos = world
    _, kind, command, em_guard, _ = case
    cwd = repos[kind]
    claude = _claude_denies(monkeypatch, env, cwd, command, subagent=False)
    assert claude == (em_guard is not None), (command, "the Claude EM path drifted from the pinned expectation")
    if em_guard and em_guard != "force-push-main":
        assert _registered(em_guard, deny_registry.GUARD_DENY_CLASS), em_guard
    assert _host_denies(world, host, cwd, command, subagent=False) == claude, (host, command)


@pytest.mark.parametrize("case", _CASES, ids=[c[0] for c in _CASES])
def test_codex_subagent_verdict_matches_the_claude_subagent_path(world, monkeypatch, case):
    _, env, repos = world
    _, kind, command, em_guard, sub_guard = case
    cwd = repos[kind]
    claude = _claude_denies(monkeypatch, env, cwd, command, subagent=True)
    assert claude == (sub_guard is not None), (command, "the Claude subagent path drifted from the pinned expectation")
    if sub_guard and sub_guard != em_guard:
        assert _registered(sub_guard, deny_registry.SUBAGENT_DENY_CLASS), sub_guard
    assert _host_denies(world, "codex", cwd, command, subagent=True) == claude, command


def test_a_subagent_only_deny_stays_an_allow_for_the_codex_em(world):
    """git clean: the subagent limit is not a wall on the EM -- the guard answers by caller, not by command."""
    _, _, repos = world
    assert _host_denies(world, "codex", repos["owned"], "git clean -fdx", subagent=False) is False
    assert _host_denies(world, "codex", repos["owned"], "git clean -fdx", subagent=True) is True
