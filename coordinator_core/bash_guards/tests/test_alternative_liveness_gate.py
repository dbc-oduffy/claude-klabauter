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


# (LIVE_TRIGGERS) or a documented reason (UNTRIGGERED) is a loud failure.


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
_UNTRIGGERED_PINNED_MAX = 5


def test_untriggered_count_never_grows_past_pin():
    assert len(altlive.UNTRIGGERED) <= _UNTRIGGERED_PINNED_MAX, (
        "UNTRIGGERED grew past its pinned ceiling (%d) -- a guard silently "
        "gained a suppression-list escape hatch; lower the pin only after "
        "actually closing entries, never raise it to make growth quiet"
        % _UNTRIGGERED_PINNED_MAX
    )


# apology plan). SEPARATE registries (WRITE_GUARD_LIVE_TRIGGERS/WRITE_GUARD_
# UNTRIGGERED, not LIVE_TRIGGERS/UNTRIGGERED) so this extension never
# touches the bash_guards-scoped EXPECTED_UNTRIGGERED/_UNTRIGGERED_PINNED_MAX


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


# Every registered LIVE_TRIGGERS row actually fires. A trigger returning


@pytest.mark.parametrize("guard", sorted(altlive.LIVE_TRIGGERS))
def test_registered_trigger_fires(guard):
    result = altlive.fire_guard(guard)
    assert result.fired, (
        "%s's registered trigger did not fire (error=%r) -- broken registry row, "
        "not a real coverage gap" % (guard, result.error)
    )


def _guard_message_text(hso: Dict[str, Any]) -> str:
    return hso.get("permissionDecisionReason") or hso.get("additionalContext") or ""


def test_fire_guard_isolates_from_a_pre_existing_session_latch(monkeypatch, tmp_path):
    fixed_sid = "altlive-isolation-fixture-session"
    monkeypatch.setattr(
        guard_inprocess_search,
        "_latch_path",
        lambda _cwd, sid: tmp_path / sid / guard_inprocess_search._LATCH_MARKER_NAME,
    )
    marker_path = guard_inprocess_search._latch_path(os.getcwd(), fixed_sid)
    marker_path.parent.mkdir(parents=True)
    marker_path.touch()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", fixed_sid)

    result = altlive.fire_guard("guard_inprocess_search")

    assert result.fired, "guard_inprocess_search's trigger did not fire: %r" % result.error
    reason = _guard_message_text(result.envelope.get("hookSpecificOutput", {}))
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


# an explicitly-counted UNVERIFIABLE, never DEAD.

#: HARNESS_CAPABILITY alternative expected to land UNVERIFIABLE (no on-disk
#: UNVERIFIABLE verdict surfaces as a diff here rather than hiding inside a
EXPECTED_UNVERIFIABLE_COUNTS: Dict[str, int] = {
    "check_sed_range_read_advise": 2,
    # With no HARNESS_CAPABILITY marker emitted, the measured count is 0 and
    # the key belongs absent rather than set to 0 -- UNVERIFIABLE shrinking is
    # 0 and the key belongs absent rather than set to 0. UNVERIFIABLE
    # exists for this guard+marker pairing yet (same UNVERIFIABLE-not-DEAD
    "guard_grep_via_bash": 1,
    # sweep): it counted an UNVERIFIABLE "in-process" harness-capability marker
    # i.e. this key is absent rather than set to 0. This is the UNVERIFIABLE ceiling
    # SHRINKING, which this module's own docstring names as the only honest
    # UNVERIFIABLE to LIVE (S0, 2026-07-29): the new harness_capability_manifest.json carries
    # so no UNVERIFIABLE count is expected here anymore -- see EXPECTED_LIVE_FLOORS below.
    # UNVERIFIABLE on purpose -- it describes a DIFFERENT, unverified (and per the roster-
    # honestly UNVERIFIABLE rather than guessed at in either direction.
    "block_reviewer_bash_outside_allowlist": 1,
    # `block_approval_sentinel_creation`'s REASON_DIRECT message names 10
    # produces. All 10 grade UNVERIFIABLE not because the alternatives are
    "block_approval_sentinel_creation": 10,
    # `block_fleet_delegation_creation`'s REASON_DIRECT copy was also cut to
    "block_fleet_delegation_creation": 2,
    # UNVERIFIABLE, not DEAD: unproven, not false. Close it by adding the
    # Entered with this guard's first LIVE_TRIGGERS row, 2026-08-30.
    "guard_host_subagent_bash_spawn_shapes": 1,
    # they land UNVERIFIABLE instead: `coordinator_core/conftest.py`'s
    # autouse `HOME`/`USERPROFILE` quarantine (the same one
    # `_ALTLIVE_HAZARD_CWD`'s own docstring names for `_is_hazard_repo`)
    # `CLAUDE_KLABAUTER_ROOT`/engine-root registry entry (real HOME's
    # than DEAD, per this module's own UNVERIFIABLE contract. Environment-
    # `EXPECTED_LIVE_FLOORS` row for why the LIVE floor for this guard
    # dropped from 2 to 1 (only the `COORDINATOR_OVERRIDE_RAW_PID_LIVENESS`
    # OVERRIDE alternative is unaffected by the quarantine).
    "check_raw_pid_liveness": 2,
}

#: the Director of Engineering's review (finding 6, "UNVERIFIABLE is an ungated sink"): pin a
#: per-guard LIVE-count FLOOR alongside the UNVERIFIABLE ceiling above, so
#: a verdict silently degrading LIVE -> UNVERIFIABLE (three structural
#: see EXPECTED_UNVERIFIABLE_COUNTS' block_reviewer_bash_outside_allowlist
#: capability's unconditional UNVERIFIABLE; probe_command's ambiguous-exit
#: hold steady or grow, UNVERIFIABLE can only hold steady or shrink, DEAD
EXPECTED_LIVE_FLOORS: Dict[str, int] = {
    "block_illegal_filename": 2,
    "block_subagent_plan_body_bash_write": 1,
    # UNTRIGGERED -> LIVE (this dispatch, C3): `_is_hazard_repo` replaced the
    # hardcoded ~/.claude gate the prior UNTRIGGERED reason described, so
    # branch-creation guards' is -- `_ALTLIVE_HAZARD_CWD` +
    # git_add`). Only the `COORDINATOR_OVERRIDE_BLANKET_ADD` override
    # still empty after the OVERRIDE regex match -- it is not, here. Not
    "check_blanket_git_add": 1,
    "check_cat_heredoc_write_advise": 1,
    "check_heredoc_repo_write_advise": 1,
    "check_destructive_git_clean": 1,
    "check_find_exec_rewrite": 2,
    # (`COORDINATOR_ALLOW_GIT_COMMIT_BARE`, `COORDINATOR_ALLOW_GIT_COMMIT_
    # `_alternative_liveness.KEY_SPECIFIC_TRIGGERS`'s own docstring for the
    "check_git_commit_safe_commit_advise": 2,
    "check_grep_via_bash_rewrite": 2,
    "check_head_tail_plumbing_rewrite": 2,
    "check_multiprobe_banner_rewrite": 2,
    "check_no_verify": 1,
    "check_offer_git_c": 2,
    # `session-liveness-cli` COMMAND alternatives now grade UNVERIFIABLE
    # `EXPECTED_UNVERIFIABLE_COUNTS` for the measured cause), leaving only
    # the `COORDINATOR_OVERRIDE_RAW_PID_LIVENESS` OVERRIDE alternative
    "check_raw_pid_liveness": 1,
    "check_sed_range_read_advise": 1,
    "guard_grep_via_bash": 1,
    # see EXPECTED_UNVERIFIABLE_COUNTS' comment above).
    # appended `operator_override_note(_DISABLE_ENV_VAR)`, giving one OVERRIDE
    # alternative alongside the two HARNESS_CAPABILITY markers. 18b73e5bc
    # (2026-08-11) deliberately removed that call -- a successfully ANSWERED
    # OVERRIDE alternatives by AST-walking for `operator_override_note` calls,
    # grade LIVE. `_DISABLE_ENV_VAR` still works, it is just documented in
    "guard_inprocess_search": 2,
    "guard_multiprobe_banner": 1,
    "guard_plumbing_and_loops": 1,
    # C2-divergence closeout): C1's deny now renders a CONCRETE
    # from EXPECTED_UNVERIFIABLE_COUNTS (removed above) to here, exactly
    "block_noncanonical_branch_creation": 1,
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
        alt = altlive.Alternative(
            altlive.AlternativeKind.OVERRIDE, "COORDINATOR_OVERRIDE_RAW_PID_LIVENESS", "COORDINATOR_OVERRIDE_RAW_PID_LIVENESS"
        )
        baseline = altlive.fire_guard("check_raw_pid_liveness")
        assert baseline.fired
        verdict = altlive.probe_override(alt, "check_raw_pid_liveness", baseline)
        assert verdict.status is altlive.VerdictStatus.LIVE

    def test_key_specific_trigger_not_reading_its_var_is_still_dead(self, monkeypatch):
        """KEY_SPECIFIC_TRIGGERS (added to close the disjoint-input-shape
        gap for check_git_commit_safe_commit_advise's two override keys)
        must stay a proven-to-fail mechanism, not a suppression one -- a
        registered key-specific trigger that never actually reads the env
        var it is supposed to prove must still grade DEAD, exactly like an
        ordinary LIVE_TRIGGERS-based probe would. Registers a throwaway
        (guard, env_var) row whose trigger always returns the SAME deny
        regardless of the env var, and asserts probe_override still reports
        DEAD -- a registry that can only turn DEAD into LIVE would pass this
        with a false LIVE."""
        guard = "check_git_commit_safe_commit_advise"
        env_var = "COORDINATOR_ALLOW_TOTALLY_MADE_UP_KEY_SPECIFIC"

        def _inert_trigger():
            return _fake_deny("Deny: fake finding, never reads its own override.\n")

        monkeypatch.setitem(altlive.KEY_SPECIFIC_TRIGGERS, (guard, env_var), _inert_trigger)

        alt = altlive.Alternative(altlive.AlternativeKind.OVERRIDE, env_var, env_var)
        baseline = altlive.fire_guard(guard)
        assert baseline.fired
        verdict = altlive.probe_override(alt, guard, baseline)
        assert verdict.status is altlive.VerdictStatus.DEAD


class TestCapabilityManifestOracle:

    def test_guard_inprocess_search_real_fired_message_grades_live(self):
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
        roster-truth audit: DoE-claude state/audits/2026-07-29-search-
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

    name = "nt"

    def access(self, path, mode):
        if mode == os.X_OK:
            return os.path.isfile(path)
        return os.access(path, mode)

    def __getattr__(self, attr):
        return getattr(os, attr)


class TestResolveOnPathOrSettingsHomeWindowsGuard:

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


EXPECTED_OURS = frozenset(
    {
        "guard_grep_via_bash",
        "guard_multiprobe_banner",
        "guard_plumbing_and_loops",
        "check_find_exec_rewrite",
        "check_grep_via_bash_rewrite",
        "check_sed_range_read_advise",
        "check_cat_heredoc_write_advise",
        "check_heredoc_repo_write_advise",
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
    all_guards = set(altlive.LIVE_TRIGGERS) | set(altlive.UNTRIGGERED)
    ours = {g for g in all_guards if altlive.classify_band(g) is altlive.GuardBand.OURS}
    assert ours == EXPECTED_OURS
    peer = all_guards - ours
    assert "block_subagent_destructive_action" in peer
    assert "check_raw_pid_liveness" in peer
    assert "guard_inprocess_search" in peer


def test_report_output_is_band_attributable():
    evaluations = altlive.evaluate_all(["guard_grep_via_bash", "block_worktree_creation"])
    report = altlive.format_report(evaluations)
    assert "[ours] guard_grep_via_bash" in report
    assert "[peer] block_worktree_creation" in report
    assert "--- per-band breakdown ---" in report
    assert "ours: " in report
    assert "peer: " in report
