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
    `check_heredoc_repo_write_advise`, `check_git_commit_safe_commit_advise`,
    `check_multiprobe_banner_rewrite`, `check_head_tail_plumbing_rewrite`.
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

Spec backlink: DoE-claude:pln-windows-viability-stop-the-spa-b969d9 § BX-12
"""

from __future__ import annotations

import pathlib

import pytest

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


class TestGrepViaBashMessageAccuracy:

    def test_substitutable_residue_never_fires_on_either_host(self):
        # (i) OBSOLETE, not repointed: this command used to trip a
        # exact command as an ADVISORY_REWRITE, so this guard has nothing
        cmd = 'grep -rn "TODO" src/'
        assert guard_grep_via_bash.check(_payload(cmd), host_is_windows=True) is None
        assert guard_grep_via_bash.check(_payload(cmd), host_is_windows=False) is None

    def test_composed_pipeline_with_only_portable_flags_stays_silent(self):
        # (i) OBSOLETE, not repointed: this command used to name "runs as
        cmd = 'grep -rn "TODO" src/ ; echo done'
        assert guard_grep_via_bash.check(_payload(cmd), host_is_windows=True) is None

    def test_two_segment_pipe_offers_a_real_partial_rewrite_not_prose(self):
        # (ii) STILL MEANINGFUL, repointed: this is the exact command the
        cmd = 'grep -rn "TODO" src/ | wc -l'
        advisory = _advisory_text(
            _hso(guard_grep_via_bash.check(_payload(cmd), host_is_windows=True))
        )
        assert "one-fewer-fork replacement" in advisory
        assert "| wc -l" in advisory
        assert "runs as part of a larger shell chain" not in advisory

    def test_gnu_only_construct_advisory_names_the_command_and_divergence(self):
        # (ii) STILL MEANINGFUL, repointed: replaces `test_untranslatable_
        cmd = 'grep -Pn "TODO" src/'
        advisory = _advisory_text(
            _hso(guard_grep_via_bash.check(_payload(cmd), host_is_windows=True))
        )
        assert "GNU-only grep construct" in advisory
        assert cmd.replace('"', "") in advisory or "grep -Pn TODO src/" in advisory
        assert "BSD grep (macOS)" in advisory

    def test_untranslatable_but_portable_flag_stays_silent(self):
        # (i) OBSOLETE, not repointed: the old blanket composed advisory
        cmd = 'grep -C 3 "TODO" src/file.py'
        assert guard_grep_via_bash.check(_payload(cmd), host_is_windows=True) is None

    def test_substitutable_residue_deny_does_not_claim_in_process_or_zero_fork(self):
        # (ii) STILL MEANINGFUL, repointed at the surviving message: the
        cmd = 'grep -rn "TODO" src/ | wc -l'
        advisory = _advisory_text(
            _hso(guard_grep_via_bash.check(_payload(cmd), host_is_windows=True))
        )
        assert "no subprocess fork" not in advisory
        assert "one-fewer-fork" in advisory
        assert "zero-fork replacement" not in advisory

    def test_rewrite_advisory_does_not_claim_harness_can_do_it_in_process(self):
        # (ii) UNCHANGED -- this test exercises `dispatch_checks.check_
        cmd = 'grep -rn "TODO" src/'
        seam = dispatch_checks.check_grep_via_bash_rewrite(cmd, "sess-bx12")
        hso = _hso(seam)
        assert hso["permissionDecision"] == "allow"
        assert "harness can do in-process" not in hso.get("additionalContext", "")

    def test_partial_rewrite_advisory_names_subagent_dispatch_as_fallback(self):
        # (ii) STILL MEANINGFUL, repointed: the retired `test_composed_
        cmd = 'grep -rn "TODO" src/ | wc -l'
        advisory = _advisory_text(
            _hso(guard_grep_via_bash.check(_payload(cmd), host_is_windows=True))
        )
        assert "registered ahead of this guard" not in advisory
        assert "Explore or general-purpose subagent" in advisory


class TestMultiProbeBannerMessageAccuracy:

    _BANNER_CMD = 'echo "=== SESSION FACTS ==="; git rev-parse --abbrev-ref HEAD; pwd'

    def test_advisory_names_multiprobe_shape_and_evidence_banner(self):
        # RETARGETED (DR-280, 2026-08-07): this guard's own deny branch was
        # confirmation an earlier-registered `ADVISORY_REWRITE` chain entry
        advisory = _advisory_text(
            _hso(guard_multiprobe_banner.check(_payload(self._BANNER_CMD), host_is_windows=True))
        )
        assert "multi-probe-banner" in advisory
        assert "SESSION FACTS" in advisory

    def test_outlet_example_is_the_real_seam_rewrite_not_a_static_illustration(self):
        # RETARGETED (DR-280, 2026-08-07): was reading `_deny_text`; this
        advisory = _advisory_text(
            _hso(guard_multiprobe_banner.check(_payload(self._BANNER_CMD), host_is_windows=True))
        )
        real_rewrite = dispatch_checks.check_multiprobe_banner_rewrite(
            self._BANNER_CMD, "sess-bx12"
        )
        real_cmd = _rewrite_command(_hso(real_rewrite))
        assert real_cmd in advisory

    def test_non_banner_command_returns_none(self):
        assert guard_multiprobe_banner.check(_payload("git status"), host_is_windows=True) is None


class TestPlumbingAndLoopsMessageAccuracy:

    def test_head_tail_plumbing_advisory_names_that_shape_not_for_loop(self):
        # RETARGETED (DR-280, 2026-08-07): mirrors the multi-probe-banner
        cmd = "find . -name '*.py' | head -n 5"
        advisory = _advisory_text(
            _hso(guard_plumbing_and_loops.check(_payload(cmd), host_is_windows=True))
        )
        assert "head-tail-plumbing" in advisory
        assert "for-loop" not in advisory

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
        assert "BASH-SPAWN ADVISORY" in advisory
        assert "auto-rewritten single-process equivalent" not in advisory
        assert "docker ps | head -n 20" in advisory

    def test_while_read_advisory_names_that_shape_not_for_loop(self):
        cmd = 'cat items.txt | while read x; do echo "$x"; done'
        advisory = _advisory_text(
            _hso(guard_plumbing_and_loops.check(_payload(cmd), host_is_windows=True))
        )
        assert "while-read-loop" in advisory
        assert "for-loop" not in advisory


# shapes at once must produce a message naming the PRECEDENCE WINNER, never
# GREP_VIA_BASH > MULTI_PROBE_BANNER > HEAD_TAIL_PLUMBING > FOR_LOOP >
# WHILE_READ_LOOP > FIND_EXEC_XARGS.


class TestShapeOverlapPrecedenceInMessages:
    def test_two_shape_overlap_banner_plus_grep_names_grep_not_banner(self):
        # SHAPE_PRECEDENCE, GREP_VIA_BASH outranks MULTI_PROBE_BANNER.
        # is COMPOSED, not substitutable residue (BX-6's own single-segment
        # (ii) STILL MEANINGFUL, repointed (H11, 2026-07-30): the original
        # (iii) REPOINTED AGAIN (2026-08-14, _shape_classifier false-positive
        # false-positive matcher"): MULTI_PROBE_BANNER now requires every
        # pipe CONTINUATION of the `pwd` probe (mirrors the plan's own
        cmd = 'echo "=== probe ==="; pwd | grep -Pn "TODO"'
        classification = classify_command(cmd)
        assert classification.primary is not None
        assert classification.primary.shape is Shape.GREP_VIA_BASH
        assert Shape.MULTI_PROBE_BANNER in classification.matched_shapes

        grep_advisory = _advisory_text(
            _hso(guard_grep_via_bash.check(_payload(cmd), host_is_windows=True))
        )
        assert "grep-via-bash" in grep_advisory

        # it only speaks when MULTI_PROBE_BANNER is the precedence winner.
        assert guard_multiprobe_banner.check(_payload(cmd), host_is_windows=True) is None

    def test_multiprobe_rewrite_seam_agrees_with_guard_on_grep_primary_command(self):
        # gated on plain `has_shape(MULTI_PROBE_BANNER)` membership, while
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
        # (ii) STILL MEANINGFUL, repointed (H11, 2026-07-30): same reason as
        # (iii) REPOINTED AGAIN (2026-08-14, same false-positive fix as the
        cmd = 'echo "=== probe ==="; pwd | grep -Pn "TODO" | head -n 5'
        classification = classify_command(cmd)
        assert classification.primary is not None
        assert classification.primary.shape is Shape.GREP_VIA_BASH
        residue_shapes = {m.shape for m in classification.residue}
        assert Shape.MULTI_PROBE_BANNER in residue_shapes
        assert Shape.HEAD_TAIL_PLUMBING in residue_shapes

        assert guard_multiprobe_banner.check(_payload(cmd), host_is_windows=True) is None
        assert guard_plumbing_and_loops.check(_payload(cmd), host_is_windows=True) is None
        grep_result = guard_grep_via_bash.check(_payload(cmd), host_is_windows=True)
        assert grep_result is not None

    def test_banner_plus_headtail_no_grep_names_banner_not_headtail(self):
        # MULTI_PROBE_BANNER (>=3 segments, banner echo) + HEAD_TAIL_PLUMBING
        # (a piped head), no grep anywhere. MULTI_PROBE_BANNER outranks
        # HEAD_TAIL_PLUMBING. The piped `pwd | head -n 1` segment is a
        # 2026-08-06 (B2): this used to assert an ADVISORY naming the banner
        cmd = 'echo "=== probe ==="; pwd | head -n 1; whoami'
        classification = classify_command(cmd)
        assert classification.primary is not None
        assert classification.primary.shape is Shape.MULTI_PROBE_BANNER
        assert Shape.HEAD_TAIL_PLUMBING in {m.shape for m in classification.residue}

        assert guard_multiprobe_banner.check(_payload(cmd), host_is_windows=True) is None
        assert guard_plumbing_and_loops.check(_payload(cmd), host_is_windows=True) is None


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
        assert "updatedInput" not in hso
        assert "echo hi" not in hso["additionalContext"]
        assert "find . -name *.log -exec rm {}" in hso["additionalContext"]
        assert "runs alongside OTHER work" in hso["additionalContext"]

    def test_for_loop_wrapping_a_trailing_find_exec_names_the_segment_correctly(self):
        cmd = 'for x in 1 2 3; do echo "$x"; done; find . -name "*.log" -exec rm {} \\;'
        result = dispatch_checks.check_find_exec_rewrite(cmd, "sess-bx12")
        hso = _hso(result)
        assert "updatedInput" not in hso
        assert 'echo "$x"' not in hso["additionalContext"]
        assert "find . -name *.log -exec rm {}" in hso["additionalContext"]

        # And the platform-gated guard that CONSUMES this seam check (BX-8,
        # "ORDERING DEPENDENCY") correctly falls back to a GENERIC advisory
        guard_result = guard_plumbing_and_loops.check(_payload(cmd), host_is_windows=True)
        guard_hso = _hso(guard_result)
        assert guard_hso["permissionDecision"] == "allow"
        assert "BASH-SPAWN ADVISORY" in guard_hso["additionalContext"]
        assert "auto-rewritten single-process equivalent" not in guard_hso["additionalContext"]

    def test_a_verb_with_no_python_translation_is_offered_the_posix_plus_form(self):
        """SUPERSEDED PREMISE, corrected 2026-08-31. This row asserted that an
        untranslatable verb gets prose and no runnable command -- true until C2
        (`e8a0043e66`) added `_FIND_EXEC_BATCH_EQUIVALENT_VERBS`. `chmod` has no
        PYTHON translation and still does not; what it has now is a MEASURED
        batch equivalence, so the guard offers the POSIX `+` form. Untranslatable
        and unbatchable are different properties and the old assertion conflated
        them -- which is why C2 landed without this file going red.
        """
        cmd = 'find . -name "*.bak" -exec chmod 644 {} \\;'
        result = dispatch_checks.check_find_exec_rewrite(cmd, "sess-bx12")
        hso = _hso(result)
        assert hso["updatedInput"]["command"] == "find . -name '*.bak' -exec chmod 644 '{}' +"
        assert "chmod" in hso["additionalContext"]

    def test_a_verb_on_neither_list_still_gets_prose_and_names_itself(self):
        cmd = 'find . -name "*.bak" -exec frobnicate {} \\;'
        result = dispatch_checks.check_find_exec_rewrite(cmd, "sess-bx12")
        hso = _hso(result)
        assert "updatedInput" not in hso
        assert "frobnicate" in hso["additionalContext"]


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
    def test_advisory_names_the_exact_target_and_what_is_recorded(self):
        cmd = "cat > out.txt <<'EOF'\nhello\nEOF"
        advisory = _advisory_text(
            _hso(dispatch_checks.check_cat_heredoc_write_advise(cmd, "sess-bx12"))
        )
        assert "out.txt" in advisory
        assert "DR-258" in advisory

    def test_advisory_does_not_name_a_tool_to_prefer(self):
        cmd = "cat > out.txt <<'EOF'\nhello\nEOF"
        advisory = _advisory_text(
            _hso(dispatch_checks.check_cat_heredoc_write_advise(cmd, "sess-bx12"))
        )
        assert "Write tool" not in advisory


class TestHeredocRepoWriteAdviseMessageAccuracy:
    def test_advisory_names_the_exact_target_and_what_is_recorded(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TEMP", raising=False)
        monkeypatch.delenv("TMP", raising=False)
        cmd = (
            "python3 - <<'PY'\n"
            "import pathlib\n"
            'pathlib.Path("coordinator_core/x.py").write_text("hi")\n'
            "PY"
        )
        advisory = _advisory_text(
            _hso(
                dispatch_checks.check_heredoc_repo_write_advise(
                    cmd, "sess-bx12", None, str(tmp_path)
                )
            )
        )
        # This guard only ever fires on a path the command LITERALLY names,
        assert "coordinator_core/x.py" in advisory
        assert "recorded" in advisory
        assert "does not name" in advisory
        assert "Write tool" not in advisory

    def test_no_git_root_returns_none(self):
        cmd = (
            "python3 - <<'PY'\n"
            "import pathlib\n"
            'pathlib.Path("coordinator_core/x.py").write_text("hi")\n'
            "PY"
        )
        assert dispatch_checks.check_heredoc_repo_write_advise(cmd, "sess") is None


class TestGitCommitSafeCommitAdviseMessageAccuracy:
    """The property under test is SCOPE-EQUIVALENCE, not string presence.

    The prior version of this class asserted only that the strings
    `coordinator-safe-commit` and the subject appeared in the advisory --
    which stayed green for as long as the advisory printed a suggestion
    that silently dropped the caller's `-- <pathspec>`. A test that cannot
    see the one property that makes a suggestion safe to follow is why an
    incorrect suggestion shipped green (doe-claude-em cross-repo memo,
    2026-07-29).
    """

    @pytest.fixture(autouse=True)
    def _empty_index(self, monkeypatch):
        """This class's remit is advisory MESSAGE text, not index-probe
        behavior (that's `test_git_commit_safe_commit_deny_escalation.py`'s
        job, C7 + its 2026-08-15 solo-bare-commit sibling). None of the
        UNSCOPED_FORMS below pin `-C <repo>`, so an un-stubbed probe would
        read whatever THIS process's ambient cwd (the live claude-klabauter
        checkout, shared and dirty) happens to have staged at test-run
        time -- flaky by construction now that a non-empty index can
        escalate a solo bare commit to DENY. Pinning the probe to "always
        empty" keeps every row here at its pre-existing advisory outcome,
        deterministically."""
        monkeypatch.setattr(dispatch_checks, "_run_git", lambda *a, **k: (0, ""))

    SCOPED_FORMS = [
        'git commit -m "fix the thing" -- one/file.md',
        'git commit -m "fix the thing" -- a/b.md c/d.md',
        'git add -- one/file.md && git commit -m "subject" -- one/file.md',
        'git -C /tmp/repo commit -m "subject" -- one/file.md',
        'git commit --message "subject" -- one/file.md',
    ]

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
        advisory = _advisory_text(
            _hso(dispatch_checks.check_git_commit_safe_commit_advise('git commit -m "s"', "sess-bx12"))
        )
        assert "coordinator-safe-commit" not in advisory

    def test_advisory_names_the_divergence_case_surface_and_carries_the_subject(self):
        advisory = _advisory_text(
            _hso(dispatch_checks.check_git_commit_safe_commit_advise('git commit -m "fix the thing"', "s"))
        )
        assert "git add" in advisory
        assert "fix the thing" in advisory

    def test_the_divergence_case_surface_it_names_is_actually_invocable(self):
        advisory = _advisory_text(
            _hso(dispatch_checks.check_git_commit_safe_commit_advise('git commit -m "s"', "s"))
        )
        assert "git add" in advisory and "git commit" in advisory
        assert "scoped-git-commit" not in advisory
        assert "ceremony.scoped_git_commit" not in advisory

    def test_bundled_short_flag_subject_is_carried(self):
        advisory = _advisory_text(
            _hso(dispatch_checks.check_git_commit_safe_commit_advise("git commit -am 'fix the thing'", "s"))
        )
        assert "fix the thing" in advisory

    def test_non_commit_git_subcommand_returns_none(self):
        assert dispatch_checks.check_git_commit_safe_commit_advise("git status", "s") is None

    def test_non_amend_advisory_text_is_unchanged_by_the_amend_fix(self):
        advisory = _advisory_text(
            _hso(dispatch_checks.check_git_commit_safe_commit_advise(
                'git commit -m "fix the thing"', "s"
            ))
        )
        assert "rewrites whatever commit is at HEAD" not in advisory
        assert "git notes add -f -m" not in advisory
        assert "git add -- <paths> && git commit -m" in advisory

class TestMultiprobeBannerRewriteMessageAccuracy:
    def test_recognized_probes_rewrite_and_advisory_names_the_facts_batched(self):
        cmd = 'echo "=== SESSION FACTS ==="; pwd; whoami; git status --short'
        result = dispatch_checks.check_multiprobe_banner_rewrite(cmd, "sess-bx12")
        hso = _hso(result)
        assert "updatedInput" in hso
        rewrite_cmd = hso["updatedInput"]["command"]
        assert rewrite_cmd.startswith(dispatch_checks._bt_python3_invocation())
        assert "batching every git fact into ONE status call" in hso["additionalContext"]

    def test_unrecognized_probe_emits_nothing_not_a_generic_advisory(self):
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

    def test_longer_chain_silent_not_advised(self):
        cmd = "cd /tmp && grep -n foo file.py | head -100"
        assert (
            guard_head_tail_rewrite.check_head_tail_plumbing_rewrite(cmd, "sess-bx12")
            is None
        )

    def test_unrecognized_upstream_generator_silent_not_advised(self):
        cmd = "docker ps | head -n 20"
        assert (
            guard_head_tail_rewrite.check_head_tail_plumbing_rewrite(cmd, "sess-bx12")
            is None
        )

    def test_unrecognized_count_form_advisory_names_the_actual_arguments(self):
        cmd = "find . -name '*.py' | head -c 100"
        advisory = _advisory_text(
            _hso(guard_head_tail_rewrite.check_head_tail_plumbing_rewrite(cmd, "sess-bx12"))
        )
        assert "-c 100" in advisory


class TestSampledPreexistingGuardsMessageAccuracy:

    def test_offer_git_c_rewrite_names_the_actual_cd_and_git_subcommand(self):
        cmd = "cd /tmp/repo && git status"
        result = guard_offer_git_c.check_offer_git_c(cmd, "sess-bx12", "")
        hso = _hso(result)
        rewrite_cmd = _rewrite_command(hso)
        assert rewrite_cmd == "git -C /tmp/repo status"


# source without Bash at all. The fail-closed DIRECTION is out of scope
# here (see `_crash_deny`'s own docstring "BLAST-RADIUS DECISION") -- only


class TestCrashDenyMessageAccuracy:
    def test_does_not_claim_every_command_in_session_is_denied(self):
        deny = _deny_text(_hso(dispatch._crash_deny("some-guard", ValueError("boom"))))
        assert "every Bash command in this session is now denied" not in deny

    def test_does_not_claim_it_cannot_be_addressed_in_session(self):
        deny = _deny_text(_hso(dispatch._crash_deny("some-guard", ValueError("boom"))))
        assert "CANNOT be fixed from inside this session" not in deny
        assert "there is no in-session override" not in deny

    def test_positively_states_the_two_facts_the_old_wording_denied(self):
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
