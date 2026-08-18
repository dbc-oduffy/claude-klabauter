"""
Tests for coordinator_core.ops.session.warm_start.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C25.

Exercises `warm_start()` (the sync core `_handler` composes) with the three
callees it wires — `is_warm_enabled`, `should_spawn`, `spawn_detached` — each
monkeypatched at their point of use in `warm_start.py`'s own namespace (the
`from X import Y` binding this module holds a local reference to), never at
their defining module, matching this repo's own convention elsewhere (e.g.
`warm/tests/test_client_fallback.py`).
"""

from __future__ import annotations

import asyncio

import pytest

from coordinator_core.ops.session import warm_start


def test_off_by_default_never_spawns(monkeypatch):
    """Warm disabled -> no debounce check, no spawn attempt."""
    monkeypatch.setattr(warm_start, "is_warm_enabled", lambda: False)

    def _boom_should_spawn(*_a, **_kw):
        raise AssertionError("should_spawn must not be consulted when warm is off")

    def _boom_spawn(*_a, **_kw):
        raise AssertionError("spawn_detached must not be called when warm is off")

    monkeypatch.setattr(warm_start, "should_spawn", _boom_should_spawn)
    monkeypatch.setattr(warm_start, "spawn_detached", _boom_spawn)

    assert warm_start.warm_start() is False


def test_enabled_and_debounced_skips_spawn():
    """Warm enabled but a live breadcrumb vouches for an in-flight spawn ->
    no second spawn (the idempotency this chunk is required to prove)."""
    spawned = []

    def _spawn(*_a, **_kw):
        spawned.append(True)
        return True

    import coordinator_core.ops.session.warm_start as mod

    orig_enabled = mod.is_warm_enabled
    orig_should_spawn = mod.should_spawn
    orig_spawn_detached = mod.spawn_detached
    try:
        mod.is_warm_enabled = lambda: True
        mod.should_spawn = lambda *_a, **_kw: False
        mod.spawn_detached = _spawn

        assert mod.warm_start() is False
        assert spawned == []
    finally:
        mod.is_warm_enabled = orig_enabled
        mod.should_spawn = orig_should_spawn
        mod.spawn_detached = orig_spawn_detached


def test_enabled_and_not_debounced_spawns_exactly_once():
    """Warm enabled and no live breadcrumb -> exactly one spawn attempt."""
    spawned = []

    def _spawn(repo_root, script):
        spawned.append((repo_root, script))
        return True

    import coordinator_core.ops.session.warm_start as mod

    orig_enabled = mod.is_warm_enabled
    orig_should_spawn = mod.should_spawn
    orig_spawn_detached = mod.spawn_detached
    try:
        mod.is_warm_enabled = lambda: True
        mod.should_spawn = lambda *_a, **_kw: True
        mod.spawn_detached = _spawn

        assert mod.warm_start() is True
        assert len(spawned) == 1
        assert spawned[0][1] == mod.SERVER_ENTRY_SCRIPT
    finally:
        mod.is_warm_enabled = orig_enabled
        mod.should_spawn = orig_should_spawn
        mod.spawn_detached = orig_spawn_detached


def test_n_concurrent_sessions_produce_at_most_one_spawn():
    """N simulated concurrent SessionStart triggers, sharing one breadcrumb
    state, produce at most ONE spawn -- idempotency by construction (module
    docstring). The first call's spawn flips a shared `spawned` flag that a
    faithful `should_spawn` stand-in consults for every subsequent call,
    mirroring the real breadcrumb's own alive-pid debounce."""
    import coordinator_core.ops.session.warm_start as mod

    state = {"spawned": False}

    def _fake_should_spawn(*_a, **_kw):
        return not state["spawned"]

    def _fake_spawn_detached(*_a, **_kw):
        state["spawned"] = True
        return True

    orig_enabled = mod.is_warm_enabled
    orig_should_spawn = mod.should_spawn
    orig_spawn_detached = mod.spawn_detached
    try:
        mod.is_warm_enabled = lambda: True
        mod.should_spawn = _fake_should_spawn
        mod.spawn_detached = _fake_spawn_detached

        results = [mod.warm_start() for _ in range(5)]
        assert results == [True, False, False, False, False]
    finally:
        mod.is_warm_enabled = orig_enabled
        mod.should_spawn = orig_should_spawn
        mod.spawn_detached = orig_spawn_detached


def test_never_raises_when_a_callee_raises():
    """FAIL OPEN unconditionally: an exception from any composed callee is
    swallowed, never propagated -- a SessionStart hook that raises greets
    every session in the fleet with a stack trace."""
    import coordinator_core.ops.session.warm_start as mod

    orig_enabled = mod.is_warm_enabled
    try:
        def _boom():
            raise RuntimeError("registry unreadable")

        mod.is_warm_enabled = _boom
        assert mod.warm_start() is False
    finally:
        mod.is_warm_enabled = orig_enabled


def test_async_handler_registered_and_shape():
    """`session.warm_start` is registered in the op-registry as an async
    handler returning {"spawned": bool}, matching the module's ASYNC=True
    contract declaration."""
    from coordinator_core.ipc import get_op_handler

    handler = get_op_handler("session.warm_start")
    assert handler is not None
    assert asyncio.iscoroutinefunction(handler)

    import coordinator_core.ops.session.warm_start as mod

    orig_enabled = mod.is_warm_enabled
    try:
        mod.is_warm_enabled = lambda: False
        result = asyncio.run(handler({}, None))
    finally:
        mod.is_warm_enabled = orig_enabled

    assert result == {"spawned": False}


def test_matcher_list_is_exhaustive():
    """Every SessionStart source is enumerated -- an omitted source
    silently means no warm start for exactly the long-lived sessions that
    benefit most (module docstring)."""
    assert warm_start.SESSIONSTART_MATCHERS == (
        "startup",
        "resume",
        "clear",
        "compact",
        "fork",
    )
    assert warm_start.ASYNC is True
