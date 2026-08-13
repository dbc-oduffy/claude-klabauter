"""Structural gate over ``_alternative_liveness``: every guard's registered
trigger must FIRE (a broken row fails loud), every alternative it names must
resolve to a non-``DEAD`` verdict, and the guard registry itself must track
the package's real guard surface so a newly-added guard cannot silently
escape this gate.

This is a MEASUREMENT test, not a policy test -- it never asserts what a
guard denies/allows/rewrites (that is `test_deny_message_accuracy.py`,
each guard's own dedicated test file, and `_guard_coverage.py`'s job). It
asserts that whatever a guard's message NAMES as the way out is real.

BAND ATTRIBUTION (2026-07-29 PM ruling): guard ownership is now split
end-to-end, by guard -- this session owns the advisory/rewrite guards
(`guard_grep_via_bash`/`guard_multiprobe_banner`/`guard_plumbing_and_loops`
plus the BX-16 rewrite/advisory family + `check_offer_git_c` in
`dispatch_checks.py`); a peer session owns chain structure and the
confinement (hard-deny) guards (every `block_*` module and every other
`check_*` function in `dispatch_checks.py`). This gate is deliberately
module-agnostic and evaluates BOTH bands without editing either -- it is
the artifact that carries the duty-of-care obligation across that boundary,
so band tagging (`_alternative_liveness.classify_band`) exists ONLY so a
finding can be handed to the right session without further investigation,
never to narrow what gets checked. See ``test_report_output_is_band_
attributable`` below for the acceptance form of that requirement.

Spec backlink: coordinator_core/bash_guards/_alternative_liveness.py
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import pytest

from coordinator_core.bash_guards import _alternative_liveness as altlive
from coordinator_core.bash_guards import guard_grep_via_bash
from coordinator_core.bash_guards import guard_inprocess_search


# ---------------------------------------------------------------------------
# Registry integrity -- the enumerated guard count is compared against
# introspection of the package, so a guard added tomorrow without a trigger
# (LIVE_TRIGGERS) or a documented reason (UNTRIGGERED) is a loud failure.
# ---------------------------------------------------------------------------


def test_every_dispatch_check_is_registered():
    discovered = set(altlive.discover_dispatch_check_names())
    registered = set(altlive.LIVE_TRIGGERS) | set(altlive.UNTRIGGERED)
    missing = discovered - registered
    assert not missing, (
        "dispatch_checks.py check_* function(s) added without a "
        "LIVE_TRIGGERS entry or a named UNTRIGGERED reason: %s" % sorted(missing)
    )


def test_every_module_guard_is_registered():
    discovered = set(altlive.discover_module_guard_names())
    registered = set(altlive.LIVE_TRIGGERS) | set(altlive.UNTRIGGERED)
    missing = discovered - registered
    assert not missing, (
        "a block_*/guard_*/check_* MODULE with a top-level check() was added "
        "without a LIVE_TRIGGERS entry or a named UNTRIGGERED reason: %s" % sorted(missing)
    )


def test_untriggered_set_is_documented_not_silently_empty():
    for name, reason in altlive.UNTRIGGERED.items():
        assert isinstance(reason, str) and len(reason) > 20, (
            "%s is in UNTRIGGERED with no real written reason" % name
        )


EXPECTED_UNTRIGGERED = frozenset(
    {
        "check_destructive_git_orphan",
        "check_blanket_git_add",
        "check_probe_spray",
        "check_validate_commit",
        "check_test_suite_invocation",
    }
)


def test_untriggered_set_matches_expected_no_silent_growth():
    """A newly-UNVERIFIABLE/untriggered guard must surface here, not hide.
    Shrinking this set (a follow-up session closes one) requires editing
    this constant -- that's the point: it cannot happen silently."""
    assert set(altlive.UNTRIGGERED) == EXPECTED_UNTRIGGERED


#: the Director of Engineering's review (finding 5, "UNTRIGGERED is an un-ratcheted suppression
#: list"): assert the pinned CEILING form explicitly, not only the exact-
#: match above -- `<=` is the literal ratchet the review asked for (growth
#: fails loud; shrinking a follow-up session's closed entries never needs
#: this line touched, unlike the exact-match test above which is stricter
#: and catches BOTH directions). Lower this pin by hand as entries close;
#: never raise it to make a new suppression quiet.
_UNTRIGGERED_PINNED_MAX = 6


def test_untriggered_count_never_grows_past_pin():
    assert len(altlive.UNTRIGGERED) <= _UNTRIGGERED_PINNED_MAX, (
        "UNTRIGGERED grew past its pinned ceiling (%d) -- a guard silently "
        "gained a suppression-list escape hatch; lower the pin only after "
        "actually closing entries, never raise it to make growth quiet"
        % _UNTRIGGERED_PINNED_MAX
    )


# ---------------------------------------------------------------------------
# write_guards/ discovery extension (chunk C9, agent-facing-messages-not-
# apology plan). SEPARATE registries (WRITE_GUARD_LIVE_TRIGGERS/WRITE_GUARD_
# UNTRIGGERED, not LIVE_TRIGGERS/UNTRIGGERED) so this extension never
# touches the bash_guards-scoped EXPECTED_UNTRIGGERED/_UNTRIGGERED_PINNED_MAX
# ratchets above -- per this dispatch's explicit "never relax an existing
# pin" instruction.
# ---------------------------------------------------------------------------


def test_every_write_guard_is_registered_or_documented():
    discovered = set(altlive.discover_write_guard_names())
    registered = set(altlive.WRITE_GUARD_LIVE_TRIGGERS) | set(altlive.WRITE_GUARD_UNTRIGGERED)
    missing = discovered - registered
    assert not missing, (
        "a write_guards/ module was added without a WRITE_GUARD_LIVE_TRIGGERS "
        "entry or a named WRITE_GUARD_UNTRIGGERED reason: %s" % sorted(missing)
    )


def test_write_guard_untriggered_set_is_documented_not_silently_empty():
    for name, reason in altlive.WRITE_GUARD_UNTRIGGERED.items():
        assert isinstance(reason, str) and len(reason) > 20, (
            "%s is in WRITE_GUARD_UNTRIGGERED with no real written reason" % name
        )


def test_write_guard_live_triggers_registry_is_non_empty():
    """The two parametrized tests below collect zero cases -- and silently
    report a pass -- if `WRITE_GUARD_LIVE_TRIGGERS` were ever emptied. Same
    dark-gate shape `guard_message_register_lint`'s own gate guards against
    with `test_gate_fails_loud_if_collection_drops_to_zero`: a green run and
    a RUNNING run must not be distinguishable only by incident."""
    assert altlive.WRITE_GUARD_LIVE_TRIGGERS, (
        "WRITE_GUARD_LIVE_TRIGGERS is empty -- the parametrized write_guards "
        "liveness tests below would collect zero cases and pass vacuously"
    )


@pytest.mark.parametrize("guard", sorted(altlive.WRITE_GUARD_LIVE_TRIGGERS))
def test_write_guard_registered_trigger_fires(guard):
    result = altlive.fire_guard(guard)
    assert result.fired, (
        "%s's registered write_guards trigger did not fire (error=%r) -- broken "
        "registry row, not a silent skip" % (guard, result.error)
    )


@pytest.mark.parametrize("guard", sorted(altlive.WRITE_GUARD_LIVE_TRIGGERS))
def test_write_guard_named_alternatives_are_not_dead(guard):
    """The write_guards/ analogue of `test_named_alternatives_are_not_dead`
    below -- a DEAD verdict here means a write_guards/ alternative was
    damaged by this chunk's own sweep (fix the message, not the gate, per
    this module's own charter)."""
    ev = altlive.evaluate_guard(guard)
    if not ev.fire.fired:
        pytest.skip("covered by test_write_guard_registered_trigger_fires; not re-asserted here")
    if ev.extraction_error:
        pytest.fail(
            "%s: message names an alternative this extractor cannot classify: %s"
            % (guard, ev.extraction_error)
        )
    dead = [(alt, v) for alt, v in ev.verdicts if v.status is altlive.VerdictStatus.DEAD]
    assert not dead, "\n".join(
        "%s: DEAD alternative [%s] %r -- %s" % (guard, alt.kind.value, alt.raw, v.evidence)
        for alt, v in dead
    )


# ---------------------------------------------------------------------------
# Every registered LIVE_TRIGGERS row actually fires. A trigger returning
# None (or raising) is a broken registry row, not a silent skip.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("guard", sorted(altlive.LIVE_TRIGGERS))
def test_registered_trigger_fires(guard):
    result = altlive.fire_guard(guard)
    assert result.fired, (
        "%s's registered trigger did not fire (error=%r) -- broken registry row, "
        "not a real coverage gap" % (guard, result.error)
    )


def _guard_message_text(hso: Dict[str, Any]) -> str:
    """Read a guard envelope's rendered message from whichever field
    carries it -- a deny's ``permissionDecisionReason`` or a rewrite/
    advisory's ``additionalContext`` (see `_alternative_liveness.
    extract_alternatives`'s own identical fallback). Guards this gate
    exercises legitimately render either shape, so tests that need the
    prose (not the envelope's decision field itself) go through this
    rather than hard-coding one field name."""
    return hso.get("permissionDecisionReason") or hso.get("additionalContext") or ""


def test_fire_guard_isolates_from_a_pre_existing_session_latch(monkeypatch):
    """``fire_guard`` must observe a guaranteed-first call for
    ``guard_inprocess_search``'s session latch, never whatever latch state
    a prior run (real or synthetic) left on disk for the AMBIENT session
    id -- the exact gotcha ``_isolated_session_scope`` exists to close (see
    that function's own docstring). Pins the fixed session id's marker
    directly rather than going through ``fire_guard`` to seed it, then
    asserts the isolated re-fire still renders the FULL explanatory
    paragraph and leaves the pre-seeded marker untouched."""
    fixed_sid = "altlive-isolation-fixture-session"
    real_cwd = os.getcwd()
    marker_path = guard_inprocess_search._latch_path(real_cwd, fixed_sid)
    assert marker_path is not None, "could not resolve a latch path against the real repo -- test cannot proceed"

    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.touch(exist_ok=True)
    try:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", fixed_sid)

        result = altlive.fire_guard("guard_inprocess_search")

        assert result.fired, "guard_inprocess_search's trigger did not fire: %r" % result.error
        reason = _guard_message_text(result.envelope.get("hookSpecificOutput", {}))
        # Guard's actual wording is "recognized as a search" -- this phrase
        # opens `_footer`'s full-paragraph branch and is absent from
        # `_ANSWERED_MARKER`, so it is a reliable full-vs-short discriminator.
        assert "recognized as a search" in reason, (
            "fire_guard observed the pre-seeded latch for the ambient session id instead of "
            "a guaranteed-first call -- isolation did not hold: %r" % reason[:200]
        )
        assert guard_inprocess_search._ANSWERED_MARKER not in reason, (
            "fire_guard rendered the already-answered short marker, not the full paragraph -- "
            "the fixed session id's pre-seeded latch leaked into the probe"
        )
        assert marker_path.is_file(), (
            "the pre-seeded marker for the fixed session id was removed or altered -- fire_guard "
            "must never touch state belonging to a session id it did not mint itself"
        )
    finally:
        import shutil

        shutil.rmtree(marker_path.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# The gate itself: every alternative named by a fired guard must be LIVE or
# an explicitly-counted UNVERIFIABLE, never DEAD.
# ---------------------------------------------------------------------------

#: Guards whose alternative(s) are known to include a NAMED override that is
#: verified LIVE against the guard's own source (probe_override), plus any
#: HARNESS_CAPABILITY alternative expected to land UNVERIFIABLE (no on-disk
#: roster-truth for that specific claim). Tracked explicitly so a NEW
#: UNVERIFIABLE verdict surfaces as a diff here rather than hiding inside a
#: passing test.
EXPECTED_UNVERIFIABLE_COUNTS: Dict[str, int] = {
    "check_sed_range_read_advise": 2,  # "the Read tool" + Read(...) -- both harness capability, no structured oracle
    "check_cat_heredoc_write_advise": 1,  # "the Write tool" -- same reason
    # H11's narrowed `guard_grep_via_bash` (2026-07-30, docs/plans/2026-07-30-
    # os-aware-guard-advisory-defaults.md) names "dispatching an Explore or
    # general-purpose subagent" as its fallback -- a real, dispatchable
    # harness capability, but no `harness_capability_manifest.json` entry
    # exists for this guard+marker pairing yet (same UNVERIFIABLE-not-DEAD
    # shape as the sed/cat rows above -- an unproven claim, not a false one).
    "guard_grep_via_bash": 1,  # "Explore or general-purpose subagent" -- harness capability, no structured oracle (C8, 2026-08-02: message-size trim dropped the redundant "dispatching an Explore" second mention)
    # check_head_tail_plumbing_rewrite's row was REMOVED (2026-07-29, false-claim
    # sweep): it counted an UNVERIFIABLE "in-process" harness-capability marker
    # emitted by this guard's own messages -- a claim the NOTE below already called
    # "likely FALSE". It was false, and the messages no longer make it: the rewrite
    # is a python3 -c subprocess (one fork replacing the pipeline's two, not zero),
    # so there is no capability marker left to grade and the expected count is 0,
    # i.e. this key is absent rather than set to 0. This is the UNVERIFIABLE ceiling
    # SHRINKING, which this module's own docstring names as the only honest
    # direction of travel. If a marker reappears here, a false capability claim has
    # been reintroduced into the message text -- fix the message, don't re-add a row.
    # guard_inprocess_search's own "in-process" + "searched in-process" markers moved from
    # UNVERIFIABLE to LIVE (S0, 2026-07-29): the new harness_capability_manifest.json carries
    # a real, human-reviewed entry for `guard_inprocess_search::in-process` /
    # `guard_inprocess_search::searched in-process` (its own genuine zero-fork capability),
    # so no UNVERIFIABLE count is expected here anymore -- see EXPECTED_LIVE_FLOORS below.
    # NOTE: check_head_tail_plumbing_rewrite's OWN "in-process" marker (row above) stays
    # UNVERIFIABLE on purpose -- it describes a DIFFERENT, unverified (and per the roster-
    # truth audit, likely FALSE) claim about its python3-subprocess rewrite, and the manifest
    # is keyed `<guard>::<marker>` specifically so one guard's true claim can never make an
    # unrelated guard's same-worded claim grade LIVE by prose coincidence.
    # `python3 <claude-klabauter-root>/coordinator/bin/coordinator-doc-new ...` --
    # the `<claude-klabauter-root>` placeholder makes this a template, not a directly
    # invocable path, AND it is a `python3 <script.py>` invocation (no -c/
    # -m flag), a form `_probe_python_dash_c` does not attempt to run --
    # honestly UNVERIFIABLE rather than guessed at in either direction.
    "block_reviewer_bash_outside_allowlist": 1,
    # `block_approval_sentinel_creation`'s REASON_DIRECT message (2026-08-03
    # dead-alternative fix) names `grep` bare and 9 real, resolvable
    # `git <query-subcommand>` forms (`status`/`diff`/`log`/`show`/
    # `ls-files`/`rev-parse`/`describe`/`check-ignore`/`check-attr`) -- none
    # are mutating, so each is EXECUTED for real in `probe_command`'s
    # throwaway (non-git) tmp dir and fails with "fatal: not a git
    # repository" / grep's no-pattern usage error: a real command, run
    # outside the one context (an actual repo/stdin) it needs to succeed in
    # -- genuinely ambiguous, not dead, per this module's own UNVERIFIABLE
    # contract.
    "block_approval_sentinel_creation": 10,
}

#: the Director of Engineering's review (finding 6, "UNVERIFIABLE is an ungated sink"): pin a
#: per-guard LIVE-count FLOOR alongside the UNVERIFIABLE ceiling above, so
#: a verdict silently degrading LIVE -> UNVERIFIABLE (three structural
#: funnels named in the review: probe_flag with no cli_name -- now fixed,
#: see EXPECTED_UNVERIFIABLE_COUNTS' block_reviewer_bash_outside_allowlist
#: comment for the one legitimate template-path exception; probe_harness_
#: capability's unconditional UNVERIFIABLE; probe_command's ambiguous-exit
#: fallback) is a loud test failure, never invisible. Together the two
#: pinned baselines make the three-valued verdict honest: LIVE can only
#: hold steady or grow, UNVERIFIABLE can only hold steady or shrink, DEAD
#: stays asserted at zero by `test_named_alternatives_are_not_dead` itself.
EXPECTED_LIVE_FLOORS: Dict[str, int] = {
    "block_illegal_filename": 2,
    "block_subagent_plan_body_bash_write": 1,
    "check_cat_heredoc_write_advise": 1,
    "check_destructive_git_clean": 1,
    "check_find_exec_rewrite": 2,
    "check_git_commit_safe_commit_advise": 1,
    "check_grep_via_bash_rewrite": 2,
    "check_head_tail_plumbing_rewrite": 2,
    "check_multiprobe_banner_rewrite": 2,
    "check_no_verify": 1,
    "check_offer_git_c": 2,
    "check_raw_pid_liveness": 2,
    "check_sed_range_read_advise": 1,
    "guard_grep_via_bash": 1,
    # 1 (pre-existing, unrelated to the capability manifest) + 2 ("in-process" +
    # "searched in-process" -- both now resolve LIVE against harness_capability_manifest.json,
    # see EXPECTED_UNVERIFIABLE_COUNTS' comment above).
    # Re-pinned 3 -> 2 on 2026-08-12, EM ruling. NOT a ratchet relaxation: the
    # third alternative stopped existing rather than stopping verifying. This
    # floor was pinned at 3 in a49cd99e8 (2026-07-29), when `_footer()` still
    # appended `operator_override_note(_DISABLE_ENV_VAR)`, giving one OVERRIDE
    # alternative alongside the two HARNESS_CAPABILITY markers. 18b73e5bc
    # (2026-08-11) deliberately removed that call -- a successfully ANSWERED
    # search is not a denial and has no bypass to offer, and the offer was
    # itself the trust defect (five dispatched executors distrusted correct
    # in-process results because of it). `_source_override_alternatives` finds
    # OVERRIDE alternatives by AST-walking for `operator_override_note` calls,
    # so it now correctly returns []; the two capability markers both still
    # grade LIVE. `_DISABLE_ENV_VAR` still works, it is just documented in
    # docs/reference/guard-override-keys.md instead of announced inline.
    "guard_inprocess_search": 2,
    "guard_multiprobe_banner": 1,
    "guard_plumbing_and_loops": 1,
    # docs/plans/2026-08-01-branch-creation-seam-guards.md, chunk C2.
    "guard_branch_set_precedence": 1,
    # docs/plans/2026-08-01-branch-creation-seam-guards.md, chunk C7 (this
    # C2-divergence closeout): both C1's deny and C7's advisory now render
    # a CONCRETE canonical-branch example (`git checkout work/<real
    # machine>/<real date>`, resolved via `coordinator_core.machine_
    # resolver.compute_machine` + `daily_day.local_day`, never a
    # `work/<machine>/<date>` template) -- a real, invocable `git checkout
    # <branch>` command that classifies LIVE via `probe_command`'s
    # mutating-git-subcommand `--help` leg. This is the ratchet moving
    # from EXPECTED_UNVERIFIABLE_COUNTS (removed above) to here, exactly
    # as the C2 comment this replaces anticipated.
    "block_noncanonical_branch_creation": 1,
    "guard_longlived_branch_naming": 1,
}


@pytest.mark.parametrize("guard", sorted(altlive.LIVE_TRIGGERS))
def test_named_alternatives_are_not_dead(guard):
    ev = altlive.evaluate_guard(guard)
    if not ev.fire.fired:
        pytest.skip("covered by test_registered_trigger_fires; not re-asserted here")
    if ev.extraction_error:
        pytest.fail("%s: message names an alternative this extractor cannot classify: %s" % (guard, ev.extraction_error))

    dead = [(alt, v) for alt, v in ev.verdicts if v.status is altlive.VerdictStatus.DEAD]
    assert not dead, "\n".join(
        "%s: DEAD alternative [%s] %r -- %s" % (guard, alt.kind.value, alt.raw, v.evidence)
        for alt, v in dead
    )

    unverifiable = [v for _alt, v in ev.verdicts if v.status is altlive.VerdictStatus.UNVERIFIABLE]
    expected = EXPECTED_UNVERIFIABLE_COUNTS.get(guard, 0)
    assert len(unverifiable) == expected, (
        "%s: expected %d UNVERIFIABLE alternative(s), got %d (%s) -- update "
        "EXPECTED_UNVERIFIABLE_COUNTS if this is a real, reviewed change"
        % (guard, expected, len(unverifiable), [v.evidence[:120] for v in unverifiable])
    )

    live_count = sum(1 for _alt, v in ev.verdicts if v.status is altlive.VerdictStatus.LIVE)
    live_floor = EXPECTED_LIVE_FLOORS.get(guard, 0)
    assert live_count >= live_floor, (
        "%s: LIVE count DROPPED below its pinned floor (%d -> %d) -- an "
        "alternative that used to verify now silently does not; this is "
        "the exact 'LIVE -> UNVERIFIABLE degrades invisibly' failure mode "
        "the ratchet exists to catch" % (guard, live_floor, live_count)
    )


# ---------------------------------------------------------------------------
# Meta-test -- the acceptance argument. A gate not proven to fail on a dead
# alternative and pass on a live one is not a gate.
# ---------------------------------------------------------------------------


def _fake_deny(reason: str) -> Dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


class TestMetaGateProvenToFail:
    def test_dead_binary_alternative_is_caught(self):
        hso = _fake_deny(
            "BLOCKED: fake finding.\n\nUse instead: `this-binary-does-not-exist-anywhere --fake-flag`\n"
        )["hookSpecificOutput"]
        alts = altlive.extract_alternatives(hso)
        assert alts, "extractor found no alternative in a message with an explicit backtick command"
        verdicts = [altlive.probe_alternative(a) for a in alts]
        assert any(v.status is altlive.VerdictStatus.DEAD for v in verdicts), (
            "a nonexistent-binary alternative was not classified DEAD: %s" % verdicts
        )

    def test_dead_flag_on_a_real_cli_is_caught(self):
        alt = altlive.Alternative(
            altlive.AlternativeKind.FLAG,
            "--this-flag-does-not-exist-anywhere",
            ("git", "--this-flag-does-not-exist-anywhere"),
        )
        verdict = altlive.probe_flag(alt)
        assert verdict.status is altlive.VerdictStatus.DEAD

    def test_dead_override_not_read_by_guard_is_caught(self):
        # check_raw_pid_liveness's real trigger genuinely never reads this
        # made-up var, so the behavioral re-fire must reproduce an
        # IDENTICAL verdict -- DEAD.
        alt = altlive.Alternative(
            altlive.AlternativeKind.OVERRIDE, "COORDINATOR_ALLOW_TOTALLY_MADE_UP", "COORDINATOR_ALLOW_TOTALLY_MADE_UP"
        )
        baseline = altlive.fire_guard("check_raw_pid_liveness")
        assert baseline.fired
        verdict = altlive.probe_override(alt, "check_raw_pid_liveness", baseline)
        assert verdict.status is altlive.VerdictStatus.DEAD

    def test_live_command_alternative_passes(self):
        hso = _fake_deny("BLOCKED: fake finding.\n\nUse instead: `git --version`\n")["hookSpecificOutput"]
        alts = altlive.extract_alternatives(hso)
        assert alts
        verdicts = [altlive.probe_alternative(a) for a in alts]
        assert all(v.status is altlive.VerdictStatus.LIVE for v in verdicts), verdicts

    def test_live_flag_on_a_real_cli_passes(self):
        alt = altlive.Alternative(altlive.AlternativeKind.FLAG, "--help", ("git", "--help"))
        verdict = altlive.probe_flag(alt)
        assert verdict.status is altlive.VerdictStatus.LIVE

    def test_live_override_read_by_guard_passes(self):
        # check_raw_pid_liveness's real trigger DOES read this var (it
        # stands down and returns None when it is set) -- the behavioral
        # re-fire must observe the verdict change.
        alt = altlive.Alternative(
            altlive.AlternativeKind.OVERRIDE, "COORDINATOR_OVERRIDE_RAW_PID_LIVENESS", "COORDINATOR_OVERRIDE_RAW_PID_LIVENESS"
        )
        baseline = altlive.fire_guard("check_raw_pid_liveness")
        assert baseline.fired
        verdict = altlive.probe_override(alt, "check_raw_pid_liveness", baseline)
        assert verdict.status is altlive.VerdictStatus.LIVE


class TestCapabilityManifestOracle:
    """S0 acceptance (docs/plans/2026-07-29-bash-guard-consolidated-execution.md
    row S0): the structured harness-capability manifest that
    `probe_harness_capability` consumes must report REAL capability state
    for this package's actual guards -- not merely make a fixture test
    pass -- and must never re-derive the T8 defect (a guard's message
    graded LIVE by phrase co-occurrence with an unrelated capability).
    """

    def test_guard_inprocess_search_real_fired_message_grades_live(self):
        """guard_inprocess_search's OWN registered trigger, fired for
        real (not a constructed fixture), must have its "in-process"/
        "searched in-process" markers resolve LIVE against the committed
        manifest -- proof the oracle answers from real guard output, not
        a stand-in."""
        ev = altlive.evaluate_guard("guard_inprocess_search")
        assert ev.fire.fired
        capability_verdicts = [v for alt, v in ev.verdicts if alt.kind is altlive.AlternativeKind.HARNESS_CAPABILITY]
        assert capability_verdicts, "expected guard_inprocess_search's real message to name its in-process marker(s)"
        assert all(v.status is altlive.VerdictStatus.LIVE for v in capability_verdicts), capability_verdicts

    def test_guard_grep_via_bash_composed_advisory_names_the_same_real_capability(self):
        """guard_grep_via_bash's composed-advisory path, narrowed 2026-07-30
        (worklist row H11(c)): a plain `&&`-chained grep with no partial
        rewrite and no genuine GNU-only construct is now SILENT (no
        actionable alternative -- design-as-offers), so that shape no
        longer names any capability at all. The surviving population --
        a genuine GNU-only construct (`-P`/`-z`/a long option other than
        `--include`/`--exclude`) with no partial rewrite -- still fires.

        2026-07-30 (a9fce05a, same-day follow-up to H11(b)/(c)): the
        sentence this test used to pin an "in-process" marker against was
        cut from the message entirely. It never described a capability the
        reader could take -- it explained that a DIFFERENT, already-run
        answerer had declined this exact invocation and could not be
        re-targeted or reordered, which is exactly the unusable-by-the-
        reader prose the message-trim exists to remove. Its removal is not
        a claim silently downgrading LIVE -> UNVERIFIABLE (the shape this
        gate exists to catch) -- there is no claim left to grade at all,
        which is the module docstring's named-legitimate direction of
        travel ("the UNVERIFIABLE ceiling may only shrink"), one step
        further: to zero. This test now pins the negative so the sentence
        cannot creep back into the reader's context (mirroring
        test_guard_grep_via_bash.py's own
        `test_message_does_not_explain_why_the_in_process_answerer_declined`),
        and asserts the composed-advisory message's remaining
        HARNESS_CAPABILITY alternative (the subagent-dispatch fallback)
        is graded consistently with EXPECTED_UNVERIFIABLE_COUNTS above --
        no manifest entry exists for that claim, so it must land
        UNVERIFIABLE, never silently LIVE by prose coincidence with
        guard_inprocess_search's own genuine "in-process" claim (the T8
        shape this module's other tests below plant deliberately)."""
        silent_payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "grep -rn foo . && echo done"},
        }
        assert guard_grep_via_bash.check(silent_payload, host_is_windows=False) is None

        payload = {"tool_name": "Bash", "tool_input": {"command": "grep -Pn foo . && echo done"}}
        envelope = guard_grep_via_bash.check(payload, host_is_windows=False)
        assert envelope is not None
        hso = envelope["hookSpecificOutput"]
        assert "registered ahead of this guard" not in hso["additionalContext"]
        alts = altlive.extract_alternatives(hso)
        capability_alts = [a for a in alts if a.kind is altlive.AlternativeKind.HARNESS_CAPABILITY]
        in_process_alts = [a for a in capability_alts if a.detail == "in-process"]
        assert not in_process_alts, (
            "guard_grep_via_bash's composed advisory should no longer name an "
            "'in-process' marker at all (cut 2026-07-30, a9fce05a) -- if one "
            "reappears here, the cut explanatory sentence has crept back in: "
            "%s" % in_process_alts
        )
        assert capability_alts, "expected the subagent-dispatch fallback to still extract as a HARNESS_CAPABILITY alternative"
        verdicts = [altlive.probe_harness_capability(a, guard="guard_grep_via_bash") for a in capability_alts]
        assert all(v.status is altlive.VerdictStatus.UNVERIFIABLE for v in verdicts), verdicts

    def test_planted_t8_shape_defect_is_not_corroborated_by_prose_coincidence(self):
        """Plant the T8 shape: a message using the SAME marker word
        ("in-process") that a DIFFERENT guard's genuinely-live capability
        also uses, fired under a guard name (`check_head_tail_plumbing_
        rewrite`) that carries NO manifest entry for that marker --
        mirroring that guard's own real, unverified "in-process, zero
        extra forks" claim about what is actually a `python3 -c`
        subprocess (see harness_capability_manifest.json's own comment;
        roster-truth audit: coordinator-claude state/audits/2026-07-29-search-
        capability-roster-truth.md). The REAL, guard-scoped oracle must
        refuse to grade this LIVE just because `guard_inprocess_search::
        in-process` happens to be a true entry."""
        hso = _fake_deny(
            "BASH-SPAWN ADVISORY: a single python3 -c collecting the same "
            "lines and slicing head/tail in-process avoids the extra fork."
        )["hookSpecificOutput"]
        alts = altlive.extract_alternatives(hso)
        capability_alts = [a for a in alts if a.kind is altlive.AlternativeKind.HARNESS_CAPABILITY]
        assert capability_alts, "expected the planted message to extract an 'in-process' HARNESS_CAPABILITY alternative"
        assert capability_alts[0].detail == "in-process"

        real_verdicts = [
            altlive.probe_harness_capability(a, guard="check_head_tail_plumbing_rewrite") for a in capability_alts
        ]
        assert all(v.status is altlive.VerdictStatus.UNVERIFIABLE for v in real_verdicts), (
            "the real, guard-scoped oracle incorrectly corroborated a false capability "
            "claim by bare-marker prose coincidence: %s" % real_verdicts
        )

        # Proof this is a genuine discriminating test, not a fixture that
        # would pass regardless of implementation: reconstruct the NAIVE
        # oracle -- keyed by the bare marker alone, ignoring which guard
        # fired, i.e. exactly the literal `capability_id -> {available}`
        # shape the filed ask proposed before this row's own guard-scoping
        # decision -- from the SAME real fact this manifest records
        # (guard_inprocess_search's capability genuinely exists), and show
        # it grades the SAME planted defect LIVE. That is the exact T8
        # inversion, reconstructed to prove this test would have caught it
        # had `probe_harness_capability` been keyed that way -- and that
        # the real, guard-scoped implementation this row actually shipped
        # does not commit it.
        naive_manifest = {"in-process": {"available": True, "note": "guard_inprocess_search exists"}}

        def _naive_lookup(_guard: str, detail: str) -> str:
            entry = naive_manifest.get(detail)
            if entry is None:
                return altlive.VerdictStatus.UNVERIFIABLE.value
            return altlive.VerdictStatus.LIVE.value if entry.get("available") is True else altlive.VerdictStatus.DEAD.value

        naive_verdict = _naive_lookup("check_head_tail_plumbing_rewrite", capability_alts[0].detail)
        assert naive_verdict == altlive.VerdictStatus.LIVE.value, (
            "expected the reconstructed naive (bare-marker, guard-blind) oracle to "
            "grade the planted T8-shape defect LIVE -- if it does not, this test no "
            "longer demonstrates a real defect the real oracle's guard-scoping "
            "closes, and is exercising a fixture rather than the actual risk"
        )
        assert real_verdicts[0].status is not altlive.VerdictStatus.LIVE, (
            "the real oracle must diverge from the naive one on this exact planted "
            "defect -- both landing on the same verdict means this test is not "
            "actually discriminating"
        )


class _FakeNTOS:
    """Forwards every attribute to the REAL ``os`` module except ``name``
    (pinned to ``"nt"``) and ``access``. Swapped in only as ``_alternative_
    liveness.os`` for the duration of one test -- never as the process-wide
    ``os`` module (patching the real ``os.name`` globally corrupts
    ``pathlib``'s own Windows/POSIX dispatch mid-run and takes pytest's own
    reporting down with it, observed directly while writing this test).

    ``access`` is overridden, not merely forwarded: real macOS/Linux
    ``os.access(path, os.X_OK)`` genuinely checks the executable bit, so a
    plain ``write_text`` fixture file (no chmod) correctly reports
    non-executable there -- the defect this test exists to pin ONLY shows up
    on real Windows, where ``os.access(path, os.X_OK)`` is "meaningless-to-
    lying" and returns True for any readable file regardless of the (POSIX-
    only) executable bit. This override reproduces that specific lie so the
    test can discriminate the fix on a POSIX dev machine -- confirmed by
    temporarily reverting ``_resolve_on_path_or_settings_home`` to its
    pre-fix literal and observing this test fail before restoring it."""

    name = "nt"

    def access(self, path, mode):
        if mode == os.X_OK:
            return os.path.isfile(path)  # the Windows lie: readable == "executable"
        return os.access(path, mode)

    def __getattr__(self, attr):
        return getattr(os, attr)


class TestResolveOnPathOrSettingsHomeWindowsGuard:
    """C11-fix: ``_resolve_on_path_or_settings_home``'s bare-file ``os.access(
    ..., os.X_OK)`` check must never fire on Windows -- that call is
    "meaningless-to-lying" there (True for ANY readable file, regardless of
    real executability), so a bare-named non-``.cmd`` file sitting at the
    settings-home candidate path must NOT be returned ahead of its ``.cmd``
    twin. This test is written to FAIL against the pre-fix literal (bare
    ``os.path.isfile(candidate) and os.access(candidate, os.X_OK)`` with no
    Windows guard, running before the ``.cmd`` fallback) -- confirmed by
    reverting the fix locally, observing this test fail, then restoring it.
    """

    def test_windows_prefers_cmd_twin_over_lying_bare_file_access(self, tmp_path, monkeypatch):
        settings_home = tmp_path / "settings-home"
        bin_dir = settings_home / "bin"
        bin_dir.mkdir(parents=True)
        bare = bin_dir / "coordinator-safe-commit"
        bare.write_text("not actually executable on windows\n", encoding="utf-8")
        cmd_twin = bin_dir / "coordinator-safe-commit.cmd"
        cmd_twin.write_text("@echo off\n", encoding="utf-8")

        monkeypatch.setattr(altlive.shutil, "which", lambda name: None)
        monkeypatch.setattr(altlive, "os", _FakeNTOS())
        monkeypatch.setattr(
            os.environ, "get", lambda key, default=None: str(settings_home) if key == "COORDINATOR_SETTINGS_HOME" else default
        )

        resolved = altlive._resolve_on_path_or_settings_home("coordinator-safe-commit")
        assert resolved == str(cmd_twin), (
            "on Windows, the bare (non-.cmd) file must never be returned via the "
            "lying os.access(X_OK) check -- expected the .cmd twin, got %r" % resolved
        )

    def test_windows_with_no_cmd_twin_is_unresolved_not_the_lying_bare_file(self, tmp_path, monkeypatch):
        settings_home = tmp_path / "settings-home"
        bin_dir = settings_home / "bin"
        bin_dir.mkdir(parents=True)
        bare = bin_dir / "coordinator-safe-commit"
        bare.write_text("not actually executable on windows\n", encoding="utf-8")

        monkeypatch.setattr(altlive.shutil, "which", lambda name: None)
        monkeypatch.setattr(altlive, "os", _FakeNTOS())
        monkeypatch.setattr(
            os.environ, "get", lambda key, default=None: str(settings_home) if key == "COORDINATOR_SETTINGS_HOME" else default
        )

        resolved = altlive._resolve_on_path_or_settings_home("coordinator-safe-commit")
        assert resolved is None, (
            "on Windows with no .cmd twin present, the bare file must not be "
            "returned via the lying os.access(X_OK) check: got %r" % resolved
        )


class TestExtractionEdgeCases:
    def test_pure_policy_message_with_no_alternative_is_empty_not_a_failure(self):
        hso = _fake_deny("BLOCKED: this is banned. No exceptions.")["hookSpecificOutput"]
        assert altlive.extract_alternatives(hso) == []

    def test_cue_phrase_with_no_recognizable_signal_raises(self):
        hso = _fake_deny("BLOCKED: this is banned. Use instead: something we describe only in prose with no code span at all.")[
            "hookSpecificOutput"
        ]
        with pytest.raises(altlive.UnclassifiableAlternative):
            altlive.extract_alternatives(hso)

    def test_updated_input_command_is_always_extracted_as_a_command_alternative(self):
        hso = {
            "hookSpecificOutput": {
                "permissionDecision": "allow",
                "additionalContext": "Auto-rewritten.",
                "updatedInput": {"command": "git -C /tmp status"},
            }
        }["hookSpecificOutput"]
        alts = altlive.extract_alternatives(hso)
        assert any(a.kind is altlive.AlternativeKind.COMMAND and a.detail == ["git", "-C", "/tmp", "status"] for a in alts)


# ---------------------------------------------------------------------------
# Band attribution -- every registered guard must be mechanically
# classifiable to exactly one band, so a finding hands over without further
# investigation. Not a policy test (it asserts NOTHING about which band is
# "right" for a guard beyond the PM ruling's own explicit membership lists).
# ---------------------------------------------------------------------------

EXPECTED_OURS = frozenset(
    {
        "guard_grep_via_bash",
        "guard_multiprobe_banner",
        "guard_plumbing_and_loops",
        "check_find_exec_rewrite",
        "check_grep_via_bash_rewrite",
        "check_sed_range_read_advise",
        "check_cat_heredoc_write_advise",
        "check_git_commit_safe_commit_advise",
        "check_multiprobe_banner_rewrite",
        "check_head_tail_plumbing_rewrite",
        "check_offer_git_c",
    }
)


def test_every_registered_guard_has_exactly_one_band():
    for name in sorted(set(altlive.LIVE_TRIGGERS) | set(altlive.UNTRIGGERED)):
        band = altlive.classify_band(name)
        assert band in (altlive.GuardBand.OURS, altlive.GuardBand.PEER)


def test_ours_band_matches_the_pm_ruling_no_silent_drift():
    """The PM ruling names an EXACT set as this session's own -- everything
    else (every block_* module, every other dispatch_checks.py hard-deny
    check_*, plus the two module-shaped confinement guards check_raw_pid_
    liveness/check_test_suite_invocation and guard_inprocess_search, none
    of which the ruling names) defaults to PEER. A guard silently moving
    bands is exactly the drift band-tagging exists to prevent."""
    all_guards = set(altlive.LIVE_TRIGGERS) | set(altlive.UNTRIGGERED)
    ours = {g for g in all_guards if altlive.classify_band(g) is altlive.GuardBand.OURS}
    assert ours == EXPECTED_OURS
    peer = all_guards - ours
    assert "block_subagent_destructive_action" in peer
    assert "check_raw_pid_liveness" in peer
    assert "guard_inprocess_search" in peer


def test_report_output_is_band_attributable():
    """The acceptance form of the brief's requirement: someone with none of
    this session's context must be able to read the report and know who
    owes which fix -- every per-guard line carries an explicit band tag,
    and a per-band breakdown section is present."""
    evaluations = altlive.evaluate_all(["guard_grep_via_bash", "block_worktree_creation"])
    report = altlive.format_report(evaluations)
    assert "[ours] guard_grep_via_bash" in report
    assert "[peer] block_worktree_creation" in report
    assert "--- per-band breakdown ---" in report
    assert "ours: " in report
    assert "peer: " in report
