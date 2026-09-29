from __future__ import annotations

from coordinator_core.hooks import postusefailure_cross_repo_memo_remediate as m


def test_exit_127_cross_repo_memo_emits_remediation_advisory():
    result = m._handler(
        {
            "tool_name": "Bash",
            "error": "Exit code 127\nzsh: command not found: cross-repo-memo",
            "tool_input": {"command": "cross-repo-memo send x"},
        }
    )
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert result["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"
    assert "cross-repo-memo" in ctx
    assert "exit 127" in ctx


def test_non_matching_failure_is_a_silent_pass():
    assert m._handler(
        {"tool_name": "Bash", "error": "Exit code 1", "tool_input": {"command": "ls"}}
    ) == {}
