"""A live Group EM's box-saving kills are never denied by any guard in the chain.

Drives the whole PreToolUse chain through ``dispatch.evaluate_payload_json`` so a
kill-shape guard added later is covered without a new case.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.group_em import nomination

GEM_SID = "S-gem"

_TOOL_SCRIPT = (
    "import os\nimport psutil\n"
    "for p in psutil.process_iter():\n    p.kill()\nos.kill(48920, 9)\n"
)

_CORPUS = [
    "python tools/gem-save-box/gem_save_box.py survey --help",
    "python tools/gem-save-box/gem_save_box.py kill --pid 48920 --reason box-saturating",
    "gem-save-box survey --help",
    "gem-save-box kill --pid 48920 --reason box-saturating --tree",
    "Get-CimInstance Win32_Process -Filter 'ProcessId=48920'; Stop-Process -Id 48920 -Force",
    "taskkill /T /F /PID 48920",
    "kill -9 48920",
    "Stop-Process -Name UnrealEditor",
    "taskkill /IM python.exe /F",
]

_LOOKUP_KILL = "Get-CimInstance Win32_Process -Filter 'ProcessId=48920'; Stop-Process -Id 48920 -Force"
_TOOLS = ("Bash", "PowerShell")


@pytest.fixture
def fixture_root(tmp_path):
    tool_dir = tmp_path / "tools" / "gem-save-box"
    tool_dir.mkdir(parents=True)
    (tool_dir / "gem_save_box.py").write_text(_TOOL_SCRIPT)
    return tmp_path


def _live(monkeypatch, holder=GEM_SID):
    monkeypatch.setattr(
        nomination, "identify", lambda root: {"state": "live", "holder": {"session_id": holder}}
    )


def _not_live(monkeypatch):
    monkeypatch.setattr(nomination, "identify", lambda root: {"state": "none", "holder": None})


def _run(tool, command, cwd, **ident):
    payload = {"tool_name": tool, "tool_input": {"command": command}, "cwd": str(cwd), **ident}
    return dispatch.evaluate_payload_json(json.dumps(payload))


def _is_deny_or_ask(result) -> bool:
    items = result if isinstance(result, list) else ([result] if result else [])
    for item in items:
        out = (item or {}).get("hookSpecificOutput") or item or {}
        if out.get("permissionDecision") in ("deny", "ask") or (item or {}).get("decision") == "block":
            return True
    return False


@pytest.mark.parametrize("tool", _TOOLS)
@pytest.mark.parametrize("command", _CORPUS)
def test_live_gem_main_session_is_never_denied(fixture_root, monkeypatch, tool, command):
    _live(monkeypatch)
    result = _run(tool, command, fixture_root, session_id=GEM_SID)
    assert not _is_deny_or_ask(result), result


@pytest.mark.parametrize("tool", _TOOLS)
def test_control_not_live_yields_a_deny_per_tool(fixture_root, monkeypatch, tool):
    _not_live(monkeypatch)
    results = [_run(tool, c, fixture_root, session_id=GEM_SID) for c in _CORPUS]
    assert any(_is_deny_or_ask(r) for r in results)


@pytest.mark.parametrize("tool", _TOOLS)
def test_control_other_session_is_denied_while_gem_is_live(fixture_root, monkeypatch, tool):
    _live(monkeypatch)
    assert _is_deny_or_ask(_run(tool, _LOOKUP_KILL, fixture_root, session_id="S-other"))


@pytest.mark.parametrize("tool", _TOOLS)
def test_gem_subagent_lookup_fed_kill_is_denied(fixture_root, monkeypatch, tool):
    _live(monkeypatch)
    result = _run(tool, _LOOKUP_KILL, fixture_root, session_id=GEM_SID, agent_id="a1")
    assert _is_deny_or_ask(result)


def test_control_gem_standing_false_would_deny(fixture_root, monkeypatch):
    _live(monkeypatch)
    from coordinator_core.bash_guards import block_kill_by_port_or_tree as guard

    monkeypatch.setattr(guard, "gem_standing", lambda payload: False)
    assert _is_deny_or_ask(_run("Bash", _LOOKUP_KILL, fixture_root, session_id=GEM_SID))
