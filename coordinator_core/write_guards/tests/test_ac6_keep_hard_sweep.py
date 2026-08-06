"""AC6 keep-hard sweep (docs/plans/2026-08-06-apply-guard-class-census.md).

Diffs the plan's whole commit range (6277a0550..HEAD, the pre-session
baseline) against the 19 keep-hard modules the census named, by PATH. The
invariant is CLASS / PRIORITY / band / fail_closed / advisory_value
STABILITY, never byte-identity -- two of the nineteen (`guard_multiprobe_
banner.py`, `guard_plumbing_and_loops.py`) legitimately got message-text-
only edits from peer chunks C19a/C19b, and `block_subagent_destructive_
action.py` (an EM ruling this session, NOT on the plan's own byte-identity
list -- see this test's own docstring section below) legitimately got a
reshape from peer chunk C18d.

===========================================================================
SWEEP LIMIT -- read before trusting a green run here
===========================================================================
This is a FILE-LEVEL sweep. It cannot see a behaviour change reaching a
keep-hard guard INDIRECTLY through a shared helper it calls but does not
itself define (e.g. `_sentinel_write_guard.py`, edited by peer chunk C12).
That risk is covered only by C12's own in-chunk spot-check, not by anything
in this file. A green run here means "the 19 keep-hard modules' own source
is untouched / class-stable" -- it does NOT mean "nothing about their
runtime behaviour could have shifted."

===========================================================================
block_subagent_destructive_action.py -- EM ruling, this session
===========================================================================
This module is NOT on the plan's own byte-identity keep-hard list. The
census (`state/audits/2026-08-06-guard-class-census/README.md:53`) classes
it a RESHAPE ("destructive core hard, indirection over-block flips"), peer
chunk C18d reshaped it accordingly, and the plan's own inclusion of it in a
byte-identity keep list was an internal inconsistency. This test applies
the SAME class-stability assertion to it that the two Windows guards get
(diff allowed, CLASS/band/fail_closed/advisory_value pinned) -- not
byte-identity, and not absent from this sweep either.

Spec backlink: docs/plans/2026-08-06-apply-guard-class-census.md, AC6.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards import dispatch as bash_dispatch
from coordinator_core.bash_guards.dispatch import AdvisoryValue, GuardBand
from coordinator_core.write_guards import engine as write_engine

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BASELINE = "6277a0550"

# AC6 asks "did the CENSUS touch a keep-hard guard?", not "did anyone touch
# it on this shared branch?". A plain _BASELINE..HEAD range sweep catches
# every commit landed by every concurrent session sharing this tree over
# that window -- including unrelated plans' chunks -- and goes red on any of
# them touching a keep-hard module's path for reasons that have nothing to
# do with the census. (Observed: 50fb58816, "C9b: import diet on nine
# write_guards modules", a pure import-deferral from the UNRELATED
# 2026-08-06-windows-hot-path-less-work-per-interpreter.md plan, touched
# guard_doctrine_surface_edits.py and made a naive range sweep fail here.)
#
# So: identify the census's OWN commits by their recorded `disposition_ref`
# shas on this plan's spine (docs/plans/2026-08-06-apply-guard-class-census.md,
# `- id: C<n>` entries), not by a subject-line pattern or a raw range. Diff
# each keep-hard module against ONLY that fixed commit set. A future reader
# tempted to simplify this back to a plain range sweep: don't -- that
# reintroduces false reds from any other session's concurrent, unrelated
# commits on this same branch.
_CENSUS_COMMITS = (
    "f3ca180e6",  # C1
    "679ea50f0",  # C2
    "18d5a571c",  # C3
    "768bc8a52",  # C4
    "a69586381",  # C5
    "c52e9c58f",  # C7
    "cea9e5cde",  # C9
    "02d9057c5",  # C10
    "018f8b371",  # C11
    "30caac162",  # C12
    "042571418",  # C12A
    "d1113d2b8",  # C13
    "b4a375273",  # C14
    "b27e894a5",  # C16
    "ef26a3998",  # C18
    "4fc6f1cb7",  # C19
    "a3eefd105",  # C20
    "71065167f",  # C21
)

# --- write_guards: byte-identity is NOT the bar; CLASS/PRIORITY pinned ----
_WRITE_KEEP_HARD = {
    "block_disarm_marker_sentinel_write.py": ("hard-deny", 130),
    "block_derived_global_doctrine_write.py": ("hard-deny", 10),
    "block_illegal_filename.py": ("hard-deny", 20),
    "block_oss_mirror_memo_delivery.py": ("hard-deny", 132),
    "block_home_dir_memo_delivery.py": ("hard-deny", 125),
    "block_goals_log_hand_write.py": ("hard-deny", 65),
    "guard_settings_json_write.py": ("hard-deny", 131),
    "guard_doctrine_surface_edits.py": ("hard-deny", 127),
    "block_worktree_sentinel_write.py": ("hard-deny", 129),
}

# --- bash_guards: (band, fail_closed, advisory_value); NO_CONTENT_DIFF for
# every entry except the two named message-text exceptions, and the
# EM-ruled destructive-action module (class-stability only, diff allowed).
_BASH_KEEP_HARD_NO_DIFF = {
    "block_subagent_commit.py": (GuardBand.CONFINEMENT_DENY, True, AdvisoryValue.NOT_COST_ARGUED),
    "block_stash_destruction.py": (GuardBand.CONFINEMENT_DENY, True, AdvisoryValue.NOT_COST_ARGUED),
    "block_subagent_stash_creation.py": (GuardBand.CONFINEMENT_DENY, True, AdvisoryValue.NOT_COST_ARGUED),
    "block_disarm_marker_sentinel_creation.py": (GuardBand.CONFINEMENT_DENY, True, AdvisoryValue.NOT_COST_ARGUED),
    "block_approval_sentinel_creation.py": (GuardBand.CONFINEMENT_DENY, True, AdvisoryValue.NOT_COST_ARGUED),
    "block_worktree_sentinel_creation.py": (GuardBand.CONFINEMENT_DENY, True, AdvisoryValue.NOT_COST_ARGUED),
    "block_reviewer_bash_outside_allowlist.py": (GuardBand.CONFINEMENT_DENY, True, AdvisoryValue.NOT_COST_ARGUED),
    "check_test_suite_invocation.py": (GuardBand.CONFINEMENT_DENY, True, AdvisoryValue.NOT_COST_ARGUED),
}

# Message-string-only edits PASS: diff allowed, class/band/fail_closed/
# advisory_value must be unchanged from these pinned values.
_BASH_KEEP_HARD_CLASS_STABLE_ONLY = {
    "guard_multiprobe_banner.py": (GuardBand.PLATFORM_CONDITIONED_DENY, True, AdvisoryValue.HOST_INDEPENDENT),
    "guard_plumbing_and_loops.py": (GuardBand.PLATFORM_CONDITIONED_DENY, True, AdvisoryValue.WINDOWS_COST_ONLY),
    # EM ruling this session (see module docstring above) -- reshaped by
    # C18d, NOT on the plan's own byte-identity list; class-stability only.
    "block_subagent_destructive_action.py": (GuardBand.CONFINEMENT_DENY, True, AdvisoryValue.NOT_COST_ARGUED),
}

_ENTRY_NAME_BY_MODULE = {
    "block_subagent_commit.py": "block-subagent-commit",
    "block_stash_destruction.py": "block-stash-destruction",
    "block_subagent_stash_creation.py": "block-subagent-stash-creation",
    "block_disarm_marker_sentinel_creation.py": "block-disarm-marker-sentinel-creation",
    "block_approval_sentinel_creation.py": "block-approval-sentinel-creation",
    "block_worktree_sentinel_creation.py": "block-worktree-sentinel-creation",
    "block_reviewer_bash_outside_allowlist.py": "block-reviewer-bash-outside-allowlist",
    "check_test_suite_invocation.py": "check-test-suite-invocation",
    "guard_multiprobe_banner.py": "multiprobe-banner",
    "guard_plumbing_and_loops.py": "plumbing-and-loops",
    "block_subagent_destructive_action.py": "block-subagent-destructive-action",
}


def _has_baseline() -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", _BASELINE],
        cwd=str(_REPO_ROOT),
        capture_output=True,
    )
    if result.returncode != 0:
        return False
    for sha in _CENSUS_COMMITS:
        result = subprocess.run(
            ["git", "cat-file", "-e", sha],
            cwd=str(_REPO_ROOT),
            capture_output=True,
        )
        if result.returncode != 0:
            return False
    return True


pytestmark = pytest.mark.skipif(
    not _has_baseline(),
    reason="plan baseline commit %s (or a recorded census disposition_ref) not "
    "present in this clone's history" % _BASELINE,
)


def _diff_is_empty(rel_path: str) -> bool:
    """True iff NONE of the census's own commits touched rel_path.

    Scoped to _CENSUS_COMMITS, not a _BASELINE..HEAD range -- see the module
    comment above _CENSUS_COMMITS for why a range sweep is wrong here.
    """
    for sha in _CENSUS_COMMITS:
        result = subprocess.run(
            ["git", "diff", "--quiet", "%s^..%s" % (sha, sha), "--", rel_path],
            cwd=str(_REPO_ROOT),
        )
        if result.returncode != 0:
            return False
    return True


@pytest.mark.parametrize("filename,expected", sorted(_WRITE_KEEP_HARD.items()))
def test_write_keep_hard_no_diff_and_class_stable(filename, expected):
    rel_path = "coordinator_core/write_guards/%s" % filename
    assert _diff_is_empty(rel_path), (
        "keep-hard write guard %r has a diff over the plan's range -- "
        "expected NO CHANGE AT ALL (not on the message-string-only "
        "exception list)" % filename
    )
    mod_name = filename[:-3]
    guards, _import_failed = write_engine._discover_guards()
    matches = [g for g in guards if g.name == mod_name]
    assert len(matches) == 1, "expected exactly one registered guard named %r" % mod_name
    g = matches[0]
    expected_cls, expected_priority = expected
    assert g.cls == expected_cls
    assert g.priority == expected_priority


@pytest.mark.parametrize("filename,expected", sorted(_BASH_KEEP_HARD_NO_DIFF.items()))
def test_bash_keep_hard_no_diff_and_class_stable(filename, expected):
    rel_path = "coordinator_core/bash_guards/%s" % filename
    assert _diff_is_empty(rel_path), (
        "keep-hard bash guard %r has a diff over the plan's range -- "
        "expected NO CHANGE AT ALL (not on the message-string-only "
        "exception list)" % filename
    )
    _assert_bash_registration_matches(filename, expected)


@pytest.mark.parametrize("filename,expected", sorted(_BASH_KEEP_HARD_CLASS_STABLE_ONLY.items()))
def test_bash_keep_hard_class_stable_message_edit_allowed(filename, expected):
    """Diff over the plan's range is EXPECTED and PERMITTED for these three
    (message-string-only for the two Windows guards; a real reshape for
    block_subagent_destructive_action.py, per the EM ruling above) -- only
    CLASS/band/fail_closed/advisory_value stability is asserted."""
    _assert_bash_registration_matches(filename, expected)


def _assert_bash_registration_matches(filename, expected):
    expected_band, expected_fail_closed, expected_advisory_value = expected
    entry_name = _ENTRY_NAME_BY_MODULE[filename]
    chain = bash_dispatch._build_guard_chain(
        cmd="echo ac6-keep-hard-sweep-probe",
        session_id="ac6-keep-hard-sweep-probe",
        cwd="/tmp",
        payload={"tool_name": "Bash", "tool_input": {"command": "echo x"}},
        policy_file=None,
        host_is_windows=None,
    )
    matches = [e for e in chain if e.name == entry_name]
    assert len(matches) == 1, "expected exactly one registered chain entry named %r" % entry_name
    entry = matches[0]
    assert entry.band is expected_band
    assert entry.fail_closed is expected_fail_closed
    assert entry.advisory_value is expected_advisory_value
