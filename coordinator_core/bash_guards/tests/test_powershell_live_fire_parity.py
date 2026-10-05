"""Guards that fire on a Bash payload fire on the same command under the
PowerShell tool name (BX-17 live-fire FAILs)."""

from __future__ import annotations

import os

import pytest

from coordinator_core import machine_profile as mp
from coordinator_core.bash_guards import (
    _write_bump_sink_shapes as sinks,
    block_approval_sentinel_creation,
    block_illegal_filename,
    guard_head_tail_rewrite,
    guard_plumbing_and_loops,
)
from coordinator_core.bash_guards import _dialect

TOOLS = ("Bash", "PowerShell")
GATE_ENV = "MACHINE_LOCAL_COORDINATOR_FEATURE_DOCTRINE_EDIT_GATE"


def _payload(tool, cmd, tmp_path):
    return {
        "tool_name": tool,
        "tool_input": {"command": cmd},
        "cwd": str(tmp_path),
        "session_id": "parity-" + tool,
    }


def _ctx(result):
    return (result or {}).get("hookSpecificOutput", {}).get("additionalContext", "")


@pytest.mark.parametrize("tool", TOOLS)
def test_illegal_filename_fires(tool, tmp_path):
    cmd = "echo x > " + str(tmp_path / "bad?name.txt")
    result = block_illegal_filename.check(_payload(tool, cmd, tmp_path))
    assert result is not None
    assert "bad-name" in result["hookSpecificOutput"]["updatedInput"]["command"]


@pytest.mark.parametrize("tool", TOOLS)
def test_for_loop_advises_and_offers_no_python_to_powershell(tool, tmp_path):
    result = guard_plumbing_and_loops.check(
        _payload(tool, "for f in *.txt; do echo $f; done", tmp_path)
    )
    assert "for-loop" in _ctx(result)
    if tool == "PowerShell":
        assert "python3" not in _ctx(result)


def test_head_pipe_advises(tmp_path):
    tool = "PowerShell"
    dialect = _dialect.dialect_from_tool_name(tool)
    result = guard_head_tail_rewrite.check_head_tail_plumbing_rewrite(
        "cat CLAUDE.md | head -5",
        "parity-" + tool,
        dialect=dialect,
        payload=_payload(tool, "cat CLAUDE.md | head -5", tmp_path),
    )
    assert result is not None
    assert "updatedInput" not in result["hookSpecificOutput"]
    assert "Select-Object" in _ctx(result)


@pytest.mark.parametrize("target", [r"C:\zz\p.txt", "C:/zz/p.txt"])
def test_powershell_redirect_is_a_write_sink(target):
    tokens = ["echo", "hi", ">", target]
    assert sinks.extract_write_sink_targets_powershell(tokens, "echo") == [target]
    assert sinks.extract_write_sink_targets_powershell(["echo", "hi", ">>" + target], "echo") == [target]
    assert sinks.extract_write_sink_targets_powershell(["echo", "hi", "2>&1"], "echo") == []
    assert sinks.extract_write_sink_targets_powershell(["echo", ">", "$null"], "echo") == []


@pytest.fixture()
def author_box(tmp_path, monkeypatch):
    reg = tmp_path / "reg"
    reg.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    for key in list(os.environ):
        if key.startswith("MACHINE_LOCAL_COORDINATOR_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_MACHINE_PROFILE", "author")
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "strict")
    mp.reset_cache()
    yield
    mp.reset_cache()


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize(
    "cmd",
    [
        "touch .coordinator-doctrine-edit-approved",
        "touch /nonexistent-zzz-bx17/.coordinator-doctrine-edit-approved",
    ],
)
def test_approval_sentinel_denies_when_gate_on_and_allows_when_off(
    tool, cmd, author_box, monkeypatch, tmp_path
):
    payload = _payload(tool, cmd, tmp_path)
    mp.reset_cache()
    result = block_approval_sentinel_creation.check(payload)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    monkeypatch.setenv(GATE_ENV, "off")
    mp.reset_cache()
    assert block_approval_sentinel_creation.check(payload) is None
