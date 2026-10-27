"""Structural gate over ``_alternative_liveness``: every guard's registered
trigger must FIRE (a broken row fails loud), every alternative its
``AlternativeRecord`` offers must resolve to a non-``DEAD`` verdict, each
(guard, caller class) pair must grade no lower than its pinned rung, and the
guard registry itself must track the package's real guard surface so a
newly-added guard cannot silently escape this gate.

This is a MEASUREMENT test, not a policy test -- it never asserts what a
guard denies/allows/rewrites (that is `test_deny_message_accuracy.py`,
each guard's own dedicated test file, and `_guard_coverage.py`'s job). It
asserts that whatever a guard's record NAMES as the way out is real and
reachable. Alternatives and rungs are read from record members; message prose
is never parsed.

BAND ATTRIBUTION (2026-07-29 PM ruling): guard ownership is split by guard --
this session owns the advisory/rewrite guards (`guard_grep_via_bash`/
`guard_multiprobe_banner`/`guard_plumbing_and_loops` plus the BX-16
rewrite/advisory family + `check_offer_git_c` in `dispatch_checks.py`); a
peer session owns chain structure and the confinement (hard-deny) guards.
This gate evaluates BOTH bands without editing either; band tagging
(`_alternative_liveness.classify_band`) exists ONLY so a finding can be handed
to the right session, never to narrow what gets checked.

Spec backlink: coordinator_core/bash_guards/_alternative_liveness.py
"""

from __future__ import annotations

import os
import shutil
from typing import Dict

import pytest

from coordinator_core.bash_guards import _alternative_liveness as altlive
from coordinator_core.bash_guards import guard_grep_via_bash
from coordinator_core.bash_guards import guard_inprocess_search
from coordinator_core.guard_messages.alternative_record import (
    AdaptableOffer,
    AlternativeRecord,
    Audience,
    CapabilityRef,
    EscalationRoute,
    Override,
    ReadyCommand,
)
from coordinator_core.subagent_sandbox.engine import load_policy


def _record(*members, kind="deny", summary="BLOCKED: fake finding.", **extra) -> AlternativeRecord:
    return AlternativeRecord(verdict_kind=kind, summary=summary, members=members, guard_name="fake", **extra)


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
        "check_validate_commit",
        "check_test_suite_invocation",
    }
)


def test_untriggered_set_matches_expected_no_silent_growth():
    """A newly-UNVERIFIABLE/untriggered guard must surface here, not hide.
    Shrinking this set (a follow-up session closes one) requires editing
    this constant -- that's the point: it cannot happen silently."""
    assert set(altlive.UNTRIGGERED) == EXPECTED_UNTRIGGERED


#: The pinned CEILING form, alongside the exact match above: growth fails loud.
#: Lower this pin by hand as entries close; never raise it to make a new
#: suppression quiet.
_UNTRIGGERED_PINNED_MAX = 5


def test_untriggered_count_never_grows_past_pin():
    assert len(altlive.UNTRIGGERED) <= _UNTRIGGERED_PINNED_MAX, (
        "UNTRIGGERED grew past its pinned ceiling (%d) -- a guard silently "
        "gained a suppression-list escape hatch; lower the pin only after "
        "actually closing entries, never raise it to make growth quiet"
        % _UNTRIGGERED_PINNED_MAX
    )


# ---------------------------------------------------------------------------
# write_guards/ discovery extension. SEPARATE registries so this extension
# never touches the bash_guards-scoped EXPECTED_UNTRIGGERED/_UNTRIGGERED_
# PINNED_MAX ratchets above.
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
    """The parametrized tests below collect zero cases -- and silently
    report a pass -- if `WRITE_GUARD_LIVE_TRIGGERS` were ever emptied."""
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
def test_registered_trigger_fires(guard, tmp_path, monkeypatch):
    if guard == "block_topic_branch":
        # The guard exempts a clone under a repo's own `scratch/`; judge a non-scratch repo.
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
    result = altlive.fire_guard(guard)
    assert result.fired, (
        "%s's registered trigger did not fire (error=%r) -- broken registry row, "
        "not a real coverage gap" % (guard, result.error)
    )


def _message_text(result) -> str:
    if isinstance(result, AlternativeRecord):
        return result.em_text or result.summary
    hso = result.get("hookSpecificOutput", {})
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
    reason = _message_text(result.envelope)
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


# ---------------------------------------------------------------------------
# The gate itself: every alternative a fired guard's record offers must be
# LIVE or an explicitly-counted UNVERIFIABLE, never DEAD.
# ---------------------------------------------------------------------------

#: UNVERIFIABLE verdicts per guard, measured over record members: a
#: `ReadyCommand` (or a `git <subcommand>` template) executed in the probe's
#: throwaway non-git cwd exits 128 with no dead-marker, so it grades ambiguous
#: rather than DEAD. A new UNVERIFIABLE verdict surfaces as a diff here; the
#: ceiling may only shrink.
EXPECTED_UNVERIFIABLE_COUNTS: Dict[str, int] = {
    "block_reviewer_bash_outside_allowlist": 1,
    "block_subagent_stash_creation": 1,
    "check_destructive_git_revert_advisory": 1,
    "check_stale_write": 1,
    "guard_deploy_dirty_tree": 1,
}

#: DEAD offers no guard has fixed yet. Shrink-only: each entry is a guard whose
#: record offers something that is not a command.
KNOWN_DEAD_OFFERS: Dict[str, int] = {
    # Its one ReadyCommand is the list "cat, ls, stat, rm, git status, git log",
    # not a command. Fix: one ReadyCommand per command.
    "block_fleet_delegation_creation": 1,
}

#: Per-guard LIVE-count FLOOR, so a verdict silently degrading LIVE ->
#: UNVERIFIABLE is a loud failure. LIVE holds or grows, UNVERIFIABLE holds or
#: shrinks, DEAD stays at KNOWN_DEAD_OFFERS.
EXPECTED_LIVE_FLOORS: Dict[str, int] = {
    "block_approval_sentinel_creation": 1,
    "block_dev_repo_sentinel_removal": 1,
    "block_hand_authored_handoff_creation": 2,
    "block_illegal_filename": 2,
    "block_noncanonical_branch_creation": 1,
    "block_stash_destruction": 1,
    "block_subagent_plan_body_bash_write": 1,
    "block_topic_branch": 1,
    "block_venv_creation": 1,
    "block_worktree_creation": 3,
    "check_blanket_git_add": 3,
    "check_cat_heredoc_write_advise": 1,
    "check_destructive_git_clean": 2,
    "check_destructive_git_revert": 3,
    "check_destructive_git_revert_advisory": 2,
    "check_destructive_rm": 1,
    "check_find_exec_rewrite": 2,
    "check_git_commit_safe_commit_advise": 2,
    "check_grep_via_bash_rewrite": 2,
    "check_head_tail_plumbing_rewrite": 2,
    "check_heredoc_repo_write_advise": 1,
    "check_multiprobe_banner_rewrite": 2,
    "check_no_verify": 1,
    "check_offer_git_c": 2,
    "check_raw_pid_liveness": 1,
    "check_sed_range_read_advise": 1,
    "check_stale_write": 1,
    "guard_grep_via_bash": 1,
    "guard_inprocess_search": 2,
    "guard_multiprobe_banner": 2,
    "guard_plumbing_and_loops": 2,
    "guard_powershell_via_bash": 1,
}


#: Guards whose alternatives name a binary only an installed door provides, so their grading
#: depends on the box rather than the package; `..._claude_klabauter_corpus.py` runs them.
_INSTALLED_BINARY_GUARDS = frozenset({"check_raw_pid_liveness"})


@pytest.mark.parametrize("guard", sorted(set(altlive.LIVE_TRIGGERS) - _INSTALLED_BINARY_GUARDS))
def test_named_alternatives_are_not_dead(guard):
    assert_named_alternatives_are_not_dead(guard)


def assert_named_alternatives_are_not_dead(guard):
    ev = altlive.evaluate_guard(guard)
    if not ev.fire.fired:
        pytest.skip("covered by test_registered_trigger_fires; not re-asserted here")

    dead = [(alt, v) for alt, v in ev.verdicts if v.status is altlive.VerdictStatus.DEAD]
    known_dead = KNOWN_DEAD_OFFERS.get(guard, 0)
    assert len(dead) == known_dead, "\n".join(
        ["%s: expected %d DEAD offer(s) (KNOWN_DEAD_OFFERS), got %d" % (guard, known_dead, len(dead))]
        + [
            "%s: DEAD alternative [%s] %r -- %s" % (guard, alt.kind.value, alt.raw, v.evidence)
            for alt, v in dead
        ]
    )

    unverifiable = [v for _alt, v in ev.verdicts if v.status is altlive.VerdictStatus.UNVERIFIABLE]
    expected = EXPECTED_UNVERIFIABLE_COUNTS.get(guard, 0)
    if guard == "check_raw_pid_liveness" and shutil.which("session-liveness-cli"):
        expected = 0
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
# Rung gate: (guard, caller class) -> A/B/C/D computed from the record.
# ---------------------------------------------------------------------------

_RUNG_CLASSES = ("em", "unresolved", "coordinator:code-reviewer")

#: Rung floor per guard, in `_RUNG_CLASSES` order, measured over record members.
#: No ledger of the 2026-07-29 rungs exists in the tree, so this measured table
#: is the baseline: a pair grading lower than its floor fails unless
#: ACKNOWLEDGED_RUNG_DROPS names it. An EM floor of A or B is never edited down.
#: A "D" floor is the lowest rung, so that (guard, class) pair cannot fail: it
#: records that the guard offers that receiver nothing, and is raised when the
#: guard gains an offer, never read as a passing grade.
RUNG_FLOORS: Dict[str, str] = {
    "block_approval_sentinel_creation": "BCC",
    "block_dev_repo_sentinel_removal": "DCC",
    "block_disarm_marker_sentinel_creation": "DCC",
    "block_drive_root_new_dir_bash_write": "BDD",
    "block_editor_kill_by_name": "DDD",
    "block_external_workspace_vcs_write": "BDD",
    "block_fleet_delegation_creation": "DCC",
    "block_hand_authored_handoff_creation": "ACA",
    "block_home_root_bash_write": "BDD",
    "block_illegal_filename": "ACC",
    "block_noncanonical_branch_creation": "ACC",
    "block_perforce_submit": "DCC",
    "block_push_pr_non_owned_remote": "DCC",
    "block_reviewer_bash_outside_allowlist": "ACA",
    "block_stash_destruction": "BCC",
    "block_subagent_commit": "DCC",
    "block_subagent_destructive_action": "CCC",
    "block_subagent_findings_reject": "CCC",
    "block_subagent_grant_acquisition": "CCC",
    "block_subagent_guard_grant": "CCC",
    "block_subagent_plan_body_bash_write": "CCC",
    "block_subagent_stash_creation": "BCC",
    "block_topic_branch": "CCC",
    "block_unreal_engine_resave": "DDD",
    "block_venv_creation": "CCC",
    "block_whole_filesystem_scan": "DDD",
    "block_worktree_creation": "ACC",
    "block_worktree_sentinel_creation": "DCC",
    "check_blanket_git_add": "BCC",
    "check_cat_heredoc_write_advise": "CCC",
    "check_destructive_git_clean": "BCC",
    "check_destructive_git_revert": "ACC",
    "check_destructive_git_revert_advisory": "BCC",
    "check_destructive_rm": "ACA",
    "check_find_exec_rewrite": "ACC",
    "check_git_commit_safe_commit_advise": "BCC",
    "check_grep_via_bash_rewrite": "CCC",
    "check_head_tail_plumbing_rewrite": "ACC",
    "check_heredoc_repo_write_advise": "CCC",
    "check_multiprobe_banner_rewrite": "CCC",
    "check_no_verify": "CCC",
    "check_offer_git_c": "ACA",
    "check_raw_pid_liveness": "BCC",
    "check_runaway_find": "BCB",
    "check_sed_range_read_advise": "BCC",
    "check_stale_write": "ACA",
    "guard_deploy_dirty_tree": "ADA",
    "guard_doctrine_surface_bash_write": "DCC",
    "guard_grep_via_bash": "CCC",
    "guard_heavy_command_admission": "DCC",
    "guard_host_subagent_bash_ban": "BCC",
    "guard_host_subagent_bash_spawn_shapes": "BCC",
    "guard_inprocess_search": "BBB",
    "guard_multiprobe_banner": "ACC",
    "guard_plumbing_and_loops": "ACC",
    "guard_post_executor_verification": "BDD",
    "guard_powershell_via_bash": "CCC",
    "guard_repo_setup_claude_home_refusal": "BCC",
    "guard_subagent_heavy_ue_launch": "CCC",
    "guard_xs_lane_shell_write": "BDD",
    "p4_verb_fence": "DCC",
}

#: (guard, caller class) pairs allowed to grade below their floor. Empty: no
#: rung drop is acknowledged.
ACKNOWLEDGED_RUNG_DROPS: frozenset = frozenset()

#: Guards whose `em_text` omits the text of an offered (non-route) member, so the
#: EM-class rung is graded from the members that do occur. Shrink-only. Fix the
#: guard (put the offer in its EM text), then delist. Each delisting changes
#: that guard's EM render, which the guard_render_baseline fixture pins byte for
#: byte, so it lands only with a ruled baseline re-freeze.
EM_TEXT_OFFER_GAPS = frozenset(
    {
        "block_dev_repo_sentinel_removal",
        "block_disarm_marker_sentinel_creation",
        "block_editor_kill_by_name",
        "block_fleet_delegation_creation",
        "block_topic_branch",
        "block_unreal_engine_resave",
        "block_venv_creation",
        "block_whole_filesystem_scan",
        "block_worktree_creation",
        "block_worktree_sentinel_creation",
        "check_blanket_git_add",
        "check_no_verify",
        "guard_doctrine_surface_bash_write",
        "guard_grep_via_bash",
        "guard_post_executor_verification",
        "guard_powershell_via_bash",
    }
)


@pytest.fixture(scope="module")
def policy():
    return load_policy(None)


def _fired_record(guard):
    result = altlive.fire_guard(guard)
    assert result.fired, "%s did not fire (error=%r)" % (guard, result.error)
    return result.envelope


def test_rung_floors_name_exactly_the_registered_guards():
    assert set(RUNG_FLOORS) == set(altlive.LIVE_TRIGGERS)


@pytest.mark.parametrize("guard", sorted(altlive.LIVE_TRIGGERS))
def test_no_guard_caller_pair_grades_below_its_rung_floor(guard, tmp_path, monkeypatch, policy):
    if guard == "block_topic_branch":
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
    record = _fired_record(guard)
    assert isinstance(record, AlternativeRecord), (
        "%s returned %s, not an AlternativeRecord -- the rung is computed from record members"
        % (guard, type(record).__name__)
    )
    table = altlive.rung_table(record, policy)
    for cls, floor in zip(_RUNG_CLASSES, RUNG_FLOORS[guard]):
        got = table[cls]
        if (guard, cls) in ACKNOWLEDGED_RUNG_DROPS:
            continue
        assert altlive.RUNG_ORDER.index(got) <= altlive.RUNG_ORDER.index(floor), (
            "%s graded %s for %s, below its floor %s" % (guard, got, cls, floor)
        )


_EM_RUNG_AB_FLOORS = 32


def test_em_rung_a_and_b_floors_are_not_edited_down():
    em_ab = sum(1 for floor in RUNG_FLOORS.values() if floor[0] in "AB")
    assert em_ab >= _EM_RUNG_AB_FLOORS, (
        "EM rung A/B floors were edited down -- a downgrade needs an ACKNOWLEDGED_RUNG_DROPS entry"
    )


def test_em_text_offer_gaps_match_the_pinned_set():
    gaps = set()
    for guard in altlive.LIVE_TRIGGERS:
        record = _fired_record(guard)
        if any(
            not isinstance(m, EscalationRoute) for m in altlive.em_text_gaps(record)
        ):
            gaps.add(guard)
    assert gaps == EM_TEXT_OFFER_GAPS, (
        "EM-text offer gaps changed: new %s, closed %s"
        % (sorted(gaps - EM_TEXT_OFFER_GAPS), sorted(EM_TEXT_OFFER_GAPS - gaps))
    )


class TestRungSemantics:
    """`rung` on synthetic records: it reads members and reachability, never the verdict kind."""

    REVIEWER = "coordinator:code-reviewer"

    def test_ready_command_reachable_for_em_is_a(self, policy):
        record = _record(ReadyCommand("git status"))
        assert altlive.rung(record, None, Audience.EM, policy) == "A"

    def test_adaptable_offer_or_capability_is_b(self, policy):
        offer = _record(AdaptableOffer("git status", "paths"))
        ref = _record(CapabilityRef("guard_inprocess_search::in-process"))
        assert altlive.rung(offer, None, Audience.EM, policy) == "B"
        assert altlive.rung(ref, None, Audience.EM, policy) == "B"

    def test_only_route_or_override_is_c(self, policy):
        record = _record(EscalationRoute("what", "authorize"), Override("COORDINATOR_ALLOW_X"))
        assert altlive.rung(record, None, Audience.EM, policy) == "C"
        assert altlive.rung(record, self.REVIEWER, Audience.SUBAGENT, policy) == "C"

    def test_nothing_reachable_is_d(self, policy):
        record = _record(ReadyCommand("rm -rf build"))
        assert altlive.rung(record, self.REVIEWER, Audience.SUBAGENT, policy) == "D"
        assert altlive.rung(record, None, Audience.SUBAGENT, policy) == "D"

    def test_unresolved_subagent_fails_closed_but_keeps_the_route(self, policy):
        record = _record(ReadyCommand("git status"), EscalationRoute("what", "authorize"))
        assert altlive.rung(record, None, Audience.SUBAGENT, policy) == "C"

    def test_verdict_kind_never_changes_the_rung(self, policy):
        members = (CapabilityRef("guard_inprocess_search::in-process"),)
        deny = _record(*members, kind="deny")
        advisory = _record(*members, kind="advisory")
        assert altlive.rung(deny, None, Audience.EM, policy) == altlive.rung(advisory, None, Audience.EM, policy) == "B"

    def test_em_text_drops_a_member_whose_text_it_does_not_offer(self, policy):
        record = _record(ReadyCommand("git status"), em_text="Use something else entirely.")
        assert altlive.rung(record, None, Audience.EM, policy) == "D"
        assert [type(m) for m in altlive.em_text_gaps(record)] == [ReadyCommand]

    def test_em_text_keeps_a_member_whose_text_it_offers(self, policy):
        record = _record(ReadyCommand("git status"), em_text="Run git status instead.")
        assert altlive.rung(record, None, Audience.EM, policy) == "A"
        assert altlive.em_text_gaps(record) == []

    def test_subagent_rung_ignores_em_text(self, policy):
        record = _record(ReadyCommand("git status"), em_text="Nothing relevant.")
        assert altlive.rung(record, self.REVIEWER, Audience.SUBAGENT, policy) == "A"

    def test_rewrite_command_counts_for_em_without_appearing_in_em_text(self, policy):
        record = _record(
            ReadyCommand("git status"),
            kind="rewrite",
            updated_input={"command": "git status"},
            em_text="Rewritten for you.",
        )
        assert altlive.rung(record, None, Audience.EM, policy) == "A"

    def test_em_text_is_never_parsed_for_alternatives(self, policy):
        record = _record(EscalationRoute("what", "authorize"), em_text="Use instead: `git status`")
        assert altlive.rung(record, None, Audience.EM, policy) == "D"

    def test_rung_table_covers_em_unresolved_and_each_confined_class(self, policy):
        table = altlive.rung_table(_record(ReadyCommand("git status")), policy)
        assert set(_RUNG_CLASSES) <= set(table)
        assert set(table) == {"em", "unresolved"} | set(altlive.confined_caller_classes(policy))


def _fake_deny(reason: str) -> AlternativeRecord:
    return _record(ReadyCommand("git --version"), summary=reason)


class TestMetaGateProvenToFail:
    def test_dead_binary_alternative_is_caught(self):
        record = _record(ReadyCommand("this-binary-does-not-exist-anywhere --fake-flag"))
        alts = altlive.record_alternatives(record)
        assert alts, "no alternative read from a record with a ReadyCommand"
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
        alts = altlive.record_alternatives(_record(ReadyCommand("git --version")))
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
        """A registered key-specific trigger that never reads the env var it is
        supposed to prove must still grade DEAD, exactly like an ordinary
        LIVE_TRIGGERS-based probe would."""
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
        assert capability_verdicts, "expected guard_inprocess_search's real record to carry its in-process capability"
        assert all(v.status is altlive.VerdictStatus.LIVE for v in capability_verdicts), capability_verdicts

    def test_guard_grep_via_bash_composed_advisory_no_longer_claims_in_process(self):
        """A plain `&&`-chained grep is SILENT. A genuine GNU-only construct
        with no partial rewrite still fires, and its record offers the
        subagent-dispatch fallback as a template -- never an `in-process`
        capability claim."""
        silent_payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "grep -rn foo . && echo done"},
        }
        assert guard_grep_via_bash.check(silent_payload, host_is_windows=False) is None

        payload = {"tool_name": "Bash", "tool_input": {"command": "grep -Pn foo . && echo done"}}
        record = guard_grep_via_bash.check(payload, host_is_windows=False)
        assert isinstance(record, AlternativeRecord)
        assert "registered ahead of this guard" not in (record.em_text or record.summary)
        refs = [m for m in record.members if isinstance(m, CapabilityRef)]
        assert not [r for r in refs if "in-process" in r.capability_id], refs
        assert any(isinstance(m, AdaptableOffer) and "subagent" in m.template for m in record.members)

    def test_planted_t8_shape_defect_is_not_corroborated_by_prose_coincidence(self):
        """The SAME marker word ("in-process") that a DIFFERENT guard's
        genuinely-live capability also uses, fired under a guard name
        (`check_head_tail_plumbing_rewrite`) that carries NO manifest entry
        for that marker. The guard-scoped oracle must refuse to grade it LIVE
        just because `guard_inprocess_search::in-process` is a true entry."""
        alt = altlive.Alternative(altlive.AlternativeKind.HARNESS_CAPABILITY, "in-process", "in-process")

        real_verdict = altlive.probe_harness_capability(alt, guard="check_head_tail_plumbing_rewrite")
        assert real_verdict.status is altlive.VerdictStatus.UNVERIFIABLE, (
            "the guard-scoped oracle incorrectly corroborated a false capability "
            "claim by bare-marker prose coincidence: %s" % real_verdict
        )

        naive_manifest = {"in-process": {"available": True, "note": "guard_inprocess_search exists"}}

        def _naive_lookup(_guard: str, detail: str) -> str:
            entry = naive_manifest.get(detail)
            if entry is None:
                return altlive.VerdictStatus.UNVERIFIABLE.value
            return altlive.VerdictStatus.LIVE.value if entry.get("available") is True else altlive.VerdictStatus.DEAD.value

        naive_verdict = _naive_lookup("check_head_tail_plumbing_rewrite", alt.detail)
        assert naive_verdict == altlive.VerdictStatus.LIVE.value, (
            "the reconstructed naive (bare-marker, guard-blind) oracle must grade the planted "
            "T8-shape defect LIVE, else this test exercises a fixture rather than the real risk"
        )
        assert real_verdict.status is not altlive.VerdictStatus.LIVE

    def test_capability_ref_id_is_graded_against_the_manifest_by_its_own_id(self):
        record = _record(CapabilityRef("guard_inprocess_search::in-process"))
        (alt,) = altlive.record_alternatives(record)
        assert alt.kind is altlive.AlternativeKind.HARNESS_CAPABILITY
        verdict = altlive.probe_alternative(alt, guard="some_other_guard")
        assert verdict.status is altlive.VerdictStatus.LIVE


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


class TestRecordAlternatives:
    def test_pure_policy_record_offers_no_alternative(self):
        record = _record(EscalationRoute("what", "authorize"))
        assert altlive.record_alternatives(record) == []

    def test_ready_command_and_rewrite_command_are_one_alternative(self):
        record = _record(
            ReadyCommand("git -C /tmp status"),
            kind="rewrite",
            updated_input={"command": "git -C /tmp status"},
        )
        alts = altlive.record_alternatives(record)
        assert len(alts) == 1
        assert alts[0].kind is altlive.AlternativeKind.COMMAND
        assert alts[0].detail == ["git", "-C", "/tmp", "status"]

    def test_updated_input_command_is_extracted_without_a_member(self):
        record = _record(EscalationRoute("what", "authorize"), kind="rewrite", updated_input={"command": "git -C /tmp status"})
        assert any(
            a.kind is altlive.AlternativeKind.COMMAND and a.detail == ["git", "-C", "/tmp", "status"]
            for a in altlive.record_alternatives(record)
        )

    def test_members_map_to_their_alternative_kinds(self):
        record = _record(
            CapabilityRef("guard_inprocess_search::in-process"),
            Override("COORDINATOR_ALLOW_X"),
        )
        kinds = [a.kind for a in altlive.record_alternatives(record)]
        assert kinds == [altlive.AlternativeKind.HARNESS_CAPABILITY, altlive.AlternativeKind.OVERRIDE]

    def test_a_git_template_is_probed_with_its_placeholders_intact(self):
        record = _record(AdaptableOffer("git stash push -u -- <paths>", "paths"))
        (alt,) = altlive.record_alternatives(record)
        assert alt.kind is altlive.AlternativeKind.COMMAND and alt.detail[0] == "git"

    @pytest.mark.parametrize(
        "template",
        [
            "taskkill /PID <pid>",
            "write under <repo>/scratch/",
            "git archive <sha> | tar -x -C <tmpdir>",
            "git show stash@{N}:<path> > <path>",
        ],
    )
    def test_other_templates_are_never_executed(self, template):
        assert altlive.record_alternatives(_record(AdaptableOffer(template, "x"))) == []

    def test_a_legacy_dict_result_offers_only_its_updated_input_command(self, monkeypatch):
        legacy = {
            "hookSpecificOutput": {
                "permissionDecision": "allow",
                "additionalContext": "Use instead: `git status`",
                "updatedInput": {"command": "git status"},
            }
        }
        monkeypatch.setitem(altlive.LIVE_TRIGGERS, "fake_legacy_guard", lambda: legacy)
        ev = altlive.evaluate_guard("fake_legacy_guard")
        assert [a.raw for a in ev.alternatives] == ["git status"]

        prose_only = {"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "Use instead: `git status`"}}
        monkeypatch.setitem(altlive.LIVE_TRIGGERS, "fake_legacy_guard", lambda: prose_only)
        assert altlive.evaluate_guard("fake_legacy_guard").alternatives == []

    def test_the_prose_extractor_is_gone(self):
        assert not [n for n in dir(altlive) if n.startswith("extract_")]
        assert not hasattr(altlive, "UnclassifiableAlternative")


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


class TestWindowsPosixTwinResolution:
    def test_only_the_colliding_system32_binary_is_remapped(self, tmp_path, monkeypatch):
        system = tmp_path / "Windows" / "System32"
        system.mkdir(parents=True)
        monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
        git_root = tmp_path / "Git"
        (git_root / "cmd").mkdir(parents=True)
        (git_root / "usr" / "bin").mkdir(parents=True)
        (git_root / "usr" / "bin" / "find.exe").write_text("")
        (git_root / "usr" / "bin" / "grep.exe").write_text("")
        monkeypatch.setattr(altlive.shutil, "which", lambda name: str(git_root / "cmd" / "git.exe"))

        twin = altlive._posix_twin_of_windows_builtin("find", str(system / "find.EXE"))
        assert twin == str(git_root / "usr" / "bin" / "find.exe")

        elsewhere = tmp_path / "tools" / "find.exe"
        assert altlive._posix_twin_of_windows_builtin("find", str(elsewhere)) is None
        assert altlive._posix_twin_of_windows_builtin("grep", str(system / "grep.exe")) is None
