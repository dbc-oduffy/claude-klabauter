"""Structural pin for `dispatch.py`'s three-band `guard_chain` split
(M1, 2026-07-29 -- CBS-C1 + DoC-C9 + the F9 widening).

Replaces `test_hard_denies_precede_rewrites.py` (deleted in the same change
that adds this file): that test asserted "no hard-deny sits behind a
rewriting guard" by regex-parsing `dispatch.py`'s source text and comparing
two named sets. This file asserts the STRONGER, more general property the
band model was built to make mechanical: every REGISTERED guard (not merely
the ones a hand-maintained set happens to name) carries an explicit `band`,
that band is the CORRECT one, and the three bands run in the fixed sequence
`CONFINEMENT_DENY -> ADVISORY_REWRITE -> PLATFORM_CONDITIONED_DENY` with no
guard out of place.

Reads the live registration via `dispatch._build_guard_chain(...)` (called
with a harmless dummy command/payload) rather than by regexing source text
-- the returned `GuardEntry` instances are inspected for their
name/fail_closed/band attributes; the `fn` closures are never invoked, so
this stays a STRUCTURAL registration check exactly as its predecessor was,
only more robust against reformatting.
"""
from __future__ import annotations

from typing import List

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards.dispatch import GuardBand, GuardEntry

# Every CONFINEMENT_DENY guard, in registration order. Fails-closed hard
# denies that must never sit behind a guard that can rewrite (`offer-git-c`
# and friends short-circuit via allow+updatedInput).
CONFINEMENT_DENY_NAMES = [
    "no-verify",
    "destructive-git-orphan",
    "destructive-rm",
    "destructive-git-clean",
    "destructive-git-revert",
    "blanket-git-add",
    "runaway-find",
    "block-worktree-creation",
    "block-stash-destruction",
    "block-subagent-stash-creation",
    "block-approval-sentinel-creation",
    "block-worktree-sentinel-creation",
    # Near-exact sibling of the entry above (dispatch.py registers it
    # immediately after, same CONFINEMENT_DENY hard-deny posture -- see
    # block_fleet_delegation_creation.py's own module docstring).
    "block-fleet-delegation-creation",
    "block-disarm-marker-sentinel-creation",
    "block-reviewer-bash-outside-allowlist",
    "block-subagent-destructive-action",
    "block-subagent-commit",
    "check-test-suite-invocation",
    "block-subagent-grant-acquisition",
    # Near-exact sibling of the entry above (dispatch.py registers it
    # immediately after, same CONFINEMENT_DENY hard-deny posture -- see
    # block_subagent_guard_grant.py's own module docstring "NEAR-EXACT
    # PORT of block_subagent_grant_acquisition.py").
    "block-subagent-guard-grant",
    # The four guards rehomed from DoE's in-process fold (C4-C7,
    # docs/plans/2026-08-28-the-four-folded-bash-guards-get-registered-not-folded.md).
    # All four are hard-deny confinement, registered in this band by their own
    # chunks; named here by C12 because no chunk declared this file.
    "guard-repo-setup-claude-home-refusal",
    "guard-host-subagent-bash-ban",
    "guard-host-subagent-bash-spawn-shapes",
    "guard-doctrine-surface-bash-write",
]

# Every ADVISORY_REWRITE guard, in registration order. `inprocess-search`
# must precede every "-rewrite"/"-advise"-suffixed entry and `probe-spray`
# (its own registration comment: those return allow+rewrite, which would
# make its search-answering seam unreachable if registered after them).
ADVISORY_REWRITE_NAMES = [
    # Review: staff-eng, Finding 0 (2026-08-05) -- the advisory floor's
    # non-hard-deny leg. Registered immediately after `check-raw-pid-
    # liveness` (the last CONFINEMENT_DENY guard) and before `offer-git-c`,
    # so it is the first ADVISORY_REWRITE entry in physical chain order.
    "destructive-git-revert-advisory",
    # Two-leg split (2026-08-05, mirrors `destructive-git-revert-advisory`
    # immediately above -- same CONFINEMENT_DENY shadowing hazard,
    # `state/audits/2026-08-05-confinement-deny-band-return-shapes.md`).
    # Registered immediately after `destructive-git-revert-advisory` and
    # before `offer-git-c`.
    "block-dev-repo-sentinel-removal-advisory",
    "offer-git-c",
    # Mechanical leg of the fleet-wide `.git/index.lock` contention
    # campaign -- registered immediately after `offer-git-c` (physical
    # chain position, dispatch.py) -- see guard_no_optional_locks.py.
    "git-no-optional-locks",
    # Self-heal leg of the same fleet-wide `.git/index.lock` contention
    # campaign, registered immediately after `git-no-optional-locks` in
    # `dispatch.py` -- a stat-gated, side-effect-only reap that always
    # returns `None` (never a rewrite or a deny) but is `fail_closed=False`
    # ADVISORY_REWRITE, same as its `git-no-optional-locks` neighbor, per
    # guard_reap_stale_git_lock.py's own module docstring and dispatch.py's
    # registration comment.
    "reap-stale-git-lock",
    "validate-commit",
    "inprocess-search",
    "probe-spray",
    "block-illegal-filename",
    "find-exec-rewrite",
    "grep-via-bash-rewrite",
    "sed-range-read-advise",
    "cat-heredoc-write-advise",
    "heredoc-repo-write-advise",
    "git-commit-safe-commit-advise",
    "multiprobe-banner-rewrite",
    "head-tail-plumbing-rewrite",
    "offer-invoke-params-stdin",
    # `grep-via-bash-guard` moved here from PLATFORM_CONDITIONED_DENY_NAMES
    # (H11(a), 2026-07-30, docs/plans/2026-07-30-os-aware-guard-advisory-
    # defaults.md) -- its own substitutable/deny branch was removed the
    # same day (0 denies on either platform, provably unreachable), so it
    # no longer has deny vocabulary and both its band AND `fail_closed`
    # flipped (see its own dispatch.py registration comment). Registered
    # last in THIS list/band, immediately before the two remaining
    # PLATFORM_CONDITIONED_DENY guards, matching its physical chain
    # position (band contiguity requires the physical move, not just the
    # label -- see dispatch.py's own comment at this guard's entry).
    "grep-via-bash-guard",
    # cross-repo/inbox/ dispatch, "Guard powershell-via-bash mangling"
    # (2026-08-08) -- registered immediately after `grep-via-bash-guard`
    # (same physical chain position, dispatch.py). Same "never denies"
    # shape as its neighbor, so this band, not PLATFORM_CONDITIONED_DENY.
    "powershell-via-bash-guard",
    # docs/plans/2026-08-01-branch-creation-seam-guards.md, chunk C5/C7 --
    # both advisory-only, registered adjacent to grep-via-bash-guard, ahead
    # of the two remaining PLATFORM_CONDITIONED_DENY guards below.
    "branch-set-precedence",
    "longlived-branch-naming",
    # docs/plans/2026-08-02-write-confinement-guards.md (DoE-claude), chunk
    # C4 -- the Bash-surface cross-repo write-confinement speed bump.
    # `ADVISORY_REWRITE`, deliberately NOT `CONFINEMENT_DENY`: the blanket-
    # disarm marker can suppress every band except `CONFINEMENT_DENY`, and
    # registering a deliberately passable bump there would make it the
    # LEAST passable guard in the suite -- see dispatch.py's own
    # registration comment for the full rationale (AC19).
    "bump-foreign-repo-write",
    # Same plan, chunk C5 -- the Bash-surface OUTSIDE-repo sibling of the
    # entry above (fires when a target resolves under NO git root at all,
    # rather than a DIFFERENT one). Same `ADVISORY_REWRITE` rationale,
    # registered immediately after C4's entry in `dispatch.py` (AC19).
    "bump-outside-repo-write",
    # C13 (docs/plans/2026-08-06-apply-guard-class-census.md) -- four guard-
    # class-census band flips (CONFINEMENT_DENY -> ADVISORY_REWRITE),
    # registered at the tail of this band, ahead of the two remaining
    # PLATFORM_CONDITIONED_DENY guards below -- see dispatch.py's own
    # registration comment for the flip's full rationale, including why
    # `block-worktree-creation` (also named in the census) is deliberately
    # NOT here.
    "block-noncanonical-branch-creation",
    "block-subagent-plan-body-bash-write",
    "check-raw-pid-liveness",
]

# The two platform-conditioned guards -- `fail_closed=True` (a crash still
# fails closed) but registered LAST, after every rewrite, per the empirically
# -tested-and-reverted ordering recorded on their own guard_chain entries.
# `grep-via-bash-guard` (formerly a third member here) moved to
# ADVISORY_REWRITE_NAMES above -- H11(a), 2026-07-30.
PLATFORM_CONDITIONED_DENY_NAMES = [
    "multiprobe-banner",
    "plumbing-and-loops",
]

_EXPECTED_BAND_BY_NAME = {
    **{name: GuardBand.CONFINEMENT_DENY for name in CONFINEMENT_DENY_NAMES},
    **{name: GuardBand.ADVISORY_REWRITE for name in ADVISORY_REWRITE_NAMES},
    **{name: GuardBand.PLATFORM_CONDITIONED_DENY for name in PLATFORM_CONDITIONED_DENY_NAMES},
}


def _dummy_chain() -> List[GuardEntry]:
    """Build the real registration with harmless dummy call-time arguments.
    None of the `fn` closures are ever called here -- only `name`,
    `fail_closed`, and `band` (registration-time facts) are inspected."""
    return dispatch._build_guard_chain(
        cmd="echo bash-guard-band-membership-probe",
        session_id="band-membership-probe",
        cwd="/tmp",
        payload={"tool_name": "Bash", "tool_input": {"command": "echo x"}},
        policy_file=None,
        host_is_windows=None,
    )


def test_chain_is_readable_and_non_trivial():
    """Guard the guard: if `_build_guard_chain` stops returning entries,
    every assertion below would pass vacuously on an empty list."""
    chain = _dummy_chain()
    assert len(chain) > 10, chain
    names = [entry.name for entry in chain]
    assert "offer-git-c" in names
    assert "grep-via-bash-guard" in names


def test_every_registered_guard_carries_a_band():
    """Every entry the chain literal produces must be a `GuardEntry` whose
    `band` attribute is a real `GuardBand` member -- a guard registered
    without a band fails `GuardEntry`'s own required-positional-field
    construction (unlike `advisory_value`, `band` carries no default),
    rather than silently defaulting into a band. This is the structural
    enforcement `_build_guard_chain`'s own typing promises; this test pins
    it as an executable property, not merely a type hint."""
    chain = _dummy_chain()
    for entry in chain:
        assert isinstance(entry.band, GuardBand), (
            "%r is registered without a valid GuardBand -- a new guard "
            "MUST carry an explicit band, never inherit one by omission" % entry.name
        )


def test_all_named_guards_are_actually_registered():
    """A typo in any of the three named lists above would silently weaken
    every other assertion in this file to a no-op against a name that was
    never checked."""
    chain = _dummy_chain()
    registered = {entry.name for entry in chain}
    all_named = set(CONFINEMENT_DENY_NAMES) | set(ADVISORY_REWRITE_NAMES) | set(PLATFORM_CONDITIONED_DENY_NAMES)
    missing = all_named - registered
    assert not missing, (
        f"named in this test but not registered in the chain: {sorted(missing)} -- "
        "either the guard was renamed/removed, or this test has gone stale."
    )


def test_no_registered_guard_is_unclassified_by_this_test():
    """The mirror image of the check above: every REGISTERED guard must be
    named in exactly one of the three lists -- a new guard landing in
    `guard_chain` without updating this file is exactly the "unclassified
    guard defaults into a band silently" failure mode this whole model
    exists to close."""
    chain = _dummy_chain()
    registered = {entry.name for entry in chain}
    all_named = set(CONFINEMENT_DENY_NAMES) | set(ADVISORY_REWRITE_NAMES) | set(PLATFORM_CONDITIONED_DENY_NAMES)
    unclassified = registered - all_named
    assert not unclassified, (
        f"registered in guard_chain but not named in any band list here: {sorted(unclassified)} -- "
        "a new guard must be classified into a band explicitly, not left to default."
    )


def test_each_guard_is_in_its_expected_band():
    chain = _dummy_chain()
    wrong = []
    for entry in chain:
        expected = _EXPECTED_BAND_BY_NAME.get(entry.name)
        if expected is not None and entry.band is not expected:
            wrong.append((entry.name, entry.band, expected))
    assert not wrong, (
        "guards registered into the wrong band (name, actual, expected): %r" % wrong
    )


def test_bands_are_contiguous_and_in_fixed_sequence():
    """CONFINEMENT_DENY, then ADVISORY_REWRITE, then PLATFORM_CONDITIONED_DENY
    -- each band's entries must all sit together, and a later band's entry
    must never precede an earlier band's. This is the property that makes
    "first deny wins within a band" and "hard-deny precedes every rewrite"
    hold simultaneously without any extra bookkeeping."""
    chain = _dummy_chain()
    sequence = [GuardBand.CONFINEMENT_DENY, GuardBand.ADVISORY_REWRITE, GuardBand.PLATFORM_CONDITIONED_DENY]
    rank = {band: i for i, band in enumerate(sequence)}

    bands_in_order = [entry.band for entry in chain]
    ranks_in_order = [rank[band] for band in bands_in_order]

    assert ranks_in_order == sorted(ranks_in_order), (
        "a guard's band rank went BACKWARD relative to an earlier entry -- "
        "the three bands must be contiguous and in the fixed sequence "
        "confinement-deny -> advisory-rewrite -> platform-conditioned-deny: %r"
        % list(zip(bands_in_order, [entry.name for entry in chain]))
    )


def test_platform_conditioned_guards_are_fail_closed_but_registered_last():
    """The three platform-conditioned guards are `fail_closed=True` (a crash
    in them still denies) YET registered in the LAST band -- band membership
    is deliberately NOT derivable from `fail_closed`."""
    chain = _dummy_chain()
    by_name = {entry.name: entry for entry in chain}
    for name in PLATFORM_CONDITIONED_DENY_NAMES:
        entry = by_name[name]
        assert entry.fail_closed is True, "%r must stay fail_closed=True" % name
        assert entry.band is GuardBand.PLATFORM_CONDITIONED_DENY


def test_advisory_rewrite_guards_never_fail_closed():
    """A crash in an ADVISORY_REWRITE guard must degrade to silence, never to
    a deny.

    The converse of the test above, and the direction that actually bites:
    `fail_closed` does not follow from band membership (a
    PLATFORM_CONDITIONED_DENY guard is fail-closed while registered last),
    but an ADVISORY_REWRITE guard offers a cheaper alternative and confines
    nothing -- there is nothing for it to fail closed ON. Registering one
    with `fail_closed=True` routes its crash through `_crash_deny`, which
    denies Bash for every session on this shared tree until a human edits the
    file. That is what happened on 2026-07-30 to a peer repo's session, while
    `grep-via-bash-guard` still carried `fail_closed=True` from its
    pre-H11(a) PLATFORM_CONDITIONED_DENY registration
    (`cross-repo/inbox/2026-07-30-doe-claude-em-guard-grep-via-bash-
    nameerror.md`): an advisory-only guard, by its own docstring, taking down
    unrelated commands in sessions that were not editing it.

    The band flip fixed that instance. This pins the class, so the next
    advisory guard cannot inherit a fail-closed flag by copy-paste.
    """
    chain = _dummy_chain()
    fail_closed_advisories = [
        entry.name
        for entry in chain
        if entry.band is GuardBand.ADVISORY_REWRITE and entry.fail_closed
    ]
    assert not fail_closed_advisories, (
        "ADVISORY_REWRITE guards registered fail_closed=True: %r -- a crash in "
        "one of these denies every Bash command in every session on the tree, "
        "for a guard that only ever offered an alternative. An advisory guard "
        "that genuinely needs a fail-closed crash belongs in a deny band, with "
        "its own `_CRASH_TRIGGER_SUBSTRINGS` entry scoping the blast radius."
        % fail_closed_advisories
    )


def test_inprocess_search_precedes_every_rewrite_and_advise_entry():
    """`inprocess-search` never denies -- it only answers a search in-process
    -- but every "-rewrite"/"-advise"-suffixed guard and `probe-spray` return
    allow+updatedInput or an advisory that short-circuits the chain before
    `inprocess-search` would run if registered after them. Its own
    registration comment: registered after them, "this seam is unreachable
    for exactly the commands it exists to answer." Pinned here structurally
    rather than left to a prose comment alone."""
    chain = _dummy_chain()
    order = [entry.name for entry in chain]
    position = {name: i for i, name in enumerate(order)}

    search_pos = position["inprocess-search"]
    must_follow = [
        "probe-spray",
        "find-exec-rewrite",
        "grep-via-bash-rewrite",
        "sed-range-read-advise",
        "cat-heredoc-write-advise",
        "git-commit-safe-commit-advise",
        "multiprobe-banner-rewrite",
        "head-tail-plumbing-rewrite",
    ]
    behind = [name for name in must_follow if position[name] < search_pos]
    assert not behind, (
        "inprocess-search must precede these rewrite/advisory entries, but "
        "sits AFTER them instead (its search-answering seam is unreachable "
        "for exactly the commands it exists to answer): %r" % behind
    )


def _evaluate_with_crashing_no_verify(command):
    """Drive the real `evaluate_payload_json` with `check_no_verify` raising."""
    import json

    def _boom(*_a, **_kw):
        raise RuntimeError("synthetic crash for band-membership gate")

    orig = dispatch._dc.check_no_verify
    dispatch._dc.check_no_verify = _boom
    try:
        payload = json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": {"command": command},
                "session_id": "crash-probe",
                "cwd": "/tmp",
            }
        )
        return dispatch.evaluate_payload_json(payload)
    finally:
        dispatch._dc.check_no_verify = orig


def test_crash_deny_short_circuits_immediately():
    """`_crash_deny` must still return immediately on the first crashing
    fail_closed guard, regardless of which band it is in -- this is a
    behavioural (not merely structural) property, so it drives the real
    `evaluate_payload_json` entry point with a guard monkeypatched to raise.

    The probe command must sit INSIDE the crashed guard's own target class,
    or it exercises the out-of-class skip path below instead of this one.
    `check_no_verify` cannot deny a command with no `git` in it, so a crash
    on `echo hi` is deliberately no longer a deny (2026-07-30 blast-radius
    reversal; see `dispatch._CRASH_TRIGGER_SUBSTRINGS`).
    """
    out = _evaluate_with_crashing_no_verify("git commit -m x")

    assert out is not None
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "no-verify" in hso["permissionDecisionReason"]


def test_crash_deny_is_scoped_to_the_crashed_guard_target_class():
    """A crashing fail_closed guard must NOT deny a command it could never
    have denied anyway. This is the property whose absence denied every Bash
    call in every session on the machine -- twice in three days -- when a
    git-only guard raised on an arity mismatch."""
    assert _evaluate_with_crashing_no_verify("echo hi") is None


def test_crash_deny_trigger_runs_on_the_cr_stripped_command():
    """The trigger runs on the normalized command, not the raw one.

    `_crlf_strip` deletes `\\r` outright rather than substituting a space, so
    it is the one transform on these guards' paths that can MANUFACTURE a
    keyword the raw string does not contain: `gi\\rt commit --no-verify`
    carries no `git` substring, normalizes to one, and `check_no_verify`
    genuinely DENIES it (verified directly, not assumed). A trigger tested
    against the raw string would therefore skip a crashed guard on a command
    that guard blocks -- a real bypass on the crash path, and exactly the
    objection the original blast-radius decision raised against hand-guessed
    pre-filters. It is answered by deriving and normalizing, not waved away.

    Note the contrast that makes the normalization set exact rather than
    defensive: `_join_backslash_newlines` substitutes a SPACE (`gi\\<newline>t`
    -> `gi t`), so it cannot manufacture a keyword, and the guard allows that
    command. Only deletions matter here.
    """
    out = _evaluate_with_crashing_no_verify("gi\rt commit --no-verify -m x")

    assert out is not None, (
        "CR-joined `git` must still reach the crashed guard's target class, "
        "or the crash path is a bypass"
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_every_crash_trigger_guard_is_a_registered_fail_closed_guard():
    """A stale or misspelled key in `_CRASH_TRIGGER_SUBSTRINGS` is silently
    inert -- it would never match a guard name, so the guard it was meant to
    narrow keeps denying chain-wide and nobody finds out. Same failure shape
    the band-membership assertions above exist to catch."""
    chain = dispatch._build_guard_chain(
        "git status", "sid", "/tmp", {"tool_input": {"command": "git status"}}, None, False, None
    )
    fail_closed_names = {entry.name for entry in chain if entry.fail_closed}
    unknown = sorted(set(dispatch._CRASH_TRIGGER_SUBSTRINGS) - fail_closed_names)
    assert not unknown, (
        "_CRASH_TRIGGER_SUBSTRINGS names guards that are not registered "
        "fail_closed entries (stale or misspelled, therefore inert): %r" % unknown
    )


# ---------------------------------------------------------------------------
# The six shape/platform combinations named in the guard_chain comment's own
# "Placed AFTER (here): all six shape/platform combinations correctly
# auto-rewrite first" passage. The 304-cell confinement corpus
# (test_confinement_attack_corpus.py) exercises confinement SHAPES and would
# not, on its own, catch a rewrite-vs-deny PLATFORM regression here -- each
# of the three platform-conditioned guards paired with its own upstream
# rewrite, on both host_is_windows=True and host_is_windows=False.
# ---------------------------------------------------------------------------

_SIX_COMBINATIONS = [
    ("grep-via-bash-guard / grep-via-bash-rewrite", 'grep -rn "TODO" .', False),
    ("grep-via-bash-guard / grep-via-bash-rewrite", 'grep -rn "TODO" .', True),
    (
        "multiprobe-banner / multiprobe-banner-rewrite",
        'echo "=== SESSION FACTS ==="; pwd; whoami; date',
        False,
    ),
    (
        "multiprobe-banner / multiprobe-banner-rewrite",
        'echo "=== SESSION FACTS ==="; pwd; whoami; date',
        True,
    ),
    ("plumbing-and-loops / head-tail-plumbing-rewrite", "find . -name '*.py' | head -n 5", False),
    ("plumbing-and-loops / head-tail-plumbing-rewrite", "find . -name '*.py' | head -n 5", True),
]


@pytest.mark.parametrize("label,cmd,host_is_windows", _SIX_COMBINATIONS)
def test_rewrite_band_wins_over_platform_conditioned_deny(label, cmd, host_is_windows, monkeypatch, tmp_path):
    """For each of the three platform-conditioned guards' own target shape,
    on EACH platform, the earlier-registered rewrite must fire first (an
    auto-rewrite, never a deny) -- proving the platform-conditioned band
    genuinely runs after, and is genuinely shadowed by, the rewrite band for
    every shape/platform cell it was moved to the tail to cover.

    `inprocess-search` (also ADVISORY_REWRITE, registered ahead of the
    "-rewrite"-suffixed entries) legitimately intercepts the grep cell before
    `grep-via-bash-rewrite` ever runs -- correct chain behavior, but not the
    seam this test targets, so it is disabled here via its own documented
    override to isolate rewrite-vs-platform-conditioned-deny ordering."""
    import json
    import tempfile

    monkeypatch.setenv("COORDINATOR_DISABLE_INPROCESS_SEARCH", "1")
    # `probe-spray` keeps its ring buffer in a temp-dir file keyed by session
    # id, and that file OUTLIVES the pytest process. The unique-session-id
    # measure below isolates cells from each other within one run, but not
    # this run from the previous one: re-running this file appends the same
    # command hash to the same ring file, and on the third run the recurrence
    # threshold trips and probe-spray's advisory short-circuits the chain
    # before the seam under test is reached. These six cells then fail with a
    # message about the rewrite not firing, which is not what went wrong.
    # Pointing `tempfile.tempdir` at pytest's per-test tmp_path gives every
    # cell a fresh state dir, so the assertion depends only on the chain.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    # A unique session id per cell -- `probe-spray`'s own ring-buffer
    # recurrence check fires on an EXACT-repeated command shape within one
    # session, and several cells here intentionally reuse the same command
    # text across the host_is_windows=True/False pair. A shared session id
    # would trip that unrelated advisory guard and short-circuit the chain
    # before it ever reaches the seam this test targets.
    session_id = "six-combo-probe-%s-%s" % (label, host_is_windows)
    payload = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "session_id": session_id,
            "cwd": "/tmp",
        }
    )
    out = dispatch.evaluate_payload_json(payload, host_is_windows=host_is_windows)
    assert out is not None, "%s (windows=%s): expected a rewrite, got silent allow" % (label, host_is_windows)
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow", (
        "%s (windows=%s): expected an auto-rewrite (allow+updatedInput), got a %s -- "
        "the platform-conditioned guard fired BEFORE the rewrite band, which is the "
        "exact regression this ordering exists to prevent"
        % (label, host_is_windows, hso["permissionDecision"])
    )
    assert "updatedInput" in hso, (
        "%s (windows=%s): allow with no updatedInput -- expected the rewrite band's "
        "auto-rewrite to have already fired" % (label, host_is_windows)
    )
