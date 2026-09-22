"""Tests for the in-session operator unlock intercept in
`coordinator_core.bash_guards.dispatch.evaluate_payload_json` — the bash leg
of C3.

Exercises the real registered guard chain end-to-end (not a mocked guard),
using `block-dev-repo-sentinel-removal` as the live target for AC8's
general bash-leg slice: absent-sentinel deny, present-sentinel one-shot
grant, per-guard isolation, per-session isolation, and fail-closed on an
unresolvable session id. AC4 (the sentinel-REMOVAL guards get the unlock
exactly like the content-edit guards, no exemption list) is covered
separately, against a different guard -- see
`TestRemovalLegGuardGrantsUnderTheSameMechanism` below.

CLASS-CENSUS NOTE (2026-08-06, `docs/plans/2026-08-06-apply-guard-class-
census.md`, C13/C14e): `block-dev-repo-sentinel-removal`'s CONFINEMENT_DENY
registration was retired -- its sole registered leg
(`block-dev-repo-sentinel-removal-advisory`) now returns an ALLOW+
additionalContext advisory for every input that used to trip the deny leg,
never `permissionDecision == "deny"`. The unlock-consumption/annotation
path in `dispatch.py` gates strictly on `_is_hard_deny_envelope`, so for
THIS guard it is now permanently unreachable: no sentinel is ever
consumed, no annotation is ever appended, because there is no longer a
hard deny to grant past. The classes below are updated to assert that
(structurally correct, not a relaxation) -- AC4's own claim is retargeted
to `block-stash-destruction` (`git stash drop`/`clear`) instead: still
CONFINEMENT_DENY, never identity-gated, and registered ahead of every
subagent-identity-gated guard in the chain (`dispatch.py`'s own ordering
comment above that entry), so nothing upstream intercepts the command
first.

A hook envelope that is merely non-`None` is NOT necessarily a deny —
`block-dev-repo-sentinel-removal` itself can return an ALLOW+
additionalContext advisory envelope for unexaminable indirection — so every
assertion here checks `permissionDecision` explicitly rather than
`out is not None`.

Isolation discipline: `tempfile.gettempdir` is monkeypatched to `tmp_path`
for every test in this module (autouse fixture) so a failed test can never
leave a live unlock sentinel in the real platform temp dir.

Spec backlink: pln-in-session-operator-unlock-for-aa6cf9 § C3/C6.
"""

from __future__ import annotations

import json
import tempfile

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.session import guard_unlock_sentinel as gus


SENTINEL_BASENAME = ".coordinator-dev-repo"
GUARD_NAME = "block-dev-repo-sentinel-removal"


@pytest.fixture(autouse=True)
def _isolated_tempdir(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    yield


def _payload(session_id, command=None):
    p = {
        "tool_name": "Bash",
        "tool_input": {"command": command or ("rm %s" % SENTINEL_BASENAME)},
        "cwd": "/repo",
    }
    if session_id is not None:
        p["session_id"] = session_id
    return p


def _decision(payload):
    out = dispatch.evaluate_payload_json(json.dumps(payload))
    if out is None:
        return "allow"
    return out.get("hookSpecificOutput", {}).get("permissionDecision")


class TestAbsentSentinelDenies:
    """Was "the most important test in the set" while this guard was a
    hard deny; now permanently "allow" (see this module's own "CLASS-
    CENSUS NOTE" docstring section) -- the guard advises, it never denies,
    so an absent unlock sentinel has nothing to gate."""

    def test_no_sentinel_denies(self):
        assert _decision(_payload("sess-1")) == "allow"


class TestPresentSentinelGrantsOnce:
    """No hard-deny envelope for this guard means the unlock-consumption
    path (`dispatch.py`, gated on `_is_hard_deny_envelope`) is never
    reached for it -- a sentinel on disk is left untouched, not consumed."""

    def test_sentinel_allows(self):
        gus.sentinel_path("sess-1", GUARD_NAME).write_text("", encoding="utf-8")
        assert _decision(_payload("sess-1")) == "allow"

    def test_one_shot_re_denies_on_immediate_retry(self):
        gus.sentinel_path("sess-1", GUARD_NAME).write_text("", encoding="utf-8")
        assert _decision(_payload("sess-1")) == "allow"
        assert _decision(_payload("sess-1")) == "allow"

    def test_sentinel_file_is_consumed_from_disk(self):
        p = gus.sentinel_path("sess-1", GUARD_NAME)
        p.write_text("", encoding="utf-8")
        dispatch.evaluate_payload_json(json.dumps(_payload("sess-1")))
        assert p.exists()


class TestPerGuardIsolation:
    def test_sentinel_for_a_different_guard_does_not_clear_this_one(self):
        gus.sentinel_path("sess-1", "no-verify").write_text("", encoding="utf-8")
        assert _decision(_payload("sess-1")) == "allow"


class TestPerSessionIsolation:
    def test_peer_session_sentinel_does_not_clear_ours(self):
        gus.sentinel_path("peer-session", GUARD_NAME).write_text("", encoding="utf-8")
        assert _decision(_payload("sess-1")) == "allow"


class TestUnresolvableSessionIdFailsClosed:
    def test_missing_session_id_denies_even_with_a_sentinel_on_disk(self):
        gus.sentinel_path("", GUARD_NAME).write_text("", encoding="utf-8")
        assert _decision(_payload(None)) == "allow"

    def test_empty_string_session_id_denies(self):
        gus.sentinel_path("", GUARD_NAME).write_text("", encoding="utf-8")
        assert _decision(_payload("")) == "allow"


#: Still CONFINEMENT_DENY and never identity-gated (module docstring
#: "CLASS-CENSUS NOTE") -- `git stash drop`/`clear` is the AC4 exemplar
#: `block-dev-repo-sentinel-removal` can no longer serve now that its own
#: registration is ADVISORY_REWRITE. `block_stash_destruction.py`'s own
#: "DELIBERATE ALLOW-LIST" restricts the deny to `drop`/`clear` only.
STASH_GUARD_NAME = "block-stash-destruction"
STASH_DROP_CMD = "git stash drop"


class TestRemovalLegGuardGrantsUnderTheSameMechanism:
    """AC4 — the removal-leg cohort is not exempt from the unlock, in
    principle; `block-dev-repo-sentinel-removal` can no longer exercise
    that claim itself post-conversion (see this module's own "CLASS-CENSUS
    NOTE"). `block-stash-destruction` is used here instead -- still
    CONFINEMENT_DENY, never identity-gated, and registered ahead of every
    subagent-identity-gated guard in the chain, so `git stash drop` reaches
    it undisturbed."""

    def test_denies_without_sentinel(self):
        assert _decision(_payload("sess-1", command=STASH_DROP_CMD)) == "deny"

    def test_grants_once_then_re_denies_on_immediate_retry(self):
        gus.sentinel_path("sess-1", STASH_GUARD_NAME).write_text("", encoding="utf-8")
        assert _decision(_payload("sess-1", command=STASH_DROP_CMD)) == "allow"
        assert _decision(_payload("sess-1", command=STASH_DROP_CMD)) == "deny"


class TestIndirectionLegIsAllowNotDeny:
    """The exact
    scenario motivating gating on `permissionDecision == "deny"` rather
    than `fail_closed` alone: `block-dev-repo-sentinel-removal` is
    `fail_closed=True` (CONFINEMENT_DENY) yet returns an ALLOW+
    additionalContext advisory envelope for genuinely unexaminable
    indirection (module docstring "POSTURE"). A sentinel present at that
    moment must be left untouched — the gate must never mistake this leg
    for a deny to unlock."""

    def test_unparseable_indirection_allows_and_leaves_sentinel_unconsumed(self):
        # An unbalanced-quote command that only TEXTUALLY mentions the
        # sentinel basename -- one of `SentinelRemovalDetector`'s
        # documented indirection triggers. A plain `xargs`/interpreter
        # indirection shape is unsuitable here: it trips the earlier
        # `block-approval-sentinel-creation`/`block-worktree-sentinel-
        # creation` guards' own outright, content-independent xargs deny
        # first (both precede this guard in the chain), so this leg is
        # only bash-leg-reachable via the unparseable-shell-shape trigger.
        cmd = "rm '%s" % SENTINEL_BASENAME
        sentinel = gus.sentinel_path("sess-1", GUARD_NAME)
        sentinel.write_text("", encoding="utf-8")
        assert _decision(_payload("sess-1", command=cmd)) == "allow"
        assert sentinel.exists()


class TestUnlockNotConsumedWhenSuppressionWouldHaveAllowedAnyway:
    """A one-shot grant
    for a `PLATFORM_CONDITIONED_DENY` guard must never be burned on a deny
    the agent would never have seen because host-default suppression would
    have turned it into an allow anyway. `plumbing-and-loops` is
    `AdvisoryValue.WINDOWS_COST_ONLY`: on a non-Windows host its own
    `check()` returns an ALLOW+advisory envelope directly (never a
    `permissionDecision == "deny"` envelope), so `_is_hard_deny_envelope`
    is `False` for it here and the unlock path is never even reached —
    this test pins that a sentinel for this guard survives a non-Windows
    dispatch untouched."""

    def test_sentinel_survives_non_windows_dispatch_of_suppressible_guard(self):
        sentinel = gus.sentinel_path("sess-1", "plumbing-and-loops")
        sentinel.write_text("", encoding="utf-8")
        payload = _payload("sess-1", command="cat some_file.txt | head -50")
        out = dispatch.evaluate_payload_json(
            json.dumps(payload), host_is_windows=False
        )
        # Non-Windows: never a hard deny for this guard, so the grant for
        # it must be left untouched, one-shot-preserved for an actual
        # future deny.
        if out is not None:
            assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
        assert sentinel.exists()
