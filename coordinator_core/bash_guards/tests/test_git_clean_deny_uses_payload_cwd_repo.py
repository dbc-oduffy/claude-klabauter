"""git-clean deny enumerates the innermost repo containing the payload cwd, never the hook process's cwd repo."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.bash_guards import dispatch

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _init(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q"], cwd=str(path), check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _reasons(command: str, tool_name: str, cwd: str) -> str:
    payload = {
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "session_id": "clean-cwd-sess",
        "cwd": cwd,
    }
    out = dispatch.evaluate_payload_json(json.dumps(payload))
    rows = out if isinstance(out, list) else [out]
    return "\n".join(
        row.get("hookSpecificOutput", {}).get("permissionDecisionReason", "")
        for row in rows
        if row
    )


@pytest.mark.parametrize("tool_name", ["Bash", "PowerShell"])
def test_deny_lists_nested_repo_files_not_outer(tmp_path, monkeypatch, tool_name):
    outer = tmp_path / "outer"
    nested = outer / "nested"
    _init(outer)
    _init(nested)
    (outer / "outer-only-handoff.md").write_text("x")
    (nested / "nested-only-handoff.md").write_text("x")
    monkeypatch.chdir(outer)

    reason = _reasons("git clean -fd", tool_name, str(nested))

    assert "nested-only-handoff.md" in reason
    assert "outer-only-handoff.md" not in reason
