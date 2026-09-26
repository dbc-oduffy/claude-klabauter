
from __future__ import annotations

import os
import shlex

import pytest

from coordinator_core.bash_guards import dispatch_checks
from coordinator_core.bash_guards import guard_offer_git_c as guard


def _reason(out):
    assert out is not None, "expected a deny/rewrite envelope, got allow (None)"
    return out["hookSpecificOutput"]


def _deny_reason(out) -> str:
    hso = _reason(out)
    assert hso["permissionDecision"] == "deny", (
        "expected deny, got %r" % hso.get("permissionDecision")
    )
    return hso["permissionDecisionReason"]


def _rewritten_command(out) -> str:
    hso = _reason(out)
    assert hso["permissionDecision"] == "allow", (
        "expected an allow-rewrite, got %r" % hso.get("permissionDecision")
    )
    return hso["updatedInput"]["command"]


@pytest.fixture(autouse=True)
def _clean_override(monkeypatch):
    monkeypatch.delenv("COORDINATOR_ALLOW_CD_PREFIX", raising=False)


class TestBareCdGitStillDenies:

    def test_bare_cd_and_git_auto_rewrites(self):
        out = guard.check_offer_git_c("cd /repo && git log -1")
        assert _rewritten_command(out) == "git -C /repo log -1"


class TestTildeTargetExpandsBeforeQuoting:

    def test_leading_tilde_is_expanded_not_quoted(self):
        home = os.path.expanduser("~")
        out = guard.check_offer_git_c("cd ~/X/peer && git log -1")
        rewritten = _rewritten_command(out)

        assert "~" not in rewritten
        assert rewritten == "git -C %s log -1" % shlex.quote(home + "/X/peer")

    def test_bare_tilde_target_is_expanded(self):
        out = guard.check_offer_git_c("cd ~ && git status")
        rewritten = _rewritten_command(out)

        assert rewritten == "git -C %s status" % shlex.quote(os.path.expanduser("~"))

    def test_non_tilde_target_is_byte_identical_to_before(self):
        out = guard.check_offer_git_c("cd /repo/sub && git log -1")
        assert _rewritten_command(out) == "git -C /repo/sub log -1"


class TestEnvPrefixEvasionNowDenies:

    def test_single_env_assignment_before_cd_denies(self):
        out = guard.check_offer_git_c("FOO=1 cd /repo && git log -1")
        reason = _deny_reason(out)
        assert "git -C /repo log -1" in reason

    def test_multiple_env_assignments_before_cd_denies(self):
        out = guard.check_offer_git_c("FOO=1 BAR=2 cd /repo && git log -1")
        reason = _deny_reason(out)
        assert "git -C /repo log -1" in reason


class TestWrapperWordEvasionNowDenies:
    """The same evasion shape via a wrapper word instead of an env
    assignment -- `_FIND_WRAPPER_WORDS` covers both."""

    def test_env_wrapper_word_before_cd_denies(self):
        out = guard.check_offer_git_c("env cd /repo && git log -1")
        _deny_reason(out)

    def test_sudo_wrapper_word_before_cd_denies(self):
        out = guard.check_offer_git_c("sudo cd /repo && git log -1")
        _deny_reason(out)


class TestGitSidePrefixAutoRewritesCarryingThePrefix:

    def test_env_assignment_before_git_auto_rewrites(self):
        out = guard.check_offer_git_c("cd /repo && FOO=1 git log -1")
        assert _rewritten_command(out) == "FOO=1 git -C /repo log -1"


class TestRewriteCasePreservesPrefix:

    def test_prefix_survives_into_rewrite_not_dropped(self):
        out = guard.check_offer_git_c("cd /repo && FOO=1 git status")
        assert _rewritten_command(out) == "FOO=1 git -C /repo status"


class TestNicePrefixOnGitSegmentAutoRewrites:

    def test_nice_bare_numeric_before_git_auto_rewrites_carrying_nice_forward(self):
        out = guard.check_offer_git_c("cd /repo && nice -19 git log -1")
        assert _rewritten_command(out) == "nice -19 git -C /repo log -1"

    def test_nice_n_flag_form_before_git_auto_rewrites(self):
        out = guard.check_offer_git_c("cd /repo && nice -n 19 git status")
        assert _rewritten_command(out) == "nice -n 19 git -C /repo status"

    def test_nice_before_git_with_all_git_followers_auto_rewrites(self):
        out = guard.check_offer_git_c(
            "cd /tmp/repo && nice -19 git status && git log -1"
        )
        assert _rewritten_command(out) == (
            "nice -19 git -C /tmp/repo status && git -C /tmp/repo log -1"
        )


class TestNicePrefixOnCdSegmentStillDenies:

    def test_nice_before_cd_still_denies_not_promoted(self):
        out = guard.check_offer_git_c("nice -19 cd /repo && git log -1")
        reason = _deny_reason(out)
        assert "git -C /repo log -1" in reason


class TestNonExecWrapperOnGitSideNeverAutoRewrites:
    """`bash_guard-consolidated-execution.md` M5 corpus addition. `which` /
    `busybox` / `setsid` are NOT in `_FIND_WRAPPER_WORDS` (the same
    strip-loop this guard shares with `check_runaway_find` via
    `_strip_leading_env_and_wrappers`), so none of them peel off `seg1`'s
    head here -- `seg1_stripped` stays `which git status` / `busybox git
    status` / `setsid git status`, none of which match `^git\\s`, and the
    guard falls through to `None` (no rewrite, no deny) for all three.

    Pinned as its own regression cell rather than folded into the `nice`/
    `sudo`/`env` wrapper-promotion tests above: `which` genuinely does not
    execute its argument (it only prints a path), `busybox` is an applet
    dispatcher, and `setsid` changes session/controlling-terminal semantics
    -- none of the three is safe to silently rewrite into `git -C <dir>
    ...` even if a future wrapper-table widening ever taught this guard to
    recognize them as command-position wrappers at all."""

    def test_which_before_git_never_auto_rewrites(self):
        assert guard.check_offer_git_c("cd /repo && which git status") is None

    def test_busybox_before_git_never_auto_rewrites(self):
        assert guard.check_offer_git_c("cd /repo && busybox git status") is None

    def test_setsid_before_git_never_auto_rewrites(self):
        assert guard.check_offer_git_c("cd /repo && setsid git status") is None


class TestExistingBailsUnaffected:

    def test_cwd_matches_target_multiline_bails_to_none(self):
        out = guard.check_offer_git_c("cd /repo && git log -1\n", cwd="/repo")
        assert out is None


class TestQuotedSemicolonHoleClosed:

    def test_quoted_semicolon_no_longer_bails_gets_rewritten(self):
        out = guard.check_offer_git_c('cd /repo && git commit -m "a; b"')
        hso = _reason(out)
        assert hso["permissionDecision"] == "allow"
        assert hso["updatedInput"]["command"] == 'git -C /repo commit -m "a; b"'

    def test_quoted_semicolon_with_real_follower_denies_not_bails(self):
        out = guard.check_offer_git_c('cd /repo && git commit -m "a; b"; echo done')
        hso = _reason(out)
        assert hso["permissionDecision"] == "deny"
        assert 'git -C /repo commit -m "a; b"' in hso["permissionDecisionReason"]


class TestOverrideStillSuppresses:
    def test_override_env_suppresses_plain_form(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_ALLOW_CD_PREFIX", "1")
        assert guard.check_offer_git_c("cd /repo && git log -1") is None

    def test_override_env_suppresses_prefixed_form(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_ALLOW_CD_PREFIX", "1")
        assert guard.check_offer_git_c("FOO=1 cd /repo && git log -1") is None


class TestAllGitFollowersAutoRewrite:

    def test_single_git_no_followers_still_auto_rewrites(self):
        out = guard.check_offer_git_c("cd /tmp/repo && git status")
        assert _rewritten_command(out) == "git -C /tmp/repo status"

    def test_two_git_followers_auto_rewrites_both_anchored(self):
        out = guard.check_offer_git_c(
            "cd /tmp/repo && git status && git log --oneline"
        )
        assert (
            _rewritten_command(out)
            == "git -C /tmp/repo status && git -C /tmp/repo log --oneline"
        )

    def test_git_followers_with_args_and_flags_all_anchored(self):
        out = guard.check_offer_git_c(
            "cd /tmp/repo && git diff --stat && git status --porcelain -- subdir/"
        )
        assert _rewritten_command(out) == (
            "git -C /tmp/repo diff --stat && "
            "git -C /tmp/repo status --porcelain -- subdir/"
        )

    def test_three_git_followers_all_anchored(self):
        out = guard.check_offer_git_c(
            "cd /tmp/repo && git fetch && git status && git log -1"
        )
        assert _rewritten_command(out) == (
            "git -C /tmp/repo fetch && git -C /tmp/repo status && "
            "git -C /tmp/repo log -1"
        )

    def test_semicolon_separated_git_followers_all_anchored(self):
        out = guard.check_offer_git_c("cd /tmp/repo && git status; git log -1")
        assert (
            _rewritten_command(out)
            == "git -C /tmp/repo status; git -C /tmp/repo log -1"
        )


class TestNonGitFollowerStaysRungBWithSafeOffer:
    """The restraint this promotion must NOT erode: a non-git follower is
    not provably equivalent under the rewrite (it resolves relative paths
    against the ORIGINAL cwd, not the cd target), so the chain must keep
    denying rather than auto-rewriting."""

    def test_non_git_follower_denies_not_rewrites(self):
        out = guard.check_offer_git_c("cd /tmp/repo && git status && ls subdir/")
        hso = _reason(out)
        assert hso["permissionDecision"] == "deny"

    def test_non_git_follower_offer_anchors_the_git_segment(self):
        out = guard.check_offer_git_c("cd /tmp/repo && git status && ls subdir/")
        reason = _deny_reason(out)
        assert "git -C /tmp/repo status && ls subdir/" in reason

    def test_non_git_follower_offer_flags_the_unanchored_segment(self):
        out = guard.check_offer_git_c("cd /tmp/repo && git status && ls subdir/")
        reason = _deny_reason(out)
        assert "'ls subdir/'" in reason
        assert "Not anchored" in reason
        assert "/tmp/repo" in reason

    def test_first_follower_non_git_second_follower_git_only_first_flagged(self):
        out = guard.check_offer_git_c(
            "cd /tmp/repo && git status && ls subdir/ && git log -1"
        )
        hso = _reason(out)
        assert hso["permissionDecision"] == "deny"
        reason = hso["permissionDecisionReason"]
        assert (
            "git -C /tmp/repo status && ls subdir/ && git -C /tmp/repo log -1"
            in reason
        )
        assert "'ls subdir/'" in reason


class TestFollowerAnchoringRespectsExistingBailOuts:

    def test_env_prefix_before_cd_with_all_git_followers_still_denies(self):
        out = guard.check_offer_git_c(
            "FOO=1 cd /tmp/repo && git status && git log -1"
        )
        hso = _reason(out)
        assert hso["permissionDecision"] == "deny"

    def test_env_prefix_before_first_git_with_all_git_followers_now_auto_rewrites(self):
        out = guard.check_offer_git_c(
            "cd /tmp/repo && FOO=1 git status && git log -1"
        )
        assert _rewritten_command(out) == (
            "FOO=1 git -C /tmp/repo status && git -C /tmp/repo log -1"
        )

    def test_env_prefix_on_a_follower_itself_is_not_anchored(self):
        out = guard.check_offer_git_c(
            "cd /tmp/repo && git status && FOO=1 git log -1"
        )
        hso = _reason(out)
        assert hso["permissionDecision"] == "deny"
        assert "'FOO=1 git log -1'" in hso["permissionDecisionReason"]

    def test_multiline_all_git_followers_bails_no_auto_rewrite(self):
        out = guard.check_offer_git_c(
            "cd /tmp/repo && git status && git log -1\n"
        )
        hso = _reason(out)
        assert hso["permissionDecision"] == "deny"

    def test_override_env_suppresses_all_git_followers_form(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_ALLOW_CD_PREFIX", "1")
        assert (
            guard.check_offer_git_c(
                "cd /tmp/repo && git status && git log -1"
            )
            is None
        )


class TestOfferAnchorFollowersHelperDirect:

    def test_all_git_segments_anchored_no_unanchored(self):
        # '&&'/';' into the PRECEDING segment's body, never into TAIL. Mirror
        rewritten, unanchored = guard._offer_anchor_followers(
            "&& git status && git log -1", "/tmp/repo"
        )
        assert rewritten == "&& git -C /tmp/repo status && git -C /tmp/repo log -1"
        assert unanchored == []

    def test_non_git_segment_left_verbatim_and_reported(self):
        rewritten, unanchored = guard._offer_anchor_followers(
            "&& git status && ls subdir/", "/tmp/repo"
        )
        assert rewritten == "&& git -C /tmp/repo status && ls subdir/"
        assert unanchored == ["ls subdir/"]

    def test_quoted_separators_inside_a_follower_do_not_split_it(self):
        rewritten, unanchored = guard._offer_anchor_followers(
            '&& git commit -m "a; b"', "/tmp/repo"
        )
        assert rewritten == "&& git -C /tmp/repo commit -m 'a; b'"
        assert unanchored == []

    def test_unterminated_quote_fails_closed_returns_none_unanchored(self):
        rewritten, unanchored = guard._offer_anchor_followers(
            '&& git commit -m "unterminated', "/tmp/repo"
        )
        assert rewritten == '&& git commit -m "unterminated'
        assert unanchored is None


class TestPowerShellIdiomDialectNeutral:

    def test_semicolon_chained_powershell_style_denies(self):
        out = guard.check_offer_git_c("cd /tmp/repo; git status; git log -1")
        assert (
            _rewritten_command(out)
            == "git -C /tmp/repo status; git -C /tmp/repo log -1"
        )

    def test_semicolon_chained_non_git_follower_still_denies(self):
        out = guard.check_offer_git_c("cd /tmp/repo; git status; ls subdir/")
        hso = _reason(out)
        assert hso["permissionDecision"] == "deny"


class TestSharedHelperPinsBothCallers:

    def test_find_is_find_segment_strips_env_assignment(self):
        assert dispatch_checks._find_is_find_segment("FOO=1 find /repo -name x")

    def test_find_is_find_segment_strips_wrapper_word(self):
        assert dispatch_checks._find_is_find_segment("sudo find /repo -name x")

    def test_find_is_find_segment_strips_chained_prefixes(self):
        assert dispatch_checks._find_is_find_segment("FOO=1 sudo BAR=2 find /repo -name x")

    def test_find_is_find_segment_rejects_non_find(self):
        assert not dispatch_checks._find_is_find_segment("FOO=1 echo find")

    def test_offer_git_c_strips_env_assignment_before_cd(self):
        out = guard.check_offer_git_c("FOO=1 cd /repo && git log -1")
        assert out is not None

    def test_strip_leading_env_and_wrappers_returns_literal_suffix(self):
        seg = "FOO=1 sudo BAR=2 find /repo"
        stripped = dispatch_checks._strip_leading_env_and_wrappers(seg)
        assert stripped == "find /repo"
        assert seg.endswith(stripped)
