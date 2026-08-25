"""Unit suite for the M2 additions to `_command_tokenizer.py`:
`resolve_command_positions`, `find_git_segment`, and the classified wrapper
table (`WrapperSemanticClass` / `EXECS_ITS_ARGV_WRAPPERS` /
`REWRITE_FACING_WRAPPERS`).

Nothing in the live guard suite consumes any of this yet (see the module's
own section docstring) -- this suite is the proof the API works and the
signatures are what later chunks (M5/M6/M7) will be authored against, not a
regression guard for an existing consumer.
"""

from __future__ import annotations

from coordinator_core.bash_guards import _command_tokenizer as ct
from coordinator_core.bash_guards import dispatch_checks


def _heads(cmd):
    return [r.head for r in ct.resolve_command_positions(cmd)]


def _confidences(cmd):
    return [r.confidence for r in ct.resolve_command_positions(cmd)]


class TestResolvedPlainCommand:
    def test_plain_command_is_resolved(self):
        results = ct.resolve_command_positions("git status")
        assert len(results) == 1
        r = results[0]
        assert r.tokens == ["git", "status"]
        assert r.head == "git"
        assert r.confidence is ct.ResolutionConfidence.RESOLVED
        assert r.pipe_before is False
        assert r.depth == 0


class TestQuotedSeparators:
    def test_separator_inside_single_quotes_is_not_a_boundary(self):
        results = ct.resolve_command_positions("echo 'a;b;c' && git log")
        assert len(results) == 2
        assert results[0].tokens == ["echo", "a;b;c"]
        assert results[1].tokens == ["git", "log"]

    def test_separator_inside_double_quotes_is_not_a_boundary(self):
        results = ct.resolve_command_positions('echo "a && b" && git log')
        assert len(results) == 2
        assert results[0].tokens == ["echo", "a && b"]
        assert results[1].tokens == ["git", "log"]

    def test_pipe_before_flag_set_on_piped_segment(self):
        results = ct.resolve_command_positions("echo hi | grep h")
        assert len(results) == 2
        assert results[0].pipe_before is False
        assert results[1].pipe_before is True


class TestGroupingAndEnvPeel:
    def test_subshell_paren_peeled(self):
        r = ct.resolve_command_positions("( git status )")[0]
        assert r.tokens[0] == "git"
        assert r.confidence is ct.ResolutionConfidence.PEELED

    def test_glued_subshell_paren_peeled(self):
        r = ct.resolve_command_positions("(git status")[0]
        assert r.tokens[0] == "git"

    def test_brace_group_peeled(self):
        r = ct.resolve_command_positions("{ git status; }")[0]
        assert r.tokens[0] == "git"

    def test_leading_env_assignment_peeled(self):
        r = ct.resolve_command_positions("FOO=1 git status")[0]
        assert r.tokens == ["git", "status"]
        assert r.confidence is ct.ResolutionConfidence.PEELED

    def test_multiple_leading_env_assignments_peeled(self):
        r = ct.resolve_command_positions("FOO=1 BAR=2 git status")[0]
        assert r.tokens == ["git", "status"]

    def test_env_invocation_peeled(self):
        r = ct.resolve_command_positions("env FOO=1 git status")[0]
        assert r.tokens == ["git", "status"]
        assert r.confidence is ct.ResolutionConfidence.PEELED


class TestWrapperPeeling:
    def test_nice_peeled_to_git(self):
        r = ct.resolve_command_positions("nice git status")[0]
        assert r.tokens == ["git", "status"]
        assert r.confidence is ct.ResolutionConfidence.PEELED

    def test_timeout_with_duration_peeled(self):
        r = ct.resolve_command_positions("timeout 30 git rebase -i HEAD~3")[0]
        assert r.tokens == ["git", "rebase", "-i", "HEAD~3"]

    def test_ionice_bundled_flag_peeled(self):
        r = ct.resolve_command_positions("ionice -c2 git stash")[0]
        assert r.tokens == ["git", "stash"]

    def test_stdbuf_flag_peeled(self):
        r = ct.resolve_command_positions("stdbuf -oL git worktree add x y")[0]
        assert r.tokens[0] == "git"

    def test_nice_bare_numeric_peeled(self):
        r = ct.resolve_command_positions("nice -19 find / -delete")[0]
        assert r.tokens == ["find", "/", "-delete"]

    def test_stacked_wrappers_peeled(self):
        r = ct.resolve_command_positions("nice timeout 5 git status")[0]
        assert r.tokens == ["git", "status"]

    def test_which_stops_peeling(self):
        r = ct.resolve_command_positions("which git")[0]
        assert r.tokens == ["which", "git"]
        assert r.head == "which"
        assert r.confidence is ct.ResolutionConfidence.RESOLVED

    def test_type_stops_peeling(self):
        r = ct.resolve_command_positions("type git")[0]
        assert r.head == "type"

    def test_busybox_stops_peeling(self):
        r = ct.resolve_command_positions("busybox git status")[0]
        assert r.head == "busybox"
        assert r.confidence is ct.ResolutionConfidence.RESOLVED

    def test_sudo_own_flags_are_peeled_to_reach_the_wrapped_command(self):
        # Deliberate flip, 2026-07-30 (this test's prior body asked for exactly
        # that: it pinned the limitation "so a future fix changes this test
        # deliberately, not by accident"). `sudo` had no arg-flags entry, so
        # the walk stopped at `-u` and command position never reached `git` --
        # a live guard bypass, not a cosmetic gap: `sudo -u root git stash
        # drop` reached allow against `block_stash_destruction`, and the
        # `git worktree add` equivalent against the worktree ban. `sudo` now
        # has both an arg-flag and a boolean-flag table.
        r = ct.resolve_command_positions("sudo -u root git push")[0]
        assert r.tokens[0] == "git"

    def test_sudo_valueless_flag_is_peeled_too(self):
        # A valueless flag stopped the walk just as hard as a value-taking
        # one, so the boolean table is load-bearing on its own -- an arg-flag
        # table alone would have left `sudo -E <cmd>` open.
        r = ct.resolve_command_positions("sudo -E git push")[0]
        assert r.tokens[0] == "git"

    def test_doas_and_setsid_and_strace_reach_the_wrapped_command(self):
        for cmd in ("doas -u root git push", "setsid git push", "strace -f git push"):
            assert ct.resolve_command_positions(cmd)[0].tokens[0] == "git", cmd


class TestNestedCommandSubstitution:
    def test_dollar_paren_substitution_resolved_first(self):
        results = ct.resolve_command_positions("echo $(git rev-parse HEAD)")
        assert len(results) == 2
        assert results[0].tokens == ["git", "rev-parse", "HEAD"]
        assert results[0].depth == 1
        assert results[1].tokens == ["echo"]
        assert results[1].depth == 0

    def test_backtick_substitution_resolved(self):
        results = ct.resolve_command_positions("echo `git rev-parse HEAD`")
        assert results[0].tokens == ["git", "rev-parse", "HEAD"]
        assert results[0].depth == 1

    def test_nested_dollar_paren_balances_parens(self):
        results = ct.resolve_command_positions("echo $(echo $(git status))")
        heads = [r.tokens[0] for r in results if r.tokens]
        assert "git" in heads
        # innermost substitution recurses one level deeper than the outer one
        depths = {tuple(r.tokens): r.depth for r in results}
        assert depths[("git", "status")] == 2
        assert depths[("echo",)] == 1 or any(
            r.depth == 1 for r in results if r.tokens == ["echo"]
        )

    def test_substitution_inside_double_quotes_still_recognized(self):
        results = ct.resolve_command_positions('echo "$(git status)"')
        assert any(r.tokens == ["git", "status"] for r in results)

    def test_substitution_inside_single_quotes_not_recognized(self):
        results = ct.resolve_command_positions("echo '$(git status)'")
        assert len(results) == 1
        assert results[0].tokens[0] == "echo"


class TestInterpreterDashC:
    def test_standalone_c_flag_recurses(self):
        results = ct.resolve_command_positions("sh -c 'git push --force'")
        assert results[0].tokens == ["sh", "-c", "git push --force"]
        assert results[0].depth == 0
        assert results[1].tokens == ["git", "push", "--force"]
        assert results[1].depth == 1

    def test_bundled_short_flags_on_c_recurse(self):
        results = ct.resolve_command_positions("bash -ic 'git push'")
        assert results[1].tokens == ["git", "push"]
        assert results[1].depth == 1

    def test_bundled_short_flags_c_first_recurse(self):
        results = ct.resolve_command_positions("bash -ci 'git push'")
        assert results[1].tokens == ["git", "push"]

    def test_non_shell_interpreter_does_not_recurse(self):
        results = ct.resolve_command_positions("python3 -c 'import git'")
        assert len(results) == 1

    def test_dash_c_payload_recursively_tokenized(self):
        results = ct.resolve_command_positions("sh -c 'git status; git log'")
        heads = [r.tokens[0] for r in results if r.tokens]
        assert heads.count("git") == 2


class TestDepthCap:
    def test_depth_cap_reached_yields_unresolved(self):
        cmd = "sh -c 'sh -c \"sh -c \\'sh -c \\\"sh -c \\\\\\'git status\\\\\\'\\\"\\'\"'"
        results = ct.resolve_command_positions(cmd)
        assert any(r.confidence is ct.ResolutionConfidence.UNRESOLVED for r in results)

    def test_depth_cap_is_enforced_directly(self):
        results = ct.resolve_command_positions("git status", _depth=ct._MAX_RESOLVE_DEPTH + 1)
        assert len(results) == 1
        assert results[0].confidence is ct.ResolutionConfidence.UNRESOLVED
        assert results[0].tokens == ["git status"]


class TestHeredocs:
    def test_heredoc_body_not_treated_as_segments(self):
        cmd = "cat <<EOF\ngit status ; git log\nEOF\necho done"
        results = ct.resolve_command_positions(cmd)
        heads = [r.tokens[0] for r in results if r.tokens]
        assert heads == ["cat", "echo"]

    def test_command_after_heredoc_terminator_is_its_own_segment(self):
        cmd = "cat <<EOF\nbody line\nEOF\ngit status"
        results = ct.resolve_command_positions(cmd)
        assert results[-1].tokens == ["git", "status"]

    def test_dash_heredoc_strips_leading_tabs_on_terminator(self):
        cmd = "cat <<-EOF\n\tbody\n\tEOF\ngit status"
        results = ct.resolve_command_positions(cmd)
        assert results[-1].tokens == ["git", "status"]

    def test_heredoc_with_no_trailing_command_still_resolves_opener(self):
        cmd = "cat <<EOF\nbody\nEOF"
        results = ct.resolve_command_positions(cmd)
        assert results[0].tokens[0] == "cat"


class TestUnresolvedConfidence:
    def test_bare_variable_head_with_trailing_args_is_unresolved(self):
        r = ct.resolve_command_positions("$UNKNOWN_CMD arg1 arg2")[0]
        assert r.confidence is ct.ResolutionConfidence.UNRESOLVED
        assert r.head == "$UNKNOWN_CMD"

    def test_unparseable_command_is_unresolved(self):
        results = ct.resolve_command_positions("echo 'unterminated")
        assert len(results) == 1
        assert results[0].confidence is ct.ResolutionConfidence.UNRESOLVED

    def test_wrapper_with_nothing_following_is_unresolved(self):
        r = ct.resolve_command_positions("sudo")[0]
        assert r.tokens == []
        assert r.head is None
        assert r.confidence is ct.ResolutionConfidence.UNRESOLVED

    def test_all_three_confidences_are_reachable(self):
        resolved = ct.resolve_command_positions("git status")[0]
        peeled = ct.resolve_command_positions("nice git status")[0]
        unresolved = ct.resolve_command_positions("$X")[0]
        assert {resolved.confidence, peeled.confidence, unresolved.confidence} == {
            ct.ResolutionConfidence.RESOLVED,
            ct.ResolutionConfidence.PEELED,
            ct.ResolutionConfidence.UNRESOLVED,
        }


class TestConfidenceReportedNotConsumed:
    def test_confidence_is_a_plain_enum_value_not_wired_to_anything(self):
        # This is an intentional no-op assertion documenting the contract:
        # ResolutionConfidence carries no side effects and nothing in this
        # module branches guard behaviour on it.
        for member in ct.ResolutionConfidence:
            assert isinstance(member.value, str)


class TestFindGitSegment:
    def test_plain_git_no_prefix(self):
        result = ct.find_git_segment("git status")
        assert result == {"prefix": "", "body": "git status", "tail": ""}

    def test_no_git_segment_returns_empty_dict(self):
        assert ct.find_git_segment("echo no git here") == {}

    def test_prefix_captures_leading_cd_and_wrapper(self):
        result = ct.find_git_segment("cd /tmp && FOO=1 nice -n5 git status && echo done")
        assert result["prefix"] == " FOO=1 nice -n5 "
        assert result["body"].strip() == "git status"
        assert result["tail"] == "&& echo done"

    def test_first_git_segment_wins_when_multiple_present(self):
        result = ct.find_git_segment("git status && git log")
        assert result["body"].strip() == "git status"
        assert "git log" in result["tail"]

    def test_no_verify_bypass_flag_travels_in_body_not_prefix(self):
        flag = "--no" + "-verify"
        result = ct.find_git_segment("git commit " + flag + " -m x")
        assert flag in result["body"]
        assert result["prefix"] == ""

    def test_quoted_separator_inside_body_is_not_a_tail_boundary(self):
        result = ct.find_git_segment('git commit -m "a ; b"')
        assert result["tail"] == ""
        assert 'a ; b' in result["body"]

    def test_wrapper_own_argv_peeled_from_prefix(self):
        result = ct.find_git_segment("timeout 30 git rebase -i HEAD~3")
        assert result["prefix"] == "timeout 30 "
        assert result["body"].startswith("git rebase")


class TestWrapperSemanticClassAxes:
    def test_execs_its_argv_members(self):
        expected = {
            "sudo", "command", "time", "exec", "nice", "nohup", "ionice",
            "timeout", "stdbuf", "env", "setsid", "strace", "doas",
        }
        assert ct.EXECS_ITS_ARGV_WRAPPERS == frozenset(expected)

    def test_inspects_without_execing_members(self):
        assert ct.INSPECTS_WITHOUT_EXECING_WRAPPERS == frozenset({"which", "type"})

    def test_applet_dispatcher_members(self):
        assert ct.APPLET_DISPATCHER_WRAPPERS == frozenset({"busybox"})

    def test_axes_are_disjoint(self):
        a, b, c = (
            ct.EXECS_ITS_ARGV_WRAPPERS,
            ct.INSPECTS_WITHOUT_EXECING_WRAPPERS,
            ct.APPLET_DISPATCHER_WRAPPERS,
        )
        assert a & b == frozenset()
        assert a & c == frozenset()
        assert b & c == frozenset()

    def test_wrapper_semantic_class_lookup(self):
        assert ct.wrapper_semantic_class("nice") is ct.WrapperSemanticClass.EXECS_ITS_ARGV
        assert ct.wrapper_semantic_class("which") is ct.WrapperSemanticClass.INSPECTS_WITHOUT_EXECING
        assert ct.wrapper_semantic_class("busybox") is ct.WrapperSemanticClass.APPLET_DISPATCHER
        assert ct.wrapper_semantic_class("not-a-wrapper") is None


class TestRewriteFacingIsNarrowerAndIndependent:
    def test_rewrite_facing_matches_dispatch_checks_find_wrapper_words_verbatim(self):
        assert ct.REWRITE_FACING_WRAPPERS == frozenset(dispatch_checks._FIND_WRAPPER_WORDS)

    def test_rewrite_facing_is_strict_subset_of_execs_its_argv(self):
        assert ct.REWRITE_FACING_WRAPPERS < ct.EXECS_ITS_ARGV_WRAPPERS

    def test_setsid_strace_doas_ionice_are_execs_but_not_rewrite_facing(self):
        for word in ("setsid", "strace", "doas", "ionice"):
            assert word in ct.EXECS_ITS_ARGV_WRAPPERS
            assert word not in ct.REWRITE_FACING_WRAPPERS
            assert ct.is_rewrite_facing_wrapper(word) is False

    def test_which_type_busybox_never_rewrite_facing(self):
        for word in ("which", "type", "busybox"):
            assert ct.is_rewrite_facing_wrapper(word) is False

    def test_time_present_in_rewrite_facing(self):
        # Precedent named in this task's brief: `time` was dropped from an
        # earlier draft and is present in every on-disk allowlist that
        # includes it -- pinning it explicitly so a future edit can't drop
        # it silently again.
        assert "time" in ct.REWRITE_FACING_WRAPPERS


class TestWrapperTableEnumeration:
    """Fail-loud enumeration: the plan named six on-disk wrapper-allowlist
    literals for this reconciliation. Five were pre-confirmed in this
    chunk's brief; the sixth (`_PASSTHROUGH_WRAPPERS_FOR_COMMIT`,
    `block_subagent_commit.py`) was found as instructed. A SEVENTH,
    `dispatch_checks._BYPASS_WRAPPER_WORDS`, was also found during this
    enumeration and is recorded here rather than silently folded in
    uncounted -- see this chunk's own report to the EM for the discrepancy.
    It contributes no word beyond the union already covered by
    `EXECS_ITS_ARGV_WRAPPERS`/`INSPECTS_WITHOUT_EXECING_WRAPPERS`.
    """

    def test_seven_not_six_on_disk_literals_found(self):
        from coordinator_core.bash_guards import block_worktree_creation
        from coordinator_core.bash_guards import block_subagent_commit
        from coordinator_core.bash_guards import block_subagent_destructive_action

        literals = [
            set(dispatch_checks._FIND_WRAPPER_WORDS),
            set(dispatch_checks._RM_WRAPPER_WORDS),
            set(dispatch_checks._BYPASS_WRAPPER_WORDS),
            set(block_worktree_creation._PASSTHROUGH_WRAPPERS),
            set(block_subagent_commit._PASSTHROUGH_WRAPPERS_FOR_COMMIT),
            set(block_subagent_destructive_action._PASSTHROUGH_WRAPPERS),
        ]
        # _sentinel_creation_guard's copy is a class attribute, not a
        # module-level name -- included by inspection, not by import, to
        # keep this test from reaching into a class internal unnecessarily.
        sentinel_literal = {
            "sudo", "command", "time", "exec", "nice", "nohup", "ionice",
            "timeout", "stdbuf", "which", "type", "setsid", "strace", "doas",
            "busybox",
        }
        literals.append(sentinel_literal)
        assert len(literals) == 7

    def test_union_of_all_literals_is_covered_by_the_table(self):
        from coordinator_core.bash_guards import block_worktree_creation
        from coordinator_core.bash_guards import block_subagent_commit
        from coordinator_core.bash_guards import block_subagent_destructive_action

        union = set()
        for literal in (
            dispatch_checks._FIND_WRAPPER_WORDS,
            dispatch_checks._RM_WRAPPER_WORDS,
            dispatch_checks._BYPASS_WRAPPER_WORDS,
            block_worktree_creation._PASSTHROUGH_WRAPPERS,
            block_subagent_commit._PASSTHROUGH_WRAPPERS_FOR_COMMIT,
            block_subagent_destructive_action._PASSTHROUGH_WRAPPERS,
        ):
            union |= set(literal)
        table_union = (
            ct.EXECS_ITS_ARGV_WRAPPERS
            | ct.INSPECTS_WITHOUT_EXECING_WRAPPERS
            | ct.APPLET_DISPATCHER_WRAPPERS
        )
        assert union <= table_union


class TestRedirectionOperatorJoining:
    """`&` is in `shlex`'s `punctuation_chars`, so a redirection containing
    one (`2>&1`, `>&2`, `&>file`) lexed as a bare `&` token and every
    segmenter in the package read it as a command separator. Confirmed live
    2026-08-04 via `block_subagent_commit`: a `coordinator:git-commit-agent`
    invocation ending in `2>&1` counted as two segments, failed that guard's
    single-segment allow precondition, and was denied with a message blaming
    its (correct) pathspec.
    """

    def test_stderr_to_stdout_is_one_token(self):
        assert ct.tokenize_full_command("foo bar 2>&1") == ["foo", "bar", "2>&1"]

    def test_stderr_to_stdout_does_not_split_the_segment(self):
        assert ct.segments_from_tokens_simple(
            ct.tokenize_full_command("foo bar 2>&1")
        ) == [["foo", "bar", "2>&1"]]

    def test_fd_dup_spellings_are_one_token(self):
        assert ct.tokenize_full_command("foo >&2") == ["foo", ">&2"]
        assert ct.tokenize_full_command("foo 2>&-") == ["foo", "2>&-"]
        assert ct.tokenize_full_command("foo 1>&2 2>&1") == ["foo", "1>&2", "2>&1"]

    def test_ampersand_redirect_form_is_one_token(self):
        assert ct.tokenize_full_command("foo &>/tmp/out.log") == [
            "foo",
            "&>/tmp/out.log",
        ]

    def test_fd_dup_target_that_is_a_filename_stays_its_own_token(self):
        assert ct.tokenize_full_command("foo 2>&fifo") == ["foo", "2>&", "fifo"]

    def test_genuine_separators_are_untouched(self):
        assert ct.segments_from_tokens_simple(
            ct.tokenize_full_command("foo 2>&1 & bar")
        ) == [["foo", "2>&1"], ["bar"]]
        assert ct.segments_from_tokens_simple(
            ct.tokenize_full_command("foo && bar")
        ) == [["foo"], ["bar"]]
        assert ct.segments_from_tokens_simple(
            ct.tokenize_full_command("foo 2>&1 | bar")
        ) == [["foo", "2>&1"], ["bar"]]

    def test_quoted_redirection_text_is_not_an_operator(self):
        assert ct.tokenize_full_command('echo "2>&1" && git log') == [
            "echo",
            "2>&1",
            "&&",
            "git",
            "log",
        ]

    def test_spaced_ampersand_before_a_redirect_is_two_commands(self):
        """`cmd & >file other` is TWO commands — `&` backgrounds the first and
        the second genuinely runs — while `cmd &>file` is one. `shlex` lexes
        both identically once whitespace is gone, so this pair is the whole
        reason masking happens on the raw text.

        Security-critical: collapsing the spaced form to one segment let a
        compound command satisfy `block_subagent_commit`'s single-segment
        precondition and ride through the git-commit-agent allow branch
        (found 2026-08-04 by security audit).
        """
        two = ct.segments_from_tokens_simple(
            ct.tokenize_full_command("mybinary -m x -- a.py & >/dev/null secondbinary -a foo")
        )
        assert len(two) == 2

        one = ct.segments_from_tokens_simple(
            ct.tokenize_full_command("mybinary -m x -- a.py &>/dev/null")
        )
        assert len(one) == 1

    def test_adjacent_combine_redirect_survives_as_one_token(self):
        assert ct.tokenize_full_command("foo &>/tmp/out.log") == [
            "foo",
            "&>/tmp/out.log",
        ]
        assert ct.tokenize_full_command("foo &>>/tmp/out.log") == [
            "foo",
            "&>>/tmp/out.log",
        ]

    def test_quoted_combine_redirect_is_data_not_an_operator(self):
        assert ct.tokenize_full_command('echo "a &>b" -- a.py') == [
            "echo",
            "a &>b",
            "--",
            "a.py",
        ]

    def test_no_sentinel_leaks_into_any_token(self):
        """The mask is an implementation detail — no caller may ever observe
        it, including on the quoted-data path where it is never applied."""
        for command in (
            "foo &>/tmp/out.log",
            "foo & >/tmp/out.log bar",
            'echo "a &>b"',
            "foo 2>&1",
        ):
            tokens = ct.tokenize_full_command(command)
            assert all(ct._AMP_REDIRECT_SENTINEL not in token for token in tokens)

    def test_the_verbatim_denied_invocation_is_one_segment(self):
        """The exact shape denied on 2026-08-04, pinned so the incident cannot
        recur through a tokenizer change alone: a redirect-terminated
        `scoped-git-commit` must read as ONE segment, because that count is
        what `block_subagent_commit`'s allow predicate gates on before it ever
        reaches the ownership check.
        """
        denied = (
            'scoped-git-commit -m "C1: subject" '
            "-- src/a.py src/a.test.py 2>&1"
        )
        segments = ct.segments_from_tokens_simple(ct.tokenize_full_command(denied))
        assert len(segments) == 1
        assert segments[0][-1] == "2>&1"
