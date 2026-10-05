"""Tests for the in-session operator unlock intercept in
`coordinator_core.bash_guards.dispatch.evaluate_payload_json` — the bash leg
of C3.

Exercises the real registered guard chain end-to-end (not a mocked guard),
using `block-dev-repo-sentinel-removal` as the live target for AC8's
general bash-leg slice: absent-sentinel deny, present-sentinel one-shot
grant, per-guard isolation, per-session isolation, and fail-closed on an
unresolvable session id.

AC4 (the sentinel-REMOVAL guards get the unlock exactly like the content-edit
guards, no exemption list) is also exercised against `block-stash-destruction`.

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

    def test_no_sentinel_denies(self):
        assert _decision(_payload("sess-1")) == "deny"


class TestPresentSentinelGrantsOnce:

    def test_sentinel_allows(self):
        gus.sentinel_path("sess-1", GUARD_NAME).write_text("", encoding="utf-8")
        assert _decision(_payload("sess-1")) == "allow"

    def test_one_shot_re_denies_on_immediate_retry(self):
        gus.sentinel_path("sess-1", GUARD_NAME).write_text("", encoding="utf-8")
        assert _decision(_payload("sess-1")) == "allow"
        assert _decision(_payload("sess-1")) == "deny"

    def test_sentinel_file_is_consumed_from_disk(self):
        p = gus.sentinel_path("sess-1", GUARD_NAME)
        p.write_text("", encoding="utf-8")
        dispatch.evaluate_payload_json(json.dumps(_payload("sess-1")))
        assert not p.exists()


class TestPerGuardIsolation:
    def test_sentinel_for_a_different_guard_does_not_clear_this_one(self):
        gus.sentinel_path("sess-1", "no-verify").write_text("", encoding="utf-8")
        assert _decision(_payload("sess-1")) == "deny"


class TestPerSessionIsolation:
    def test_peer_session_sentinel_does_not_clear_ours(self):
        gus.sentinel_path("peer-session", GUARD_NAME).write_text("", encoding="utf-8")
        assert _decision(_payload("sess-1")) == "deny"


class TestUnresolvableSessionIdFailsClosed:
    def test_missing_session_id_denies_even_with_a_sentinel_on_disk(self):
        gus.sentinel_path("", GUARD_NAME).write_text("", encoding="utf-8")
        assert _decision(_payload(None)) == "deny"

    def test_empty_string_session_id_denies(self):
        gus.sentinel_path("", GUARD_NAME).write_text("", encoding="utf-8")
        assert _decision(_payload("")) == "deny"


#: `git stash drop`/`clear` is a second CONFINEMENT_DENY exemplar, never identity-gated.
STASH_GUARD_NAME = "block-stash-destruction"
STASH_DROP_CMD = "git stash drop"


class TestRemovalLegGuardGrantsUnderTheSameMechanism:
    """AC4 -- the removal-leg cohort is not exempt from the unlock."""

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
        if out is not None:
            assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
        assert sentinel.exists()
