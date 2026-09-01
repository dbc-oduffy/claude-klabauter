# test_name_ladder — pins coordinator_core.session.name_ladder.resolve_name
# in isolation, plus the DRIFT-IMPOSSIBLE property this extraction exists
# for: session-claim-cli.py's `_render_claimant_name` and dispatch_checks.
# py's `_resolve_owner_writer_name` both delegate rung/reason resolution to
# this module, so they cannot answer differently for the same input again
# (state/debt-backlog/2026-09-01-shared-name-resolution-ladder-for-sessio-
# 026b33fcd43d.yaml). Each surface's own RENDERING (markers, prose, byte
# budget) is pinned by its own test suite, not here.
from __future__ import annotations

import pytest

from coordinator_core.session import name_ladder


class _Record:
    def __init__(self, name):
        self.name = name


def test_rung1_recorded_name_wins_without_calling_lookup():
    def _boom(sid):
        raise AssertionError("rung 2 must not be consulted when rung 1 resolves")

    name, rung, reason = name_ladder.resolve_name("alice", "sid-1", _boom)
    assert (name, rung, reason) == ("alice", name_ladder.RUNG_RECORDED, None)


def test_rung2_live_lookup_when_no_recorded_name():
    name, rung, reason = name_ladder.resolve_name(
        None, "sid-2", lambda sid: _Record("bob")
    )
    assert (name, rung, reason) == ("bob", name_ladder.RUNG_LIVE_LOOKUP, None)


def test_rung3_no_registry_record():
    name, rung, reason = name_ladder.resolve_name(None, "sid-3", lambda sid: None)
    assert (name, rung, reason) == (
        None,
        name_ladder.RUNG_UNRESOLVED,
        name_ladder.REASON_NO_REGISTRY_RECORD,
    )


def test_rung3_lookup_raises_degrades_never_propagates():
    def _raise(sid):
        raise RuntimeError("registry unavailable")

    name, rung, reason = name_ladder.resolve_name(None, "sid-4", _raise)
    assert (name, rung, reason) == (
        None,
        name_ladder.RUNG_UNRESOLVED,
        name_ladder.REASON_LOOKUP_FAILED,
    )


def test_rung3_record_resolves_but_carries_no_name():
    name, rung, reason = name_ladder.resolve_name(
        None, "sid-5", lambda sid: _Record(None)
    )
    assert (name, rung, reason) == (
        None,
        name_ladder.RUNG_UNRESOLVED,
        name_ladder.REASON_UNNAMED_RECORD,
    )


@pytest.mark.parametrize(
    "recorded_name,lookup,expected",
    [
        ("carol", lambda sid: (_ for _ in ()).throw(AssertionError("unreachable")),
         ("carol", name_ladder.RUNG_RECORDED, None)),
        (None, lambda sid: _Record("dave"),
         ("dave", name_ladder.RUNG_LIVE_LOOKUP, None)),
        (None, lambda sid: None,
         (None, name_ladder.RUNG_UNRESOLVED, name_ladder.REASON_NO_REGISTRY_RECORD)),
    ],
)
def test_both_surfaces_agree_on_rung_and_reason_for_the_same_input(
    recorded_name, lookup, expected
):
    """The deliverable this extraction exists for: exercise the SAME shared
    resolver both `session-claim-cli._render_claimant_name` and
    `dispatch_checks._resolve_owner_writer_name` now delegate to, for a
    matrix of inputs spanning all three rungs, and assert one answer -- the
    two call sites can no longer independently decide a rung or a rung-3
    reason, because there is only one resolver deciding either."""
    result = name_ladder.resolve_name(recorded_name, "sid-shared", lookup)
    assert result == expected
