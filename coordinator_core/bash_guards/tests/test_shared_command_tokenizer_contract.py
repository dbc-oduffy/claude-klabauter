"""Contract tests for coordinator_core.bash_guards._command_tokenizer.

This is the deliverable, not the consolidation itself (see
``_command_tokenizer.py``'s own module docstring for the incident this
responds to). Two failure classes are pinned here:

1. **Re-duplication.** Before 2026-07-29, ``_normalize_executable_basename``/
   ``_tokenize_full_command``/``_segments_from_tokens`` existed in two
   independently hand-maintained copies (``block_subagent_commit.py`` and
   ``block_subagent_destructive_action.py``), and had already silently
   drifted (a missing case-fold step in one copy). ``TestSingleSourceOfTruth``
   asserts every guard module's imported name is object-identical to the
   canonical function -- a future edit that re-introduces a local
   redefinition (shadowing the import) fails this immediately, rather than
   drifting silently again.

2. **Return-shape drift.** The two ``_segments_from_tokens`` shapes
   (``List[List[str]]`` vs. ``list[tuple[list[str], bool]]``) must stay
   pinned to their own callers' expectations. ``TestReturnShapes`` locks the
   exact shape each public function returns.

Neither class is the exact bug that bricked Bash fleet-wide on 2026-07-28
(that was a torn edit to `_sentinel_creation_guard.evaluate()`'s OWN return
arity, unrelated to this trio -- see the two `cross-repo/inbox/` memos on
the sentinel-guard crash, and `_sentinel_creation_guard.py`'s own
`Tuple[bool, str, str]` annotation) -- but it is the SAME shape of failure
(a shared helper's contract changing underneath a `fail_closed=True`
consumer, discovered only via a live `ValueError`), applied to the
different shared surface that DOES exist between these guards today.
``TestEvaluateArityMatchesConsumers`` guards the sentinel-guard incident's
own arity directly, end to end, through the real dispatch entrypoints.
"""

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
    """Every guard's imported trio name must be the SAME function object as
    the canonical one in ``_command_tokenizer`` -- not a re-copy that
    happens to behave the same today. `is` (identity), not `==`
    (equivalence): a redefinition that is behaviorally identical at the
    moment it's written still reintroduces the maintenance hazard this
    module exists to close, and `is` is the only check that catches that.
    """

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
    """Pin the exact return shape of each public function -- the specific
    thing a future "simplify this" edit is most likely to quietly change.
    """

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
        # Third segment (`bash`) was immediately preceded by a `|`.
        assert segments[-1] == (["bash"], True)
        # First segment (`echo ...`) was not.
        assert segments[0][1] is False

    def test_segments_from_tokens_simple_shape(self):
        tokens = _command_tokenizer.tokenize_full_command("git commit -m x ; ls")
        segments = _command_tokenizer.segments_from_tokens_simple(tokens)
        assert isinstance(segments, list)
        for item in segments:
            assert isinstance(item, list)
            assert not isinstance(item, tuple)

    def test_simple_and_pipe_flag_partition_identically(self):
        """The two shapes must agree on WHICH tokens land in which segment
        -- only the pipe-flag annotation may differ. `segments_from_tokens_
        simple` is derived from the pipe-flag variant precisely so this
        can never drift apart again.
        """
        tokens = _command_tokenizer.tokenize_full_command(
            "git commit -m x ; ls -la | grep foo"
        )
        with_flag = _command_tokenizer.segments_from_tokens_with_pipe_flag(tokens)
        simple = _command_tokenizer.segments_from_tokens_simple(tokens)
        assert simple == [seg for seg, _pipe_before in with_flag]

    def test_normalize_executable_basename_case_folds(self):
        # The specific drift this consolidation closed: block_subagent_
        # commit.py's own prior copy did not lowercase, so `GIT.EXE` at
        # argv0 position silently bypassed that one guard's detection.
        assert _command_tokenizer.normalize_executable_basename("GIT.EXE") == "git"
        assert _command_tokenizer.normalize_executable_basename("Git.exe") == "git"
        assert _command_tokenizer.normalize_executable_basename("git") == "git"
        assert _command_tokenizer.normalize_executable_basename("gitk") == "gitk"

    def test_normalize_executable_basename_strips_trailing_dots_and_spaces(self):
        # code-reviewer Finding 3 (2026-07-29): NTFS silently strips
        # trailing dots/spaces at resolution time, so `git.exe.`/`git.exe `
        # resolve to `git.exe` on a real Windows invocation -- same
        # OS-normalization axis as the `.exe`/`.cmd` suffix strip, one
        # character further along it.
        neb = _command_tokenizer.normalize_executable_basename
        assert neb("git.exe.") == "git"
        assert neb("git.exe ") == "git"
        assert neb("git.exe...") == "git"
        assert neb("git.exe. ") == "git"
        assert neb(r"C:\Program Files\Git\bin\git.exe.") == "git"
        # Must not over-broaden: a trailing dot/space on a DIFFERENT
        # basename still doesn't collapse into "git".
        assert neb("gitk.") == "gitk"
        assert neb("mygit ") == "mygit"

    def test_normalize_executable_basename_preserves_all_dot_source_tokens(self):
        # Regression guard: a token that is ENTIRELY dots/spaces (POSIX `.`
        # dot-source, `..` parent-dir) must survive intact -- these are
        # meaningful shell tokens in their own right, not OS-normalization
        # noise on a real filename. Caught while landing the trailing-dot
        # strip above: it silently emptied `.` and broke
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
        # coordinator-safe-commit.cmd is this project's OWN generated
        # Windows launcher twin (coordinator/bin/gen-launcher-shim.py),
        # confirmed present on disk -- not a hypothetical spelling.
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
        # The .cmd widening must not turn a hyphen boundary into a
        # separator boundary -- stripping happens on the TOKEN's own
        # basename, never on the binary name being compared against.
        tmb = _command_tokenizer.token_matches_binary
        assert not tmb("evil-coordinator-safe-commit.cmd", "coordinator-safe-commit")


class TestCaseFoldNowAppliesInCommitGuardsArgv0Rewrite:
    """`block_subagent_commit._normalize_windows_git_argv0` decides whether
    to rewrite a backslash Windows argv0 path to forward-slash form by
    checking `_normalize_executable_basename(token) == "git"`. Before
    consolidation, this module's own copy of that helper did not
    case-fold, so an uppercase-spelled `GIT.EXE`/`Git.exe` path was never
    rewritten (while `block_subagent_destructive_action.py`'s already-
    case-folded copy did rewrite the equivalent path). Consolidating onto
    the canonical, case-folded helper closes that inconsistency.

    UPDATE (2026-07-29, part 2) -- the follow-up gap this class's docstring
    used to flag as separate and unfixed is now closed too:
    `_token_matches_binary` (both here and in
    `block_reviewer_bash_outside_allowlist.py`) is no longer an own-module
    exact-`/git`-suffix match -- it now delegates to `_command_tokenizer.
    token_matches_binary`, which strips a `.exe` suffix case-insensitively
    before comparing. A `git.exe`-spelled invocation (lowercase or
    uppercase) is now recognized as `git` by `_tokens_reach_commit_after_
    git`, end to end. See `TestTokenMatchesBinaryClosesExeAndCmdBypass`
    below for the direct regression (which also covers the `.cmd` twin of
    this same bypass, found in the same follow-up).
    """

    def test_uppercase_backslash_git_exe_is_rewritten_to_forward_slash(self):
        cmd = r"C:\Git\bin\GIT.EXE commit -m 'msg'"
        rewritten = block_subagent_commit._normalize_windows_git_argv0(cmd)
        assert rewritten == "C:/Git/bin/GIT.EXE commit -m 'msg'"


class TestSharedTokenMatchesBinaryIdentity:
    """Both consumers of the canonical `token_matches_binary` matcher must
    import the SAME function object, not a re-copy -- the same
    single-source-of-truth discipline `TestSingleSourceOfTruth` applies to
    the tokenizer trio.
    """

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
    """Direct regression for two confirmed bypasses, closed in the same
    change: (1, 2026-07-29 part 1) a subagent running `git.exe commit -m x`
    (the ordinary Windows spelling of `git commit`) was silently ALLOWED by
    `block_subagent_commit.py` because `_token_matches_binary` never
    stripped a `.exe` suffix; (2, 2026-07-29 part 2) a subagent running
    `coordinator-safe-commit.cmd -m x` -- THIS PROJECT'S OWN generated
    Windows launcher twin for that helper, confirmed present on disk at
    `coordinator/bin/coordinator-safe-commit.cmd`, not a hypothetical
    spelling -- was ALSO silently ALLOWED, for the same underlying reason
    (`.cmd` was not stripped either). Windows is this project's primary
    platform, so both are the ordinary invocation form, not an exotic edge
    case. Exercised through the real detector entrypoints (`_has_git_
    commit`, `_has_coordinator_safe_commit`), not just the bare matcher, so
    a future edit that reintroduces either gap at a call-site level (not
    just inside `token_matches_binary` itself) is still caught.
    """

    # -- red cases: newly-recognized spellings, must now be caught --

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
        # THE confirmed bypass: coordinator-safe-commit.cmd (the real,
        # on-disk generated Windows launcher twin) was silently ALLOWED
        # before this fix.
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
        # coordinator-doc-new.cmd is ALSO a real, on-disk generated launcher
        # twin (coordinator/bin/coordinator-doc-new.py.cmd). Before this fix,
        # this was a Windows-usability defect in the OPPOSITE direction from
        # the git.exe/coordinator-safe-commit.cmd bypasses: the Tier B
        # scaffolder-allow gate (_first_token_is_allowlisted_binary) would
        # have wrongly DENIED the ordinary Windows invocation of a
        # legitimately-allowed tool, not admitted something that should be
        # denied.
        from coordinator_core.bash_guards import block_reviewer_bash_outside_allowlist as m

        assert m._token_matches_binary("coordinator-doc-new.cmd", "coordinator-doc-new")

    # -- negative controls: the widening must not swallow these --

    def test_evil_coordinator_safe_commit_still_not_matched(self):
        assert not block_subagent_commit._has_coordinator_safe_commit(
            "evil-coordinator-safe-commit -m x"
        )

    def test_evil_coordinator_safe_commit_cmd_still_not_matched(self):
        # The .cmd widening must not turn the hyphen boundary into a
        # separator boundary.
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

    # -- plain git commit still detected (pre-existing behavior, unchanged) --

    def test_plain_git_commit_still_detected(self):
        assert block_subagent_commit._has_git_commit("git commit -m x")

    def test_absolute_posix_git_commit_still_detected(self):
        assert block_subagent_commit._has_git_commit("/usr/bin/git commit -m x")


class TestEvaluateArityMatchesConsumers:
    """Direct regression for the 2026-07-28 fleet-wide Bash brick: `_sentinel_
    creation_guard.SentinelCreationDetector.evaluate()`'s return arity (a
    3-tuple, `Tuple[bool, str, str]`) must match what BOTH of its registered
    fail_closed=True consumers unpack. Exercised end-to-end through the real
    `check()` entrypoints (not just introspected) so a future arity change
    in one place without the other fails a test instead of bricking a live
    session's Bash tool.
    """

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
            # Must not raise ValueError (the exact crash class from the
            # 2026-07-28 incident) regardless of allow/deny verdict.
            block_approval_sentinel_creation.check(_payload(cmd))

    def test_worktree_sentinel_guard_check_does_not_crash(self):
        for cmd in self._COMMANDS:
            block_worktree_sentinel_creation.check(_payload(cmd))


class TestSplitUnquotedNewlines:
    """`_command_tokenizer.split_unquoted_newlines` -- the 2026-07-30 fix for
    the multi-line-Bash-command bypass (`tokenize_full_command`'s shlex pass
    ran with `whitespace_split=True`, which silently consumed an unquoted
    newline as ordinary whitespace instead of emitting a separator, folding
    every line after the first into the first line's segment). Covers each
    semantic bullet from this function's own docstring directly, plus
    `tokenize_full_command` end to end for the plain multi-line case."""

    def test_newline_inside_single_quotes_stays_literal(self):
        assert _command_tokenizer.split_unquoted_newlines("echo 'a\nb'") == "echo 'a\nb'"

    def test_newline_inside_double_quotes_stays_literal(self):
        assert _command_tokenizer.split_unquoted_newlines('echo "a\nb"') == 'echo "a\nb"'

    def test_backslash_newline_inside_double_quotes_is_a_line_continuation(self):
        # Review: coordinator:code-reviewer P2 -- `\<newline>` (LF, not
        # preceded by CR) is a real shell line continuation EVEN inside
        # double quotes, confirmed empirically against real bash
        # (`x="line one \`<newline>`line two"` -> `x=line one line two`,
        # no embedded newline, no separator). Both characters are removed,
        # same as the unquoted case below -- this was the actual P2 defect:
        # the code used to copy the backslash+newline pair through
        # literally instead of stripping it.
        assert (
            _command_tokenizer.split_unquoted_newlines('echo "line one \\\nline two"')
            == 'echo "line one line two"'
        )

    def test_backslash_crlf_inside_double_quotes_is_not_a_continuation(self):
        # Deliberate divergence from the LF-only case directly above,
        # confirmed empirically against real bash: a backslash followed by
        # `\r` (not an immediate `\n`) does not match bash's in-quote
        # escape rule (only `$`/backtick/`"`/`\`/an actual `<newline>`
        # qualify), so the backslash is NOT consumed -- both the backslash
        # and the `\r` pass through literally, and the `\n` that follows is
        # then handled by the plain "newline inside double quotes stays
        # literal" rule (test_newline_inside_double_quotes_stays_literal
        # above), not stripped as part of a continuation.
        assert (
            _command_tokenizer.split_unquoted_newlines('echo "a\\\r\nb"')
            == 'echo "a\\\r\nb"'
        )

    def test_unquoted_backslash_newline_is_a_line_continuation(self):
        # Both the backslash and the newline are removed -- the two lines
        # join with no separator emitted, so a continued command is not
        # split in two.
        assert (
            _command_tokenizer.split_unquoted_newlines("git stash \\\ndrop")
            == "git stash drop"
        )

    def test_escaped_quote_does_not_open_a_quote_span(self):
        # An unquoted, escaped single-quote must not be read as opening a
        # quote -- the newline that follows it stays subject to conversion.
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
    """`preserve_windows_backslashes` (commit `05fb6ef70`) masks unquoted
    backslashes with a private-use sentinel before `shlex` runs and
    un-masks them afterward, leaving `shlex.escape` at its POSIX default --
    chosen over the alternative of setting `lex.escape = ""`, because that
    alternative would have disturbed POSIX escape handling. The canonical
    example (`state/audits/2026-08-07-bash-guard-tokenizer-eats-windows-path-separators.md`)
    is `find . -name "*.log" -exec rm {} \\;`, whose standalone `\\;` must
    still lex to `;` identically whether the flag is on or off -- a change
    that breaks this is a regression toward the rejected `lex.escape = ""`
    shape, not a Windows-path fix. This is the discriminator: no existing
    test in this file or `test_bump_foreign_repo_write.py` /
    `test_bump_outside_repo_write.py` pins this find/exec case."""

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
    """Regression for the bypass itself, through the real guard
    entrypoints -- reproduces exactly the two commands the 2026-07-30 dispatch
    brief used to confirm the gap before any fix landed."""

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
        """The four wrappers that already had tables must be unaffected by the
        additions -- this fix widens coverage, it must not perturb it."""
        from coordinator_core.bash_guards import block_stash_destruction

        for prefix in ["nice ", "timeout 30 ", "ionice -c2 ", "stdbuf -oL ", ""]:
            assert (
                block_stash_destruction.check(_payload(prefix + "git stash drop"))
                is not None
            ), "regressed on %r" % prefix
