
from __future__ import annotations

from typing import Any, Dict

import pytest

from coordinator_core.bash_guards import (
    block_reviewer_bash_outside_allowlist as guard,
)


@pytest.fixture(autouse=True)
def _stub_roster_absence_leg(monkeypatch):
    monkeypatch.setattr(guard, "is_confined_by_roster_absence", lambda effective_type: False)

# SSOT confined-findings-agent type string, per _helpers._CONFINED_FINDINGS_AGENTS.
_CONFINED_TYPE = "coordinator:code-reviewer"
_NON_CONFINED_TYPE = "coordinator:enricher"

_CLAUDE_KLABAUTER_ABS_PATH = "/x/claude-klabauter/coordinator/bin/coordinator-doc-new.py"


def _payload(
    command, agent_id="deadbeef0123", agent_type=None, session_id="sess1", tool_name="Bash"
):
    p = {
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "session_id": session_id,
        "cwd": None,
    }
    if agent_id is not None:
        p["agent_id"] = agent_id
    if agent_type is not None:
        p["agent_type"] = agent_type
    return p


def _confine(monkeypatch, subagent_type=_CONFINED_TYPE):
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        guard,
        "_read_backpointer_subagent_type",
        lambda git_root, agent_id, **kw: subagent_type,
    )


def test_non_bash_tool_allows():
    payload = {"tool_name": "Write", "tool_input": {"command": "rm -rf /"}}
    assert guard.check(payload) is None


def test_no_agent_id_allows():
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "rm -rf /"},
        "session_id": "sess1",
    }
    assert guard.check(payload) is None


def test_non_confined_agent_type_allows(monkeypatch):
    _confine(monkeypatch, subagent_type="")
    payload = _payload("rm -rf /", agent_type=_NON_CONFINED_TYPE)
    assert guard.check(payload) is None


def test_confined_absolute_path_invocation_allows(monkeypatch):
    _confine(monkeypatch)
    cmd = f"{_CLAUDE_KLABAUTER_ABS_PATH} --type review-findings --plan p.md"
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    assert guard.check(payload) is None


def test_confined_python3_prefixed_invocation_allows(monkeypatch):
    _confine(monkeypatch)
    cmd = f"python3 {_CLAUDE_KLABAUTER_ABS_PATH} --type review-findings"
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    assert guard.check(payload) is None


def test_confined_legacy_bareword_invocation_still_allows(monkeypatch):
    _confine(monkeypatch)
    payload = _payload(
        "coordinator-doc-new --type review-findings", agent_type=_CONFINED_TYPE
    )
    assert guard.check(payload) is None


def test_confined_type_arg_at_end_of_command_allows(monkeypatch):
    # Exercises the _REQUIRED_TYPE_ARG_END branch (cmd.endswith(...)),
    # distinct from the _REQUIRED_TYPE_ARG_MID substring branch.
    _confine(monkeypatch)
    payload = _payload(
        f"{_CLAUDE_KLABAUTER_ABS_PATH} --type review-findings", agent_type=_CONFINED_TYPE
    )
    assert guard.check(payload) is None


def test_bash_prefixed_invocation_now_denies(monkeypatch):
    _confine(monkeypatch)
    cmd = f"bash {_CLAUDE_KLABAUTER_ABS_PATH} --type review-findings"
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "bash" in reason
    assert _CLAUDE_KLABAUTER_ABS_PATH in reason


def test_metacharacter_command_denies(monkeypatch):
    _confine(monkeypatch)
    cmd = f"{_CLAUDE_KLABAUTER_ABS_PATH} --type review-findings; rm -rf /"
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "shell-chaining metacharacter"
        in result["hookSpecificOutput"]["permissionDecisionReason"]
    )


def test_missing_type_arg_denies(monkeypatch):
    _confine(monkeypatch)
    payload = _payload(_CLAUDE_KLABAUTER_ABS_PATH, agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "--type review-findings"
        in result["hookSpecificOutput"]["permissionDecisionReason"]
    )


def test_word_boundary_violation_denies(monkeypatch):
    _confine(monkeypatch)
    cmd = f"{_CLAUDE_KLABAUTER_ABS_PATH} --type review-findingsXYZ"
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_empty_command_denies(monkeypatch):
    _confine(monkeypatch)
    payload = _payload("", agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "command could not be parsed"
        in result["hookSpecificOutput"]["permissionDecisionReason"]
    )


def test_arbitrary_command_denies(monkeypatch):
    _confine(monkeypatch)
    payload = _payload("rm -rf /", agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_python3_prefixed_lookalike_suffix_denies(monkeypatch):
    _confine(monkeypatch)
    cmd = "python3 evil-coordinator-doc-new --type review-findings"
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "evil-coordinator-doc-new" in reason


def test_unprefixed_lookalike_suffix_denies(monkeypatch):
    _confine(monkeypatch)
    cmd = "evil-coordinator-doc-new --type review-findings"
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_windows_path_prefixed_invocation_allows(monkeypatch):
    _confine(monkeypatch)
    cmd = r"C:\claude-klabauter\coordinator\bin\coordinator-doc-new --type review-findings"
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    assert guard.check(payload) is None


def test_bin_relative_path_invocation_allows(monkeypatch):
    _confine(monkeypatch)
    cmd = "bin/coordinator-doc-new --type review-findings"
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    assert guard.check(payload) is None


def test_confined_quoted_absolute_path_invocation_allows(monkeypatch):
    _confine(monkeypatch)
    cmd = f'"{_CLAUDE_KLABAUTER_ABS_PATH}" --type review-findings'
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    assert guard.check(payload) is None


def test_confined_quoted_var_expanded_path_invocation_allows(monkeypatch):
    _confine(monkeypatch)
    cmd = (
        '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}'
        '/bin/coordinator-doc-new" --type review-findings'
    )
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    assert guard.check(payload) is None


def test_confined_python3_quoted_path_invocation_allows(monkeypatch):
    _confine(monkeypatch)
    cmd = f'python3 "{_CLAUDE_KLABAUTER_ABS_PATH}" --type review-findings'
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    assert guard.check(payload) is None


def test_confined_single_quoted_path_invocation_allows(monkeypatch):
    _confine(monkeypatch)
    cmd = f"'{_CLAUDE_KLABAUTER_ABS_PATH}' --type review-findings"
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    assert guard.check(payload) is None


def test_quoted_non_allowlisted_binary_denies(monkeypatch):
    _confine(monkeypatch)
    cmd = '"/usr/bin/whoami" -la'
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_quoted_metacharacter_injection_still_denies(monkeypatch):
    _confine(monkeypatch)
    cmd = '"; rm -rf" --type review-findings'
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "not coordinator-doc-new"
        in result["hookSpecificOutput"]["permissionDecisionReason"]
    )


def test_grep_double_quoted_pipe_alternation_allows(monkeypatch):
    _allow('grep -n "A|B" file', monkeypatch)


def test_git_log_single_quoted_format_pipe_allows(monkeypatch):
    _allow("git log --format='%h|%s'", monkeypatch)


def test_grep_double_quoted_semicolon_allows(monkeypatch):
    _allow('grep -c ";" file', monkeypatch)


def test_grep_unquoted_pipe_alternation_denies(monkeypatch):
    result = _deny("grep -n A|B file", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_grep_escaped_unquoted_redirect_allows(monkeypatch):
    _allow(r"grep foo \> bar", monkeypatch)


def test_grep_command_substitution_inside_double_quotes_denies(monkeypatch):
    result = _deny('grep "$(whoami)" file', monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_grep_backtick_inside_single_quotes_allows(monkeypatch):
    _allow("grep 'a`b' file", monkeypatch)


def test_unterminated_double_quote_denies_fail_closed(monkeypatch):
    result = _deny('grep foo "unterminated', monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "unbalanced/unterminated quote" in reason


def test_unterminated_single_quote_denies_fail_closed(monkeypatch):
    result = _deny("grep foo 'unterminated", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "unbalanced/unterminated quote" in reason


def test_newline_in_command_denies_regardless_of_quoting(monkeypatch):
    result = _deny('grep "foo\nbar" file', monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_nested_mixed_quoting_concatenation_allows(monkeypatch):
    _allow('grep a"|"b file', monkeypatch)


def test_quote_adjacent_unquoted_pipe_denies(monkeypatch):
    result = _deny("grep 'a'|'b' file", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_quote_adjacent_unquoted_pipe_after_double_quote_denies(monkeypatch):
    result = _deny('grep "a"|b file', monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_ansi_c_quoting_escaped_apostrophe_denies_pinned_limitation(monkeypatch):
    result = _deny(r"grep $'a\'; rm -rf /'", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_backslash_newline_line_continuation_denies_unconditionally(monkeypatch):
    result = _deny("grep foo \\\nbar", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_trailing_lone_unquoted_backslash_denies_fail_closed(monkeypatch):
    result = _deny("grep foo \\", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "unquoted backslash" in reason


def test_unterminated_double_quote_names_offset_and_offers_corrected_command(monkeypatch):
    result = _deny('grep foo "unterminated', monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "unbalanced/unterminated quote" in reason
    assert "unmatched \" opens at character 9" in reason
    assert "'grep foo \"unterminated\"'" in reason


def test_unterminated_single_quote_names_offset_and_offers_corrected_command(monkeypatch):
    result = _deny("grep foo 'unterminated", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "unbalanced/unterminated quote" in reason
    assert "unmatched ' opens at character 9" in reason
    assert '"grep foo \'unterminated\'"' in reason


def test_unterminated_quote_corrected_command_actually_tokenizes(monkeypatch):
    from coordinator_core.bash_guards._command_tokenizer import tokenize_full_command

    original = 'grep foo "unterminated'
    assert tokenize_full_command(original) is None
    corrected = original + '"'
    assert tokenize_full_command(corrected) == ["grep", "foo", "unterminated"]


def test_unterminated_quote_corrected_command_survives_200_char_truncation(monkeypatch):
    long_prefix = "grep " + ("x" * 250) + " \"unterminated"
    result = _deny(long_prefix, monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "unbalanced/unterminated quote" in reason
    assert '..."\'' in reason, (
        "corrected command lost its closing quote to truncation: %r" % reason
    )


def test_trailing_backslash_offers_corrected_command(monkeypatch):
    result = _deny("grep foo \\", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "unquoted backslash" in reason
    assert "'grep foo '" in reason


def test_trailing_backslash_corrected_command_actually_tokenizes(monkeypatch):
    from coordinator_core.bash_guards._command_tokenizer import tokenize_full_command

    original = "grep foo \\"
    assert tokenize_full_command(original) is None
    corrected = original[:-1]
    assert tokenize_full_command(corrected) == ["grep", "foo"]


def test_empty_command_deny_reason_names_the_fix(monkeypatch):
    _confine(monkeypatch)
    payload = _payload("", agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "could not be parsed" in reason
    assert "Resend the Bash call with a non-empty command" in reason


def test_deny_reason_offers_resolved_absolute_path_form(monkeypatch):
    _confine(monkeypatch)
    payload = _payload("curl https://evil.example/x", agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "<claude-klabauter-live-root>/coordinator/bin/coordinator-doc-new.py" in reason
    assert "python3 <claude-klabauter-live-root>/coordinator/bin/coordinator-doc-new.py" in reason
    assert "  bin/coordinator-doc-new --type review-findings ...\n" not in reason
    assert "bash /abs/path/to/coordinator-doc-new" not in reason
    assert "  coordinator-doc-new --type review-findings ...\n" not in reason


def _allow(cmd: str, monkeypatch) -> None:
    _confine(monkeypatch)
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    assert guard.check(payload) is None, f"expected allow for: {cmd!r}"


def _deny(cmd: str, monkeypatch) -> Dict[str, Any]:
    _confine(monkeypatch)
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None, f"expected deny for: {cmd!r}"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    return result


def test_git_show_sha_allows(monkeypatch):
    _allow("git show abc1234", monkeypatch)


def test_git_diff_head_allows(monkeypatch):
    _allow("git diff HEAD~1", monkeypatch)


def test_git_log_oneline_allows(monkeypatch):
    _allow("git log --oneline -5", monkeypatch)


def test_git_show_stat_sha_allows(monkeypatch):
    _allow("git show --stat abc1234", monkeypatch)


def test_ls_directory_allows(monkeypatch):
    _allow("ls coordinator_core", monkeypatch)


def test_cat_file_allows(monkeypatch):
    _allow("cat pyproject.toml", monkeypatch)


def test_find_name_pattern_allows(monkeypatch):
    _allow("find . -name '*.py'", monkeypatch)


def test_git_commit_denies(monkeypatch):
    _deny("git commit -m x", monkeypatch)


def test_git_push_denies(monkeypatch):
    _deny("git push", monkeypatch)


def test_git_add_denies(monkeypatch):
    _deny("git add .", monkeypatch)


def test_git_checkout_branch_denies(monkeypatch):
    _deny("git checkout -b y", monkeypatch)


def test_git_dash_c_commit_denies(monkeypatch):
    _deny("git -C /tmp commit -m x", monkeypatch)


def test_git_config_denies(monkeypatch):
    _deny("git config user.name x", monkeypatch)


def test_git_stash_denies(monkeypatch):
    _deny("git stash", monkeypatch)


def test_find_delete_denies(monkeypatch):
    _deny("find . -delete", monkeypatch)


def test_find_exec_denies(monkeypatch):
    _deny("find . -exec rm {} ;", monkeypatch)


def test_git_show_piped_to_tee_denies_metacharacter(monkeypatch):
    result = _deny("git show abc1234 | tee out", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_git_diff_redirected_denies_metacharacter(monkeypatch):
    result = _deny("git diff > /tmp/d", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_cat_and_rm_chained_denies_metacharacter(monkeypatch):
    result = _deny("cat x && rm y", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_ls_semicolon_git_push_denies_metacharacter(monkeypatch):
    result = _deny("ls; git push", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_rm_rf_unknown_binary_denies(monkeypatch):
    _deny("rm -rf /", monkeypatch)


def test_curl_unknown_binary_denies(monkeypatch):
    _deny("curl https://evil.example/x", monkeypatch)


def test_python3_script_unknown_binary_denies(monkeypatch):
    _deny("python3 foo.py", monkeypatch)


def test_git_suffix_bypass_denies(monkeypatch):
    _deny("evil-git show", monkeypatch)


def test_ls_suffix_bypass_denies(monkeypatch):
    _deny("notls", monkeypatch)


def test_grep_recursive_search_allows(monkeypatch):
    _allow('grep -rn "foo" coordinator_core', monkeypatch)


def test_grep_suffix_bypass_denies(monkeypatch):
    _deny("grepfoo bar", monkeypatch)


def test_grep_redirected_denies_metacharacter(monkeypatch):
    result = _deny("grep foo bar > out.txt", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_git_show_output_equals_denies(monkeypatch):
    result = _deny("git show --output=/tmp/x abc123", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--output=/tmp/x" in reason


def test_git_show_output_space_denies(monkeypatch):
    result = _deny("git show --output /tmp/x abc123", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--output" in reason


def test_git_log_output_equals_denies(monkeypatch):
    _deny("git log --output=/tmp/x", monkeypatch)


def test_git_diff_output_equals_denies(monkeypatch):
    _deny("git diff --output=/tmp/x", monkeypatch)


def test_git_show_dash_o_attached_denies(monkeypatch):
    result = _deny("git show -o/tmp/x abc123", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "-o/tmp/x" in reason


def test_git_show_dash_o_space_denies(monkeypatch):
    result = _deny("git show -o /tmp/x abc123", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "-o" in reason


def test_git_show_ext_diff_denies(monkeypatch):
    result = _deny("git show --ext-diff HEAD", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--ext-diff" in reason


def test_git_show_output_attached_no_equals_denies(monkeypatch):
    result = _deny("git show --output/tmp/x abc", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--output/tmp/x" in reason


def test_git_log_output_attached_no_equals_denies(monkeypatch):
    _deny("git log --output/tmp/x", monkeypatch)


def test_git_diff_output_attached_no_equals_denies(monkeypatch):
    _deny("git diff --output/tmp/x", monkeypatch)


def test_git_dash_c_core_pager_denies(monkeypatch):
    result = _deny("git -c core.pager=evil log", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "-c" in reason


def test_git_dash_c_diff_command_denies(monkeypatch):
    result = _deny("git -c diff.x.command=evil show HEAD", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "-c" in reason


def test_git_exec_path_equals_denies(monkeypatch):
    result = _deny("git --exec-path=/tmp/evil show HEAD", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--exec-path" in reason


def test_git_exec_path_space_denies(monkeypatch):
    result = _deny("git --exec-path /tmp/evil show HEAD", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--exec-path" in reason


def test_git_paginate_denies(monkeypatch):
    result = _deny("git --paginate log", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--paginate" in reason


def test_git_dash_p_global_denies(monkeypatch):
    result = _deny("git -p log", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "-p" in reason


def test_git_namespace_denies(monkeypatch):
    result = _deny("git --namespace foo show", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--namespace" in reason


def test_git_config_env_denies(monkeypatch):
    result = _deny("git --config-env=x=y log", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--config-env" in reason


def test_git_log_dash_p_subcommand_position_allows(monkeypatch):
    _allow("git log -p", monkeypatch)


def test_git_log_dash_p_dash_5_allows(monkeypatch):
    _allow("git log -p -5", monkeypatch)


def test_git_show_stat_allows(monkeypatch):
    _allow("git show --stat abc1234", monkeypatch)


def test_git_diff_name_only_allows(monkeypatch):
    _allow("git diff --name-only HEAD~1", monkeypatch)


def test_git_log_oneline_dash_5_allows(monkeypatch):
    _allow("git log --oneline -5", monkeypatch)


def test_git_log_format_allows(monkeypatch):
    _allow("git log --format=%H", monkeypatch)


def test_git_show_output_indicator_new_allows(monkeypatch):
    _allow("git show --output-indicator-new=X abc1234", monkeypatch)


def test_git_show_output_indicator_old_allows(monkeypatch):
    _allow("git show --output-indicator-old=X abc1234", monkeypatch)


def test_git_show_output_indicator_context_allows(monkeypatch):
    _allow("git show --output-indicator-context=X abc1234", monkeypatch)


def test_git_dash_capital_c_path_show_allows(monkeypatch):
    _allow("git -C /some/path show abc1234", monkeypatch)


def test_git_no_pager_log_allows(monkeypatch):
    _allow("git --no-pager log", monkeypatch)


def test_git_no_optional_locks_log_allows(monkeypatch):
    _allow("git --no-optional-locks log --oneline -1", monkeypatch)


def test_git_no_optional_locks_status_allows(monkeypatch):
    _allow("git --no-optional-locks status", monkeypatch)


def test_git_dash_c_config_injection_still_denies(monkeypatch):
    _deny("git -c core.pager=evil log", monkeypatch)


def test_git_dir_equals_status_allows(monkeypatch):
    _allow("git --git-dir=/x/.git status", monkeypatch)


def test_git_log_double_dash_output_pathspec_allows(monkeypatch):
    _allow("git log -- --output=weird-filename", monkeypatch)


# surface -- ADDITIVE ONLY, no existing assertion touched. Each block below


def test_git_status_bare_allows(monkeypatch):
    _allow("git status", monkeypatch)


def test_git_blame_allows(monkeypatch):
    _allow("git blame file.py", monkeypatch)


def test_git_ls_files_allows(monkeypatch):
    _allow("git ls-files", monkeypatch)


def test_git_rev_parse_allows(monkeypatch):
    _allow("git rev-parse HEAD", monkeypatch)


def test_git_describe_allows(monkeypatch):
    _allow("git describe --tags", monkeypatch)


# direct allow coverage; --work-tree, also in _GIT_VALUE_TAKING_OPTIONS,


def test_git_work_tree_equals_allows(monkeypatch):
    _allow("git --work-tree=/x/wt status", monkeypatch)


def test_git_work_tree_space_form_allows(monkeypatch):
    _allow("git --work-tree /x/wt status", monkeypatch)


def test_head_file_allows(monkeypatch):
    _allow("head -n 20 pyproject.toml", monkeypatch)


def test_tail_file_allows(monkeypatch):
    _allow("tail -n 20 pyproject.toml", monkeypatch)


def test_wc_file_allows(monkeypatch):
    _allow("wc -l pyproject.toml", monkeypatch)


def test_file_binary_allows(monkeypatch):
    _allow(
        "file coordinator_core/bash_guards/block_reviewer_bash_outside_allowlist.py",
        monkeypatch,
    )


def test_stat_file_allows(monkeypatch):
    _allow("stat pyproject.toml", monkeypatch)


# ---- Gap: 4 of the 9 metacharacters had no UNQUOTED-deny test at all


def test_double_pipe_unquoted_denies(monkeypatch):
    result = _deny("git status || rm -rf /", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_bare_unquoted_backtick_denies(monkeypatch):
    result = _deny("cat `whoami`", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_bare_unquoted_command_substitution_denies(monkeypatch):
    result = _deny("cat $(whoami)", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_unquoted_less_than_redirect_denies(monkeypatch):
    result = _deny("cat < /etc/passwd", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_bare_unquoted_ampersand_denies(monkeypatch):
    result = _deny("cat pyproject.toml &", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


# ---- Gap: the OR-resolver's SECONDARY leg (subagent_type via the
# top-level agent_type -- carries the confined _CONFINED_FINDINGS_AGENTS


def test_secondary_leg_backpointer_confinement_denies(monkeypatch):
    _confine(monkeypatch, subagent_type=_CONFINED_TYPE)
    payload = _payload("rm -rf /", agent_type=_NON_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


# top-level UNQUOTED pipe was an unconditional deny even when every segment


def test_git_show_piped_to_wc_allows(monkeypatch):
    _allow("git show abc1234:foo.py | wc -c", monkeypatch)


def test_find_piped_to_wc_allows(monkeypatch):
    _allow('find . -name "*.py" | wc -l', monkeypatch)


def test_cat_piped_to_git_log_allows(monkeypatch):
    _allow("cat pyproject.toml | git log --format='%h'", monkeypatch)


def test_git_show_piped_to_tee_denies_non_allowlisted_segment(monkeypatch):
    result = _deny("git show abc1234 | tee out", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "pipeline segment" in reason
    assert "'tee out'" in reason


def test_git_show_piped_to_rm_denies_non_allowlisted_segment(monkeypatch):
    result = _deny("git show abc1234 | rm -rf /", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "pipeline segment" in reason
    assert "'rm -rf /'" in reason


def test_double_pipe_still_denies_unconditionally(monkeypatch):
    result = _deny("git status || rm -rf /", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_grep_stderr_redirect_to_devnull_allows(monkeypatch):
    _allow("grep foo bar.py 2>/dev/null", monkeypatch)


def test_grep_stdout_redirect_to_devnull_allows(monkeypatch):
    _allow("grep foo bar.py >/dev/null", monkeypatch)


def test_grep_spaced_redirect_to_devnull_allows(monkeypatch):
    _allow("grep foo bar.py > /dev/null", monkeypatch)


def test_git_diff_redirect_to_real_path_still_denies(monkeypatch):
    result = _deny("git diff > /tmp/d", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_devnull_redirect_then_chained_command_still_denies(monkeypatch):
    result = _deny("echo hi > /dev/null; git push", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_stderr_to_stdout_redirect_allows(monkeypatch):
    _allow("grep foo bar.py 2>&1", monkeypatch)


def test_stdout_to_stderr_redirect_allows(monkeypatch):
    _allow("grep foo bar.py 1>&2", monkeypatch)


def test_stderr_to_stdout_redirect_then_pipe_allows(monkeypatch):
    _allow("grep foo bar.py 2>&1 | wc -l", monkeypatch)


def test_bare_ampersand_backgrounding_still_denies(monkeypatch):
    result = _deny("git status &", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_double_ampersand_chaining_still_denies(monkeypatch):
    result = _deny("git status && git push", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_semicolon_still_denies(monkeypatch):
    result = _deny("git status; git push", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_pipe_to_non_allowlisted_still_denies(monkeypatch):
    result = _deny("git show HEAD | rm -rf /", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "pipeline segment" in reason


def test_redirect_to_real_file_still_denies(monkeypatch):
    result = _deny("git diff > /tmp/evil", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_general_ampersand_digit_not_exact_fd_dup_token_still_denies(monkeypatch):
    # complement in `_FD_DUP_COMPLEMENT`, so this must still fall through to
    result = _deny("git diff 3>&1", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_ampersand_digit_no_redirect_prefix_still_denies(monkeypatch):
    result = _deny("git diff &2", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_command_substitution_in_pipeline_segment_still_denies(monkeypatch):
    result = _deny("git show HEAD $(whoami) | wc -c", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_staging_commit_push_still_deny_with_pipe_present(monkeypatch):
    _deny("git add . | git commit -m x", monkeypatch)
    _deny("git push origin main | cat", monkeypatch)


def test_double_quoted_output_flag_denies(monkeypatch):
    _deny('git show "--output=/tmp/evil" HEAD', monkeypatch)


def test_single_quoted_output_flag_denies(monkeypatch):
    _deny("git show '--output=/tmp/evil' HEAD", monkeypatch)


def test_single_quoted_ext_diff_flag_denies(monkeypatch):
    _deny("git show '--ext-diff' HEAD", monkeypatch)


def test_double_quoted_dash_o_short_flag_denies(monkeypatch):
    _deny('git log "-o/tmp/evil"', monkeypatch)


def test_quoted_c_global_option_denies(monkeypatch):
    _deny('git "-c" "core.pager=evil" log', monkeypatch)


def test_quoted_exec_path_global_option_denies(monkeypatch):
    _deny('git "--exec-path=/tmp/evil" show HEAD', monkeypatch)


def test_quoted_find_exec_flag_denies(monkeypatch):
    _deny('find . -type f "-exec" rm -rf {} +', monkeypatch)


def test_unquoted_output_flag_still_denies(monkeypatch):
    _deny("git show --output=/tmp/evil HEAD", monkeypatch)


def test_quoted_write_flag_in_pipeline_segment_denies(monkeypatch):
    _deny('git show "--output=/tmp/evil" HEAD | wc -c', monkeypatch)


def test_quoted_legitimate_arg_still_allows(monkeypatch):
    _allow('git show HEAD -- "some file.py"', monkeypatch)


def test_quoted_output_indicator_flag_still_allows(monkeypatch):
    _allow('git log "--output-indicator-new=+"', monkeypatch)


def test_windows_spaced_username_git_exe_allows(monkeypatch):
    cmd = r"C:\Users\John Doe\Git\bin\git.exe show HEAD"
    _allow(cmd, monkeypatch)


def test_windows_spaced_username_coordinator_doc_new_cmd_allows(monkeypatch):
    cmd = (
        r"C:\Users\John Doe\.coordinator-claude-settings\bin\coordinator-doc-new.cmd"
        " --type review-findings"
    )
    _allow(cmd, monkeypatch)


def test_windows_spaced_username_coordinator_doc_new_py_allows(monkeypatch):
    cmd = (
        r"C:\Users\John Doe\.coordinator-claude-settings\bin\coordinator-doc-new.py"
        " --type review-findings"
    )
    _allow(cmd, monkeypatch)


def test_windows_quoted_spaced_username_git_allows(monkeypatch):
    cmd = r'"C:\Users\John Doe\Git\bin\git.exe" show HEAD'
    _allow(cmd, monkeypatch)


def test_windows_plain_backslash_git_no_space_allows(monkeypatch):
    cmd = r"C:\Git\bin\git.exe show HEAD"
    _allow(cmd, monkeypatch)


def test_windows_evil_lookalike_directory_still_denies(monkeypatch):
    # CONTAINS "git" as a substring, with no separator immediately before
    cmd = r"C:\Users\evilgit\tool.exe show HEAD"
    _deny(cmd, monkeypatch)


# against the pre-Amendment-2 module, per _CONFINED_TYPE ==


def test_confined_python3_dash_m_pytest_now_allows(monkeypatch):
    _allow("python3 -m pytest -q", monkeypatch)


def test_confined_python3_dash_m_pytest_with_stderr_redirect_allows(monkeypatch):
    _allow("python3 -m pytest -q 2>&1", monkeypatch)


def test_confined_python3_dash_c_inline_code_still_denies(monkeypatch):
    # (_PY_INLINE_CODE_FLAGS is a bare module constant, not a ruleset
    result = _deny('python3 -c "import os; os.system(\'rm -rf /\')"', monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "-c" in reason


def test_confined_python3_dash_e_inline_code_still_denies(monkeypatch):
    _deny('python3 -e "print(1)"', monkeypatch)


def test_confined_python3_dash_m_unlisted_module_still_denies(monkeypatch):
    result = _deny("python3 -m http.server", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "http.server" in reason


def test_confined_python3_script_path_still_denies(monkeypatch):
    _deny("python3 myscript.py", monkeypatch)


def test_python3_script_path_deny_reason_names_both_tokens(monkeypatch):
    result = _deny('python3 "myscript.py"', monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "invoked via python3" in reason
    assert "myscript.py" in reason
    assert "not coordinator-doc-new (got: myscript.py)" not in reason


def test_python3_quoted_script_path_with_spaces_deny_reason_names_both_tokens(monkeypatch):
    cmd = 'python3 "C:/Users/example/scratch dir/opt34.py"'
    result = _deny(cmd, monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "invoked via python3" in reason
    assert "C:/Users/example/scratch dir/opt34.py" in reason


def test_python3_backslash_separator_script_path_deny_reason_names_both_tokens(monkeypatch):
    cmd = r"python3 C:\Users\example\scratch\opt34.py"
    result = _deny(cmd, monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "invoked via python3" in reason


def test_curl_deny_reason_unchanged_when_tokens_coincide(monkeypatch):
    result = _deny("curl https://evil.example/x", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "not coordinator-doc-new (got: curl)" in reason
    assert "invoked via" not in reason


def test_tokenizer_extracts_python3_as_raw_first_token(monkeypatch):
    quoted_with_spaces = guard._tokenize_segment(
        'python3 "C:/path/with spaces/z.py"'
    )
    assert quoted_with_spaces[0] == "python3"

    backslash_separator = guard._tokenize_segment(r"python3 C:\path\to\z.py")
    assert backslash_separator[0] == "python3"


# The eight probe commands below are copied VERBATIM from the verdict record
# sole _CONFINED_FINDINGS_AGENTS member) via the existing _confine/_payload


def _allow_dialect(cmd: str, tool_name: str, monkeypatch) -> None:
    _confine(monkeypatch)
    payload = _payload(cmd, agent_type=_CONFINED_TYPE, tool_name=tool_name)
    assert guard.check(payload) is None, f"expected allow for [{tool_name}]: {cmd!r}"


def _deny_dialect(cmd: str, tool_name: str, monkeypatch) -> Dict[str, Any]:
    _confine(monkeypatch)
    payload = _payload(cmd, agent_type=_CONFINED_TYPE, tool_name=tool_name)
    result = guard.check(payload)
    assert result is not None, f"expected deny for [{tool_name}]: {cmd!r}"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    return result


def test_probe_git_status_porcelain_allows_bash(monkeypatch):
    _allow_dialect("git status --porcelain", "Bash", monkeypatch)


def test_probe_git_status_porcelain_allows_powershell(monkeypatch):
    _allow_dialect("git status --porcelain", "PowerShell", monkeypatch)


def test_probe_rg_denies_bash(monkeypatch):
    _deny_dialect("rg -n 'pattern' .", "Bash", monkeypatch)


def test_probe_rg_denies_powershell(monkeypatch):
    _deny_dialect("rg -n 'pattern' .", "PowerShell", monkeypatch)


def test_probe_curl_denies_bash(monkeypatch):
    _deny_dialect("curl https://example.com", "Bash", monkeypatch)


def test_probe_curl_denies_powershell(monkeypatch):
    _deny_dialect("curl https://example.com", "PowerShell", monkeypatch)


def test_probe_get_childitem_denies_bash(monkeypatch):
    _deny_dialect("Get-ChildItem -Recurse", "Bash", monkeypatch)


def test_probe_get_childitem_allows_powershell(monkeypatch):
    _allow_dialect("Get-ChildItem -Recurse", "PowerShell", monkeypatch)


def test_probe_select_string_denies_bash(monkeypatch):
    _deny_dialect("Select-String -Pattern 'foo' -Path *.py", "Bash", monkeypatch)


def test_probe_select_string_allows_powershell(monkeypatch):
    _allow_dialect("Select-String -Pattern 'foo' -Path *.py", "PowerShell", monkeypatch)


def test_probe_get_content_denies_bash(monkeypatch):
    _deny_dialect("Get-Content README.md", "Bash", monkeypatch)


def test_probe_get_content_allows_powershell(monkeypatch):
    _allow_dialect("Get-Content README.md", "PowerShell", monkeypatch)


def test_probe_gci_alias_denies_bash(monkeypatch):
    _deny_dialect("gci -Recurse", "Bash", monkeypatch)


def test_probe_gci_alias_allows_powershell(monkeypatch):
    _allow_dialect("gci -Recurse", "PowerShell", monkeypatch)


def test_probe_pipeline_where_object_denies_bash(monkeypatch):
    _deny_dialect(
        "Get-ChildItem | Where-Object { $_.Length -gt 100 }", "Bash", monkeypatch
    )


def test_probe_pipeline_where_object_allows_powershell(monkeypatch):
    _allow_dialect(
        "Get-ChildItem | Where-Object { $_.Length -gt 100 }", "PowerShell", monkeypatch
    )


# valid as a NON-FIRST pipeline segment (see

def test_bare_where_object_denies_powershell(monkeypatch):
    _deny_dialect("Where-Object { $_.Length -gt 100 }", "PowerShell", monkeypatch)


def test_remove_item_still_denies_powershell(monkeypatch):
    _deny_dialect("Remove-Item -Recurse -Force /tmp/x", "PowerShell", monkeypatch)


def test_unrecognised_tool_name_allows(monkeypatch):
    _confine(monkeypatch)
    payload = _payload("Get-ChildItem -Recurse", agent_type=_CONFINED_TYPE, tool_name="Zsh")
    assert guard.check(payload) is None


# file's `_CONFINED_TYPE`) already holds `interpreter_allowed_modules:


def test_bare_python_dash_m_pytest_gets_specific_remedy(monkeypatch):
    result = _deny("python -m pytest -q", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "python3 -m pytest -q" in reason
    assert "not coordinator-doc-new" not in reason


def test_bare_python_dash_m_pytest_no_dont_retry_clause(monkeypatch):
    result = _deny("python -m pytest -q", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "report the blocker to the dispatching EM rather than retrying" not in reason


def test_out_of_scope_command_keeps_generic_message_and_dont_retry_advice():
    # with _DENY_MESSAGE_STANZA_OVERRIDES, this plan's C1), so an
    reason = guard._deny_reason(
        "coordinator:code-reviewer",
        "curl https://evil.example/x",
        "not coordinator-doc-new (got: curl)",
        suppress_retry_advice=False,
    )
    assert "report the blocker to the dispatching EM rather than retrying it." not in reason


def test_python3_dash_c_inline_code_denial_untouched(monkeypatch):
    result = _deny('python3 -c "import os"', monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "-c" in reason
    assert "python3 -m pytest" not in reason


def test_python3_dash_m_pytest_still_allows_clean(monkeypatch):
    _allow("python3 -m pytest -q", monkeypatch)


def test_python_dash_c_remedy_would_also_deny_no_retry_suggested(monkeypatch):
    result = _deny('python -c "import os"', monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "not coordinator-doc-new (got: python)" in reason
    assert "Retry with" not in reason


def test_py_dash_m_pytest_alias_gets_remedy(monkeypatch):
    result = _deny("py -m pytest -q", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "python3 -m pytest -q" in reason


def test_python_versioned_alias_gets_remedy(monkeypatch):
    result = _deny("python3.11 -m pytest -q", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "python3 -m pytest -q" in reason


def test_deny_reason_suppress_retry_advice_is_a_no_op_on_the_empty_default_closing_stanza():
    # _DEFAULT_CLOSING_STANZA is already empty today (the only non-empty
    # _DENY_MESSAGE_STANZA_OVERRIDES, this plan's C1) -- so both calls below
    args = (
        "coordinator:code-reviewer",
        "python -m pytest -q",
        "first command token is a python interpreter spelling other than "
        "the accepted `python3` -- this command IS in scope, just "
        "misspelled. Retry with: 'python3 -m pytest -q'",
    )
    base = guard._deny_reason(*args, suppress_retry_advice=True)
    assert "report the blocker to the dispatching EM rather than retrying it." not in base
    without_suppress = guard._deny_reason(*args, suppress_retry_advice=False)
    assert "report the blocker to the dispatching EM rather than retrying it." not in without_suppress
    assert base == without_suppress


# SUBSTRATE NOTE (found during this dispatch, not assumed): the dispatch
# brief for this chunk asked for coverage "for both _REVIEWER_TYPE and
# _EXECUTOR_TYPE". As of DR-125 (docs/plans/2026-08-03-narrow-subagent-
# REMOVED from `_helpers._CONFINED_FINDINGS_AGENTS` and is confined by
# exact history. `_EXECUTOR_TYPE`'s `_DEFAULT_RULESET_TYPE_OVERRIDES` entry
# vacuously). Parametrizing this chunk's AC1-AC3 tests over `_EXECUTOR_TYPE`
# scoped to `_REVIEWER_TYPE` (`coordinator:code-reviewer`), the one type this

_C1_PYTHON3_SPELLINGS = (
    ".venv/Scripts/python3.exe",
    "/repo/.venv/bin/python3.exe",
)


def _c1_check(cmd: str, monkeypatch):
    _confine(monkeypatch)
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    return guard.check(payload)


@pytest.mark.parametrize("spelling", _C1_PYTHON3_SPELLINGS)
def test_c1_normalized_python3_spelling_allows_pytest(spelling, monkeypatch):
    result = _c1_check(f"{spelling} -m pytest -q", monkeypatch)
    assert result is None, f"expected allow for: {spelling!r}"


@pytest.mark.parametrize("spelling", _C1_PYTHON3_SPELLINGS)
def test_c1_normalized_python3_spelling_still_denies_inline_code(spelling, monkeypatch):
    result = _c1_check(f'{spelling} -c "import os"', monkeypatch)
    assert result is not None, f"expected deny for: {spelling!r} -c"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "-c" in reason
    assert "inline code" in reason


@pytest.mark.parametrize("spelling", _C1_PYTHON3_SPELLINGS)
def test_c1_normalized_python3_spelling_denies_unallowlisted_module(spelling, monkeypatch):
    result = _c1_check(f"{spelling} -m http.server", monkeypatch)
    assert result is not None, f"expected deny for: {spelling!r} -m http.server"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "http.server" in reason


# PATH-PREFIXED python-family basename (raw token contains `/` or `\` before

_C1B_PATH_PREFIXED_PYTHON_SPELLINGS = (
    ".venv/Scripts/python.exe",
    ".venv/bin/python",
)


@pytest.mark.parametrize("spelling", _C1B_PATH_PREFIXED_PYTHON_SPELLINGS)
def test_c1b_path_prefixed_python_spelling_allows_pytest(spelling, monkeypatch):
    result = _c1_check(f"{spelling} -m pytest -q", monkeypatch)
    assert result is None, f"expected allow for: {spelling!r}"


@pytest.mark.parametrize("spelling", _C1B_PATH_PREFIXED_PYTHON_SPELLINGS)
def test_c1b_path_prefixed_python_spelling_still_denies_inline_code(spelling, monkeypatch):
    for flag in ("-c", "-e"):
        result = _c1_check(f'{spelling} {flag} "import os"', monkeypatch)
        assert result is not None, f"expected deny for: {spelling!r} {flag}"
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert flag in reason
        assert "inline code" in reason


@pytest.mark.parametrize("spelling", _C1B_PATH_PREFIXED_PYTHON_SPELLINGS)
def test_c1b_path_prefixed_python_spelling_denies_unallowlisted_module(spelling, monkeypatch):
    result = _c1_check(f"{spelling} -m http.server", monkeypatch)
    assert result is not None, f"expected deny for: {spelling!r} -m http.server"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "http.server" in reason


def test_c1b_bare_python_still_denies_with_unchanged_remedy(monkeypatch):
    # is a NAME the caller got wrong, not a LOCATION -- it must stay on the
    result = _c1_check("python -m pytest -q", monkeypatch)
    assert result is not None, "expected deny for bare `python -m pytest -q`"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "python3 -m pytest -q" in reason
    assert "misspelled" in reason


def test_c1b_trailing_separator_no_directory_still_denies_as_bare(monkeypatch):
    # chosen LOCATION -- it must NOT take the path-prefixed leg. Same
    result = _c1_check("python/ -m pytest -q", monkeypatch)
    assert result is not None, "expected deny for degenerate `python/ -m pytest -q`"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "python3 -m pytest -q" in reason
    assert "misspelled" in reason


@pytest.mark.parametrize(
    "bare_spelling",
    ("py", "python2", "python3.11"),
)
def test_c1b_bare_python_family_alias_still_denies_with_unchanged_remedy(bare_spelling, monkeypatch):
    # `python` only; `_PYTHON_FAMILY_ALIAS_RE` also matches `py`,
    # `_PYTHON_FAMILY_ALIAS_RE.match(basename)` gate, so this is expected to
    result = _c1_check(f"{bare_spelling} -m pytest -q", monkeypatch)
    assert result is not None, f"expected deny for bare `{bare_spelling} -m pytest -q`"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "python3 -m pytest -q" in reason
    assert "misspelled" in reason


def test_py_suffix_hyphen_boundary_bypass_denies(monkeypatch):
    result = _deny("evil-coordinator-doc-new.py --type review-findings", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "coordinator-doc-new" in reason


def test_py_suffix_hyphen_boundary_bypass_python3_prefixed_denies(monkeypatch):
    result = _deny(
        "python3 evil-coordinator-doc-new.py --type review-findings", monkeypatch
    )
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "coordinator-doc-new" in reason


def test_py_suffix_hyphen_boundary_bypass_quoted_denies(monkeypatch):
    _deny('"evil-coordinator-doc-new.py" --type review-findings', monkeypatch)


def test_py_suffix_longer_extension_not_exact_suffix_denies(monkeypatch):
    _deny("coordinator-doc-new.python --type review-findings", monkeypatch)


def test_py_suffix_inside_semicolon_chain_denies_metacharacter(monkeypatch):
    result = _deny(
        f"ls; {_CLAUDE_KLABAUTER_ABS_PATH} --type review-findings", monkeypatch
    )
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_py_suffix_inside_pipeline_non_allowlisted_first_segment_denies(monkeypatch):
    result = _deny(
        f"rm -rf / | {_CLAUDE_KLABAUTER_ABS_PATH} --type review-findings", monkeypatch
    )
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "pipeline segment" in reason


def test_py_suffix_command_substitution_denies(monkeypatch):
    result = _deny(
        f"echo $({_CLAUDE_KLABAUTER_ABS_PATH} --type review-findings)", monkeypatch
    )
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_py_suffix_missing_type_arg_still_denies(monkeypatch):
    result = _deny(_CLAUDE_KLABAUTER_ABS_PATH, monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--type review-findings" in reason


def test_py_suffix_lookalike_double_extension_denies(monkeypatch):
    _deny("coordinator-doc-new.py.evil --type review-findings", monkeypatch)


def test_env_assignment_prefix_allows_sanctioned_pytest(monkeypatch):
    _allow("REPO_CLAUDE_KLABAUTER=/tmp/empty python3 -m pytest coordinator/tests/t.py", monkeypatch)


def test_bare_env_prefix_allows_sanctioned_pytest(monkeypatch):
    _allow("env REPO_CLAUDE_KLABAUTER=/tmp/empty python3 -m pytest coordinator/tests/t.py", monkeypatch)


def test_multiple_assignments_and_env_stack_allow(monkeypatch):
    _allow("A=1 env -i B=2 python3 -m pytest -q", monkeypatch)


def test_env_assignment_prefix_allows_tier_a_git(monkeypatch):
    _allow("REPO_CLAUDE_KLABAUTER=/tmp/empty git log --oneline -5", monkeypatch)


def test_env_prefix_does_not_admit_a_denied_binary(monkeypatch):
    _deny("REPO_CLAUDE_KLABAUTER=/tmp/empty rm -rf /", monkeypatch)


def test_env_prefix_does_not_admit_inline_code(monkeypatch):
    _deny("FOO=1 python3 -c 'import os'", monkeypatch)


def test_env_prefix_does_not_admit_unlisted_module(monkeypatch):
    _deny("FOO=1 python3 -m http.server", monkeypatch)


@pytest.mark.parametrize(
    "name", ["PATH", "PYTHONPATH", "PYTHONSTARTUP", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "BASH_ENV"]
)
def test_exec_influencing_assignment_is_not_peeled(name, monkeypatch):
    result = _deny(f"{name}=/tmp/evil python3 -m pytest -q", monkeypatch)
    assert name in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_exec_influencing_assignment_behind_env_is_not_peeled(monkeypatch):
    _deny("env PATH=/tmp/evil python3 -m pytest -q", monkeypatch)


def test_exec_influencing_assignment_after_a_safe_one_is_not_peeled(monkeypatch):
    _deny("FOO=1 PATH=/tmp/evil python3 -m pytest -q", monkeypatch)


def test_env_split_string_flag_is_not_peeled(monkeypatch):
    _deny("env -S 'python3 -m pytest -q' extra", monkeypatch)


def test_bare_env_with_no_command_still_denies(monkeypatch):
    _deny("env", monkeypatch)
    _deny("env FOO=1", monkeypatch)


def test_peel_helper_is_identity_for_unprefixed_tokens():
    tokens = ["python3", "-m", "pytest", "-q"]
    assert guard.peel_env_assignment_prefix(tokens) == tokens
    assert guard.peel_env_assignment_prefix([]) == []
