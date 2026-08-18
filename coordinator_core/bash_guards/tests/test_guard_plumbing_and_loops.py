"""Tests for coordinator_core.bash_guards.guard_plumbing_and_loops (BX-8).

Coverage:
  - HEAD_TAIL_PLUMBING: a genuine `find | head` / `grep ... | tail -n N`
    two-stage pipeline ADVISES on both Windows (`host_is_windows=True`) and
    macOS (`host_is_windows=False`), since `check_head_tail_plumbing_
    rewrite` confirms a concrete outlet for both -- this guard's own
    platform-conditioned DENY branch was retired 2026-08-07 as structurally
    unreachable (DR-280): it gated on the same seam-confirmation an
    earlier-registered `ADVISORY_REWRITE` chain entry already consumes and
    returns on first, so through the real dispatcher the deny gate could
    never open. `host_is_windows` is still accepted and still exercised
    below (it is the chain-wide threading contract every registered
    shape-guard honors), but no longer changes this guard's own verdict.
  - FOR_LOOP wrapping a literal `find ... -exec rm {} \\;`: stays
    advisory-only on BOTH platforms, even with `host_is_windows=True`
    forced. UPDATED (BX-12 audit, same day as this guard's own authoring):
    `check_find_exec_rewrite` no longer treats a `find -exec` segment that
    is NOT the command's only segment as a confirmed rewrite target -- its
    prior behavior silently replaced the ENTIRE command (dropping the
    for-loop's own body) via `updatedInput.command`, a corrupting
    auto-rewrite, not merely a misdescribing message (see
    `dispatch_checks.check_find_exec_rewrite`'s own inline
    "LATENT-BUG FIX" comment and `tests/test_deny_message_accuracy.py`'s
    `TestFindExecRewriteMessageAccuracy`). `_seam_confirmed_rewrite` now
    correctly sees no `updatedInput` for this shape and this guard degrades
    to its own generic "no confirmed outlet" advisory, identically to the
    bare-glob case below.
  - FOR_LOOP over a bare glob (`for f in *.txt; do rm "$f"; done`): stays
    advisory-only on BOTH platforms (even with `host_is_windows=True`
    forced) -- `check_find_exec_rewrite` has no outlet for it (the narrow
    seam only recognizes a `find -exec` wrapper).
  - AC-7 precedence correctness: a command that is simultaneously
    grep-via-Bash and head/tail-plumbing (or a multi-probe banner and a
    for-loop) leaves this guard silent (`None`), since GREP_VIA_BASH /
    MULTI_PROBE_BANNER outrank this guard's two shapes in
    `_shape_classifier.SHAPE_PRECEDENCE`.
  - non-Bash tool, empty command, malformed tool_input, and a plain command
    all allow silently (`None`).
  - `COORDINATOR_OVERRIDE_PLUMBING_AND_LOOPS=1` suppresses every verdict
    this guard would otherwise return, on both shapes.
  - a seam check returning a BARE ADVISORY (no `updatedInput`, i.e. no
    confirmed rewrite -- an unrecognized head/tail upstream generator, a
    pipeline longer than two segments, or a `find -exec` verb outside
    rm/cat/wc -l) never denies, even under a forced Windows host --
    adversarial-review regression coverage: an earlier revision of this
    guard treated ANY non-``None`` seam return as a confirmed outlet and
    denied common benign commands (`docker ps | head`, `git log --oneline
    | head`) toward an "Example" that was just the seam's own disclaimer
    prose.
  - the escape hatch's own name is present in every advisory message
    (self-describing, per the standing "every guard names its own escape
    hatch" rule).
  - no deny envelope EVER fires, even with `host_is_windows=True` forced --
    pinned by re-asserting the same commands under both `host_is_windows`
    values (DR-280).

Pure Python -- no shell spawns, no git repo required, EXCEPT
`TestVerbatimHeadTailAlternativeIsRealAndEquivalent` below, which spawns both
the original command and `_verbatim_head_tail_alternative`'s emitted `python3
-c` replacement and diffs their stdout byte-for-byte -- the one thing that
must be checked by actual execution, not by inspecting the generated source.

Spec backlink: coordinator_core/bash_guards/guard_plumbing_and_loops.py
Spec backlink (verbatim-alternative promotion): DoE-claude:pln-bash-guard-merged-execution-shape-a71e05 M3
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys

import pytest

from coordinator_core.bash_guards import guard_plumbing_and_loops as guard
from coordinator_core.bash_guards._helpers import OVERRIDE_KEYS_DOC

#: Same bridge-to-C8 skip pattern as `test_guard_multiprobe_banner.py`'s own
#: `requires_powershell_grammar` -- the grammar package is not yet declared
#: in `pyproject.toml` (C8), so a peer/clean-install run without it must not
#: go red for a dependency no manifest asked them to have.
_GRAMMAR_PRESENT = all(
    importlib.util.find_spec(name) is not None
    for name in ("tree_sitter", "tree_sitter_pwsh")
)
requires_powershell_grammar = pytest.mark.skipif(
    not _GRAMMAR_PRESENT,
    reason=(
        "PowerShell grammar package not installed; C8 declares it in "
        "pyproject.toml."
    ),
)


def _payload(command):
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": "/repo",
    }


# `find . -type f | head -n 5` -- a two-stage `find | head` pipeline
# `check_head_tail_plumbing_rewrite` translates outright (find census +
# head slice, both recognized forms).
_HEAD_TAIL_CMD = "find . -type f | head -n 5"

# A genuine for-loop (FOR_LOOP is the shape-classifier's primary match)
# immediately followed by a literal top-level `find ... -exec rm {} \;`
# segment -- the narrow case `check_find_exec_rewrite`'s own segment scan
# recognizes and translates (rm is a translatable verb), confirmed against
# the real seam function rather than assumed: `_shape_classifier` matches
# FOR_LOOP on the leading `for ... do ... done` and `check_find_exec_rewrite`
# separately finds the trailing `find -exec` as its own top-level segment
# (segment scanning is command-wide, not scoped to the loop body).
_FOR_LOOP_FIND_EXEC_CMD = (
    'for i in 1 2 3; do echo $i; done; find . -name "*.tmp" -exec rm {} \\;'
)

# A bare glob for-loop -- FOR_LOOP-shaped per `_shape_classifier`, but no
# `find -exec` anywhere, so `check_find_exec_rewrite` returns `None`.
_FOR_LOOP_BARE_GLOB_CMD = 'for f in *.txt; do rm "$f"; done'

# `docker ps | head -n 20` -- genuinely HEAD_TAIL_PLUMBING-shaped (a
# two-segment `generator | head` pipeline), but `docker` is not one of
# `check_head_tail_plumbing_rewrite`'s recognized upstream generators
# (find/ls/grep), so that seam returns a BARE ADVISORY (no `updatedInput`)
# saying the rewrite is "not offered automatically" -- NOT a confirmed
# outlet. This is a common, entirely benign command that must never deny.
_HEAD_TAIL_UNRECOGNIZED_UPSTREAM_CMD = "docker ps | head -n 20"

# A three-segment pipeline into `tail` -- HEAD_TAIL_PLUMBING-shaped, but
# `check_head_tail_plumbing_rewrite`'s own two-segment-only shape means this
# gets a bare advisory ("longer chain than this rewrite... covers"), not a
# rewrite.
_HEAD_TAIL_LONG_CHAIN_CMD = "cat file.txt | tail -n +2 | sort | uniq -c"

# FOR_LOOP wrapping a literal `find -exec chmod ...` -- `chmod` is outside
# `check_find_exec_rewrite`'s translatable-verb set (rm/cat/wc -l), so that
# seam returns a bare advisory, not a rewrite.
_FOR_LOOP_FIND_EXEC_UNTRANSLATABLE_VERB_CMD = (
    'for i in 1 2 3; do echo $i; done; find . -name "*.log" -exec chmod 644 {} \\;'
)

# A while-read loop -- WHILE_READ_LOOP-shaped, no `find`/`for` anywhere.
_WHILE_READ_CMD = 'cat items.txt | while read x; do echo "$x"; done'


def _advisory_context(out):
    assert out is not None, "expected an allow_advisory envelope, got None"
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "permissionDecisionReason" not in hso
    return hso["additionalContext"]


class TestNonBashOrEmpty:
    def test_non_bash_tool_allows(self):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "x"}}
        assert guard.check(payload) is None

    def test_empty_command_allows(self):
        assert guard.check(_payload("")) is None

    def test_malformed_tool_input_allows(self):
        payload = {"tool_name": "Bash", "tool_input": "not-a-dict"}
        assert guard.check(payload) is None

    def test_plain_command_allows(self):
        assert guard.check(_payload("git status")) is None


class TestHeadTailPlumbing:
    def test_advises_even_with_windows_forced(self):
        # RETARGETED (DR-280, 2026-08-07): was `test_denies_on_windows`,
        # asserting a deny envelope under `host_is_windows=True`. This
        # guard's own deny branch is retired as structurally unreachable --
        # it gated on `_seam_confirmed_rewrite` against the SAME seam an
        # earlier-registered `ADVISORY_REWRITE` chain entry
        # (`head-tail-plumbing-rewrite`) already consumes and returns on
        # first, so through the real dispatcher the gate could never open.
        # Now asserts the guard advises (never denies) even with Windows
        # forced.
        #
        # `_pl_python3_invocation()` (aka `_bt_python3_invocation`)
        # deliberately resolves a REAL, runnable interpreter path rather
        # than emitting the literal string "python3" -- per its own
        # docstring, a bare `python3` is frequently absent on stock
        # Windows. Asserting the resolved invocation itself (matched
        # structurally, not a second hardcoded literal) keeps this pinned
        # to what the guard actually promises without going stale on the
        # next box -- see C2's identical fix (commit 39eedda26) for the
        # BX-16 rewrite fixtures.
        out = guard.check(_payload(_HEAD_TAIL_CMD), host_is_windows=True)
        ctx = _advisory_context(out)
        assert "head-tail-plumbing" in ctx
        assert guard._pl_python3_invocation() in ctx

    def test_advises_on_macos(self):
        out = guard.check(_payload(_HEAD_TAIL_CMD), host_is_windows=False)
        ctx = _advisory_context(out)
        assert "head-tail-plumbing" in ctx
        assert guard._pl_python3_invocation() in ctx

    def test_advisory_message_names_its_escape_hatch(self):
        # RETARGETED (DR-280, 2026-08-07): was
        # `test_deny_message_names_its_escape_hatch`, reading `_deny_reason`
        # under `host_is_windows=True`. This guard never denies any more --
        # read the advisory context instead, still under a forced Windows
        # host to confirm the escape hatch survives that leg too.
        #
        # RETARGETED AGAIN (2026-08-17, PM ruling on the override-key
        # message-register doctrine): a guard message names the guard that
        # fired and nothing else about its override -- no key, no assignment
        # form (docs/reference/guard-override-keys.md, opening sentence).
        # `operator_override_note` no longer interpolates `_OVERRIDE_ENV`
        # into the rendered text at all; the escape hatch is "named" via a
        # doc pointer, not the literal `COORDINATOR_*` key. Asserting the
        # bare key string was stale against that doctrine.
        out = guard.check(_payload(_HEAD_TAIL_CMD), host_is_windows=True)
        ctx = _advisory_context(out)
        assert OVERRIDE_KEYS_DOC in ctx


class TestSeamConfirmedOutletMessageShape:
    """Review: code-reviewer -- Finding 1 (C19b): pins the two message-
    accuracy claims for the seam-confirmed leg (`_outlet_from_seam_result`)
    that had no regression coverage -- a refactor of that function could
    silently reintroduce either defect with nothing to catch it.
    """

    def test_summary_is_self_contained_not_a_dangling_placeholder(self):
        # RETARGETED (DR-280, 2026-08-07): this guard's deny branch is
        # retired as structurally unreachable, so there is no deny template
        # left to read -- was asserting "Use instead: ..." (the DENY
        # template's own sentence) via `_deny_reason` under
        # `host_is_windows=True`. Now reads the advisory template instead
        # (which this guard renders on every host, forced or real), whose
        # own "consider %s here too" sentence carries the identical
        # regression risk the old "below." placeholder produced ("consider
        # below. here too", both misdescribing the outlet and dragging the
        # override note out of that sentence) -- see `_outlet_from_seam_
        # result`'s docstring for why `summary` must read sensibly standing
        # alone in EITHER template.
        out = guard.check(_payload(_HEAD_TAIL_CMD), host_is_windows=True)
        ctx = _advisory_context(out)
        assert "consider the seam-confirmed single-process rewrite here too" in ctx
        assert "below." not in ctx.split("consider")[1].split("\n")[0]

    def test_override_note_lands_in_example_cue_window_not_consider_sentence(self):
        # RETARGETED (DR-280, 2026-08-07): was `test_override_note_lands_
        # in_example_cue_window_not_use_instead_sentence`, reading
        # `_deny_reason` -- the deny template's "Use instead:" sentence no
        # longer renders (this guard never denies). Same regression check,
        # against the advisory template's "consider ... here too" sentence
        # instead.
        #
        # RETARGETED AGAIN (2026-08-17, override-key message-register
        # ruling): `operator_override_note` no longer interpolates the bare
        # `COORDINATOR_OVERRIDE_PLUMBING_AND_LOOPS` key -- it renders a doc
        # pointer only. The regression this test guards against (the note
        # drifting back into the "consider" sentence instead of trailing the
        # Example) still applies to the doc pointer.
        out = guard.check(_payload(_HEAD_TAIL_CMD), host_is_windows=True)
        ctx = _advisory_context(out)
        consider_line = next(
            line for line in ctx.splitlines() if "consider" in line
        )
        assert OVERRIDE_KEYS_DOC not in consider_line
        example_idx = ctx.index("Example:")
        override_idx = ctx.index(OVERRIDE_KEYS_DOC)
        assert override_idx > example_idx

    def test_advisory_summary_reads_sensibly_on_macos_too(self):
        out = guard.check(_payload(_HEAD_TAIL_CMD), host_is_windows=False)
        ctx = _advisory_context(out)
        assert "consider the seam-confirmed single-process rewrite here too" in ctx


class TestForLoopWrappingFindExec:
    """UPDATED (BX-12 audit): a for-loop followed by a top-level trailing
    `find -exec` is no longer denied on Windows. `check_find_exec_rewrite`'s
    fix (see this file's own docstring and `dispatch_checks.py`'s inline
    "LATENT-BUG FIX" comment) means this shape's seam call no longer returns
    a confirmed `updatedInput` for a multi-segment match, so
    `_seam_confirmed_rewrite` is `False` and this guard falls back to its
    own generic advisory on BOTH platforms -- the same outcome as the
    bare-glob for-loop case, and for the identical reason: no BX-16 seam
    confirms a full-command outlet, so no deny points at one.
    """

    def test_stays_advisory_on_windows_no_confirmed_outlet(self):
        out = guard.check(_payload(_FOR_LOOP_FIND_EXEC_CMD), host_is_windows=True)
        hso = out["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow"
        assert "permissionDecisionReason" not in hso
        ctx = hso["additionalContext"]
        assert "for-loop" in ctx
        assert "subprocess per iteration" in ctx

    def test_advises_on_macos_too(self):
        out = guard.check(_payload(_FOR_LOOP_FIND_EXEC_CMD), host_is_windows=False)
        ctx = _advisory_context(out)
        assert "for-loop" in ctx
        assert "subprocess per iteration" in ctx


class TestForLoopBareGlobStaysAdvisoryOnly:
    def test_advises_even_when_windows_is_forced(self):
        # No `find -exec` anywhere in this command -- `check_find_exec_rewrite`
        # returns None, so this guard must NEVER deny it, even under a forced
        # Windows host.
        out = guard.check(_payload(_FOR_LOOP_BARE_GLOB_CMD), host_is_windows=True)
        hso = out["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow"
        assert "permissionDecisionReason" not in hso

    def test_advisory_names_no_confirmed_outlet(self):
        out = guard.check(_payload(_FOR_LOOP_BARE_GLOB_CMD), host_is_windows=True)
        ctx = _advisory_context(out)
        assert "for-loop" in ctx
        assert "subprocess per iteration" in ctx

    def test_advises_on_macos_too(self):
        out = guard.check(_payload(_FOR_LOOP_BARE_GLOB_CMD), host_is_windows=False)
        assert out is not None
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_generic_example_stays_a_skeleton_not_a_fabricated_translation(self):
        """Worklist Row P4: the bare-glob for-loop fallback is
        architecturally capped at a generic skeleton, decided explicitly
        (see `_FOR_LOOP_GENERIC_SUMMARY`'s own comment block) rather than
        promoted -- this pins that the example still names itself as a
        template ("do the per-item work") and never claims a concrete,
        command-specific translation exists for the actual glob/body in
        front of it."""
        out = guard.check(_payload(_FOR_LOOP_BARE_GLOB_CMD), host_is_windows=True)
        ctx = _advisory_context(out)
        assert "glob.glob" in ctx
        assert "do the per-item work in-process" in ctx
        # The example itself is a bare `...` body -- it must not claim this
        # specific command's OWN body (`rm "$f"`) was translated into the
        # python3 example; only a generic enumeration skeleton is offered.
        example_line = next(line for line in ctx.splitlines() if "glob.glob" in line)
        assert 'rm "$f"' not in example_line
        assert "..." in example_line or "'..." in ctx


class TestWhileReadLoop:
    """New verdict arm (docs/plans/2026-08-10-the-one-fan-out-shape-the-
    classifier-nev.md § C2/C3/AC-4). No seam is consulted for this shape --
    always `_generic_advisory`, never a deny, on every platform."""

    def test_advisory_never_deny_on_windows(self):
        out = guard.check(_payload(_WHILE_READ_CMD), host_is_windows=True)
        hso = out["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow"
        assert "permissionDecisionReason" not in hso

    def test_advisory_never_deny_on_macos(self):
        out = guard.check(_payload(_WHILE_READ_CMD), host_is_windows=False)
        hso = out["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow"
        assert "permissionDecisionReason" not in hso

    def test_message_names_the_while_read_shape(self):
        # AC-5: the message must not misdescribe what tripped it.
        out = guard.check(_payload(_WHILE_READ_CMD), host_is_windows=True)
        ctx = _advisory_context(out)
        assert "while-read-loop" in ctx

    def test_advisory_names_its_escape_hatch(self):
        # RETARGETED (2026-08-17, override-key message-register ruling):
        # see `TestHeadTailPlumbing.test_advisory_message_names_its_escape_
        # hatch` above for the same fix on this guard's other shape -- the
        # escape hatch is named via a doc pointer, never the bare key.
        out = guard.check(_payload(_WHILE_READ_CMD), host_is_windows=True)
        ctx = _advisory_context(out)
        assert OVERRIDE_KEYS_DOC in ctx


class TestBareSeamAdvisoryNeverDenies:
    """Adversarial-review regression coverage: a seam check returning a
    bare `_advisory` (no `updatedInput` -- no confirmed rewrite) must be
    treated identically to a `None` return, never as a licensed deny
    target, on EITHER shape."""

    def test_head_tail_unrecognized_upstream_never_denies(self):
        out = guard.check(
            _payload(_HEAD_TAIL_UNRECOGNIZED_UPSTREAM_CMD), host_is_windows=True
        )
        hso = out["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow"
        assert "permissionDecisionReason" not in hso
        assert "head-tail-plumbing" in hso["additionalContext"]

    def test_head_tail_long_chain_never_denies(self):
        out = guard.check(_payload(_HEAD_TAIL_LONG_CHAIN_CMD), host_is_windows=True)
        hso = out["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow"
        assert "permissionDecisionReason" not in hso

    def test_for_loop_untranslatable_exec_verb_never_denies(self):
        out = guard.check(
            _payload(_FOR_LOOP_FIND_EXEC_UNTRANSLATABLE_VERB_CMD), host_is_windows=True
        )
        hso = out["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow"
        assert "permissionDecisionReason" not in hso
        assert "for-loop" in hso["additionalContext"]

    def test_head_tail_unrecognized_upstream_advises_on_macos_too(self):
        out = guard.check(
            _payload(_HEAD_TAIL_UNRECOGNIZED_UPSTREAM_CMD), host_is_windows=False
        )
        assert out is not None
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


class TestPrecedence:
    def test_grep_via_bash_precedence_stays_silent(self):
        # Simultaneously grep-via-Bash and head/tail-plumbing:
        # GREP_VIA_BASH outranks HEAD_TAIL_PLUMBING in SHAPE_PRECEDENCE, so
        # this guard must not fire (AC-7).
        cmd = "grep -rn TODO src/ | head -n 5"
        assert guard.check(_payload(cmd), host_is_windows=True) is None

    def test_multi_probe_banner_precedence_stays_silent_for_for_loop(self):
        # A banner-echoed command followed by several probes, immediately
        # followed by a for-loop -- MULTI_PROBE_BANNER outranks FOR_LOOP.
        cmd = (
            'echo "=== probes ==="; pwd; whoami; '
            'for f in *.txt; do rm "$f"; done'
        )
        assert guard.check(_payload(cmd), host_is_windows=True) is None

    def test_for_loop_precedence_fires_for_loop_arm_over_while_read(self):
        # A command that is both FOR_LOOP and WHILE_READ_LOOP shaped fires
        # this guard's FOR_LOOP arm (its own "for-loop" summary), never the
        # while-read arm -- FOR_LOOP outranks WHILE_READ_LOOP in
        # SHAPE_PRECEDENCE (AC-1/AC-7).
        cmd = (
            'for f in *.py; do wc -l "$f"; done; '
            "cat items.txt | while read x; do echo \"$x\"; done"
        )
        out = guard.check(_payload(cmd), host_is_windows=True)
        ctx = _advisory_context(out)
        assert "for-loop" in ctx
        assert "while-read-loop" not in ctx


class TestCrashPropagatesForFailClosed:
    """Review: code-reviewer -- Finding 3: this guard is registered in
    `dispatch.py`'s `guard_chain` with `fail_closed=True`, whose whole
    contract is that an internal bug reaches `dispatch._crash_deny` rather
    than being swallowed as a silent allow. Before this fix, `check()`
    wrapped its entire body in a catch-all that returned `None` on ANY
    exception -- defeating that registration. Pins that a crash inside
    `check()` now propagates all the way out (uncaught), the same as
    `guard_grep_via_bash.check`."""

    def test_classify_command_crash_propagates(self, monkeypatch):
        def _boom(cmd):
            raise RuntimeError("boom")

        monkeypatch.setattr(guard, "classify_command", _boom)
        try:
            guard.check(_payload(_HEAD_TAIL_CMD), host_is_windows=True)
        except RuntimeError as exc:
            assert str(exc) == "boom"
        else:
            raise AssertionError("expected the guard to propagate the crash")


class TestVerbatimHeadTailAlternativeIsRealAndEquivalent:
    """`_verbatim_head_tail_alternative` (M3, `docs/plans/2026-07-29-bash-
    guard-merged-execution-shape.md`) promotes the unrecognized-upstream-
    generator case from a bare "no confirmed outlet" advisory to a genuine
    runnable single-`python3 -c` replacement, by piping the upstream
    VERBATIM into an in-process slicer instead of trying to recognize what
    it is. Verified DIFFERENTIALLY here -- both the original pipeline and
    the emitted alternative are actually EXECUTED and their stdout diffed
    byte-for-byte, never asserted equivalent by inspection alone (the exact
    shortcut this workstream's own source plans call out as insufficient).
    """

    def _run(self, cmd):
        return subprocess.run(
            cmd, shell=True, capture_output=True, text=True, check=True
        ).stdout

    def _alternative_stdout(self, original_cmd):
        alt_cmd = guard._verbatim_head_tail_alternative(original_cmd)
        assert alt_cmd is not None, "expected a concrete alternative, got None"
        assert alt_cmd.count("|") == 0, (
            "the alternative must be a single interpreter invocation, not a "
            "shell pipeline that reintroduces a fork"
        )
        return self._run(alt_cmd)

    #: state/bash-guards/known-red.json group "guard-windows-branch-verdicts".
    #: `_verbatim_head_tail_alternative` builds a shell=True POSIX pipeline
    #: that fails on cmd.exe -- see
    #: state/audits/2026-08-07-guard-windows-branch-verdicts.md. Owner:
    #: docs/plans/2026-08-07-command-guards-fire-under-both-tool-names.md
    #: (its C4 body).
    @pytest.mark.pending_fix
    def test_unrecognized_generator_head(self, tmp_path):
        # `cat` is not one of `check_head_tail_plumbing_rewrite`'s recognized
        # upstream generators (find/ls/grep) -- the whole point of this
        # chunk is that the verbatim alternative does not need it to be.
        f = tmp_path / "lines.txt"
        f.write_text("a\nb\nc\nd\ne\n")
        cmd = "cat %s | head -n 3" % f
        assert self._run(cmd) == self._alternative_stdout(cmd)

    @pytest.mark.pending_fix
    def test_unrecognized_generator_tail(self, tmp_path):
        f = tmp_path / "lines2.txt"
        f.write_text("1\n2\n3\n4\n5\n6\n")
        cmd = "cat %s | tail -n 2" % f
        assert self._run(cmd) == self._alternative_stdout(cmd)

    @pytest.mark.pending_fix
    def test_quoting_hazard_apostrophe_in_filename(self, tmp_path):
        # A literal apostrophe in the filename -- the exact shape of
        # quoting hazard the seam's own `find`/`ls` census parsers had a
        # dedicated regression for (see `_bt_parse_ls_segment`'s docstring).
        # The upstream token here re-quotes via `shlex.quote`, not
        # string-splicing, so this must survive intact.
        f = tmp_path / "it's a file.txt"
        f.write_text("alpha\nbeta\ngamma\ndelta\n")
        cmd = 'cat "%s" | tail -n 2' % f
        assert self._run(cmd) == self._alternative_stdout(cmd)

    def test_no_alternative_for_long_chain(self):
        # A three-segment pipeline stays out of scope for this function too
        # (same conservative two-segment-only shape as the seam's own
        # check) -- must return None, not guess.
        assert (
            guard._verbatim_head_tail_alternative(_HEAD_TAIL_LONG_CHAIN_CMD)
            is None
        )

    def test_no_alternative_for_unparseable_count(self):
        # `head -c 100` (byte-count mode) is not one of
        # `_bt_head_tail_count`'s recognized line-count forms.
        assert (
            guard._verbatim_head_tail_alternative("cat file.txt | head -c 100")
            is None
        )


class TestOverrideEscapeHatch:
    def test_override_env_suppresses_head_tail_deny(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_PLUMBING_AND_LOOPS", "1")
        assert guard.check(_payload(_HEAD_TAIL_CMD), host_is_windows=True) is None

    def test_override_env_suppresses_for_loop_deny(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_PLUMBING_AND_LOOPS", "1")
        assert (
            guard.check(_payload(_FOR_LOOP_FIND_EXEC_CMD), host_is_windows=True)
            is None
        )

    def test_override_env_suppresses_bare_glob_advisory(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_PLUMBING_AND_LOOPS", "1")
        assert guard.check(_payload(_FOR_LOOP_BARE_GLOB_CMD)) is None


def _ps_payload(command):
    return {
        "tool_name": "PowerShell",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": None,
    }


class TestPowerShellDialect:
    """Row 14, docs/reference/guard-dialect-coverage.md: HEAD_TAIL_PLUMBING
    gets the same `Select-Object -First`/`-Last` fix as row 13's
    `check_head_tail_plumbing_rewrite`; FOR_LOOP has no PowerShell grammar
    analogue at all and must declare SILENT rather than assert clean."""

    def test_head_tail_plumbing_advises_on_powershell_even_with_windows_forced(self):
        # RETARGETED (DR-280, 2026-08-07): was `test_head_tail_plumbing_
        # rewrites_on_powershell`, asserting `permissionDecision == "deny"`
        # -- this guard's deny branch (including its PowerShell leg, which
        # shares `_verdict_head_tail`'s own `platform_verdict_for_shape`
        # call site) is retired as structurally unreachable. Now asserts an
        # advisory allow instead, even with Windows forced.
        out = guard.check(
            _ps_payload("ls . | Select-Object -First 5"),
            host_is_windows=True,
        )
        assert out is not None
        hso = out["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow"
        assert "permissionDecisionReason" not in hso

    def test_non_head_tail_powershell_command_fires_no_advisory(self):
        # A no-shape-matched PowerShell command must not fire an advisory --
        # that is this test's load-bearing assertion, and it is unchanged.
        #
        # It does, however, record SILENT rather than returning a bare
        # clean. C6 widened this guard's `MATCHERS` to include PowerShell,
        # which subjects it to the standing repo-wide contract in
        # `tests/test_no_false_clean_on_unparsed_dialect.py`: a guard
        # DECLARING PowerShell must back that declaration with measured
        # behaviour, and a bare `None` is indistinguishable from "this
        # guard was never invoked" -- precisely the confusion C6 exists to
        # end. That contract is owned by another workstream and is not this
        # plan's to weaken, so the recorded-silence side won.
        #
        # `record_silent` is inert outside `collecting()`, so nothing about
        # what an agent actually sees changed here.
        from coordinator_core.bash_guards._verdict import collecting, was_silent

        with collecting() as silences:
            result = guard.check(_ps_payload("Get-Process"))
        assert result is None
        assert was_silent("guard_plumbing_and_loops", silences)

    def test_empty_powershell_command_allows_no_silent(self):
        from coordinator_core.bash_guards._verdict import collecting, was_silent

        with collecting() as silences:
            result = guard.check(_ps_payload(""))
        assert result is None
        assert not was_silent("guard_plumbing_and_loops", silences)


class TestPowerShellForLoopAndPipelineForeachObject:
    """C3 (pln-the-shape-classifier-reaches-a-e743e5): row 14's SILENT
    ruling for FOR_LOOP is overturned by D2, and PIPELINE_FOREACH_OBJECT
    (new member, no bash analogue) gets its own generic advisory. Both route
    through `classify_command(cmd, dialect=Dialect.POWERSHELL)` -- the same
    call `_verdict_powershell` now makes for both shapes, no private
    classification path (AC11)."""

    @requires_powershell_grammar
    def test_powershell_for_loop_advises_not_silent(self):
        # Row-14 superseding note (D2): `foreach ($x in $y) { git log -1 $x }`
        # now classifies as a real FOR_LOOP match and gets the same generic
        # advisory the bash leg's bare-glob FOR_LOOP fallback renders.
        out = guard.check(
            _ps_payload("foreach ($f in $files) { git log -1 $f }"),
            host_is_windows=True,
        )
        assert out is not None
        hso = out["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow"
        ctx = hso["additionalContext"]
        assert "for-loop" in ctx
        # AC9: the alternative is a subprocess invocation (`python3 -c`),
        # never a bash-only construct -- no `xargs`, no `$(...)`, no
        # `for ... do ... done`.
        assert "python3" in ctx
        assert "xargs" not in ctx
        assert "do ... done" not in ctx and " do \n" not in ctx

    @requires_powershell_grammar
    def test_powershell_pipeline_foreach_object_advises(self):
        out = guard.check(
            _ps_payload("Get-ChildItem *.py | ForEach-Object { python3 lint.py $_.FullName }"),
            host_is_windows=True,
        )
        assert out is not None
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "pipeline-foreach-object" in ctx
        # AC9: PowerShell-valid alternative -- a `python3 -c` invocation,
        # never `xargs -P` or any other bash-only remediation.
        assert "python3" in ctx
        assert "xargs" not in ctx

    @requires_powershell_grammar
    def test_powershell_percent_alias_for_foreach_object_advises(self):
        out = guard.check(
            _ps_payload("Get-ChildItem -Recurse | % { git log -1 $_ }"),
            host_is_windows=False,
        )
        assert out is not None
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "pipeline-foreach-object" in ctx

    def test_no_private_shape_precedence_walk_remains(self):
        # AC11: `_verdict_powershell` must classify via
        # `_shape_classifier.classify_command` -- the module-level
        # `classify_command` name it calls is that same function, not a
        # locally re-derived SHAPE_PRECEDENCE walk.
        import inspect

        from coordinator_core.bash_guards._shape_classifier import (
            classify_command as _canonical_classify_command,
        )

        assert guard.classify_command is _canonical_classify_command
        source = inspect.getsource(guard)
        assert "SHAPE_PRECEDENCE" not in source
