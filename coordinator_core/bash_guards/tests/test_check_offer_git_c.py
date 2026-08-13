"""Tests for ``coordinator_core.bash_guards.guard_offer_git_c.check_offer_git_c``
-- the offer-git-c-over-cd guard -- pinning the prefix-evasion close and the
regression it guards.

Subject: coordinator-claude-EM-relayed defect, reproduced live on macOS --

    cd /repo && git log -1          -> DENIED (correct)
    FOO=1 cd /repo && git log -1    -> RAN    (evasion)

Cause: the `cd`/`git` detection at the top of `check_offer_git_c` tested
`seg0.split()[0] == "cd"` and `re.match(r"^cd\\s+\\S", seg0)` against the raw
segment text -- a single leading environment-assignment token (`FOO=1 `)
makes the first split-token an assignment, not `cd`, and the guard silently
returned `None` (allow) instead of denying or rewriting.

Fix: `check_offer_git_c` now runs both the `seg0` (`cd`-side) and `seg1`
(`git`-side) detection through `_strip_leading_env_and_wrappers` --
extracted as a shared module-level helper (`_skip_leading_env_and_wrappers_
idx` / `_strip_leading_env_and_wrappers`) so this guard and
`_find_is_find_segment` (the runaway-find guard) share ONE strip-loop
implementation instead of two independently-drifting copies (the drift
between those two copies is exactly how this evasion appeared while
`_find_is_find_segment` already handled the equivalent case).

Rewrite-vs-deny choice: `prefix0` (a leading env-assignment/wrapper-word
prefix BEFORE `cd`, e.g. `FOO=1 cd X && git Y`) always DENIES, never
auto-rewrites -- see `check_offer_git_c`'s own inline comment. `prefix0` is
POSIX-inert past the `cd` command and would be technically safe to drop, but
relying on that distinction inside an auto-rewrite is exactly the kind of
cleverness that reintroduces a silent-drop bug later, so it stays at
deny+offer with the prefix-preserving suggestion embedded (dropping
`prefix0`, since it was never semantically live past `cd`).

`prefix1` (the prefix immediately before the first `git`, e.g. `cd X &&
nice -19 git Y` or `cd X && FOO=1 git Y`) is promoted to rung A (prompt-free
auto-rewrite) whenever `prefix0` is absent: the prefix scopes to the single
command it precedes, and that command is `git` either way, so carrying it
forward onto `git -C <target> ...` verbatim is provably equivalent to the
original `cd`-then-`git` chain -- the same argument `_offer_anchor_followers`
already makes for a bare-git follower, one position over. Both env-
assignment and wrapper-word prefixes hit this promotion; nothing about the
rewrite depends on which kind it is.

Spec backlink: coordinator_core/bash_guards/guard_offer_git_c.py
(``check_offer_git_c``, extracted from dispatch_checks.py's "8. check_offer_git_c
-- offer-git-c-over-cd.sh" section, M1 2026-07-29); the shared
``_strip_leading_env_and_wrappers``/``_skip_leading_env_and_wrappers_idx``
helper it still imports from there stays in
coordinator_core/bash_guards/dispatch_checks.py ("7. check_runaway_find"
section, for the shared helper's other caller, ``_find_is_find_segment``).
"""

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
    """No regression: the plain, unprefixed shape keeps its existing
    silent-rewrite behaviour exactly as before this fix."""

    def test_bare_cd_and_git_auto_rewrites(self):
        out = guard.check_offer_git_c("cd /repo && git log -1")
        assert _rewritten_command(out) == "git -C /repo log -1"


class TestTildeTargetExpandsBeforeQuoting:
    """coordinator-claude memo (2026-08-12): `cd ~/X/peer && git log` was rewritten
    into `git -C '~/X/peer' log`, which dies with "cannot change to
    '~/X/peer'" -- `shlex.quote` quotes the tilde, and a quoted tilde is
    never expanded by the shell that runs the suggestion. The rewrite must
    carry the expanded absolute path."""

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
    """AC1 -- the reported evasion: a leading env-assignment on the `cd`
    side used to make the guard return None (allow) outright."""

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
    """rung-C -> rung-A promotion: an assignment on the SECOND segment
    (`FOO=1 git ...`, i.e. `prefix1`) scopes to the git invocation either
    way, so it is now auto-rewritten (prompt-free) rather than denied --
    `FOO=1` is carried forward verbatim onto the rewritten `git -C`
    invocation."""

    def test_env_assignment_before_git_auto_rewrites(self):
        out = guard.check_offer_git_c("cd /repo && FOO=1 git log -1")
        assert _rewritten_command(out) == "FOO=1 git -C /repo log -1"


class TestRewriteCasePreservesPrefix:
    """Direct assertion (independent of the rewrite-text check above) that
    the rewrite-eligible machinery itself -- `_offer_awk_parse`'s PREFIX
    extraction -- produces the prefix-preserving `updatedInput` string."""

    def test_prefix_survives_into_rewrite_not_dropped(self):
        out = guard.check_offer_git_c("cd /repo && FOO=1 git status")
        assert _rewritten_command(out) == "FOO=1 git -C /repo status"


class TestNicePrefixOnGitSegmentAutoRewrites:
    """The dispatch target for this promotion: `nice` on the GIT side
    (`prefix1`, e.g. `cd X && nice -19 git Y`) is a wrapper-word prefix like
    any other -- being polite about machine load must not cost the
    prompt-free rewrite an unprefixed `cd X && git Y` already gets. This is
    the exact regression the peer's `nice`-hardening fix in
    `_skip_leading_env_and_wrappers_idx` introduced: that fix correctly
    denies `cd X && nice -19 git Y` where it previously passed silently
    (a real defect, since `nice` was unrecognized), but before this
    promotion the deny had no rung-A path back out, unlike the plain
    spelling."""

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
    """`prefix0` (before `cd`) never promotes, even for the same wrapper
    word that just got promoted on the git side above -- the restraint in
    `check_offer_git_c`'s inline comment is deliberate and this pins it
    for `nice` specifically, not just the `sudo`/`env` shapes
    `TestWrapperWordEvasionNowDenies` already covers."""

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
    """Pre-existing bail-to-None conditions that must still behave the same
    -- the quoted-semicolon fix (BX-9) must not touch this one.

    A multiline command whose `cd` target already equals `cwd` hits the
    cwd-strip rewrite branch, but that branch explicitly declines to
    rewrite across a real newline (`if ml_bail: return None`) rather than
    silently altering line structure. This is a genuinely different escape
    valve from the quoted-semicolon case below (real, not decoy, additional
    lines are present), so it is unaffected by that fix.
    """

    def test_cwd_matches_target_multiline_bails_to_none(self):
        out = guard.check_offer_git_c("cd /repo && git log -1\n", cwd="/repo")
        assert out is None


class TestQuotedSemicolonHoleClosed:
    """BX-9: the naive (non-quote-aware) `re.split(r"&&|;", cmd)` this guard
    used to compute `seg_count`/seg0/seg1 could not tell a `;` inside a
    quoted git commit message apart from a real top-level separator. It
    split `git commit -m "a; b"` into a truncated `git commit -m "a`
    segment with an odd (=1) double-quote count, which tripped the
    unrelated "give up rather than guess" odd-quote-count bail -- silently
    letting a perfectly well-formed, unprefixed `cd && git` command fall
    through this guard entirely (no rewrite offered, no deny raised),
    instead of being auto-rewritten like any other no-follower, no-prefix
    case. `_offer_quote_aware_segments` closes this by tracking quote state
    the same way `_offer_awk_parse` already did, so segmentation and the
    BODY/TAIL parse can no longer disagree about where the real separators
    are."""

    def test_quoted_semicolon_no_longer_bails_gets_rewritten(self):
        out = guard.check_offer_git_c('cd /repo && git commit -m "a; b"')
        hso = _reason(out)
        assert hso["permissionDecision"] == "allow"
        assert hso["updatedInput"]["command"] == 'git -C /repo commit -m "a; b"'

    def test_quoted_semicolon_with_real_follower_denies_not_bails(self):
        # A quoted `;` inside the git segment plus a REAL top-level `;`
        # follower afterward: must still deny (real followers present, so
        # no silent rewrite), not bail to None as the pre-fix naive split
        # would have (it would have seen 4 naive segments here, not the
        # real 2, and tripped the same odd-quote-count escape valve).
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
    """CLAUDE-KLABAUTER-side rung-B -> rung-A promotion: when every follower segment
    after the first git command is ITSELF a bare git invocation, anchoring
    each one with '-C <target>' is provably equivalent to the original
    'cd <target> && ...' chain (see `_offer_anchor_followers`'s docstring),
    so the whole chain now auto-rewrites instead of falling to deny+offer.
    Covers the four rows from the dispatch brief."""

    def test_single_git_no_followers_still_auto_rewrites(self):
        # Row 1 -- unchanged baseline behaviour.
        out = guard.check_offer_git_c("cd /tmp/repo && git status")
        assert _rewritten_command(out) == "git -C /tmp/repo status"

    def test_two_git_followers_auto_rewrites_both_anchored(self):
        # Row 2.
        out = guard.check_offer_git_c(
            "cd /tmp/repo && git status && git log --oneline"
        )
        assert (
            _rewritten_command(out)
            == "git -C /tmp/repo status && git -C /tmp/repo log --oneline"
        )

    def test_git_followers_with_args_and_flags_all_anchored(self):
        # Row 3 -- the defect this promotion closes: the un-anchored
        # follower a caller could previously paste and silently run
        # against the WRONG repository.
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
        # Row 4.
        out = guard.check_offer_git_c("cd /tmp/repo && git status && ls subdir/")
        hso = _reason(out)
        assert hso["permissionDecision"] == "deny"

    def test_non_git_follower_offer_anchors_the_git_segment(self):
        # Item 3 of the brief: even the rung-B offer must anchor what it
        # safely can, not hand back a bare 'git status' with no -C at all.
        out = guard.check_offer_git_c("cd /tmp/repo && git status && ls subdir/")
        reason = _deny_reason(out)
        assert "git -C /tmp/repo status && ls subdir/" in reason

    def test_non_git_follower_offer_flags_the_unanchored_segment(self):
        # The un-anchored segment must be called out explicitly (not just
        # silently present in the suggestion) so a caller who reads the
        # message -- and one who doesn't but at least gets a correct
        # suggestion string -- is never misled.
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
    """The all-git-followers auto-rewrite must never fire where the
    pre-existing restraint applies -- prefix evasion and multiline commands
    stay denied/bailed exactly as before."""

    def test_env_prefix_before_cd_with_all_git_followers_still_denies(self):
        out = guard.check_offer_git_c(
            "FOO=1 cd /tmp/repo && git status && git log -1"
        )
        hso = _reason(out)
        assert hso["permissionDecision"] == "deny"

    def test_env_prefix_before_first_git_with_all_git_followers_now_auto_rewrites(self):
        # prefix1 (on the FIRST git segment) is promoted to rung A -- unlike
        # prefix0, it carries forward onto the anchored chain.
        out = guard.check_offer_git_c(
            "cd /tmp/repo && FOO=1 git status && git log -1"
        )
        assert _rewritten_command(out) == (
            "FOO=1 git -C /tmp/repo status && git -C /tmp/repo log -1"
        )

    def test_env_prefix_on_a_follower_itself_is_not_anchored(self):
        # A follower's OWN prefix is not provably inert either -- treat it
        # as non-git for anchoring purposes even though it names 'git'.
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
    """Direct unit coverage of `_offer_anchor_followers` itself, independent
    of the guard's own deny/rewrite envelope plumbing. Segmentation and
    quoting are delegated to the shared tokenizer
    (`_bt_tokenize_full_command`) -- reconstructed text is NOT required to
    be byte-identical to the input (re-quoting via `shlex.quote` may change
    quote style, e.g. double- to single-quoted), only semantically
    equivalent and syntactically valid."""

    def test_all_git_segments_anchored_no_unanchored(self):
        # NOTE: `followers` starts exactly at the separator, with no leading
        # whitespace -- `_offer_awk_parse` absorbs any space before the
        # '&&'/';' into the PRECEDING segment's body, never into TAIL. Mirror
        # that shape here rather than a hand-picked leading space.
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
        # The tokenizer (shlex) parses the quotes correctly either way --
        # the semicolon inside "a; b" must NOT be treated as a top-level
        # separator. Re-quoting via `shlex.quote` renders it single-quoted
        # rather than preserving the original double quotes; that quoting
        # STYLE difference is not a correctness issue (both are valid,
        # equivalent shell), only the split-boundary behaviour is pinned.
        rewritten, unanchored = guard._offer_anchor_followers(
            '&& git commit -m "a; b"', "/tmp/repo"
        )
        assert rewritten == "&& git -C /tmp/repo commit -m 'a; b'"
        assert unanchored == []

    def test_unterminated_quote_fails_closed_returns_none_unanchored(self):
        # `_bt_tokenize_full_command` returns None on a ValueError (e.g. an
        # unterminated quote); this function must fail the same way every
        # other consumer of that tokenizer does -- hand the original text
        # back unchanged and report "don't know" (None), never guess at a
        # boundary.
        rewritten, unanchored = guard._offer_anchor_followers(
            '&& git commit -m "unterminated', "/tmp/repo"
        )
        assert rewritten == '&& git commit -m "unterminated'
        assert unanchored is None


class TestPowerShellIdiomDialectNeutral:
    """C4a (guard-dialect-coverage.md row 6): this guard gates on
    `_bt_token_matches_binary(seg_tokens[0], "git")` -- the external `git`
    exe, byte-identical in both shell dialects. `check_offer_git_c` takes a
    raw command string directly (no `tool_name` parameter at all, no
    `_dialect.py` import), so a PowerShell-idiom surrounding shape --
    `;`-chained rather than `&&`-chained, PowerShell's idiomatic separator
    -- reaches the SAME shared tokenizer and must reach the SAME verdict as
    the already-pinned bash-spelled equivalent.

    Spec backlink: docs/reference/guard-dialect-coverage.md row 6 (C4a).
    """

    def test_semicolon_chained_powershell_style_denies(self):
        # Mirrors TestQuotedSemicolonHoleClosed's all-git-followers case,
        # but chained throughout with `;` (PowerShell idiom) rather than
        # `&&` after the `cd`.
        out = guard.check_offer_git_c("cd /tmp/repo; git status; git log -1")
        assert (
            _rewritten_command(out)
            == "git -C /tmp/repo status; git -C /tmp/repo log -1"
        )

    def test_semicolon_chained_non_git_follower_still_denies(self):
        # A non-git follower after a `;`-chained cd must still deny, not
        # auto-rewrite -- same restraint as the `&&`-chained case in
        # TestNonGitFollowerStaysRungBWithSafeOffer.
        out = guard.check_offer_git_c("cd /tmp/repo; git status; ls subdir/")
        hso = _reason(out)
        assert hso["permissionDecision"] == "deny"


class TestSharedHelperPinsBothCallers:
    """Regression pin for the refactor itself: `_find_is_find_segment` (the
    runaway-find guard's segment classifier) and `check_offer_git_c` now
    share one implementation (`_strip_leading_env_and_wrappers` /
    `_skip_leading_env_and_wrappers_idx`). Exercise the shared helper
    through BOTH callers so a future edit to one path can't silently
    diverge from the other again."""

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
