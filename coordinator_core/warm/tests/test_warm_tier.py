
from __future__ import annotations

import pytest

pytestmark = pytest.mark.warm_tier


def test_overlapping_dispatch_keeps_distinct_identities():
    pytest.importorskip(
        "coordinator_core.warm.election",
        reason="C14 (PIPEWARDEN election) not yet landed",
    )
    pytest.importorskip(
        "coordinator_core.warm.client",
        reason="C15 (client preamble: connect, error-class table, cold fallback) not yet landed",
    )
    pytest.skip(
        "awaiting C14/C15: no warm server + client to dispatch two "
        "overlapping requests against"
    )


def test_wedged_op_does_not_stall_the_next():
    pytest.importorskip(
        "coordinator_core.warm.election",
        reason="C14 (PIPEWARDEN election) not yet landed",
    )
    pytest.importorskip(
        "coordinator_core.warm.lifecycle",
        reason="C17 (drain/exit epilogue, idle reclaim timer) not yet landed",
    )
    pytest.skip(
        "awaiting C14/C17: no election-owned listener + lifecycle drain "
        "to isolate a wedged op from a concurrent dispatch"
    )


def test_skew_eviction_under_concurrent_clients():
    pytest.importorskip(
        "coordinator_core.warm.skew",
        reason="C16 (version tokens and skew eviction) not yet landed",
    )
    pytest.skip(
        "awaiting C16: coordinator_core.warm.skew has no eviction policy "
        "to exercise under concurrent clients yet"
    )


def test_cold_fallback_under_every_warm_failure():
    """Every failure the warm path can produce -- no pipe, connect refused,
    a version-skew eviction, a wedged read -- must fall back to cold, and
    must never wait for a server to boot (design change 2026-08-15,
    START_DEADLINE deleted not shortened). C15's own subject.
    """
    pytest.importorskip(
        "coordinator_core.warm.client",
        reason="C15 (client preamble: connect, error-class table, cold fallback) not yet landed",
    )
    pytest.skip(
        "awaiting C15: coordinator_core.warm.client has no error-class "
        "table / cold-fallback path to exercise yet"
    )
