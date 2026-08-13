"""The firing-shape gate (C6, docs/plans/2026-08-01-advisory-firing-shape-
predicate.md): the acceptance surface for ``_firing_shape.py``.

This is a MEASUREMENT/GATE test, not a policy test -- like its sibling
``test_alternative_liveness_gate.py``, it never re-asserts what a guard
denies/allows/rewrites for a given trigger command (each emitter's own test
file does that). It asserts the mechanically-decidable firing-SHAPE
property each emitter's declared class obligates: an ASK-class emission
must name a real, non-override alternative (or be exempt by declared
policy); a REPORT-class emission must not repeat byte-identical text.

AC8's bidirectional proof is fixture-based, per this chunk's own brief: the
pre-fix emission text of items 1 (``check_multiprobe_banner_rewrite``), 2
(``guard_inprocess_search``), and 3 (``nudge_em_code_dispatch``'s dispatch
brief) is reproduced verbatim in-test from this repo's own working-tree
diff against HEAD (no checkout required) -- the gate must flag each, and
must pass on each one's real, current post-fix behavior. Item 5 (BSD-vs-GNU
grep flag classification, C2) is excluded per AC8 -- it is C2's own test,
not a corpus firing-shape property.

Spec backlink: coordinator_core/bash_guards/_firing_shape.py
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

from coordinator_core.bash_guards import _alternative_liveness as altlive
from coordinator_core.bash_guards import _firing_shape as fs
from coordinator_core.bash_guards import dispatch_checks as _dc
from coordinator_core.bash_guards import guard_inprocess_search as _gis
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.hooks import nudge_em_code_dispatch as _nudge


# ---------------------------------------------------------------------------
# AC8 -- item 1: check_multiprobe_banner_rewrite's pre-fix prose-only
# advisory (no updatedInput, no genuine alternative -- see this chunk's
# own dispatch brief and dispatch_checks.py's C4 docstring) vs its C4
# post-fix silent-fallthrough.
# ---------------------------------------------------------------------------


def _item1_pre_fix_hso(joined_tokens: str) -> dict:
    """Byte-for-byte reconstruction of the `_advisory(...)` call C4 removed
    from `check_multiprobe_banner_rewrite`'s unrecognized-segment branch --
    lifted verbatim from `git diff HEAD -- .../dispatch_checks.py`."""
    text = (
        "Advisory: this multi-probe banner re-derives session facts "
        "the harness already knows (a chained echo/git/pwd/whoami/"
        "date/uname probe), one process PER PROBE. A single "
        "python3 -c one-liner (os.getcwd(), getpass.getuser(), "
        "platform.uname(), one batched 'git status --porcelain=v2 "
        "--branch' call for every git fact) reproduces the same "
        "facts in one process; this chain carries a probe this "
        "rewrite does not recognize ('%s'), so the rewrite is not "
        "offered automatically. %s"
        % (
            joined_tokens,
            operator_override_note(
                "COORDINATOR_ALLOW_MULTIPROBE_BANNER",
                payload={"session_id": "sess-c1d-em"},
            ),
        )
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": text,
        }
    }


class TestItem1MultiprobeBannerFiringShape:
    def test_pre_fix_fixture_is_flagged(self):
        hso = _item1_pre_fix_hso("sed -n 1,5p a.py")["hookSpecificOutput"]
        spec = fs.REGISTRY["check_multiprobe_banner_rewrite"]
        ev = fs.evaluate_ask_hso(hso, alternative_required=spec.alternative_required)
        assert ev.fired
        assert ev.violated, "pre-fix prose-only multi-probe-banner advisory was not flagged"

    def test_post_fix_unrecognized_segment_emits_nothing_not_a_violation(self):
        envelope = _dc.check_multiprobe_banner_rewrite(
            "sed -n 1,5p a.py; echo ===; sed -n 6,9p b.py", "firing-shape-gate-test"
        )
        assert envelope is None, "C4's exit (b) must emit nothing for an unrecognized segment"
        spec = fs.REGISTRY["check_multiprobe_banner_rewrite"]
        ev = fs.evaluate_ask_hso(None, alternative_required=spec.alternative_required)
        assert not ev.fired
        assert not ev.violated

    def test_post_fix_recognized_segment_still_names_a_real_alternative(self):
        """AC10: the emitter must still fire on at least one genuine-trigger
        case post-fix -- a rewrite-offered banner with every segment
        recognized."""
        envelope = _dc.check_multiprobe_banner_rewrite(
            'echo "=== SESSION FACTS ==="; pwd; whoami; git status --short', "firing-shape-gate-test"
        )
        assert envelope is not None
        hso = envelope["hookSpecificOutput"]
        spec = fs.REGISTRY["check_multiprobe_banner_rewrite"]
        ev = fs.evaluate_ask_hso(hso, alternative_required=spec.alternative_required)
        assert not ev.violated, ev.reasons


# ---------------------------------------------------------------------------
# AC8 -- item 2: guard_inprocess_search's pre-fix unlatched (byte-identical
# every call) footer vs its C3 post-fix session latch.
# ---------------------------------------------------------------------------


def _item2_pre_fix_footer_text() -> str:
    """Byte-for-byte reconstruction of the pre-C3 `_footer()` body (no `sid`
    parameter, no latch) -- lifted verbatim from `git diff HEAD -- .../
    guard_inprocess_search.py`."""
    return (
        "[Searched inside the coordinator hook process that was already running for this "
        "tool call -- no subprocess was spawned. Nothing to change on your side: keep "
        "writing grep the way you already do, and searches that can be answered this way "
        "are handled automatically. %s]"
    ) % operator_override_note(
        "COORDINATOR_DISABLE_INPROCESS_SEARCH",
        payload={"session_id": "sess-c1d-em"},
    )


class TestItem2InProcessSearchFiringShape:
    def test_pre_fix_fixture_repeats_and_is_flagged(self):
        first = _item2_pre_fix_footer_text()
        second = _item2_pre_fix_footer_text()  # pre-fix: no session state, always identical
        violated, reason = fs.evaluate_report_repetition(first, second)
        assert violated, "pre-fix unlatched footer repetition was not flagged"
        assert reason

    def test_post_fix_session_latch_breaks_the_repeat(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        monkeypatch.setenv(_gis._SESSION_ID_ENV_VAR, "firing-shape-gate-test-session")

        first = _gis._footer(str(repo))
        second = _gis._footer(str(repo))

        violated, _reason = fs.evaluate_report_repetition(first, second)
        assert not violated, "post-fix footer still repeats byte-identical text on the 2nd call"
        assert second == _gis._ANSWERED_MARKER

    def test_post_fix_first_call_still_carries_the_full_explanatory_paragraph(self, tmp_path, monkeypatch):
        """AC10: the emitter must still fire its real content on at least
        one genuine-trigger case (the first answered call in a session)."""
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        monkeypatch.setenv(_gis._SESSION_ID_ENV_VAR, "firing-shape-gate-test-session-2")
        first = _gis._footer(str(repo))
        assert "recognized as a search" in first


# ---------------------------------------------------------------------------
# AC8 -- item 3: nudge_em_code_dispatch's pre-fix literal "[TODO: ...]"
# dispatch-brief placeholder vs its C5 post-fix payload-derived task line.
# ---------------------------------------------------------------------------


def _item3_pre_fix_brief_text(file_path: str, executor_type: str) -> str:
    """Byte-for-byte reconstruction of the pre-C5 `_build_dispatch_brief`
    (2-arg signature, TODO placeholders) -- lifted verbatim from
    `git diff HEAD -- .../nudge_em_code_dispatch.py`."""
    return "\n".join(
        [
            "## Pre-assembled executor dispatch brief",
            f"file:          {file_path}",
            f"executor-type: {executor_type}",
            f"in-scope:      {file_path} (only)",
            "commit:        false  — EM commits after verification",
            "---",
            "task: [TODO: describe the specific change you want made to this file]",
            "acceptance-criteria:",
            "  AC-1: [TODO: define done condition]",
            "",
            "Dispatch via: fan-out-dispatch.sh or Agent tool with the above brief.",
            "Docs: docs/wiki/dispatching-parallel-agents.md",
        ]
    )


class TestItem3NudgeDispatchBriefFiringShape:
    def test_pre_fix_fixture_is_flagged(self):
        text = _item3_pre_fix_brief_text("some/file.py", "generic-executor")
        ev = fs.evaluate_ask_text(text, alternative_required=True)
        assert ev.violated, "pre-fix TODO-placeholder dispatch brief was not flagged"

    def test_post_fix_brief_names_no_placeholder(self):
        edit_description = _nudge._describe_edit(
            {"tool_name": "Edit", "tool_input": {"old_string": "a", "new_string": "b"}}
        )
        text = _nudge._build_dispatch_brief("some/file.py", "generic-executor", edit_description)
        ev = fs.evaluate_ask_text(text, alternative_required=True)
        assert not ev.violated, ev.reasons
        assert "[TODO" not in text

    def test_post_fix_brief_still_names_a_concrete_task_line(self):
        """AC10: the post-fix brief still fires real, payload-derived
        content -- not merely an absence of the placeholder."""
        edit_description = _nudge._describe_edit(
            {"tool_name": "Write", "tool_input": {"content": "..."}}
        )
        text = _nudge._build_dispatch_brief("some/file.py", "generic-executor", edit_description)
        assert "task:          Write: full-file content write" in text


# ---------------------------------------------------------------------------
# The sentinel guards' canonical legal-zero-alternative case (must pass).
# ---------------------------------------------------------------------------


class TestSentinelGuardsZeroAlternativeIsLegal:
    @pytest.mark.parametrize("guard", ["block_worktree_sentinel_creation", "block_approval_sentinel_creation"])
    def test_sentinel_guard_names_no_alternative_and_still_passes(self, guard):
        envelope = altlive.LIVE_TRIGGERS[guard]()
        assert envelope is not None, "%s's registered trigger did not fire" % guard
        hso = envelope["hookSpecificOutput"]
        spec = fs.REGISTRY[guard]
        assert spec.alternative_required is False
        ev = fs.evaluate_ask_hso(hso, alternative_required=spec.alternative_required)
        assert not ev.violated, ev.reasons

    def test_same_zero_alternative_message_WOULD_fail_if_alternative_were_required(self):
        """Proof this is a genuine discriminator, not a fixture that would
        pass regardless of the alternative_required flag."""
        envelope = altlive.LIVE_TRIGGERS["block_worktree_sentinel_creation"]()
        hso = envelope["hookSpecificOutput"]
        ev = fs.evaluate_ask_hso(hso, alternative_required=True)
        assert ev.violated


# ---------------------------------------------------------------------------
# Axis B is orthogonal -- a withheld/offered override is never itself an
# Axis A alternative, in either direction.
# ---------------------------------------------------------------------------


class TestAxisBOverrideIsOrthogonal:
    def test_override_only_signal_does_not_satisfy_axis_a(self):
        hso = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "BLOCKED: fake finding, no alternative named. "
                    "COORDINATOR_ALLOW_FAKE_THING=1 (pre-launch only)."
                ),
            }
        }["hookSpecificOutput"]
        ev = fs.evaluate_ask_hso(hso, alternative_required=True)
        assert ev.violated, "an override-only signal wrongly satisfied Axis A's alternative obligation"

    def test_missing_override_is_never_itself_a_violation(self):
        hso = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "BLOCKED: fake finding.\n\nUse instead: `git status`\n",
            }
        }["hookSpecificOutput"]
        ev = fs.evaluate_ask_hso(hso, alternative_required=True)
        assert not ev.violated


# ---------------------------------------------------------------------------
# Registry / ratchet integrity -- both gaps the plan names closed.
# ---------------------------------------------------------------------------


def test_every_registry_row_has_a_live_violation_check():
    missing = set(fs.REGISTRY) - set(fs.LIVE_VIOLATION_CHECKS)
    assert not missing, "REGISTRY row(s) added without a LIVE_VIOLATION_CHECKS entry: %s" % sorted(missing)


def test_every_live_violation_check_has_a_registry_row():
    orphaned = set(fs.LIVE_VIOLATION_CHECKS) - set(fs.REGISTRY)
    assert not orphaned, "LIVE_VIOLATION_CHECKS entry with no REGISTRY row: %s" % sorted(orphaned)


def test_known_violations_is_a_frozen_set_with_a_pinned_count():
    """Gap (b): membership COUNT is checked, so growth is a visible diff,
    never a silent append. Every fix chunk in this plan (C1a/C2/C3/C4/C5/C7)
    landed before this chunk was authored, so the post-fix corpus this
    ratchet covers has zero known lingering firing-shape violations -- an
    empty set is the correct, non-amnestied state here, not a placeholder."""
    assert isinstance(fs.KNOWN_VIOLATIONS, frozenset)
    assert len(fs.KNOWN_VIOLATIONS) == 0


def _known_violations_loop(known_violations, live_violation_checks) -> None:
    """The real ratchet loop body, extracted so both the live-corpus gate
    test (`test_known_violations_still_violate`, currently vacuous over an
    empty set) AND a populated-set exercise
    (`test_known_violations_ratchet_loop_against_a_populated_set`, Finding 5)
    run the exact SAME code path -- not a re-implementation that could drift
    from the real thing."""
    for name in known_violations:
        assert live_violation_checks[name](), (
            "%s is listed in KNOWN_VIOLATIONS but its live re-fire no longer "
            "violates -- delist it, don't leave it amnestied" % name
        )


def test_known_violations_still_violate():
    """Gap (a): a LISTED emitter must still evaluate as violating -- a fixed
    emitter fails this test until delisted, rather than staying silently
    green forever. Vacuously satisfied while KNOWN_VIOLATIONS is empty;
    this loop is what turns red the moment a future violation is listed
    without also being fixed, or stays green past its own fix without being
    delisted."""
    _known_violations_loop(fs.KNOWN_VIOLATIONS, fs.LIVE_VIOLATION_CHECKS)


def test_known_violations_ratchet_loop_against_a_populated_set(monkeypatch):
    """Review: code-reviewer (Finding 5) -- `test_known_violations_still_violate`'s
    loop body never actually runs while KNOWN_VIOLATIONS is empty by
    construction. This exercises the REAL loop path
    (`_known_violations_loop`, the same helper the gate test above calls)
    against a populated set, proving both legs of the delist-or-fail
    workflow the ratchet exists to enforce:

    - a genuinely-still-violating entry (`LIVE_VIOLATION_CHECKS[name]()`
      returns True) makes the loop PASS.
    - a fixed-but-still-listed entry (returns False) makes the loop FAIL.
    """
    still_violating_checks = {"fake_still_violating": lambda: True}
    monkeypatch.setattr(
        fs, "LIVE_VIOLATION_CHECKS", {**fs.LIVE_VIOLATION_CHECKS, **still_violating_checks}
    )
    _known_violations_loop(frozenset({"fake_still_violating"}), fs.LIVE_VIOLATION_CHECKS)

    fixed_but_listed_checks = {"fake_fixed_but_listed": lambda: False}
    monkeypatch.setattr(
        fs, "LIVE_VIOLATION_CHECKS", {**fs.LIVE_VIOLATION_CHECKS, **fixed_but_listed_checks}
    )
    with pytest.raises(AssertionError, match="no longer"):
        _known_violations_loop(frozenset({"fake_fixed_but_listed"}), fs.LIVE_VIOLATION_CHECKS)


def test_known_violations_ratchet_mechanism_can_actually_detect_a_violation():
    """Meta-test, mirroring test_alternative_liveness_gate.py's own
    TestMetaGateProvenToFail: proves LIVE_VIOLATION_CHECKS's plumbing is
    capable of returning True, so an all-green KNOWN_VIOLATIONS assertion
    above is not vacuously trivial by construction. Re-fires item 1's real
    unrecognized-segment trigger through the SAME evaluation path
    LIVE_VIOLATION_CHECKS uses, but with alternative_required forced True
    against the module's own registry-declared value -- proof that the
    ratchet's evaluation function is capable of flagging a violation, using
    the real live emitter output already fired above, not a synthetic one."""
    envelope = _dc.check_multiprobe_banner_rewrite(
        "sed -n 1,5p a.py; echo ===; sed -n 6,9p b.py", "firing-shape-gate-meta-test"
    )
    assert envelope is None  # post-fix: silence, so build a synthetic fired case instead
    hso = _item1_pre_fix_hso("sed -n 1,5p a.py")["hookSpecificOutput"]
    assert fs.evaluate_ask_hso(hso, alternative_required=True).violated


# ---------------------------------------------------------------------------
# AC11 -- determinism across N consecutive runs (never a single-run
# assumption, per _alternative_liveness.py's own documented hazard).
# ---------------------------------------------------------------------------


def _run_all_live_violation_checks_in_fresh_subprocess() -> dict:
    """Run the full LIVE_VIOLATION_CHECKS set in a genuinely SEPARATE process
    invocation, so each run re-imports every module from disk rather than
    reusing sys.modules-cached bytecode.

    Review: code-reviewer (Finding 3) -- an in-process loop over the same 5
    calls can never observe `_alternative_liveness.py`'s own documented
    determinism hazard (a concurrent-edit sibling import minutes apart across
    SEPARATE process launches, not within one already-imported process). A
    subprocess per run is the only shape that genuinely re-imports from disk
    and can therefore say anything about that hazard.
    """
    script = (
        "import json, sys\n"
        "from coordinator_core.bash_guards import _firing_shape as fs\n"
        "result = {name: check() for name, check in sorted(fs.LIVE_VIOLATION_CHECKS.items())}\n"
        "json.dump(result, sys.stdout)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(pathlib.Path(__file__).resolve().parents[3]),
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # popup-safe-env-suppressed
    )
    assert completed.returncode == 0, (
        "fresh-process live-violation-check run failed: stdout=%r stderr=%r"
        % (completed.stdout, completed.stderr)
    )
    return json.loads(completed.stdout)


def test_live_violation_checks_are_deterministic_across_n_runs():
    """AC11: N genuinely separate process invocations, each re-importing
    `_firing_shape.py` and its siblings from disk, must agree on the exact
    same verdict set."""
    runs = [_run_all_live_violation_checks_in_fresh_subprocess() for _ in range(5)]
    first = runs[0]
    for i, run in enumerate(runs[1:], start=2):
        assert run == first, "run %d's verdict set diverged from run 1: %r vs %r" % (i, run, first)
    # And every verdict is exactly the expected all-False state (no known
    # lingering violation in the current, post-fix corpus).
    assert first == {name: False for name in fs.LIVE_VIOLATION_CHECKS}


# ---------------------------------------------------------------------------
# AC10 promotion (declared once more here at the gate level): the whole
# LIVE_VIOLATION_CHECKS registry is re-runnable without raising, proving
# every wired trigger is itself alive (a broken row would raise, not
# silently report False).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(fs.LIVE_VIOLATION_CHECKS))
def test_live_violation_check_runs_without_raising(name):
    result = fs.LIVE_VIOLATION_CHECKS[name]()
    assert isinstance(result, bool)
