"""A roster with no review-wave `named` entry composes without engine-derived reviewers, never refuses.

DoE's review-wave roster (schema_version 6, dc548166dd) carries no `named` stage, so a UE repo's
compose must not demand staff-game-dev or docs-checker entries it cannot have.
"""
from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_compose import _roster_has_named_wave_entry


def _roster(*agents):
    return {"execute_review": {"stages": [{"kind": "review-wave", "agents": list(agents)}]}}


def test_named_entry_is_detected():
    assert _roster_has_named_wave_entry(_roster({"type": "x", "accepts_signals": "named"}))


def test_v6_roster_without_named_entry_is_not():
    assert not _roster_has_named_wave_entry(_roster({"type": "coordinator:staff-eng"}, {"accepts_signals": "front-end"}))


def test_missing_or_malformed_roster_is_not():
    for fragment in (None, {}, {"execute_review": {"stages": ["junk", {"kind": "prep"}]}}):
        assert not _roster_has_named_wave_entry(fragment)
