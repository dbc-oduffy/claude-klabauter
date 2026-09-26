
from __future__ import annotations

from coordinator_core.bash_guards import block_worktree_creation as guard


def _payload(command, agent_id=None, agent_type=None):
    p = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": "/repo",
    }
    if agent_id is not None:
        p["agent_id"] = agent_id
    if agent_type is not None:
        p["agent_type"] = agent_type
    return p


def _reason(out):
    assert out is not None, "expected a deny envelope, got allow"
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    return hso["permissionDecisionReason"]


class TestNonBashOrEmpty:
    def test_non_bash_tool_allows(self):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "x"}}
        assert guard.check(payload) is None

    def test_empty_command_allows(self):
        assert guard.check(_payload("")) is None

    def test_malformed_tool_input_allows(self):
        payload = {"tool_name": "Bash", "tool_input": "not-a-dict"}
        assert guard.check(payload) is None

    def test_no_worktree_mention_allows(self):
        assert guard.check(_payload("git status && ls -la")) is None


class TestDenyCreationSubcommands:
    def test_git_worktree_add_denies(self):
        out = guard.check(_payload("git worktree add ../wt-1 feature-branch"))
        reason = _reason(out)
        assert "worktree" in reason.lower()
        assert "banned fleet-wide" in reason

    def test_git_worktree_move_denies(self):
        _reason(guard.check(_payload("git worktree move ../wt-1 ../wt-2")))

    def test_git_worktree_repair_denies(self):
        _reason(guard.check(_payload("git worktree repair")))

    def test_git_worktree_lock_denies(self):
        _reason(guard.check(_payload("git worktree lock ../wt-1")))

    def test_git_worktree_unlock_denies(self):
        _reason(guard.check(_payload("git worktree unlock ../wt-1")))

    def test_chained_command_denies(self):
        out = guard.check(_payload("cd /x && git worktree add y"))
        _reason(out)

    def test_unrecognized_subcommand_denies_default(self):
        out = guard.check(_payload("git worktree frobnicate"))
        reason = _reason(out)
        assert "default-deny" in reason.lower() or "unrecognized" in reason.lower()


class TestAllowCleanupAndReadonly:
    def test_git_worktree_list_allows(self):
        assert guard.check(_payload("git worktree list")) is None

    def test_git_worktree_remove_allows(self):
        assert guard.check(_payload("git worktree remove ../wt-1")) is None

    def test_git_worktree_prune_allows(self):
        assert guard.check(_payload("git worktree prune")) is None

    def test_bare_git_worktree_allows(self):
        assert guard.check(_payload("git worktree")) is None


class TestNoNaiveWorktreeFlagSubstringMatch:
    def test_git_restore_worktree_flag_allows(self):
        out = guard.check(_payload("git restore --worktree foo.py"))
        assert out is None

    def test_git_restore_dash_w_short_flag_allows(self):
        assert guard.check(_payload("git restore -W foo.py")) is None


class TestEnvAssignmentPrefix:
    def test_leading_env_assignment_still_denies(self):
        out = guard.check(_payload("GIT_TRACE=1 git worktree add ../wt-1 x"))
        _reason(out)


class TestMentionIsNotInvocation:
    """2026-07-28): the pre-fix
    `_evaluate` scanned EVERY token in a segment for the first `git`-basename
    match, not just the command-position head, so a `git` mention as an
    ARGUMENT to another command (an echo/printf/grep operand) was treated as
    a real invocation. `echo git worktree add x` tokenizes to `['echo',
    'git', 'worktree', 'add', 'x']` -- pre-fix this found `git` at index 1,
    walked the remainder through `_real_git_subcommand`, and denied it as
    `git worktree add`, even though `echo` never invokes git at all. Fixed
    by anchoring on the command-position head (mirrors
    `block_subagent_destructive_action.py`'s own "COMMAND-POSITION
    GIT-TOKEN FIX", same diff).
    """

    def test_echo_git_worktree_add_mention_allows(self):
        out = guard.check(_payload("echo git worktree add x"))
        assert out is None

    def test_printf_git_worktree_add_mention_allows(self):
        out = guard.check(_payload("printf 'git worktree add\\n'"))
        assert out is None

    def test_grep_pattern_mentioning_git_worktree_add_allows(self):
        out = guard.check(_payload('grep -n "git worktree add" file.py'))
        assert out is None

    def test_commit_message_mentioning_git_worktree_add_allows(self):
        out = guard.check(
            _payload('git commit -m "document git worktree add usage"')
        )
        assert out is None

    def test_a_real_invocation_after_an_echo_mention_still_denies(self):
        out = guard.check(_payload("echo git worktree add x && git worktree add ../wt-1 y"))
        _reason(out)


class TestPowerShellIdiomDialectNeutral:

    def test_semicolon_chained_powershell_style_denies(self):
        _reason(guard.check(_payload("Get-Location; git worktree add ../wt-1 x")))

    def test_semicolon_chained_powershell_style_allow_case_unaffected(self):
        assert guard.check(_payload("Get-Location; git worktree list")) is None


class TestHeredocBodyIsNotShellText:
    """Heredoc BODY text is stdin DATA, never a shell command -- see the
    guard module's own "HEREDOC-BODY FALSE-DENY FIX" docstring section.

    Observed live 2026-07-29: a dispatched reviewer persisted a findings
    document via a sanctioned ``cat <<EOF > review.md`` heredoc; the body
    quoted this guard's own filename and a ``git worktree add`` example as
    prose, and the pre-fix guard denied the write. Root cause: the body was
    NOT quote-fenced against shell segmentation, so an unquoted ``;``/``|``
    inside the prose started a new ``_segments_from_tokens`` segment whose
    head word happened to be the literal token ``git``, which the guard then
    misread as a real invocation.

    `state/bug-backlog/2026-07-29-worktree-guard-false-denies-documents-
    naming-guard-files.yaml` (DoE-claude).
    """

    def test_plain_mention_of_add_and_filename_in_heredoc_body_allows(self):
        cmd = (
            "cat <<EOF > /tmp/review.md\n"
            "Discussion of git worktree add and block_worktree_creation.py "
            "behavior.\nEOF\n"
        )
        assert guard.check(_payload(cmd)) is None

    def test_semicolon_before_git_mention_in_heredoc_body_allows(self):
        cmd = (
            "cat <<EOF > /tmp/review.md\n"
            "See notes; git worktree add x is denied by design.\nEOF\n"
        )
        assert guard.check(_payload(cmd)) is None

    def test_pipe_before_git_mention_in_heredoc_body_allows(self):
        cmd = (
            "cat <<EOF > /tmp/review.md\n"
            "notes | git worktree add x\nEOF\n"
        )
        assert guard.check(_payload(cmd)) is None

    def test_git_mention_at_start_of_heredoc_body_line_allows(self):
        cmd = "cat <<EOF > /tmp/review.md\ngit worktree add x\nEOF\n"
        assert guard.check(_payload(cmd)) is None

    def test_guard_filename_only_no_verb_allows(self):
        cmd = (
            "cat <<EOF > /tmp/review.md\n"
            "See block_worktree_creation.py for the guard logic.\nEOF\n"
        )
        assert guard.check(_payload(cmd)) is None

    def test_quoted_heredoc_delimiter_with_git_mention_allows(self):
        cmd = (
            "cat <<'EOF' > /tmp/review.md\n"
            "\\`git worktree add ../wt-1 x\\` should still deny.\nEOF\n"
        )
        assert guard.check(_payload(cmd)) is None

    def test_real_invocation_still_denies_when_no_heredoc_present(self):
        _reason(guard.check(_payload("git worktree add ../wt-1 x")))

    def test_real_invocation_after_heredoc_write_still_denies(self):
        cmd = (
            "cat <<EOF > /tmp/review.md\n"
            "git worktree add is a real command.\nEOF\n"
            " && git worktree add ../wt-1 y"
        )
        _reason(guard.check(_payload(cmd)))

    def test_real_invocation_preceding_unrelated_heredoc_still_denies(self):
        # Anti-bypass: stripping an UNRELATED heredoc's body must not mask a
        cmd = "git worktree add ../wt-1 x\ncat <<EOF\nharmless\nEOF\n"
        _reason(guard.check(_payload(cmd)))

    def test_interpreter_fed_by_heredoc_via_worktree_guard_is_a_known_open_residual(self):
        # KNOWN-OPEN RESIDUAL, not a regression from the `<<\EOF` regex
        # triggering text away before `_WORKTREE_WORD_RE` ever sees it, and
        # before this file touched `_HEREDOC_OP_RE` at all. Widening the
        for spelling, cmd in (
            ("<<EOF", "bash <<EOF\ngit worktree add ../wt-1 x\nEOF\n"),
            ("<<'EOF'", "bash <<'EOF'\ngit worktree add ../wt-1 x\nEOF\n"),
            ("<<\\EOF", "bash <<\\EOF\ngit worktree add ../wt-1 x\nEOF\n"),
        ):
            result = guard.check(_payload(cmd))
            assert result is None, (
                f"expected known-open residual (ALLOW) for {spelling!r}, "
                f"got a deny -- if this now denies, the residual has closed "
                f"and this test/docstring should be updated, not deleted"
            )


class TestNotIdentityGated:
    def test_denies_without_any_identity_fields(self):
        out = guard.check(_payload("git worktree add ../wt-1 x"))
        _reason(out)

    def test_denies_with_subagent_identity(self):
        out = guard.check(
            _payload(
                "git worktree add ../wt-1 x",
                agent_id="a0123456789abcdef",
                agent_type="coordinator:executor",
            )
        )
        _reason(out)


class TestReachableThroughTheDispatchChain:

    @staticmethod
    def _decision(command):
        import json

        from coordinator_core.bash_guards import dispatch

        out = dispatch.evaluate_payload_json(
            json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        )
        return "deny" if (out and '"deny"' in json.dumps(out)) else "allow"

    def test_bare_creation_denied_end_to_end(self):
        assert self._decision("git worktree add ../wt-1 x") == "deny"

    def test_cd_prefixed_creation_denied_end_to_end(self):
        assert self._decision("cd /tmp && git worktree add ../y") == "deny"

    def test_semicolon_chained_creation_denied_end_to_end(self):
        assert self._decision("cd /tmp; git worktree add ../y") == "deny"

    def test_git_c_form_denied_end_to_end(self):
        assert self._decision("git -C /tmp worktree add ../y") == "deny"

    def test_cleanup_subcommands_still_allowed_end_to_end(self):
        assert self._decision("git worktree list") == "allow"
        assert self._decision("git worktree remove ../wt-1") == "allow"
        assert self._decision("git worktree prune") == "allow"

    def test_git_restore_worktree_flag_still_allowed_end_to_end(self):
        assert self._decision("git restore --worktree foo.py") == "allow"
