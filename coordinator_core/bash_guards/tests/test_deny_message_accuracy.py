"""BX-12 -- deny/advisory MESSAGE-ACCURACY audit across every guard this
plan (docs/plans/2026-07-29-windows-viability-stop-the-spawn-storms.md)
touches. RELEASE BLOCKER per that row's own body: "No shipped deny message
may misdescribe what tripped it. Verified by executing every deny path and
reading its message against the command that triggered it -- not by reading
code."

This file is deliberately NOT a re-run of each guard's own unit-test file
(`test_guard_grep_via_bash.py`, `test_guard_multiprobe_banner.py`,
`test_guard_plumbing_and_loops.py`, `test_bx16_multiprobe_and_headtail_
rewrite.py`, `test_bx678_tail_placement.py`, `test_check_offer_git_c.py`,
`test_platform_verdict.py`, `test_shape_classifier.py` all already assert
correctness of individual behaviors). This file's OWN job is narrower and
cross-cutting: for every deny/advisory path in scope, EXECUTE it and assert
the emitted message (a) names the shape/segment that ACTUALLY tripped it,
(b) shows a "Did you mean"/"Use instead" alternative that is reachable
(BX-16 actually emits it) and applies to THIS exact command, and (c) never
claims a full-command rewrite for work the guard never inspected.

===========================================================================
SCOPE OF THIS PASS (read before assuming blanket coverage)
===========================================================================
Full-depth message-accuracy coverage:
  - The three platform-conditioned shape guards this plan authored/retargeted:
    `guard_grep_via_bash` (BX-6), `guard_multiprobe_banner` (BX-7),
    `guard_plumbing_and_loops` (BX-8).
  - The seven BX-16 rewrite/advisory functions in `dispatch_checks.py`:
    `check_find_exec_rewrite`, `check_grep_via_bash_rewrite`,
    `check_sed_range_read_advise`, `check_cat_heredoc_write_advise`,
    `check_git_commit_safe_commit_advise`, `check_multiprobe_banner_rewrite`,
    `check_head_tail_plumbing_rewrite`.
  - `_shape_classifier`'s shape-overlap precedence rule, exercised through
    the three shape guards above (two- and three-shape overlapping
    commands) -- the leg this row calls out by name.

Sampled only (documented, not exhaustive -- see report for the follow-up
this implies): ONE pre-existing hard-deny already registered in
`dispatch.py`'s `guard_chain` (`check_offer_git_c`) as a spot-check that the
audit METHOD this file establishes also holds outside the BX-6/7/8/16
family. `check_destructive_rm` and `check_blanket_git_add` were also tried
and are recorded as a residual follow-up (see
`TestSampledPreexistingGuardsMessageAccuracy`'s own docstring) rather than
silently dropped. Exhaustive per-guard coverage of every OTHER hard-deny in
the chain (destructive-git-*, worktree/sentinel guards, subagent-commit/
destructive-action, reviewer-bash-allowlist, test-suite-invocation,
raw-pid-liveness) is not attempted here -- each of those already carries its
own dedicated test file (see `coordinator_core/bash_guards/tests/`), and
re-deriving a message-accuracy assertion for all of them in one pass was
judged out of this dispatch's bounded scope; flagged as a residual BX-12
follow-up.

ORDERING DEPENDENCY (per this row's own dispatch brief): `guard_grep_via_
bash`, `guard_multiprobe_banner`, and `guard_plumbing_and_loops` (BX-6/BX-7/
BX-8) are NOT YET registered in `dispatch.py`'s `guard_chain` -- their
registration order is being settled by a concurrent session. Every
assertion below calls each guard's `check()` function DIRECTLY (never
through `dispatch.evaluate_payload_json`), which is unaffected by
registration order or position. The dispatcher-level assertion ("observed
firing through the real dispatcher, not in isolation") is BX-9/BX-10's own
obligation once registration lands, and is marked inline below wherever a
finding here can only be FULLY confirmed after that lands.

Spec backlink: docs/plans/2026-07-29-windows-viability-stop-the-spawn-storms.md § BX-12
"""

from __future__ import annotations

import pathlib

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards import dispatch_checks
from coordinator_core.bash_guards import guard_grep_via_bash
from coordinator_core.bash_guards import guard_head_tail_rewrite
from coordinator_core.bash_guards import guard_multiprobe_banner
from coordinator_core.bash_guards import guard_offer_git_c
from coordinator_core.bash_guards import guard_plumbing_and_loops
from coordinator_core.bash_guards._shape_classifier import Shape, classify_command


def _payload(command, session_id="sess-bx12"):
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": session_id,
        "cwd": None,
    }


def _hso(result):
    assert result is not None, "expected a non-None envelope"
    return result["hookSpecificOutput"]


def _deny_text(hso):
    assert hso["permissionDecision"] == "deny", hso
    return hso["permissionDecisionReason"]


def _advisory_text(hso):
    assert hso["permissionDecision"] == "allow", hso
    assert "additionalContext" in hso, hso
    return hso["additionalContext"]


def _rewrite_command(hso):
    assert hso["permissionDecision"] == "allow", hso
    updated = hso.get("updatedInput")
    assert isinstance(updated, dict) and updated.get("command"), hso
    return updated["command"]


# ---------------------------------------------------------------------------
# Section 1 -- the three platform-conditioned shape guards (BX-6/7/8),
# message-vs-trigger correspondence on BOTH platform legs.
# ---------------------------------------------------------------------------


class TestGrepViaBashMessageAccuracy:
    """guard_grep_via_bash.check -- BX-6, narrowed by H11 (2026-07-30,
    docs/plans/2026-07-30-os-aware-guard-advisory-defaults.md).

    Classification note for every test rewritten in this class (H11
    dispatch, 2026-07-30): the guard's substitutable/deny branch
    (`_platform_verdict_for_shape`) was REMOVED (H11(a)) because it fired
    on the same set `grep-via-bash-rewrite` already claims and produced 0
    denies on either platform -- provably unreachable in production. Every
    test below that used to exercise a deny, or an advisory for a command
    with no genuine GNU-only construct or partial-rewrite outlet, covered
    behaviour that was deliberately deleted, not merely reworded -- see
    each test's own (i)/(ii) note.
    """

    def test_substitutable_residue_never_fires_on_either_host(self):
        # (i) OBSOLETE, not repointed: this command used to trip a
        # Windows-only deny / macOS-only advisory (the two tests this one
        # replaces, `test_substitutable_residue_deny_names_grep_shape_and_
        # real_rewrite` and `test_advisory_leg_same_platform_conditioning`).
        # That branch is gone (H11(a)) -- `grep-via-bash-rewrite`,
        # registered earlier in dispatch.py's chain, already claims this
        # exact command as an ADVISORY_REWRITE, so this guard has nothing
        # left to say about it on ANY host. Kept (not deleted outright) as
        # a regression guard: if the deny/advise branch is ever
        # accidentally reintroduced, this is the test that catches it.
        cmd = 'grep -rn "TODO" src/'
        assert guard_grep_via_bash.check(_payload(cmd), host_is_windows=True) is None
        assert guard_grep_via_bash.check(_payload(cmd), host_is_windows=False) is None

    def test_composed_pipeline_with_only_portable_flags_stays_silent(self):
        # (i) OBSOLETE, not repointed: this command used to name "runs as
        # part of a larger shell chain" in a ~2.2KB composed advisory.
        # H11(b)/(c) narrowed the composed-advisory path to only a real
        # partial-pipe rewrite or a genuine GNU-only construct -- neither
        # applies here (`-r`/`-n` are portable, and `grep ... | wc -l` is
        # exactly the eligible-for-partial-rewrite shape, exercised
        # separately below where it now offers a REAL alternative instead
        # of prose). This command uses `;`, which `_partial_pipe_rewrite`
        # never widens to cover (see that function's own docstring), so
        # per design-as-offers the honest output is silence.
        cmd = 'grep -rn "TODO" src/ ; echo done'
        assert guard_grep_via_bash.check(_payload(cmd), host_is_windows=True) is None

    def test_two_segment_pipe_offers_a_real_partial_rewrite_not_prose(self):
        # (ii) STILL MEANINGFUL, repointed: this is the exact command the
        # retired `test_composed_pipeline_advisory_names_chained_not_
        # untranslatable` used, but the composed-advisory prose it used to
        # produce ("runs as part of a larger shell chain") is gone --
        # H11(b)/G2's `_partial_pipe_rewrite` now recognizes this narrow
        # two-segment `grep | downstream` shape and offers a REAL,
        # runnable one-fewer-fork replacement instead. The property under
        # test survives: this guard still has something to say about a
        # composed grep pipeline, just an actionable rewrite now, not
        # prose about a flag it never named to begin with.
        cmd = 'grep -rn "TODO" src/ | wc -l'
        advisory = _advisory_text(
            _hso(guard_grep_via_bash.check(_payload(cmd), host_is_windows=True))
        )
        assert "one-fewer-fork replacement" in advisory
        assert "| wc -l" in advisory
        assert "runs as part of a larger shell chain" not in advisory

    def test_gnu_only_construct_advisory_names_the_command_and_divergence(self):
        # (ii) STILL MEANINGFUL, repointed: replaces `test_untranslatable_
        # flag_advisory_names_flag_not_chained`, whose original trigger
        # (`grep -C 3 ...`) no longer fires at all -- `-C` is untranslatable
        # but NOT GNU-only (H11(c): only `-P`/`-z`/an unrecognized long
        # option trip the narrowed check), so per design-as-offers that
        # command is now silence (asserted separately below). `-P` IS
        # genuinely GNU-only (behaves differently on BSD grep/macOS), so
        # this is the live case the narrowed check still fires on.
        cmd = 'grep -Pn "TODO" src/'
        advisory = _advisory_text(
            _hso(guard_grep_via_bash.check(_payload(cmd), host_is_windows=True))
        )
        assert "GNU-only grep construct" in advisory
        assert cmd.replace('"', "") in advisory or "grep -Pn TODO src/" in advisory
        assert "BSD grep (macOS)" in advisory

    def test_untranslatable_but_portable_flag_stays_silent(self):
        # (i) OBSOLETE, not repointed: the old blanket composed advisory
        # fired for ANY untranslatable flag and named "a flag" as the
        # reason. `-C` (context lines) is untranslatable by BX-16's rewrite
        # but exists identically on BSD grep -- not a genuine portability
        # hazard, so H11(c)'s narrowed `_has_gnu_only_construct` correctly
        # does not flag it, and this guard is silent per design-as-offers
        # (no actionable alternative to name).
        cmd = 'grep -C 3 "TODO" src/file.py'
        assert guard_grep_via_bash.check(_payload(cmd), host_is_windows=True) is None

    def test_substitutable_residue_deny_does_not_claim_in_process_or_zero_fork(self):
        # (ii) STILL MEANINGFUL, repointed at the surviving message: the
        # guard's outlet_summary previously claimed "in-process python3
        # search -- no subprocess fork", which is false: `python3 -c` is a
        # genuine subprocess. That deny is gone (H11(a)), but the identical
        # overstatement risk exists in the guard's remaining composed
        # advisory (the partial-rewrite lede also names a python3
        # replacement) -- re-pointed at that surviving message rather than
        # a now-nonexistent deny.
        cmd = 'grep -rn "TODO" src/ | wc -l'
        advisory = _advisory_text(
            _hso(guard_grep_via_bash.check(_payload(cmd), host_is_windows=True))
        )
        assert "no subprocess fork" not in advisory
        # The advisory's own lede names its replacement "one-fewer-fork",
        # never zero-fork/in-process. (2026-07-30, a9fce05a: the message
        # used to also name a genuinely zero-fork in-process answerer
        # elsewhere in this text -- that explanation was cut as unusable
        # prose, so "in-process" no longer appears in this message at all;
        # see test_partial_rewrite_advisory_names_subagent_dispatch_as_fallback.)
        assert "one-fewer-fork" in advisory
        assert "zero-fork replacement" not in advisory

    def test_rewrite_advisory_does_not_claim_harness_can_do_it_in_process(self):
        # (ii) UNCHANGED -- this test exercises `dispatch_checks.check_
        # grep_via_bash_rewrite` (BX-16's own seam), not `guard_grep_via_
        # bash`, and H11 does not touch that function at all.
        cmd = 'grep -rn "TODO" src/'
        seam = dispatch_checks.check_grep_via_bash_rewrite(cmd, "sess-bx12")
        hso = _hso(seam)
        assert hso["permissionDecision"] == "allow"
        assert "harness can do in-process" not in hso.get("additionalContext", "")

    def test_partial_rewrite_advisory_names_subagent_dispatch_as_fallback(self):
        # (ii) STILL MEANINGFUL, repointed: the retired `test_composed_
        # pipeline_advisory_names_subagent_dispatch_as_fallback` used a
        # `;`-joined command that is now silent entirely (see
        # `test_composed_pipeline_with_only_portable_flags_stays_silent`
        # above) -- there is no advisory left on that command to assert
        # the fallback mention against. The property this test protects
        # (the composed advisory still names the subagent-dispatch
        # fallback) is still real and still asserted, on the two-segment
        # pipe shape that still produces an advisory.
        #
        # 2026-07-30 (a9fce05a): the sentence explaining that the
        # in-process search answerer already evaluated and declined this
        # exact invocation was cut as unusable prose (the reader cannot
        # re-target that answerer or reorder the chain) -- "in-process" no
        # longer appears in this message at all, so this test no longer
        # pins that phrase, only the fallback the reader can actually take.
        cmd = 'grep -rn "TODO" src/ | wc -l'
        advisory = _advisory_text(
            _hso(guard_grep_via_bash.check(_payload(cmd), host_is_windows=True))
        )
        assert "registered ahead of this guard" not in advisory
        assert "Explore or general-purpose subagent" in advisory


class TestMultiProbeBannerMessageAccuracy:
    """guard_multiprobe_banner.check -- BX-7."""

    _BANNER_CMD = 'echo "=== SESSION FACTS ==="; git rev-parse --abbrev-ref HEAD; pwd'

    def test_deny_names_multiprobe_shape_and_evidence_banner(self):
        deny = _deny_text(
            _hso(guard_multiprobe_banner.check(_payload(self._BANNER_CMD), host_is_windows=True))
        )
        assert "multi-probe-banner" in deny
        assert "SESSION FACTS" in deny

    def test_outlet_example_is_the_real_seam_rewrite_not_a_static_illustration(self):
        """2026-07-29 duty-of-care promotion, supersedes the prior pinned
        finding this test used to document: this guard now CALLS
        `dispatch_checks.check_multiprobe_banner_rewrite` (the same function
        the separate `multiprobe-banner-rewrite` chain entry uses to perform
        the actual rewrite) to read its `updatedInput.command` whenever it
        confirms a genuine outlet for THIS exact command, and renders the
        Example around that literal string -- exactly matching
        `guard_grep_via_bash`'s stricter contract (that guard's outlet
        example already IS the real seam rewrite, verified in
        `TestGrepViaBashMessageAccuracy` above). The prior static, hand-
        written `python3 -c "import os, getpass..."` illustration is now
        used ONLY as the fallback for a command the seam does NOT confirm
        (see `TestMultiProbeBannerVerdict.test_banner_with_unrecognized_
        segment_never_denies_even_on_windows` in `test_guard_multiprobe_
        banner.py`) -- a genuinely composed command never reaches a deny at
        all now (see the shape-overlap section below), so the old "is this
        static text a misdescription" question this test used to answer no
        longer has a live deny case to apply to.
        """
        deny = _deny_text(
            _hso(guard_multiprobe_banner.check(_payload(self._BANNER_CMD), host_is_windows=True))
        )
        real_rewrite = dispatch_checks.check_multiprobe_banner_rewrite(
            self._BANNER_CMD, "sess-bx12"
        )
        real_cmd = _rewrite_command(_hso(real_rewrite))
        assert real_cmd in deny  # the Example IS the real per-command rewrite now

    def test_non_banner_command_returns_none(self):
        assert guard_multiprobe_banner.check(_payload("git status"), host_is_windows=True) is None


class TestPlumbingAndLoopsMessageAccuracy:
    """guard_plumbing_and_loops.check -- BX-8 (head/tail-plumbing half)."""

    def test_head_tail_plumbing_deny_names_that_shape_not_for_loop(self):
        cmd = "find . -name '*.py' | head -n 5"
        deny = _deny_text(
            _hso(guard_plumbing_and_loops.check(_payload(cmd), host_is_windows=True))
        )
        assert "head-tail-plumbing" in deny
        assert "for-loop" not in deny

    def test_unconfirmed_seam_outlet_degrades_to_generic_advisory_not_a_deny(self):
        """`docker ps | head` -- classifier matches HEAD_TAIL_PLUMBING, but
        the seam recognizes no upstream generator for `docker`, so
        `_seam_confirmed_rewrite` must be False and this must NOT deny
        toward the seam's own disclaimer text (the exact hazard this
        guard's own docstring documents as previously shipped and reverted).
        """
        cmd = "docker ps | head -n 20"
        advisory = _advisory_text(
            _hso(guard_plumbing_and_loops.check(_payload(cmd), host_is_windows=True))
        )
        # 2026-07-30 message-trim (76797f17) moved the BX-16/seam-mechanics
        # explanation to the module docstring; the generic-advisory marker
        # (never the confirmed-rewrite branch's own "auto-rewritten ...
        # equivalent" phrasing) is what actually distinguishes this path.
        assert "BASH-SPAWN ADVISORY" in advisory
        assert "auto-rewritten single-process equivalent" not in advisory
        assert "docker ps | head -n 20" in advisory


# ---------------------------------------------------------------------------
# Section 2 -- shape-overlap precedence: a command matching TWO or THREE
# shapes at once must produce a message naming the PRECEDENCE WINNER, never
# whichever matcher happened to run first. BX-2's own pinned contract:
# GREP_VIA_BASH > MULTI_PROBE_BANNER > HEAD_TAIL_PLUMBING > FOR_LOOP >
# FIND_EXEC_XARGS.
# ---------------------------------------------------------------------------


class TestShapeOverlapPrecedenceInMessages:
    def test_two_shape_overlap_banner_plus_grep_names_grep_not_banner(self):
        # Simultaneously a multi-probe banner (>=3 segments, banner-marked
        # echo) AND grep-via-bash (a segment invoking grep). Per
        # SHAPE_PRECEDENCE, GREP_VIA_BASH outranks MULTI_PROBE_BANNER.
        # Because the grep segment is chained alongside 2 other segments it
        # is COMPOSED, not substitutable residue (BX-6's own single-segment
        # rule) -- so this is an advisory on every platform, never a deny;
        # the precedence claim under test is WHICH guard speaks, not
        # deny-vs-advise.
        #
        # (ii) STILL MEANINGFUL, repointed (H11, 2026-07-30): the original
        # `-rn` trigger used only portable flags, so H11(c)'s narrowed
        # composed-advisory gate now leaves this exact command silent
        # (see `test_composed_pipeline_with_only_portable_flags_stays_
        # silent` above) -- there would be no advisory left for THIS
        # assertion to inspect. Swapped to `-Pn` (a genuine GNU-only
        # construct) so the grep guard still has something actionable to
        # say; the precedence property under test (grep wins, banner stays
        # silent) is unchanged and still exercised.
        cmd = 'echo "=== probe ==="; pwd; grep -Pn "TODO" src/'
        classification = classify_command(cmd)
        assert classification.primary is not None
        assert classification.primary.shape is Shape.GREP_VIA_BASH
        assert Shape.MULTI_PROBE_BANNER in classification.matched_shapes  # residue present

        # The grep guard must fire (it IS the precedence winner)...
        grep_advisory = _advisory_text(
            _hso(guard_grep_via_bash.check(_payload(cmd), host_is_windows=True))
        )
        assert "grep-via-bash" in grep_advisory

        # ...and the banner guard must NOT fire on this command at all --
        # it is explicit in guard_multiprobe_banner's own negative-spec that
        # it only speaks when MULTI_PROBE_BANNER is the precedence winner.
        assert guard_multiprobe_banner.check(_payload(cmd), host_is_windows=True) is None

    def test_multiprobe_rewrite_seam_agrees_with_guard_on_grep_primary_command(self):
        # `check_multiprobe_banner_rewrite` (dispatch_checks.py) previously
        # gated on plain `has_shape(MULTI_PROBE_BANNER)` membership, while
        # its sibling `guard_multiprobe_banner.check` correctly gates on
        # `primary.shape is MULTI_PROBE_BANNER` (the precedence winner). A
        # command whose PRIMARY shape is GREP_VIA_BASH (which outranks
        # MULTI_PROBE_BANNER) must make BOTH stay silent about banner text.
        cmd = 'echo "=== probe ==="; pwd; grep -rn "TODO" src/'
        classification = classify_command(cmd)
        assert classification.primary is not None
        assert classification.primary.shape is Shape.GREP_VIA_BASH

        assert guard_multiprobe_banner.check(_payload(cmd), host_is_windows=True) is None
        assert dispatch_checks.check_multiprobe_banner_rewrite(cmd, "sess-bx12") is None

    def test_three_shape_overlap_names_the_single_highest_precedence_winner(self):
        # banner-marked echo (MULTI_PROBE_BANNER) + a grep segment
        # (GREP_VIA_BASH) + a piped head (HEAD_TAIL_PLUMBING), all in one
        # command, >=3 segments. GREP_VIA_BASH must win.
        #
        # (ii) STILL MEANINGFUL, repointed (H11, 2026-07-30): same reason as
        # the two-shape overlap test above -- `-rn` alone leaves this
        # command silent post-H11(c), so swapped to `-Pn` (GNU-only) to
        # keep the grep guard actionable while the precedence property
        # (grep wins over both banner and head-tail residue) stays under
        # test unchanged.
        cmd = 'echo "=== probe ==="; grep -Pn "TODO" src/ | head -n 5; pwd'
        classification = classify_command(cmd)
        assert classification.primary is not None
        assert classification.primary.shape is Shape.GREP_VIA_BASH
        residue_shapes = {m.shape for m in classification.residue}
        assert Shape.MULTI_PROBE_BANNER in residue_shapes
        assert Shape.HEAD_TAIL_PLUMBING in residue_shapes

        # Only the grep guard may speak for this command; the other two
        # guards' own negative-specs require them to stay silent when they
        # are not the precedence winner.
        assert guard_multiprobe_banner.check(_payload(cmd), host_is_windows=True) is None
        assert guard_plumbing_and_loops.check(_payload(cmd), host_is_windows=True) is None
        grep_result = guard_grep_via_bash.check(_payload(cmd), host_is_windows=True)
        assert grep_result is not None

    def test_banner_plus_headtail_no_grep_names_banner_not_headtail(self):
        # MULTI_PROBE_BANNER (>=3 segments, banner echo) + HEAD_TAIL_PLUMBING
        # (a piped head), no grep anywhere. MULTI_PROBE_BANNER outranks
        # HEAD_TAIL_PLUMBING. The piped `pwd | head -n 1` segment is a
        # genuinely composed stage the sibling rewrite chain entry treats as
        # unrecognized (2026-07-29 duty-of-care promotion: a piped stage
        # inside a banner chain is composed, not a bare fact probe -- see
        # `check_multiprobe_banner_rewrite`'s own docstring).
        #
        # 2026-08-06 (B2): this used to assert an ADVISORY naming the banner
        # shape. The unconfirmed-seam branch rendered a fixed
        # `pwd`/`whoami`/`git status` template regardless of the real command,
        # so it satisfied "names the right shape" while still misdescribing the
        # command it fired on -- and offered a subagent no action it could take.
        # That branch is now silent. Neither guard speaks for this command, and
        # a message that does not exist cannot misname anything; the
        # names-banner-not-headtail contract is asserted where the guard DOES
        # speak (the seam-confirmed deny at line ~276, and
        # test_guard_multiprobe_banner.py's own outlet cases).
        cmd = 'echo "=== probe ==="; pwd | head -n 1; whoami'
        classification = classify_command(cmd)
        assert classification.primary is not None
        assert classification.primary.shape is Shape.MULTI_PROBE_BANNER
        assert Shape.HEAD_TAIL_PLUMBING in {m.shape for m in classification.residue}

        assert guard_multiprobe_banner.check(_payload(cmd), host_is_windows=True) is None
        assert guard_plumbing_and_loops.check(_payload(cmd), host_is_windows=True) is None


# ---------------------------------------------------------------------------
# Section 3 -- the seven BX-16 rewrite/advisory functions in
# dispatch_checks.py. None of these ever deny (see their own module
# comment) -- the audit here is that a returned rewrite/advisory correctly
# names ITS OWN trigger and never claims a full-command replacement for
# work it never inspected.
# ---------------------------------------------------------------------------


class TestFindExecRewriteMessageAccuracy:
    def test_single_segment_find_exec_names_verb_and_actually_rewrites(self):
        cmd = 'find . -name "*.log" -exec rm {} \\;'
        result = dispatch_checks.check_find_exec_rewrite(cmd, "sess-bx12")
        hso = _hso(result)
        assert hso["permissionDecision"] == "allow"
        assert "updatedInput" in hso
        assert "rm" in hso["additionalContext"]

    def test_multi_segment_find_exec_never_silently_drops_the_other_work(self):
        """KNOWN DEFECT #1 (fixed by this dispatch, same file/function --
        see the `LATENT-BUG FIX (BX-12 audit, same day)` comment inline in
        `check_find_exec_rewrite`). Before the fix, ANY command containing a
        `find ... -exec` segment ALONGSIDE other work -- not only the named
        for-loop-followed-by-a-trailing-find-exec shape, but any `;`/`&`-
        chained compound at all -- got its ENTIRE command silently replaced
        by `updatedInput.command` with just that one segment's python
        rewrite, discarding everything else (verified live against this
        exact repo's pre-fix code: `echo hi; find . -exec rm {} \\;` and
        `for x in 1 2 3; do echo $x; done; find . -exec rm {} \\;` both
        returned an `updatedInput.command` that ONLY performed the `rm`,
        with the `echo`/loop silently gone). That is not a message-wording
        defect -- it is a correctness defect worse than any deny-message
        typo: an auto-applied rewrite that executes something materially
        different from what the operator typed, with no error and no
        deny. Fixed here by restricting the full-command `_allow_rewrite`
        to the single-segment case (mirroring BX-6's own substitutable-
        residue rule); a multi-segment match now degrades to an advisory
        that names the specific offending segment and does not touch
        `updatedInput` at all.
        """
        cmd = 'echo hi; find . -name "*.log" -exec rm {} \\;'
        result = dispatch_checks.check_find_exec_rewrite(cmd, "sess-bx12")
        hso = _hso(result)
        assert hso["permissionDecision"] == "allow"
        assert "updatedInput" not in hso  # must NOT silently replace the whole command
        assert "echo hi" not in hso["additionalContext"]
        assert "find . -name *.log -exec rm {}" in hso["additionalContext"]
        assert "runs alongside OTHER work" in hso["additionalContext"]

    def test_for_loop_wrapping_a_trailing_find_exec_names_the_segment_correctly(self):
        """The plan's own named known-defect scenario, reproduced exactly:
        a for-loop followed by a trailing `find -exec`. The VERDICT (an
        allow+advisory here, or a deny once `guard_plumbing_and_loops` is
        registered -- see below) is correct either way; this test pins that
        the message/rewrite no longer treats the for-loop's own body as
        interchangeable with the unrelated trailing find-exec segment.
        """
        cmd = 'for x in 1 2 3; do echo "$x"; done; find . -name "*.log" -exec rm {} \\;'
        result = dispatch_checks.check_find_exec_rewrite(cmd, "sess-bx12")
        hso = _hso(result)
        assert "updatedInput" not in hso
        assert 'echo "$x"' not in hso["additionalContext"]
        assert "find . -name *.log -exec rm {}" in hso["additionalContext"]

        # And the platform-gated guard that CONSUMES this seam check (BX-8,
        # not yet registered in dispatch.py -- see module docstring
        # "ORDERING DEPENDENCY") correctly falls back to a GENERIC advisory
        # rather than denying toward the (no-longer-produced) corrupting
        # rewrite -- this is the guard's own `_seam_confirmed_rewrite` gate
        # working as designed once the seam stopped over-claiming.
        guard_result = guard_plumbing_and_loops.check(_payload(cmd), host_is_windows=True)
        guard_hso = _hso(guard_result)
        assert guard_hso["permissionDecision"] == "allow"  # advisory, NOT a deny
        # 2026-07-30 message-trim (76797f17) moved the BX-16/seam-mechanics
        # explanation to the module docstring; the generic-advisory marker
        # (never the confirmed-rewrite branch's own "auto-rewritten ...
        # equivalent" phrasing) is what actually distinguishes this path.
        assert "BASH-SPAWN ADVISORY" in guard_hso["additionalContext"]
        assert "auto-rewritten single-process equivalent" not in guard_hso["additionalContext"]

    def test_advisory_for_untranslatable_verb_names_that_verb(self):
        cmd = 'find . -name "*.bak" -exec chmod 644 {} \\;'
        result = dispatch_checks.check_find_exec_rewrite(cmd, "sess-bx12")
        hso = _hso(result)
        assert "updatedInput" not in hso
        assert "chmod" in hso["additionalContext"]


class TestGrepViaBashRewriteMessageAccuracy:
    def test_rewrite_uses_resolved_interpreter_not_a_bare_python3_literal(self):
        """KNOWN DEFECT #2 (interpreter/quoting fix), verified: today's
        `_bt_python3_invocation` resolves through `coordinator_core.pyresolve`
        (falling back to the literal `"python3"` only on resolution failure)
        and every emitted `-c` script is `shlex.quote`-escaped. Assert the
        rewrite this function emits actually goes through that helper by
        checking it is IDENTICAL to `_bt_python3_invocation()`'s own output
        joined the same way, rather than a hand-duplicated `"python3"`
        string that could silently drift from the real resolver.
        """
        cmd = 'grep -rn "TODO" src/'
        result = dispatch_checks.check_grep_via_bash_rewrite(cmd, "sess-bx12")
        hso = _hso(result)
        rewrite_cmd = hso["updatedInput"]["command"]
        resolved_prefix = dispatch_checks._bt_python3_invocation()
        assert rewrite_cmd.startswith(resolved_prefix)


class TestSedRangeReadAdviseMessageAccuracy:
    def test_advisory_names_the_read_tool_and_the_exact_range(self):
        cmd = "sed -n '10,20p' path/to/file.py"
        advisory = _advisory_text(
            _hso(dispatch_checks.check_sed_range_read_advise(cmd, "sess-bx12"))
        )
        assert "Read(path/to/file.py, offset=10, limit=11)" in advisory

    def test_non_range_sed_returns_none(self):
        assert dispatch_checks.check_sed_range_read_advise("sed 's/a/b/' f.txt", "s") is None


class TestCatHeredocWriteAdviseMessageAccuracy:
    def test_advisory_names_the_write_tool_and_the_exact_target(self):
        cmd = "cat > out.txt <<'EOF'\nhello\nEOF"
        advisory = _advisory_text(
            _hso(dispatch_checks.check_cat_heredoc_write_advise(cmd, "sess-bx12"))
        )
        assert "Write tool" in advisory
        assert "out.txt" in advisory


class TestGitCommitSafeCommitAdviseMessageAccuracy:
    """The property under test is SCOPE-EQUIVALENCE, not string presence.

    The prior version of this class asserted only that the strings
    `coordinator-safe-commit` and the subject appeared in the advisory --
    which stayed green for as long as the advisory printed a suggestion
    that silently dropped the caller's `-- <pathspec>`. A test that cannot
    see the one property that makes a suggestion safe to follow is why an
    incorrect suggestion shipped green (example-doctrine-repo-em cross-repo memo,
    2026-07-29).
    """

    #: Commit forms that already carry the ratified scope. Firing on any of
    #: these is a false positive on the doctrinally-correct form.
    SCOPED_FORMS = [
        'git commit -m "fix the thing" -- one/file.md',
        'git commit -m "fix the thing" -- a/b.md c/d.md',
        'git add -- one/file.md && git commit -m "subject" -- one/file.md',
        'git -C /tmp/repo commit -m "subject" -- one/file.md',
        'git commit --message "subject" -- one/file.md',
    ]

    #: Commit forms that name no scope at all -- the cases the advisory is
    #: actually for.
    UNSCOPED_FORMS = [
        'git commit -m "fix the thing"',
        "git commit -am 'fix the thing'",
        "git commit",
        'git commit -m "subject" --',
    ]

    def test_explicit_pathspec_suppresses_the_advisory(self):
        for cmd in self.SCOPED_FORMS:
            assert dispatch_checks.check_git_commit_safe_commit_advise(cmd, "sess-bx12") is None, cmd

    def test_unscoped_forms_still_fire(self):
        for cmd in self.UNSCOPED_FORMS:
            assert dispatch_checks.check_git_commit_safe_commit_advise(cmd, "sess-bx12") is not None, cmd

    def test_every_suggested_commit_command_carries_a_scope(self):
        """Scope-equivalence: any runnable `git commit` the advisory prints
        must itself be scoped. A suggestion narrower than what the caller
        typed is the defect this asserts against."""
        for cmd in self.UNSCOPED_FORMS:
            advisory = _advisory_text(
                _hso(dispatch_checks.check_git_commit_safe_commit_advise(cmd, "sess-bx12"))
            )
            suggested = [
                line.strip()
                for line in advisory.splitlines()
                if line.startswith("  ") and "git commit" in line
            ]
            assert suggested, "advisory prints no runnable suggestion at all: %r" % advisory
            for line in suggested:
                commit_tail = line.split("git commit", 1)[1]
                assert " -- " in commit_tail, "unscoped suggestion %r for %r" % (line, cmd)

    def test_advisory_does_not_offer_the_deprecated_raw_helper_form(self):
        """`coordinator-safe-commit "<subject>"` (no flags) is deprecated by
        the shared wiki, has no pathspec flag to take a scope back, and its
        own multi-session abort path bottoms out in `git add -A`. It must
        not be what this advisory offers."""
        advisory = _advisory_text(
            _hso(dispatch_checks.check_git_commit_safe_commit_advise('git commit -m "s"', "sess-bx12"))
        )
        assert "coordinator-safe-commit" not in advisory

    def test_advisory_names_the_divergence_case_surface_and_carries_the_subject(self):
        advisory = _advisory_text(
            _hso(dispatch_checks.check_git_commit_safe_commit_advise('git commit -m "fix the thing"', "s"))
        )
        assert "scoped-git-commit" in advisory
        assert "fix the thing" in advisory

    def test_the_divergence_case_surface_it_names_is_actually_invocable(self):
        """The advisory must name a CLI that EXISTS on the entrypoint surface
        a caller can reach, not an op id.

        `ceremony.scoped_git_commit` — which this advisory named for its first
        few hours of life — is a registered op with no bin artifact, and the
        settings-home forwarder set is derived from a scan of
        `coordinator/bin/`. Naming a dotted op id reads to a caller exactly
        like naming nothing (example-doctrine-repo-em, 2026-07-29: "an op named in
        doctrine with no reachable entrypoint reads to a caller exactly like
        no mechanism at all"). This asserts against the artifact whose
        presence is what makes the forwarder generate at all.
        """
        advisory = _advisory_text(
            _hso(dispatch_checks.check_git_commit_safe_commit_advise('git commit -m "s"', "s"))
        )
        repo_root = pathlib.Path(dispatch_checks.__file__).resolve().parents[2]
        entrypoint = repo_root / "coordinator" / "bin" / "scoped-git-commit"
        assert "scoped-git-commit" in advisory
        assert entrypoint.exists(), (
            "the advisory names scoped-git-commit but %s does not exist, so no "
            "settings-home forwarder can be derived for it" % entrypoint
        )
        # The ops namespace is not a caller-reachable surface, so a dotted op
        # id must never stand in as the offer.
        assert "ceremony.scoped_git_commit" not in advisory

    def test_bundled_short_flag_subject_is_carried(self):
        advisory = _advisory_text(
            _hso(dispatch_checks.check_git_commit_safe_commit_advise("git commit -am 'fix the thing'", "s"))
        )
        assert "fix the thing" in advisory

    def test_non_commit_git_subcommand_returns_none(self):
        assert dispatch_checks.check_git_commit_safe_commit_advise("git status", "s") is None


class TestMultiprobeBannerRewriteMessageAccuracy:
    def test_recognized_probes_rewrite_and_advisory_names_the_facts_batched(self):
        cmd = 'echo "=== SESSION FACTS ==="; pwd; whoami; git status --short'
        result = dispatch_checks.check_multiprobe_banner_rewrite(cmd, "sess-bx12")
        hso = _hso(result)
        assert "updatedInput" in hso
        rewrite_cmd = hso["updatedInput"]["command"]
        # The rewrite must use the resolved interpreter, not a bare literal.
        assert rewrite_cmd.startswith(dispatch_checks._bt_python3_invocation())
        assert "ONE 'git status --porcelain=v2 --branch' call" in hso["additionalContext"]

    def test_unrecognized_probe_emits_nothing_not_a_generic_advisory(self):
        # C4 (2026-08-01): a probe segment with no concrete rewrite makes
        # the whole check emit nothing -- exit (b) -- rather than a
        # prose-only advisory naming the unrecognized segment with no
        # applicable alternative offered.
        cmd = 'echo "=== SESSION FACTS ==="; pwd; docker ps'
        result = dispatch_checks.check_multiprobe_banner_rewrite(cmd, "sess-bx12")
        assert result is None


class TestHeadTailPlumbingRewriteMessageAccuracy:
    def test_two_segment_find_pipe_head_rewrites_with_resolved_interpreter(self):
        cmd = "find . -name '*.py' | head -n 5"
        result = guard_head_tail_rewrite.check_head_tail_plumbing_rewrite(cmd, "sess-bx12")
        hso = _hso(result)
        rewrite_cmd = hso["updatedInput"]["command"]
        assert rewrite_cmd.startswith(dispatch_checks._bt_python3_invocation())

    def test_longer_chain_advisory_names_the_actual_segment_count_reason(self):
        # Reproduces the exact PreToolUse advisory this dispatch's own Bash
        # calls tripped mid-session (`cd X && grep ... | head -100`, a
        # 3-segment chain) -- the message must say "longer chain", never
        # misattribute this to an unrecognized upstream generator.
        cmd = "cd /tmp && grep -n foo file.py | head -100"
        advisory = _advisory_text(
            _hso(guard_head_tail_rewrite.check_head_tail_plumbing_rewrite(cmd, "sess-bx12"))
        )
        assert "longer chain than this rewrite's conservative two-stage" in advisory
        # Wrong-reason check. The discriminator used to be the phrase "no
        # known translation on file", which was retired 2026-07-29: it
        # framed a structural impossibility (reproducing an unrecognized
        # upstream costs MORE forks, so no rewrite could help) as a mere
        # gap in this guard's translation table. Both branches now state
        # the real cost, so the discriminator is the branch-specific
        # wording -- kept as a live negative, not a phrase that no longer
        # appears anywhere and would pass tautologically.
        assert "not one this guard can reproduce in Python" not in advisory

    def test_unrecognized_upstream_generator_advisory_names_that_generator(self):
        cmd = "docker ps | head -n 20"
        advisory = _advisory_text(
            _hso(guard_head_tail_rewrite.check_head_tail_plumbing_rewrite(cmd, "sess-bx12"))
        )
        assert "docker ps" in advisory
        assert "not one this guard can reproduce in Python" in advisory
        assert "longer chain than this rewrite's conservative two-stage" not in advisory

    def test_unrecognized_count_form_advisory_names_the_actual_arguments(self):
        cmd = "find . -name '*.py' | head -c 100"
        advisory = _advisory_text(
            _hso(guard_head_tail_rewrite.check_head_tail_plumbing_rewrite(cmd, "sess-bx12"))
        )
        assert "-c 100" in advisory


# ---------------------------------------------------------------------------
# Section 4 -- sampled spot-check of pre-existing hard-denies already
# registered in dispatch.py's guard_chain, confirming the audit method this
# file establishes also holds outside the BX-6/7/8/16 family. NOT exhaustive
# -- see module docstring "SCOPE OF THIS PASS".
# ---------------------------------------------------------------------------


class TestSampledPreexistingGuardsMessageAccuracy:
    """One spot-check only (`check_offer_git_c`) -- see module docstring
    "SCOPE OF THIS PASS". `check_destructive_rm` and `check_blanket_git_add`
    were tried here and both returned `None` for their obvious triggering
    commands (`rm -rf /`, `git add -A`) when called with only the
    `(cmd, session_id)` signature this module's other checks accept --
    those two guards evidently require additional preconditions/state this
    file did not chase down (their own dedicated test files,
    `test_check_blanket_git_add.py` and the destructive-action suite in
    `test_block_subagent_destructive_action.py`, are the correct place for
    that investigation, not a quick sample here). Not treated as a BX-12
    finding since no message was ever produced to misdescribe; recorded as
    a residual follow-up in this dispatch's own report instead.
    """

    def test_offer_git_c_rewrite_names_the_actual_cd_and_git_subcommand(self):
        cmd = "cd /tmp/repo && git status"
        result = guard_offer_git_c.check_offer_git_c(cmd, "sess-bx12", "")
        hso = _hso(result)
        rewrite_cmd = _rewrite_command(hso)
        assert rewrite_cmd == "git -C /tmp/repo status"


# ---------------------------------------------------------------------------
# Section 5 -- dispatch._crash_deny's own message, the generic fail-closed
# envelope emitted when a fail_closed=True guard itself raises. Two claims
# in this message were observed FALSE in a live session (2026-07-29): (1)
# that the deny is total across every subsequent Bash command regardless of
# shape, when the dispatcher denies per-command and a shape-conditioned
# crash leaves non-matching shapes unaffected; and (2) that nothing can be
# done from inside the session, when file-reading tools reach the guard
# source without Bash at all. The fail-closed DIRECTION is out of scope
# here (see `_crash_deny`'s own docstring "BLAST-RADIUS DECISION") -- only
# the message's factual claims are under test.
# ---------------------------------------------------------------------------


class TestCrashDenyMessageAccuracy:
    def test_does_not_claim_every_command_in_session_is_denied(self):
        deny = _deny_text(_hso(dispatch._crash_deny("some-guard", ValueError("boom"))))
        assert "every Bash command in this session is now denied" not in deny

    def test_does_not_claim_it_cannot_be_addressed_in_session(self):
        deny = _deny_text(_hso(dispatch._crash_deny("some-guard", ValueError("boom"))))
        assert "CANNOT be fixed from inside this session" not in deny
        assert "there is no in-session override" not in deny

    def test_positively_states_the_two_facts_the_old_wording_denied(self):
        """The two tests above assert the ABSENCE of specific false
        substrings, which a reworded version of the same false claim would
        slip past. These positive assertions are the durable half: the
        message must actually tell the reader that the deny may be
        shape-conditioned (so a trivial command is worth trying) and that
        the guard source is reachable without Bash. Rewording is free;
        dropping either fact is not.
        """
        deny = _deny_text(_hso(dispatch._crash_deny("some-guard", ValueError("boom"))))
        assert "shape" in deny, (
            "message must convey that the crash may be shape-conditioned "
            f"rather than a total session wedge, got: {deny}"
        )
        assert "without Bash" in deny or "not require the Bash tool" in deny, (
            "message must tell the reader diagnosis does not depend on the "
            f"very tool being denied, got: {deny}"
        )

    def test_still_names_the_guard_and_exception_and_points_at_the_source(self):
        deny = _deny_text(_hso(dispatch._crash_deny("some-guard", ValueError("boom"))))
        assert "some-guard" in deny
        assert "ValueError" in deny
        assert "boom" in deny
        assert "coordinator_core/bash_guards/" in deny
        assert "dispatch.py" in deny
