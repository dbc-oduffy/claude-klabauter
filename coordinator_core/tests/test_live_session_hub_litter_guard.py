"""Pins `coordinator_core/conftest.py`'s live session-hub litter guard.

The guard exists because three test-fixture-named directories (`sess-1`,
`sess-abc`, `altlive-probe`) were found sitting in this repo's REAL
`.git/coordinator-sessions/` on 2026-08-26, minted by tests that resolved a
repo root from the process cwd while taking their session id from a
monkeypatched env var. The two producers were fixed at the source; this guard
is the backstop for the next producer, which by construction is not one anyone
thought to fix.

Both halves are pinned, and the SECOND matters as much as the first: on a box
running 50-70 concurrent sessions against this same tree, a guard that fires on
any new hub entry would fire on a live peer's session directory and be turned
off within the day.
"""

from __future__ import annotations

import pytest

from coordinator_core import conftest as cc_conftest


def test_a_fixture_named_dir_created_during_a_test_fails_loudly(tmp_path, monkeypatch):
    hub = tmp_path / "coordinator-sessions"
    hub.mkdir()
    monkeypatch.setattr(cc_conftest, "_LIVE_HUB", str(hub))
    gen = cc_conftest._no_new_live_session_hub_entries.__wrapped__()
    next(gen)
    (hub / "sess-abc").mkdir()
    with pytest.raises(AssertionError, match="sess-abc"):
        try:
            next(gen)
        except StopIteration:
            pass


def test_a_uuid_shaped_dir_is_never_flagged(tmp_path, monkeypatch):
    """A live peer's session directory appears in this hub at any moment and is
    always a harness UUID. Flagging it would make the guard flaky on every box
    the fleet runs on, which is the same as deleting the guard.
    """
    hub = tmp_path / "coordinator-sessions"
    hub.mkdir()
    monkeypatch.setattr(cc_conftest, "_LIVE_HUB", str(hub))
    gen = cc_conftest._no_new_live_session_hub_entries.__wrapped__()
    next(gen)
    (hub / "3f2a1c9e-0000-4000-8000-0000000000aa").mkdir()
    with pytest.raises(StopIteration):
        next(gen)


def test_a_registered_harness_session_is_never_flagged(tmp_path, monkeypatch):
    """Second exemption, independent of name shape: anything the harness
    session registry currently knows about is a real session, not litter.
    """
    from coordinator_core.session import harness_registry

    hub = tmp_path / "coordinator-sessions"
    hub.mkdir()
    monkeypatch.setattr(cc_conftest, "_LIVE_HUB", str(hub))
    monkeypatch.setattr(harness_registry, "snapshot", lambda: {"not-a-uuid-peer": object()})
    gen = cc_conftest._no_new_live_session_hub_entries.__wrapped__()
    next(gen)
    (hub / "not-a-uuid-peer").mkdir()
    with pytest.raises(StopIteration):
        next(gen)


def test_an_absent_hub_is_not_an_error(tmp_path, monkeypatch):
    """A checkout with no session hub yet (a fresh clone, a CI runner) must not
    turn every test in the suite into an error.
    """
    monkeypatch.setattr(cc_conftest, "_LIVE_HUB", str(tmp_path / "nope"))
    gen = cc_conftest._no_new_live_session_hub_entries.__wrapped__()
    next(gen)
    with pytest.raises(StopIteration):
        next(gen)
