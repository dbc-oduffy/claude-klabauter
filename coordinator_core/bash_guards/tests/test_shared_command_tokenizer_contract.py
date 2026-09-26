
from __future__ import annotations

from coordinator_core.bash_guards import _command_tokenizer
from coordinator_core.bash_guards import block_approval_sentinel_creation
from coordinator_core.bash_guards import block_subagent_commit
from coordinator_core.bash_guards import block_subagent_destructive_action
from coordinator_core.bash_guards import block_worktree_creation
from coordinator_core.bash_guards import block_worktree_sentinel_creation
from coordinator_core.bash_guards import _sentinel_creation_guard
from coordinator_core.bash_guards import dispatch_checks


def _payload(command: str):
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": "/repo",
    }


class TestSingleSourceOfTruth:

    def test_destructive_action_reexports_canonical_trio(self):
        assert (
            block_subagent_destructive_action._normalize_executable_basename
            is _command_tokenizer.normalize_executable_basename
        )
        assert (
            block_subagent_destructive_action._tokenize_full_command
            is _command_tokenizer.tokenize_full_command
        )
        assert (
            block_subagent_destructive_action._segments_from_tokens
            is _command_tokenizer.segments_from_tokens_with_pipe_flag
        )

    def test_subagent_commit_reexports_canonical_trio(self):
        assert (
            block_subagent_commit._normalize_executable_basename
            is _command_tokenizer.normalize_executable_basename
        )
        assert (
            block_subagent_commit._tokenize_full_command
            is _command_tokenizer.tokenize_full_command
        )
        assert (
            block_subagent_commit._segments_from_tokens
            is _command_tokenizer.segments_from_tokens_simple
        )

    def test_worktree_creation_imports_canonical_trio(self):
        assert (
            block_worktree_creation._normalize_executable_basename
            is _command_tokenizer.normalize_executable_basename
        )
        assert (
            block_worktree_creation._tokenize_full_command
            is _command_tokenizer.tokenize_full_command
        )
        assert (
            block_worktree_creation._segments_from_tokens
            is _command_tokenizer.segments_from_tokens_with_pipe_flag
        )

    def test_sentinel_creation_guard_imports_canonical_trio(self):
        assert (
            _sentinel_creation_guard._normalize_executable_basename
            is _command_tokenizer.normalize_executable_basename
        )
        assert (
            _sentinel_creation_guard._tokenize_full_command
            is _command_tokenizer.tokenize_full_command
        )
        assert (
            _sentinel_creation_guard._segments_from_tokens
            is _command_tokenizer.segments_from_tokens_with_pipe_flag
        )

    def test_dispatch_checks_imports_canonical_basename(self):
        assert (
            dispatch_checks._normalize_executable_basename
            is _command_tokenizer.normalize_executable_basename
        )


class TestReturnShapes:

    def test_tokenize_full_command_returns_list_or_none(self):
        assert _command_tokenizer.tokenize_full_command("git commit -m x") == [
            "git",
            "commit",
            "-m",
            "x",
        ]
        assert _command_tokenizer.tokenize_full_command("echo 'unterminated") is None

    def test_segments_from_tokens_with_pipe_flag_shape(self):
        tokens = _command_tokenizer.tokenize_full_command(
            "echo YmFzaA== | base64 -d | bash"
        )
        segments = _command_tokenizer.segments_from_tokens_with_pipe_flag(tokens)
        assert isinstance(segments, list)
        for item in segments:
            assert isinstance(item, tuple)
            assert len(item) == 2
            seg_tokens, pipe_before = item
            assert isinstance(seg_tokens, list)
            assert isinstance(pipe_before, bool)
        assert segments[-1] == (["bash"], True)
        assert segments[0][1] is False

    def test_segments_from_tokens_simple_shape(self):
        tokens = _command_tokenizer.tokenize_full_command("git commit -m x ; ls")
        segments = _command_tokenizer.segments_from_tokens_simple(tokens)
        assert isinstance(segments, list)
        for item in segments:
            assert isinstance(item, list)
            assert not isinstance(item, tuple)

    def test_simple_and_pipe_flag_partition_identically(self):
        tokens = _command_tokenizer.tokenize_full_command(
            "git commit -m x ; ls -la | grep foo"
        )
        with_flag = _command_tokenizer.segments_from_tokens_with_pipe_flag(tokens)
        simple = _command_tokenizer.segments_from_tokens_simple(tokens)
        assert simple == [seg for seg, _pipe_before in with_flag]

    def test_normalize_executable_basename_case_folds(self):
        assert _command_tokenizer.normalize_executable_basename("GIT.EXE") == "git"
        assert _command_tokenizer.normalize_executable_basename("Git.exe") == "git"
        assert _command_tokenizer.normalize_executable_basename("git") == "git"
        assert _command_tokenizer.normalize_executable_basename("gitk") == "gitk"

    def test_normalize_executable_basename_strips_trailing_dots_and_spaces(self):
        neb = _command_tokenizer.normalize_executable_basename
        assert neb("git.exe.") == "git"
        assert neb("git.exe ") == "git"
        assert neb("git.exe...") == "git"
        assert neb("git.exe. ") == "git"
        assert neb(r"C:\Program Files\Git\bin\git.exe.") == "git"
        # Must not over-broaden: a trailing dot/space on a DIFFERENT
        assert neb("gitk.") == "gitk"
        assert neb("mygit ") == "mygit"

    def test_normalize_executable_basename_preserves_all_dot_source_tokens(self):
        # Regression guard: a token that is ENTIRELY dots/spaces (POSIX `.`
        # `block_subagent_destructive_action.py`'s `_SOURCE_VERBS` check.
        neb = _command_tokenizer.normalize_executable_basename
        assert neb(".") == "."
        assert neb("..") == ".."
        assert neb("...") == "..."

    def test_token_matches_binary_recognizes_exe_and_separator_forms(self):
        tmb = _command_tokenizer.token_matches_binary
        assert tmb("git", "git")
        assert tmb("bin/git", "git")
        assert tmb("/usr/bin/git", "git")
        assert tmb("git.exe", "git")
        assert tmb("GIT.EXE", "git")
        assert tmb(r"C:\Git\bin\git.exe", "git")

    def test_token_matches_binary_recognizes_cmd_launcher_twin(self):
        tmb = _command_tokenizer.token_matches_binary
        assert tmb("coordinator-safe-commit.cmd", "coordinator-safe-commit")
        assert tmb("COORDINATOR-SAFE-COMMIT.CMD", "coordinator-safe-commit")
        assert tmb("bin/coordinator-safe-commit.cmd", "coordinator-safe-commit")

    def test_token_matches_binary_rejects_hyphen_boundary(self):
        tmb = _command_tokenizer.token_matches_binary
        assert not tmb("evil-coordinator-safe-commit", "coordinator-safe-commit")
        assert not tmb("mygit", "git")
        assert not tmb("git-foo", "git")
        assert not tmb("gitk", "git")

    def test_token_matches_binary_cmd_suffix_does_not_widen_hyphen_boundary(self):
        tmb = _command_tokenizer.token_matches_binary
        assert not tmb("evil-coordinator-safe-commit.cmd", "coordinator-safe-commit")


class TestCaseFoldNowAppliesInCommitGuardsArgv0Rewrite:

    def test_uppercase_backslash_git_exe_is_rewritten_to_forward_slash(self):
        cmd = r"C:\Git\bin\GIT.EXE commit -m 'msg'"
        rewritten = block_subagent_commit._normalize_windows_git_argv0(cmd)
        assert rewritten == "C:/Git/bin/GIT.EXE commit -m 'msg'"


class TestSharedTokenMatchesBinaryIdentity:

    def test_subagent_commit_reexports_canonical_token_matcher(self):
        assert (
            block_subagent_commit._token_matches_binary
            is _command_tokenizer.token_matches_binary
        )

    def test_reviewer_bash_outside_allowlist_reexports_canonical_token_matcher(self):
        from coordinator_core.bash_guards import block_reviewer_bash_outside_allowlist

        assert (
            block_reviewer_bash_outside_allowlist._token_matches_binary
            is _command_tokenizer.token_matches_binary
        )


class TestTokenMatchesBinaryClosesExeAndCmdBypass:


    def test_git_exe_commit_is_detected(self):
        assert block_subagent_commit._has_git_commit("git.exe commit -m x")

    def test_uppercase_git_exe_commit_is_detected(self):
        assert block_subagent_commit._has_git_commit("GIT.EXE commit -m x")

    def test_mixed_case_git_exe_commit_is_detected(self):
        assert block_subagent_commit._has_git_commit("Git.Exe commit -m x")

    def test_absolute_windows_path_git_exe_commit_is_detected(self):
        cmd = r"C:\Git\bin\git.exe commit -m x"
        assert block_subagent_commit._has_git_commit(cmd)

    def test_forward_slash_windows_path_git_exe_commit_is_detected(self):
        assert block_subagent_commit._has_git_commit("C:/Git/bin/git.exe commit -m x")

    def test_coordinator_safe_commit_exe_is_detected(self):
        assert block_subagent_commit._has_coordinator_safe_commit(
            "coordinator-safe-commit.exe -m x"
        )

    def test_coordinator_safe_commit_cmd_is_detected(self):
        assert block_subagent_commit._has_coordinator_safe_commit(
            "coordinator-safe-commit.cmd -m x"
        )

    def test_uppercase_coordinator_safe_commit_cmd_is_detected(self):
        assert block_subagent_commit._has_coordinator_safe_commit(
            "COORDINATOR-SAFE-COMMIT.CMD -m x"
        )

    def test_reviewer_allowlist_recognizes_git_exe_as_git(self):
        from coordinator_core.bash_guards import block_reviewer_bash_outside_allowlist as m

        assert m._token_matches_binary("git.exe", "git")
        assert m._token_matches_binary("GIT.EXE", "git")

    def test_reviewer_allowlist_recognizes_coordinator_doc_new_cmd(self):
        # this was a Windows-usability defect in the OPPOSITE direction from
        from coordinator_core.bash_guards import block_reviewer_bash_outside_allowlist as m

        assert m._token_matches_binary("coordinator-doc-new.cmd", "coordinator-doc-new")


    def test_evil_coordinator_safe_commit_still_not_matched(self):
        assert not block_subagent_commit._has_coordinator_safe_commit(
            "evil-coordinator-safe-commit -m x"
        )

    def test_evil_coordinator_safe_commit_cmd_still_not_matched(self):
        assert not block_subagent_commit._has_coordinator_safe_commit(
            "evil-coordinator-safe-commit.cmd -m x"
        )

    def test_mygit_commit_still_not_matched(self):
        assert not block_subagent_commit._has_git_commit("mygit commit -m x")

    def test_git_foo_commit_still_not_matched(self):
        assert not block_subagent_commit._has_git_commit("git-foo commit -m x")

    def test_gitk_commit_still_not_matched(self):
        assert not block_subagent_commit._has_git_commit("gitk commit -m x")

    def test_reviewer_allowlist_negative_controls_unaffected(self):
        from coordinator_core.bash_guards import block_reviewer_bash_outside_allowlist as m

        assert not m._token_matches_binary("evil-git", "git")
        assert not m._token_matches_binary("mygit", "git")
        assert not m._token_matches_binary("gitk", "git")
        assert not m._token_matches_binary(
            "evil-coordinator-doc-new.cmd", "coordinator-doc-new"
        )


    def test_plain_git_commit_still_detected(self):
        assert block_subagent_commit._has_git_commit("git commit -m x")

    def test_absolute_posix_git_commit_still_detected(self):
        assert block_subagent_commit._has_git_commit("/usr/bin/git commit -m x")


class TestEvaluateArityMatchesConsumers:

    _COMMANDS = [
        "echo probe",
        "git status",
        "touch .coordinator-doctrine-edit-approved",
        "git worktree add ../scratch",
        "bats tests/ | grep 'not ok'",
    ]

    def test_evaluate_returns_three_tuple(self):
        for cmd in self._COMMANDS:
            result = _sentinel_creation_guard.SentinelCreationDetector(
                "irrelevant-sentinel"
            ).evaluate(cmd)
            assert len(result) == 3
            deny, reason_kind, reason_class = result
            assert isinstance(deny, bool)

    def test_approval_sentinel_guard_check_does_not_crash(self):
        for cmd in self._COMMANDS:
            block_approval_sentinel_creation.check(_payload(cmd))

    def test_worktree_sentinel_guard_check_does_not_crash(self):
        for cmd in self._COMMANDS:
            block_worktree_sentinel_creation.check(_payload(cmd))


class TestSplitUnquotedNewlines:

    def test_newline_inside_single_quotes_stays_literal(self):
        assert _command_tokenizer.split_unquoted_newlines("echo 'a\nb'") == "echo 'a\nb'"

    def test_newline_inside_double_quotes_stays_literal(self):
        assert _command_tokenizer.split_unquoted_newlines('echo "a\nb"') == 'echo "a\nb"'

    def test_backslash_newline_inside_double_quotes_is_a_line_continuation(self):
        assert (
            _command_tokenizer.split_unquoted_newlines('echo "line one \\\nline two"')
            == 'echo "line one line two"'
        )

    def test_backslash_crlf_inside_double_quotes_is_not_a_continuation(self):
        assert (
            _command_tokenizer.split_unquoted_newlines('echo "a\\\r\nb"')
            == 'echo "a\\\r\nb"'
        )

    def test_unquoted_backslash_newline_is_a_line_continuation(self):
        assert (
            _command_tokenizer.split_unquoted_newlines("git stash \\\ndrop")
            == "git stash drop"
        )

    def test_escaped_quote_does_not_open_a_quote_span(self):
        assert (
            _command_tokenizer.split_unquoted_newlines("echo \\'\nb")
            == "echo \\';b"
        )

    def test_crlf_behaves_as_lf(self):
        assert _command_tokenizer.split_unquoted_newlines("a\r\nb") == "a;b"

    def test_plain_unquoted_newline_becomes_semicolon(self):
        assert (
            _command_tokenizer.split_unquoted_newlines("echo hi\necho bye")
            == "echo hi;echo bye"
        )

    def test_tokenize_full_command_splits_a_plain_multiline_command(self):
        tokens = _command_tokenizer.tokenize_full_command(
            "echo hi\ngit status"
        )
        assert tokens == ["echo", "hi", ";", "git", "status"]

    def test_tokenize_full_command_keeps_quoted_newline_as_one_token(self):
        tokens = _command_tokenizer.tokenize_full_command("echo 'a\nb'")
        assert tokens == ["echo", "a\nb"]


class TestPreserveWindowsBackslashesLeavesPosixEscapeUntouched:

    def test_find_exec_standalone_semicolon_lexes_identically_with_flag_on_and_off(
        self,
    ):
        cmd = r'find . -name "*.log" -exec rm {} \;'
        tokens_without_flag = _command_tokenizer.tokenize_full_command(
            cmd, preserve_windows_backslashes=False
        )
        tokens_with_flag = _command_tokenizer.tokenize_full_command(
            cmd, preserve_windows_backslashes=True
        )
        assert tokens_without_flag == tokens_with_flag
        assert tokens_with_flag == [
            "find",
            ".",
            "-name",
            "*.log",
            "-exec",
            "rm",
            "{}",
            ";",
        ]


class TestMultilineBypassClosedEndToEnd:

    def test_worktree_creation_denies_across_a_newline(self):
        from coordinator_core.bash_guards import block_worktree_creation

        result = block_worktree_creation.check(
            _payload("echo hi\ngit worktree add ../wt x")
        )
        assert result is not None

    def test_stash_destruction_denies_across_a_newline(self):
        from coordinator_core.bash_guards import block_stash_destruction

        result = block_stash_destruction.check(
            _payload("echo hi\ngit stash drop")
        )
        assert result is not None


class TestPrivilegeWrapperBypassClosed:
    """Regression for the wrapper-flag bypass found in review, 2026-07-30.

    `_skip_wrapper_own_argv` had argument-flag tables for only
    `timeout`/`nice`/`ionice`/`stdbuf`. For any other passthrough wrapper the
    walk stopped at its first separate-token flag, so command position never
    reached `git` and the guard allowed. Both shapes below were confirmed
    ALLOWED before the fix, against both guards.

    `sudo` matters most of the four: it is the prefix a caller reaches for
    immediately after a command has been refused. The `-E` case is here
    because a VALUELESS flag stopped the walk just as hard as a value-taking
    one -- an argument-flag table alone would have left it open.
    """

    WRAPPERS = ["sudo -u root ", "doas -u root ", "sudo -E ", "setsid ", "strace -f "]

    def test_stash_destruction_denies_behind_privilege_wrappers(self):
        from coordinator_core.bash_guards import block_stash_destruction

        for prefix in self.WRAPPERS:
            assert (
                block_stash_destruction.check(_payload(prefix + "git stash drop"))
                is not None
            ), "bypassed via %r" % prefix

    def test_worktree_creation_denies_behind_privilege_wrappers(self):
        from coordinator_core.bash_guards import block_worktree_creation

        for prefix in self.WRAPPERS:
            assert (
                block_worktree_creation.check(
                    _payload(prefix + "git worktree add ../wt x")
                )
                is not None
            ), "bypassed via %r" % prefix

    def test_previously_covered_wrappers_still_deny(self):
        from coordinator_core.bash_guards import block_stash_destruction

        for prefix in ["nice ", "timeout 30 ", "ionice -c2 ", "stdbuf -oL ", ""]:
            assert (
                block_stash_destruction.check(_payload(prefix + "git stash drop"))
                is not None
            ), "regressed on %r" % prefix


class TestExtractCommandSubstitutionsIsQuoteAwareWhileBalancing:

    def test_quoted_close_paren_does_not_truncate_the_span(self):
        _neutralized, subs = _command_tokenizer._extract_command_substitutions(
            """echo "$(echo ')' ; sh -c 'x')" """
        )
        assert len(subs) == 1, "expected one substitution, got %r" % (subs,)
        assert "sh -c 'x'" in subs[0], "span truncated at the quoted `)`: %r" % (subs[0],)

    def test_quoted_open_paren_does_not_extend_the_span(self):
        _neutralized, subs = _command_tokenizer._extract_command_substitutions(
            """echo "$(echo '(' )" tail"""
        )
        assert len(subs) == 1, "expected one substitution, got %r" % (subs,)
        assert "tail" not in subs[0], "span over-ran its closing paren: %r" % (subs[0],)

    def test_double_quoted_paren_inside_single_quoted_span_is_literal(self):
        _neutralized, subs = _command_tokenizer._extract_command_substitutions(
            '''echo "$(echo '")"' ; sh -c 'x')"'''
        )
        assert len(subs) == 1, "expected one substitution, got %r" % (subs,)
        assert "sh -c 'x'" in subs[0], "nested-quote desync: %r" % (subs[0],)

    def test_single_quotes_still_suppress_substitution(self):
        _neutralized, subs = _command_tokenizer._extract_command_substitutions(
            """echo '$(sh -c "x")'"""
        )
        assert subs == [], "single-quoted text must yield no substitutions: %r" % (subs,)

    def test_unterminated_substitution_still_captures_to_end(self):
        _neutralized, subs = _command_tokenizer._extract_command_substitutions(
            """echo "$(sh -c 'x'"""
        )
        assert len(subs) == 1, "expected one substitution, got %r" % (subs,)
        assert "sh -c 'x'" in subs[0], "unterminated span dropped content: %r" % (subs[0],)
