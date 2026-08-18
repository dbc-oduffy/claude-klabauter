"""The named warm-engine behavioral tier — before this file, the whole test
tree had zero occurrences of warm-engine / warm_engine / multi-op, so any
warm coverage that existed was purely incidental. Registers `warm_tier`
(pyproject.toml) and anchors the four behaviors the warm-engine premise
depends on, so future warm-server chunks (C14-C18) land against a named
target instead of prose.

Each behavior below is skipped, naming the chunk that supplies its subject
module, until that chunk's module exists in this tree — a skip that names
its blocker, never a green test standing in for coverage that isn't there
yet.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C20
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.warm_tier


def test_overlapping_dispatch_keeps_distinct_identities():
    """Two concurrent dispatches through the warm server must not
    cross-contaminate each other's declared writes or results — the
    multi-op isolation property the whole warm-engine premise rests on.
    """
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
    """A hung op on one connection must not block a fresh dispatch on
    another — the server-side isolation the election/lifecycle machinery
    (C14, C17) is responsible for maintaining under one wedged worker.
    """
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
    """A version-token mismatch between client and server must evict the
    stale server under real concurrent traffic, not merely in isolation —
    C16's own subject.
    """
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
