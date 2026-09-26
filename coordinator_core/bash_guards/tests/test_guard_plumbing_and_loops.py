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
from coordinator_core.bash_guards._helpers import OVERRIDE_KEYS_DOC_DISPLAY as OVERRIDE_KEYS_DOC
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

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


_HEAD_TAIL_CMD = "find . -type f | head -n 5"

# A genuine for-loop (FOR_LOOP is the shape-classifier's primary match)
# FOR_LOOP on the leading `for ... do ... done` and `check_find_exec_rewrite`
_FOR_LOOP_FIND_EXEC_CMD = (
    'for i in 1 2 3; do echo $i; done; find . -name "*.tmp" -exec rm {} \\;'
)

# A bare glob for-loop -- FOR_LOOP-shaped per `_shape_classifier`, but no
_FOR_LOOP_BARE_GLOB_CMD = 'for f in *.txt; do rm "$f"; done'

# `docker ps | head -n 20` -- genuinely HEAD_TAIL_PLUMBING-shaped (a
# (find/ls/grep), so that seam returns a BARE ADVISORY (no `updatedInput`)
_HEAD_TAIL_UNRECOGNIZED_UPSTREAM_CMD = "docker ps | head -n 20"

# A three-segment pipeline into `tail` -- HEAD_TAIL_PLUMBING-shaped, but
_HEAD_TAIL_LONG_CHAIN_CMD = "cat file.txt | tail -n +2 | sort | uniq -c"

# FOR_LOOP wrapping a literal `find -exec chmod ...` -- `chmod` is outside
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
        # earlier-registered `ADVISORY_REWRITE` chain entry
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
        # RETARGETED AGAIN (2026-08-17, PM ruling on the override-key
        # `operator_override_note` no longer interpolates `_OVERRIDE_ENV`
        # doc pointer, not the literal `COORDINATOR_*` key. Asserting the
        out = guard.check(_payload(_HEAD_TAIL_CMD), host_is_windows=True)
        ctx = _advisory_context(out)
        assert OVERRIDE_KEYS_DOC in ctx


class TestSeamConfirmedOutletMessageShape:
    """Pins the two message-
    accuracy claims for the seam-confirmed leg (`_outlet_from_seam_result`)
    that had no regression coverage -- a refactor of that function could
    silently reintroduce either defect with nothing to catch it.
    """

    def test_summary_is_self_contained_not_a_dangling_placeholder(self):
        # RETARGETED (DR-280, 2026-08-07): this guard's deny branch is
        out = guard.check(_payload(_HEAD_TAIL_CMD), host_is_windows=True)
        ctx = _advisory_context(out)
        assert "consider the seam-confirmed single-process rewrite here too" in ctx
        assert "below." not in ctx.split("consider")[1].split("\n")[0]

    def test_override_note_lands_in_example_cue_window_not_consider_sentence(self):
        # RETARGETED (DR-280, 2026-08-07): was `test_override_note_lands_
        # RETARGETED AGAIN (2026-08-17, override-key message-register
        # `COORDINATOR_OVERRIDE_PLUMBING_AND_LOOPS` key -- it renders a doc
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
        example_line = next(line for line in ctx.splitlines() if "glob.glob" in line)
        assert 'rm "$f"' not in example_line
        assert "..." in example_line or "'..." in ctx

    def test_example_names_the_resolved_interpreter_not_a_bare_literal(self):
        out = guard.check(_payload(_FOR_LOOP_BARE_GLOB_CMD), host_is_windows=True)
        ctx = _advisory_context(out)
        assert guard._pl_python3_invocation() in ctx


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
        out = guard.check(_payload(_WHILE_READ_CMD), host_is_windows=True)
        ctx = _advisory_context(out)
        assert "while-read-loop" in ctx

    def test_advisory_names_its_escape_hatch(self):
        # RETARGETED (2026-08-17, override-key message-register ruling):
        out = guard.check(_payload(_WHILE_READ_CMD), host_is_windows=True)
        ctx = _advisory_context(out)
        assert OVERRIDE_KEYS_DOC in ctx

    def test_example_names_the_resolved_interpreter_not_a_bare_literal(self):
        out = guard.check(_payload(_WHILE_READ_CMD), host_is_windows=True)
        ctx = _advisory_context(out)
        assert guard._pl_python3_invocation() in ctx


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
        # GREP_VIA_BASH outranks HEAD_TAIL_PLUMBING in SHAPE_PRECEDENCE, so
        cmd = "grep -rn TODO src/ | head -n 5"
        assert guard.check(_payload(cmd), host_is_windows=True) is None

    def test_multi_probe_banner_precedence_stays_silent_for_for_loop(self):
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
    """This guard is registered in
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
            # cmd.exe intermediary that CREATE_NO_WINDOW does not suppress; the
            # STARTUPINFO route is a separate, wider fix (review: code-reviewer).
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

    @pytest.mark.pending_fix
    def test_unrecognized_generator_head(self, tmp_path):
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
        f = tmp_path / "it's a file.txt"
        f.write_text("alpha\nbeta\ngamma\ndelta\n")
        cmd = 'cat "%s" | tail -n 2' % f
        assert self._run(cmd) == self._alternative_stdout(cmd)

    def test_no_alternative_for_long_chain(self):
        assert (
            guard._verbatim_head_tail_alternative(_HEAD_TAIL_LONG_CHAIN_CMD)
            is None
        )

    def test_no_alternative_for_unparseable_count(self):
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
        out = guard.check(
            _ps_payload("ls . | Select-Object -First 5"),
            host_is_windows=True,
        )
        assert out is not None
        hso = out["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow"
        assert "permissionDecisionReason" not in hso

    def test_non_head_tail_powershell_command_fires_no_advisory(self):
        # clean. C6 widened this guard's `MATCHERS` to include PowerShell,
        # DECLARING PowerShell must back that declaration with measured
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
        assert "python3" in ctx
        assert "xargs" not in ctx
        assert guard._pl_python3_invocation() in ctx

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
        # locally re-derived SHAPE_PRECEDENCE walk.
        import inspect

        from coordinator_core.bash_guards._shape_classifier import (
            classify_command as _canonical_classify_command,
        )

        assert guard.classify_command is _canonical_classify_command
        source = inspect.getsource(guard)
        assert "SHAPE_PRECEDENCE" not in source


class TestBtPython3InvocationLeavesTheAdvisoryHotPath:
    """C2 (2026-08-21-guards-under-the-brightline): the interpreter
    resolution `_bt_python3_invocation` performs on every firing of this
    fleet's highest-firing advisory used to cost two `python.exe` spawns
    (`pyresolve._validate_interpreter`'s probe, plus a fresh
    `resolve_python_bin` walk every call). Half A retires the first spawn
    when the candidate IS `sys.executable`; Half B retires the second by
    memoizing the resolved invocation to an on-disk, cross-process cache."""

    @pytest.fixture(autouse=True)
    def _isolated_cache(self, tmp_path, monkeypatch):
        from coordinator_core.bash_guards import dispatch_checks as dc

        cache_file = tmp_path / "bt-python3-invocation-cache.json"
        monkeypatch.setattr(dc, "_bt_python3_invocation_cache_path", lambda: str(cache_file))
        self.dc = dc
        self.cache_file = cache_file

    def test_half_a_validate_interpreter_skips_the_spawn_for_self(self, monkeypatch):
        from coordinator_core import pyresolve as pr

        pr.clear_resolution_cache()
        calls = {"n": 0}
        orig_run = pr.subprocess.run

        def _counting_run(*args, **kwargs):
            calls["n"] += 1
            return orig_run(*args, **kwargs)

        monkeypatch.setattr(pr.subprocess, "run", _counting_run)
        assert pr._validate_interpreter(sys.executable) is True
        assert calls["n"] == 0

    def test_half_a_still_probes_a_different_path(self, monkeypatch):
        from coordinator_core import pyresolve as pr

        pr.clear_resolution_cache()
        calls = {"n": 0}
        orig_run = pr.subprocess.run

        def _counting_run(*args, **kwargs):
            calls["n"] += 1
            return orig_run(*args, **kwargs)

        monkeypatch.setattr(pr.subprocess, "run", _counting_run)
        pr._validate_interpreter("this-binary-does-not-exist-xyz")
        assert calls["n"] == 1

    def test_half_b_second_call_reads_the_cache_not_a_fresh_resolve(self, monkeypatch):
        first = self.dc._bt_python3_invocation()
        assert self.cache_file.is_file()

        from coordinator_core import pyresolve as pr

        def _boom(*args, **kwargs):
            raise AssertionError("resolve_python_bin should not be called on a cache hit")

        monkeypatch.setattr(pr, "resolve_python_bin", _boom)
        second = self.dc._bt_python3_invocation()

        assert second == first

    def test_half_b_cache_is_a_torn_or_missing_file_falls_open(self):
        self.cache_file.write_text("not valid json {{{", encoding="utf-8")
        result = self.dc._bt_python3_invocation()
        assert isinstance(result, str) and result

    def test_half_b_cache_key_changes_with_coordinator_python_env(self, monkeypatch):
        monkeypatch.delenv("COORDINATOR_PYTHON", raising=False)
        key_before = self.dc._bt_python3_invocation_cache_key()
        monkeypatch.setenv("COORDINATOR_PYTHON", "some-other-interpreter")
        key_after = self.dc._bt_python3_invocation_cache_key()
        if key_before is not None and key_after is not None:
            assert key_before != key_after

    def test_half_b_write_is_atomic_replace_not_truncate(self):
        import inspect

        source = inspect.getsource(self.dc._bt_python3_invocation)
        assert "os.replace(tmp_path, cache_path)" in source

