"""The real bash dispatcher carries the trailing-ampersand and move-from-inside-cwd advisories to the envelope."""

from __future__ import annotations

import json

import pytest

from coordinator_core.bash_guards import dispatch


def _reason(out) -> str:
    if not isinstance(out, dict):
        return ""
    hso = out.get("hookSpecificOutput", {})
    return str(hso.get("additionalContext", "")) + str(hso.get("permissionDecisionReason", ""))


def _payload(command: str, cwd: str, tool_name: str = "Bash", **tool_input) -> str:
    return json.dumps(
        {
            "tool_name": tool_name,
            "tool_input": {"command": command, **tool_input},
            "session_id": "",
            "cwd": cwd,
        }
    )


def test_trailing_ampersand_advisory_reaches_the_envelope(tmp_path):
    out = dispatch.evaluate_payload_json(_payload("sleep 5 &", str(tmp_path)))
    assert "run_in_background" in _reason(out)


def test_trailing_ampersand_is_silent_when_run_in_background(tmp_path):
    out = dispatch.evaluate_payload_json(
        _payload("sleep 5 &", str(tmp_path), run_in_background=True)
    )
    assert "run_in_background" not in _reason(out)


def test_move_from_inside_cwd_advisory_reaches_the_envelope(tmp_path):
    root = tmp_path.as_posix()
    out = dispatch.evaluate_payload_json(
        _payload(f"mv {root}/a {root}/z", f"{root}/a"), host_is_windows=True
    )
    reason = _reason(out)
    assert "MOVE-FROM-INSIDE-CWD" in reason
    assert f"{root}/a" in reason.replace("\\", "/")


def test_move_outside_the_cwd_is_silent(tmp_path):
    root = tmp_path.as_posix()
    out = dispatch.evaluate_payload_json(
        _payload(f"mv {root}/a {root}/z", f"{root}/b"), host_is_windows=True
    )
    assert "MOVE-FROM-INSIDE-CWD" not in _reason(out)


@pytest.mark.parametrize("name", ["trailing-background-ampersand", "move-from-inside-cwd"])
def test_both_guards_are_registered_in_the_advisory_band(name):
    chain = {e.name: e for e in dispatch._build_guard_chain("echo hi", "", "", {}, None, None)}
    assert chain[name].band is dispatch.GuardBand.ADVISORY_REWRITE
