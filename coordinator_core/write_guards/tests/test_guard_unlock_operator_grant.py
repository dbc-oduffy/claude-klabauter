"""Tests for the in-session operator unlock intercept in
`coordinator_core.write_guards.engine.evaluate` — the write leg of C2.

Exercises the real hard-deny cohort end-to-end through `engine.evaluate`
(not a mocked guard), using `block_worktree_sentinel_write` as the live
target guard — same shape as the working smoke script this test suite is
modeled on. Covers AC8's write-leg slice: absent-sentinel deny, present-
sentinel one-shot grant, per-guard isolation, per-session isolation, and
fail-closed on an unresolvable session id.

`block_worktree_sentinel_write` replaces the original `block_dev_repo_
sentinel_write` target guard (2026-08-06, docs/plans/2026-08-06-apply-
guard-class-census.md C12 reclassified the latter to CLASS = "advisory").
An advisory guard never reaches the hard-deny phase in `engine.evaluate`,
so it no longer consults the unlock sentinel at all — the property this
module exists to prove (absent/present/one-shot/per-guard/per-session/
fail-closed unlock semantics) can only be observed through a guard that is
still `CLASS = "hard-deny"`. `block_worktree_sentinel_write` is exactly
that: same `_sentinel_write_guard` mechanism, same shape, untouched by the
census flip. `TestAdvisoryGuardNeverConsultsUnlock` below separately pins
the now-advisory guard's own behavior so that flip stays covered too.

A hook envelope that is merely non-`None` is NOT necessarily a deny — the
advisory phase also returns an envelope — so every assertion here checks
`out["hookSpecificOutput"]["permissionDecision"]` explicitly rather than
`out is not None`.

Isolation discipline: `tempfile.gettempdir` is monkeypatched to `tmp_path`
for every test in this module (autouse fixture) so a failed test can never
leave a live unlock sentinel in the real platform temp dir.

Spec backlink: docs/plans/2026-08-03-in-session-operator-unlock-for-the-hard-.md § C2/C6.
"""

from __future__ import annotations

import tempfile

import pytest

from coordinator_core.session import guard_unlock_sentinel as gus
from coordinator_core.write_guards import engine


SENTINEL_BASENAME = ".coordinator-override-worktree-guard"
GUARD_NAME = "block_worktree_sentinel_write"
ADVISORY_SENTINEL_BASENAME = ".coordinator-dev-repo"
ADVISORY_GUARD_NAME = "block_dev_repo_sentinel_write"


@pytest.fixture(autouse=True)
def _isolated_tempdir(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    yield


def _payload(session_id, tool_name="Write", file_path=None):
    p = {
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path or "/repo/%s" % SENTINEL_BASENAME},
    }
    if session_id is not None:
        p["session_id"] = session_id
    return p


def _is_deny(out):
    if out is None:
        return False
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


class TestAbsentSentinelDenies:
    """The most important test in the set — it is the one that fails
    loudly if someone later widens the unlock by accident."""

    def test_no_sentinel_denies(self):
        out = engine.evaluate(_payload("sess-1"))
        assert _is_deny(out)


class TestPresentSentinelGrantsOnce:
    def test_sentinel_allows(self):
        gus.sentinel_path("sess-1", GUARD_NAME).write_text("", encoding="utf-8")
        out = engine.evaluate(_payload("sess-1"))
        assert not _is_deny(out)

    def test_one_shot_re_denies_on_immediate_retry(self):
        gus.sentinel_path("sess-1", GUARD_NAME).write_text("", encoding="utf-8")
        first = engine.evaluate(_payload("sess-1"))
        assert not _is_deny(first)
        second = engine.evaluate(_payload("sess-1"))
        assert _is_deny(second)

    def test_sentinel_file_is_consumed_from_disk(self):
        p = gus.sentinel_path("sess-1", GUARD_NAME)
        p.write_text("", encoding="utf-8")
        engine.evaluate(_payload("sess-1"))
        assert not p.exists()


class TestPerGuardIsolation:
    def test_sentinel_for_a_different_guard_does_not_clear_this_one(self):
        gus.sentinel_path("sess-1", "block_tracker_edit").write_text("", encoding="utf-8")
        out = engine.evaluate(_payload("sess-1"))
        assert _is_deny(out)


class TestPerSessionIsolation:
    def test_peer_session_sentinel_does_not_clear_ours(self):
        gus.sentinel_path("peer-session", GUARD_NAME).write_text("", encoding="utf-8")
        out = engine.evaluate(_payload("sess-1"))
        assert _is_deny(out)


class TestUnresolvableSessionIdFailsClosed:
    def test_missing_session_id_denies_even_with_a_sentinel_on_disk(self):
        # Sentinel keyed to the empty-string session id -- if the engine
        # ever coerced a missing session_id to "", this would wrongly grant.
        gus.sentinel_path("", GUARD_NAME).write_text("", encoding="utf-8")
        out = engine.evaluate(_payload(None))
        assert _is_deny(out)

    def test_empty_string_session_id_denies(self):
        gus.sentinel_path("", GUARD_NAME).write_text("", encoding="utf-8")
        out = engine.evaluate(_payload(""))
        assert _is_deny(out)


class TestAdvisoryGuardNeverConsultsUnlock:
    """`block_dev_repo_sentinel_write` (2026-08-06 census flip, C12) is now
    `CLASS = "advisory"` and never reaches `engine.evaluate`'s hard-deny
    phase, so it never calls `guard_unlock_sentinel.consume` — an unlock
    sentinel written for it is inert and is never touched by evaluation.
    This is the flip's actual, intended effect; it is not a fail-closed
    regression to guard against, since the property "an unresolvable
    session id must not grant" only has meaning for a guard whose outcome
    an unlock sentinel could otherwise change."""

    def _advisory_payload(self, session_id):
        return _payload(
            session_id,
            file_path="/repo/%s" % ADVISORY_SENTINEL_BASENAME,
        )

    def test_fires_as_advisory_regardless_of_sentinel(self):
        out = engine.evaluate(self._advisory_payload("sess-1"))
        assert not _is_deny(out)
        assert out is not None
        assert (
            out.get("hookSpecificOutput", {}).get("permissionDecision")
            is None
        )

    def test_unlock_sentinel_for_the_advisory_guard_is_left_untouched(self):
        p = gus.sentinel_path("sess-1", ADVISORY_GUARD_NAME)
        p.write_text("", encoding="utf-8")
        engine.evaluate(self._advisory_payload("sess-1"))
        assert p.exists()
