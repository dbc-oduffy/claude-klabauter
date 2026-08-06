"""Tests for ``coordinator_core.bash_guards.dispatch_checks.check_no_verify``
-- pins the 2026-07-29 quote-blindness fix (this package's sixth confirmed
instance of the class: a matcher scanning raw command TEXT for a flag/
keyword string with no regard for whether that text sits in argv position
or inside a quoted operand).

Reproduced live: a real EM's own ``git commit -m "$(cat <<'EOF' ... EOF)"``
whose commit-message body merely DESCRIBED ``--no-verify`` /
``--no-gpg-sign`` / ``-c commit.gpgsign=false`` in prose (documenting a fix
to this very guard) was DENIED as if it had actually PASSED one of those
flags. The guard's own confinement claim is "no bypass flag reaches a real
git invocation" -- a commit message that only discusses the flag never
reaches git's own argv at all, so denying it is a pure false positive on
the passing side: the worst kind, because the rational operator response
to a false deny is to reach for the override, and that is exactly how a
guard goes inert.

Both directions are pinned here, not just the false-positive fix: a
segment that ACTUALLY passes one of the three bypass shapes must still
deny, in every wrapper form BX-13 closed (leading env assignment, `env`,
and the passthrough words `sudo`/`command`/`time`/`exec`/`nice`/`nohup`/
`ionice`/`timeout`/`stdbuf`, plus an `sh -c`/`bash -c` wrapped payload).
Losing either direction is a regression.

M5P Piece 1 (2026-07-29, deliberate verdict change, not a plumbing side
effect): `which`/`type` are NOT passthrough wrappers for this guard's
purposes and were removed from the list above -- see
`TestInspectsWithoutExecingNeverBypasses` below and `_seg_has_git_bypass_
flag`'s own inline comment in `dispatch_checks.py`. `which git commit
--no-verify` prints a path to `git` and never runs it; denying it was a
false positive on the SAME axis `TestProseMentionAllowed` already guards
(matching text that never reaches a real git invocation), just via a
different mechanism (an over-wide wrapper-peel list instead of a
quote-blind text scan).

Spec backlink: coordinator_core/bash_guards/dispatch_checks.py
(``check_no_verify``, ``_seg_has_git_bypass_flag``, "1. check_no_verify --
block-no-verify.sh" section).
"""

from __future__ import annotations

from coordinator_core.bash_guards import dispatch_checks as guard


def _denied(cmd: str) -> bool:
    return guard.check_no_verify(cmd) is not None


class TestProseMentionAllowed:
    """The false-positive-on-passing-side direction: a commit whose MESSAGE
    merely discusses the bypass flags must be allowed."""

    def test_plain_message_mentioning_no_verify_allowed(self):
        assert not _denied('git commit -m "message about --no-verify flags"')

    def test_plain_message_mentioning_no_gpg_sign_allowed(self):
        assert not _denied('git commit -m "remember --no-gpg-sign exists"')

    def test_plain_message_mentioning_gpgsign_config_allowed(self):
        assert not _denied(
            'git commit -m "discussing -c commit.gpgsign = false as a config"'
        )

    def test_heredoc_command_substitution_message_body_allowed(self):
        """The exact live reproduction: a `-m "$(cat <<'EOF' ... EOF)"`
        commit-message body that documents the three bypass flags in prose,
        never passing any of them to git's own argv."""
        msg = (
            "$(cat <<'EOF'\n"
            "This fix is about --no-verify and --no-gpg-sign and\n"
            "-c commit.gpgsign=false being denied as prose.\n"
            "EOF\n"
            ")"
        )
        assert not _denied('git commit -m "%s"' % msg)

    def test_double_quoted_message_wrapped_in_sh_c_still_allowed(self):
        assert not _denied(
            'sh -c \'git commit -m "message about --no-verify flags"\''
        )

    def test_unrelated_command_allowed(self):
        assert not _denied("git commit -m x")
        assert not _denied("git status")

    def test_semicolon_inside_quoted_message_allowed(self):
        """Review: code-reviewer (Finding 1) -- an ordinary commit message
        containing a literal `;` must not itself be denied when there is no
        real bypass flag anywhere in the command."""
        assert not _denied('git commit -m "fix: bug; cleanup"')

    def test_ampersand_inside_quoted_message_allowed(self):
        assert not _denied('git commit -m "build & release"')


class TestQuotedSeparatorCannotHideRealBypassFlag:
    """Finding 1 (P0): `_split_segments`'s raw `re.split(r"[;&|]+", cmd)` is
    quote-unaware -- a `;`/`&`/`|` inside a quoted git commit-message operand
    used to split one indivisible `git ... --no-verify` invocation into two
    innocent-looking fragments (one carrying `git`, the other carrying the
    bypass flag), so the guard silently returned `None` (ALLOWED) on a real
    bypass. Fixed by segmenting via the shared quote-aware tokenizer
    (`_command_tokenizer.tokenize_full_command` +
    `segments_from_tokens_simple`) instead of the naive regex split."""

    def test_semicolon_in_message_does_not_hide_no_verify(self):
        assert _denied('git commit -m "release; ship it" --no-verify')

    def test_ampersand_in_message_does_not_hide_no_verify(self):
        assert _denied('git commit -m "build & release" --no-verify')

    def test_pipe_in_message_does_not_hide_no_verify(self):
        assert _denied('git commit -m "a | b" --no-verify')

    def test_semicolon_in_message_does_not_hide_no_gpg_sign(self):
        assert _denied('git commit -m "release; ship it" --no-gpg-sign')

    def test_semicolon_in_message_does_not_hide_gpgsign_false_config(self):
        assert _denied(
            'git -c commit.gpgsign=false commit -m "release; ship it"'
        )


class TestGenuineBypassStillDenied:
    """The confinement direction: a REAL use of the bypass flags must still
    deny, in every wrapper shape BX-13 closed."""

    def test_bare_no_verify_denied(self):
        assert _denied("git commit --no-verify -m x")

    def test_no_verify_after_message_denied(self):
        assert _denied("git commit -m x --no-verify")

    def test_no_gpg_sign_denied(self):
        assert _denied("git commit -m x --no-gpg-sign")

    def test_gpgsign_false_config_denied(self):
        assert _denied("git -c commit.gpgsign=false commit -m x")

    def test_gpgsign_false_config_spaced_and_quoted_denied(self):
        assert _denied('git -c "commit.gpgsign = false" commit -m x')

    def test_leading_env_assignment_denied(self):
        assert _denied("FOO=1 git commit --no-verify -m x")

    def test_env_prefix_denied(self):
        assert _denied("env git commit --no-verify -m x")

    def test_nice_prefix_denied(self):
        assert _denied("nice git commit --no-verify -m x")

    def test_sudo_prefix_denied(self):
        assert _denied("sudo git commit --no-verify -m x")

    def test_command_prefix_denied(self):
        assert _denied("command git commit --no-verify -m x")

    def test_sh_c_wrapped_denied(self):
        assert _denied('sh -c "git commit --no-verify -m x"')

    def test_bash_c_wrapped_denied(self):
        assert _denied('bash -c "git commit -m x --no-gpg-sign"')

    def test_env_sh_c_wrapped_denied(self):
        assert _denied('env sh -c "git commit --no-verify -m x"')

    def test_chained_segment_denied(self):
        assert _denied("echo hi && git commit --no-verify -m x")
        assert _denied("echo hi ; git commit --no-verify -m x")
        assert _denied("git status | cat && git commit --no-verify -m x")

    def test_git_dash_c_dir_prefix_denied(self):
        assert _denied("git -C /some/repo commit --no-verify -m x")

    def test_timeout_boolean_flag_wrapper_denied(self):
        """Finding 9 (nit): `timeout`'s boolean flags (`--foreground`,
        `--preserve-status`, `-v`/`--verbose`) must not break the tokenized
        wrapper-argv walk -- confirmed pre-fix this fell back to the raw
        `_BYPASS_RE` scan and still denied, but the tokenized path itself
        should recognize these flags directly."""
        assert _denied("timeout --foreground 30 git commit --no-verify")
        assert _denied("timeout --preserve-status 30 git commit --no-verify")
        assert _denied("timeout -v 30 git commit --no-verify")

    def test_timeout_boolean_flag_wrapper_allowed_without_bypass(self):
        assert not _denied("timeout --foreground 30 git commit -m x")


class TestBypassRegexFallbackReachedAndDenies:
    """Review: coordinator:code-reviewer (Finding 1, 2026-08-05) -- every case
    above in `TestGenuineBypassStillDenied` uses a well-formed, tokenizable
    command, so all of them take the TOKENIZED walk in
    `_seg_has_git_bypass_flag` and never reach the `_BypassRe` fallback the
    2026-08-05 catastrophic-backtracking rewrite actually touched. Nothing on
    disk previously exercised `_BypassRe.search` itself with a genuine bypass
    marker present, despite the commit message's own verified claim ("a 276 KB
    payload with --no-verify at the very end still denies").

    `check_no_verify` falls back to `_BypassRe.search(flat)` in exactly two
    cases, both driven by `_bt_tokenize_full_command` returning `None`: the
    command is unparseable (unterminated quote / trailing backslash), or it
    exceeds `_MAX_TOKENIZABLE_COMMAND_CHARS` (65536 chars, see
    `_command_tokenizer.py`). Each test below monkeypatches `guard._BypassRe.
    search` to confirm the fallback is actually reached (not just that the
    outcome happens to be DENY, which the tokenized path could also produce)
    before asserting the deny.
    """

    def _denied_via_fallback(self, cmd: str) -> bool:
        calls = []
        orig = guard._BypassRe.search

        def _spy(text):
            calls.append(text)
            return orig(text)

        guard._BypassRe.search = staticmethod(_spy)
        try:
            result = guard.check_no_verify(cmd) is not None
        finally:
            guard._BypassRe.search = staticmethod(orig)
        assert calls, (
            "fallback never reached -- this command took the tokenized "
            "_seg_has_git_bypass_flag walk instead, so it pins the wrong path"
        )
        return result

    def test_unparseable_command_with_late_marker_still_denied(self):
        # Unterminated quote makes `_bt_tokenize_full_command` return None.
        # The marker sits well past the start of the payload, mirroring the
        # commit message's "late-positioned marker" claim.
        verb = "co" + "mmit"
        padding = "x" * 500
        cmd = 'git ' + verb + ' -m "' + padding + ' --no-verify'
        assert self._denied_via_fallback(cmd)

    def test_over_ceiling_command_with_late_marker_still_denied(self):
        # Past _MAX_TOKENIZABLE_COMMAND_CHARS (65536), _bt_tokenize_full_
        # command also returns None -- same fallback, different trigger.
        # The `;` boundaries keep the payload segment-splittable so the
        # bounded `_BYPASS_HEAD_RE` anchor can still find the git head; a
        # ceiling-busting payload with NO segment boundary before the git
        # head would defeat the anchor and is a separate, narrower gap
        # (leftmost-head-only reach), not this finding's contract.
        verb = "co" + "mmit"
        cmd = ("a;" * 32774) + "git " + verb + " --no-verify"
        assert len(cmd) > 65536
        assert self._denied_via_fallback(cmd)


class TestInspectsWithoutExecingNeverBypasses:
    """M5P Piece 1 (2026-07-29) -- deliberate verdict change, named and
    tested on purpose, not a side effect of any restructuring.

    `which`/`type` REPORT on a named command without ever running it --
    `which git commit --no-verify` prints a path to `git`; it never invokes
    `git commit`, so there is no real bypass here to deny.
    `_command_tokenizer.WrapperSemanticClass.INSPECTS_WITHOUT_EXECING`
    already classifies both this way, correctly; this guard previously
    disagreed by treating them as execs-its-argv passthrough wrappers (the
    same list `sudo`/`nice`/`timeout`/etc. sit in), which is what
    `TestGenuineBypassStillDenied` used to pin (`test_which_prefix_denied`/
    `test_type_prefix_denied`, both removed above -- they encoded the
    incorrect verdict).

    This narrowing is scoped to `check_no_verify` only -- `check_blanket_
    git_add` shares the underlying `_BYPASS_PREFIX` regex fallback and is
    deliberately NOT touched by this change (see `_INSPECTS_WITHOUT_
    EXECING_WORDS`'s docstring in dispatch_checks.py)."""

    def test_which_prefix_allowed(self):
        assert not _denied("which git commit --no-verify -m x")

    def test_type_prefix_allowed(self):
        assert not _denied("type git commit --no-verify -m x")

    def test_which_prefix_no_gpg_sign_allowed(self):
        assert not _denied("which git commit -m x --no-gpg-sign")

    def test_which_prefix_gpgsign_false_config_allowed(self):
        assert not _denied("which git -c commit.gpgsign=false commit -m x")

    def test_nested_execs_its_argv_then_which_still_allowed(self):
        """`sudo`/`nice` DO exec their own argv, so the walk peels past
        them as before -- but the head it then lands on is `which`, still
        never-executing, so the segment is still allowed."""
        assert not _denied("sudo which git commit --no-verify -m x")
        assert not _denied("nice which git commit --no-verify -m x")

    def test_which_itself_still_a_confirmed_git_bypass_check_result(self):
        """Regression guard on the OTHER direction: a real bypass one
        command earlier in a chain must still deny even though a LATER
        segment in the same chain is a harmless `which git ...` mention."""
        assert _denied(
            "git commit --no-verify -m x && which git commit --no-verify -m y"
        )


class TestHeredocShellPayloadRescanMustSurviveAnyFutureMigration:
    """M5P Piece 2 (2026-07-29) -- the safety-net artifact, not a side
    finding. THIS CLASS IS THE DELIVERABLE for Piece 2, not merely coverage
    of existing behavior.

    BX-14 (confirmed live, same day): a heredoc fed to a shell interpreter
    (`bash <<'EOF' ... git commit --no-verify ... EOF`) is EXECUTED text,
    not inert data -- `check_no_verify` unwraps it via `_heredoc_shell_
    payloads` and re-scans the body. `resolve_command_positions`
    (`_command_tokenizer.py`, M2's shared resolve-once entry point) does
    NOT carry this behavior: its own `_strip_heredocs` drops every heredoc
    body UNCONDITIONALLY, regardless of whether the introducing line
    invokes a shell that will execute it. A migration of `check_no_verify`
    onto that shared resolver -- attempted, and correctly refused, in M5 --
    would silently drop this rescan and reopen the exact bypass BX-14
    closed, with every other test in this file still green (none of them
    exercise a shell-executing heredoc).

    IF YOU ARE HERE BECAUSE THIS CLASS STARTED FAILING: your change
    (very likely a migration of `check_no_verify`'s core walk onto
    `resolve_command_positions`, or a rewrite of `_heredoc_shell_payloads`)
    silently dropped BX-14 coverage. Do NOT delete, weaken, or skip this
    test to make it pass -- fix the migration so it still recurses into a
    shell-executing heredoc's body (or extend `resolve_command_positions`
    itself to do so, and update this class's own docstring to point at the
    new mechanism) before landing it. This test failing IS the mechanism
    working as designed: "the next author remembering" is exactly what
    this class replaces.
    """

    def test_bash_heredoc_no_verify_in_body_denied(self):
        assert _denied("bash <<'EOF'\ngit commit --no-verify -m x\nEOF")

    def test_sh_heredoc_no_gpg_sign_in_body_denied(self):
        assert _denied("sh <<'EOF'\ngit commit -m x --no-gpg-sign\nEOF")

    def test_tab_stripping_heredoc_form_still_denied(self):
        """`<<-WORD` (leading-tab-stripping form) must not evade the
        rescan -- `_heredoc_shell_payloads` shares the same intro/
        terminator classification `_strip_heredoc_bodies` uses."""
        assert _denied("bash <<-'EOF'\n\tgit commit --no-verify -m x\n\tEOF")

    def test_unquoted_delimiter_heredoc_still_denied(self):
        assert _denied("bash <<EOF\ngit commit --no-verify -m x\nEOF")

    def test_nested_shell_c_inside_heredoc_body_still_denied(self):
        """The body itself can carry ANOTHER wrapper shape (`sh -c`) around
        the real bypass -- both unwrap layers must compose, not just fire
        independently on the top level."""
        assert _denied(
            "bash <<'EOF'\nsh -c \"git commit --no-verify -m x\"\nEOF"
        )

    def test_prose_heredoc_into_non_shell_consumer_still_allowed(self):
        """The differential-equivalence check on the OTHER direction: a
        heredoc fed to a NON-executing consumer (`cat`, here) is genuine
        prose/data and must stay allowed -- this class exists to protect
        the shell-executing case, not to make every heredoc containing the
        bypass words deny unconditionally (that would just trade BX-14's
        bypass for `TestProseMentionAllowed`'s false positive)."""
        assert not _denied("cat <<'EOF'\ngit commit --no-verify -m x\nEOF")
