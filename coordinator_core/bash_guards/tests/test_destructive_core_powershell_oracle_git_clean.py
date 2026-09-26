
from __future__ import annotations

import json
import os
import subprocess

import pytest

from coordinator_core.bash_guards import dispatch

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


_SPELLINGS = [
    "rm -rf .git",
    "Remove-Item -Recurse -Force .git",
    "ri -Recurse -Force .git",
    "Remove-Item .git -Recurse -Force",
]

_TOOL_NAMES = ("Bash", "PowerShell")


def _payload(command: str, tool_name: str, cwd: str) -> dict:
    return {
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "session_id": "oracle-sess",
        "cwd": cwd,
    }


def _denies(command: str, tool_name: str, cwd: str) -> bool:
    out = dispatch.evaluate_payload_json(json.dumps(_payload(command, tool_name, cwd)))
    if out is None:
        return False
    rows = out if isinstance(out, list) else [out]
    return any(
        row.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
        for row in rows
    )


@pytest.fixture()
def repo_cwd() -> str:
    cwd = os.getcwd()
    assert os.path.isdir(os.path.join(cwd, ".git")), (
        "this oracle requires the process cwd to be a real git checkout "
        "(found no %r) -- see check_destructive_rm's own docstring for why "
        "cwd, not payload['cwd'], governs its target-existence probe"
        % os.path.join(cwd, ".git")
    )
    return cwd


@pytest.fixture()
def fresh_git_repo(tmp_path, monkeypatch):
    subprocess.run(
        ["git", "init", "-q"], cwd=str(tmp_path), check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "config", "user.email", "oracle@example.invalid"], cwd=str(tmp_path), check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "config", "user.name", "oracle"], cwd=str(tmp_path), check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "scratch.txt").write_text("load-bearing per _GC_LOADBEARING_PREFIXES")
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestDestructiveGitCleanLoadBearing:

    def test_backtick_escaped_git_clean_denies_on_the_powershell_leg_only(self, fresh_git_repo) -> None:
        cmd = "g`it clean -fdx"
        cwd = str(fresh_git_repo)
        assert _denies(cmd, "PowerShell", cwd)
        assert not _denies(cmd, "Bash", cwd)

    def test_plain_git_clean_denies_under_both_dialects(self, fresh_git_repo) -> None:
        cmd = "git clean -fdx"
        cwd = str(fresh_git_repo)
        assert _denies(cmd, "Bash", cwd)
        assert _denies(cmd, "PowerShell", cwd)

    def test_unparseable_powershell_does_not_deny(self, fresh_git_repo) -> None:
        cwd = str(fresh_git_repo)
        assert not _denies("g`it clean -fdx @'", "PowerShell", cwd)
        assert not _denies("g`it clean -fdx @'", "Bash", cwd)

