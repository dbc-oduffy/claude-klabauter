"""Tests for coordinator_core.write_guards._subagent_identity.

Covers ``_resolve_subagent_identity``'s four branches, with particular focus
on branch (d) — the already-canonical ``<name>@session-<short>`` shape folded
in from ``coordinator_core.hooks.track_touched_files``'s copy (C9,
docs/plans/2026-08-25-the-touched-files-record-gets-a-designed-shape.md).
Before this fold, branch (d) fail-closed to ``""`` here even though the
harness hands back exactly this shape verbatim on dispatch, and
``normalize_teammate_agent_id`` already existed to rebuild it against the
live session.
"""

from __future__ import annotations

from coordinator_core.write_guards._subagent_identity import (
    _resolve_subagent_identity,
)


class TestBareHex:
    def test_bare_hex_returned_unchanged(self) -> None:
        assert _resolve_subagent_identity("a" * 12, "") == "a" * 12

    def test_bare_hex_ignores_session_id(self) -> None:
        agent_id = "0123456789abcdef"
        assert _resolve_subagent_identity(agent_id, "short") == agent_id


class TestNamedTeammate:
    def test_named_teammate_builds_canonical_id(self) -> None:
        agent_id = "aprobe2-teammate-64cd7f42c270a899"
        session_id = "deadbeef1234"
        assert (
            _resolve_subagent_identity(agent_id, session_id)
            == "probe2-teammate@session-deadbeef"
        )

    def test_named_teammate_fails_closed_on_short_session_id(self) -> None:
        agent_id = "aprobe2-teammate-64cd7f42c270a899"
        assert _resolve_subagent_identity(agent_id, "short") == ""


class TestAlreadyCanonicalTeammate:
    """Branch (d) — the fold-in this chunk performs."""

    def test_rebuilds_against_live_session_when_short_stale(self) -> None:
        agent_id = "probe2-teammate@session-11111111"
        live_session_id = "22222222abcdef"
        assert (
            _resolve_subagent_identity(agent_id, live_session_id)
            == "probe2-teammate@session-22222222"
        )

    def test_unchanged_when_embedded_short_already_matches_live(self) -> None:
        agent_id = "probe2-teammate@session-22222222"
        live_session_id = "22222222abcdef"
        assert _resolve_subagent_identity(agent_id, live_session_id) == agent_id

    def test_unchanged_when_live_session_id_too_short(self) -> None:
        agent_id = "probe2-teammate@session-11111111"
        assert _resolve_subagent_identity(agent_id, "short") == agent_id

    def test_does_not_fail_closed_to_empty_string(self) -> None:
        agent_id = "probe2-teammate@session-11111111"
        live_session_id = "22222222abcdef"
        result = _resolve_subagent_identity(agent_id, live_session_id)
        assert result != ""


class TestUnrecognisedShape:
    def test_unrecognised_shape_fails_closed(self) -> None:
        assert _resolve_subagent_identity("not-a-known-shape", "deadbeef1234") == ""

    def test_empty_agent_id_fails_closed(self) -> None:
        assert _resolve_subagent_identity("", "deadbeef1234") == ""
