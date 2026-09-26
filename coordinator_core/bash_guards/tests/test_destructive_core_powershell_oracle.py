
from __future__ import annotations

import json
import os

import pytest

from coordinator_core.bash_guards import dispatch


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


class TestEightRowOracle:

    @pytest.mark.parametrize("tool_name", _TOOL_NAMES)
    def test_rm_rf_dot_git_denies(self, repo_cwd, tool_name) -> None:
        assert _denies(_SPELLINGS[0], tool_name, repo_cwd)

    @pytest.mark.parametrize("tool_name", _TOOL_NAMES)
    @pytest.mark.parametrize("command", _SPELLINGS[1:])
    def test_powershell_spelling_denies(self, repo_cwd, command, tool_name) -> None:
        assert _denies(command, tool_name, repo_cwd), (
            "%r under tool_name=%r must deny post-C3 -- pre-C3 this was "
            "one of the seven silent rows the plan's own oracle table "
            "names" % (command, tool_name)
        )


class TestVocabularyDetail:

    @pytest.mark.parametrize("tool_name", _TOOL_NAMES)
    def test_abbreviated_flags_deny(self, repo_cwd, tool_name) -> None:
        assert _denies("Remove-Item -r -fo .git", tool_name, repo_cwd)

    @pytest.mark.parametrize("tool_name", _TOOL_NAMES)
    def test_rd_alias_denies(self, repo_cwd, tool_name) -> None:
        assert _denies("rd -Recurse -Force .git", tool_name, repo_cwd)

    @pytest.mark.parametrize("tool_name", _TOOL_NAMES)
    def test_del_alias_denies(self, repo_cwd, tool_name) -> None:
        assert _denies("del -Recurse -Force .git", tool_name, repo_cwd)

    @pytest.mark.parametrize("tool_name", _TOOL_NAMES)
    def test_erase_alias_denies(self, repo_cwd, tool_name) -> None:
        assert _denies("erase -Recurse -Force .git", tool_name, repo_cwd)

    def test_ordinary_powershell_file_removal_still_allowed(self, repo_cwd) -> None:
        assert not _denies(
            "Remove-Item -Recurse -Force ./scratch-not-a-real-path-oracle-probe",
            "PowerShell",
            repo_cwd,
        )

    def test_unparseable_powershell_does_not_deny(self, repo_cwd) -> None:
        """AC4: a PowerShell command the tree-sitter-pwsh parser cannot
        tokenize must yield silence, never a deny -- fail-closed-on-
        unparseable is a BASH-leg-only property (see `_dialect.py`'s own
        "THE CRITICAL PROPERTY IS FAIL DIRECTION")."""
        assert not _denies("Remove-Item -Recurse -Force @'", "PowerShell", repo_cwd)


class TestBashLegUnchanged:

    def test_bash_rm_of_ordinary_file_untouched(self, tmp_path, repo_cwd) -> None:
        scratch = tmp_path / "scratch-oracle-file.txt"
        scratch.write_text("scratch")
        assert not _denies("rm %s" % scratch, "Bash", repo_cwd)


class TestDestructiveGitOrphanForcePush:

    def test_plain_force_push_denies_under_both_dialects(self, repo_cwd) -> None:
        cmd = "git push --force origin main"
        assert _denies(cmd, "Bash", repo_cwd)
        assert _denies(cmd, "PowerShell", repo_cwd)

    def test_backtick_escaped_git_denies_under_both_dialects(self, repo_cwd) -> None:
        cmd = "g`it push --force origin main"
        assert _denies(cmd, "PowerShell", repo_cwd)
        assert not _denies(cmd, "Bash", repo_cwd)

    def test_unparseable_powershell_does_not_deny(self, repo_cwd) -> None:
        assert not _denies("g`it push --force origin main @'", "PowerShell", repo_cwd)
        assert not _denies("g`it push --force origin main @'", "Bash", repo_cwd)


@pytest.mark.real_home
class TestBlanketGitAdd:

    def test_backtick_escaped_git_add_denies_on_the_powershell_leg_only(self, repo_cwd) -> None:
        cmd = "g`it add -A"
        assert _denies(cmd, "PowerShell", repo_cwd)
        assert not _denies(cmd, "Bash", repo_cwd)

    def test_plain_git_add_dash_a_denies_under_both_dialects(self, repo_cwd) -> None:
        cmd = "git add -A"
        assert _denies(cmd, "Bash", repo_cwd)
        assert _denies(cmd, "PowerShell", repo_cwd)

    def test_unparseable_powershell_does_not_deny(self, repo_cwd) -> None:
        assert not _denies("g`it add -A @'", "PowerShell", repo_cwd)
        assert not _denies("g`it add -A @'", "Bash", repo_cwd)
