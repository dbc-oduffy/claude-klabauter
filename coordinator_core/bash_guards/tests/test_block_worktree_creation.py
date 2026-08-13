"""Tests for coordinator_core.bash_guards.block_worktree_creation.

Covers the DENY/ALLOW second-level `git worktree` subcommand split, the
default-deny-on-unrecognized posture, chaining/env-assignment shell shapes,
the `--worktree` false-positive regression (git-restore's own flag, not a
worktree invocation), and that the guard is NOT identity-gated (fires with
or without `agent_id`/`agent_type` present -- unlike its identity-gated
sibling `block_subagent_destructive_action`, which exempts the main-loop EM).

Pure Python -- no shell spawns, no git repo required.

Spec backlink: coordinator_core/bash_guards/block_worktree_creation.py
"""

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
        # Regression: `git restore -W`/`--worktree` is git-restore's own
        # flag, unrelated to worktree creation -- must NOT match a naive
        # `--worktree` substring ban.
        out = guard.check(_payload("git restore --worktree foo.py"))
        assert out is None

    def test_git_restore_dash_w_short_flag_allows(self):
        assert guard.check(_payload("git restore -W foo.py")) is None


class TestEnvAssignmentPrefix:
    def test_leading_env_assignment_still_denies(self):
        out = guard.check(_payload("GIT_TRACE=1 git worktree add ../wt-1 x"))
        _reason(out)


class TestMentionIsNotInvocation:
    """Review: code-reviewer -- Finding 2 (P2, 2026-07-28): the pre-fix
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
        # The mention must not mask a REAL invocation elsewhere in a
        # compound command -- only the echo's own segment is a mention;
        # the chained segment genuinely invokes git worktree add.
        out = guard.check(_payload("echo git worktree add x && git worktree add ../wt-1 y"))
        _reason(out)


class TestPowerShellIdiomDialectNeutral:
    """C4a (guard-dialect-coverage.md row 3): this guard gates on
    `head_base != "git"` -- the external `git` exe, byte-identical in both
    shell dialects. No `_dialect.py` import exists in this module
    (confirmed by grep), so a PowerShell-idiom surrounding shape (`;` chain
    instead of `&&`) reaches the SAME tokenizer and must reach the SAME
    verdict.

    Spec backlink: docs/reference/guard-dialect-coverage.md row 3 (C4a).
    """

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
    naming-guard-files.yaml` (coordinator-claude).
    """

    def test_plain_mention_of_add_and_filename_in_heredoc_body_allows(self):
        cmd = (
            "cat <<EOF > /tmp/review.md\n"
            "Discussion of git worktree add and block_worktree_creation.py "
            "behavior.\nEOF\n"
        )
        assert guard.check(_payload(cmd)) is None

    def test_semicolon_before_git_mention_in_heredoc_body_allows(self):
        # The false-denial repro: an unquoted `;` inside heredoc PROSE
        # (not a real shell separator) used to start a new tokenizer
        # segment whose head word was literally "git".
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
        # A benign heredoc write followed by a genuine chained invocation
        # must not have its real invocation masked by the body strip.
        cmd = (
            "cat <<EOF > /tmp/review.md\n"
            "git worktree add is a real command.\nEOF\n"
            " && git worktree add ../wt-1 y"
        )
        _reason(guard.check(_payload(cmd)))

    def test_real_invocation_preceding_unrelated_heredoc_still_denies(self):
        # Anti-bypass: stripping an UNRELATED heredoc's body must not mask a
        # real invocation living on an earlier line. This guard has no
        # interpreter-indirection probe of its own (that lives in the
        # identity-gated sibling -- see that module's own
        # `test_heredoc_interpreter_fed_wrapper_still_denies`, which covers
        # the genuine interpreter-FED-by-heredoc shape, i.e. `bash <<EOF ...
        # EOF` where the body is what gets executed). This test does NOT
        # exercise that shape -- it puts a real invocation on a line before
        # an unrelated, harmless heredoc; see
        # `test_interpreter_fed_by_heredoc_via_worktree_guard_is_a_known_open_residual`
        # below for this guard's own (allow-side) coverage of the genuine
        # shape.
        cmd = "git worktree add ../wt-1 x\ncat <<EOF\nharmless\nEOF\n"
        _reason(guard.check(_payload(cmd)))

    def test_interpreter_fed_by_heredoc_via_worktree_guard_is_a_known_open_residual(self):
        # KNOWN-OPEN RESIDUAL, not a regression from the `<<\EOF` regex
        # widening (2026-07-29 review finding 1). This guard (unlike its
        # identity-gated sibling in `block_subagent_destructive_action.py`)
        # has no interpreter-wrapper probe of its own -- it only looks for
        # the literal `worktree` word in the (heredoc-body-stripped) command
        # text. So `bash <<EOF ... git worktree add ... EOF`, where the
        # heredoc body IS what bash actually executes, strips the deny-
        # triggering text away before `_WORKTREE_WORD_RE` ever sees it, and
        # this guard ALLOWS -- for `<<EOF` and `<<'EOF'` identically, already,
        # before this file touched `_HEREDOC_OP_RE` at all. Widening the
        # regex to also recognize `<<\EOF` extends this SAME pre-existing
        # allow to one more delimiter spelling; it does not open a new class
        # of bypass. Recorded here as an explicit, named assertion (not a
        # silent pass) so a future reader sees the gap rather than assuming
        # it's covered by the "still denies" test above.
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
        # No agent_id/agent_type at all -- top-level EM call in the
        # identity-gated sibling's convention (which would ALLOW here);
        # this guard fires regardless.
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
    """Guard-level tests are not sufficient for this guard.

    ``guard.check()`` denied ``cd /tmp && git worktree add ...`` from the
    first commit, yet the same command was ALLOWED end-to-end: ``offer-git-c``
    sits earlier in the chain, rewrites ``cd <dir> && git <sub>`` into
    ``git -C <dir> <sub>``, and returns allow+updatedInput -- which
    short-circuits every later guard. The ban was bypassable by prefixing a
    ``cd`` while every guard-level test stayed green.

    These tests go through ``dispatch.evaluate_payload_json`` so that any
    future reordering that puts a rewrite/offer check ahead of this guard
    fails loudly here instead of silently disarming the ban.
    """

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
