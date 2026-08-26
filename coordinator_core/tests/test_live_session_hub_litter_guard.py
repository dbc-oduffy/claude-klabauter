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


# --- Third exemption: recorded ownership (DR-none; see conftest's guard note) ---
#
# Added 2026-08-26 after `c7-cold-fwd-probe` — a peer session's probe, minted
# 21:40:54Z mid-run — failed a full-file run and was reported as the frozen-
# inventory test's leak. The guard had detected a real directory and named the
# wrong owner, which its own negative-spec had already predicted and deferred.


def _mint(hub, name, stable_pid):
    import json

    d = hub / name
    d.mkdir()
    if stable_pid is not None:
        (d / "meta.json").write_text(json.dumps({"stable_pid": stable_pid}), encoding="utf-8")
    return d


def test_a_peer_sessions_dir_is_not_our_leak(tmp_path, monkeypatch):
    """The false positive this exemption exists to kill. A concurrent peer mints
    a non-UUID, unregistered directory while our test is mid-flight; its
    `meta.json` names a `stable_pid` that is not ours, so it is not our litter.
    """
    hub = tmp_path / "coordinator-sessions"
    hub.mkdir()
    monkeypatch.setattr(cc_conftest, "_LIVE_HUB", str(hub))
    monkeypatch.setenv("CLAUDE_PID", "20204")
    gen = cc_conftest._no_new_live_session_hub_entries.__wrapped__()
    next(gen)
    _mint(hub, "c7-cold-fwd-probe", "19100")
    with pytest.raises(StopIteration):
        next(gen)


def test_our_own_sessions_dir_is_still_our_leak(tmp_path, monkeypatch):
    """The negative control, and the reason this exemption does not gut the
    guard: same shape, same absent registration, but the stamp is OURS. A test
    in this session that mints a hub dir must still fail loudly.
    """
    hub = tmp_path / "coordinator-sessions"
    hub.mkdir()
    monkeypatch.setattr(cc_conftest, "_LIVE_HUB", str(hub))
    monkeypatch.setenv("CLAUDE_PID", "20204")
    gen = cc_conftest._no_new_live_session_hub_entries.__wrapped__()
    next(gen)
    _mint(hub, "sess-abc", "20204")
    with pytest.raises(AssertionError, match="sess-abc"):
        try:
            next(gen)
        except StopIteration:
            pass


def test_a_dir_with_no_meta_json_is_still_flagged(tmp_path, monkeypatch):
    """Fail-closed, and historically the important arm: all three original leaks
    (`sess-1`, `sess-abc`, `altlive-probe`) carried a single log file and NO
    `meta.json`, because a fixture leak is minted by a log-appending guard
    rather than by session init. No stamp means no owner means flag it.
    """
    hub = tmp_path / "coordinator-sessions"
    hub.mkdir()
    monkeypatch.setattr(cc_conftest, "_LIVE_HUB", str(hub))
    monkeypatch.setenv("CLAUDE_PID", "20204")
    gen = cc_conftest._no_new_live_session_hub_entries.__wrapped__()
    next(gen)
    _mint(hub, "altlive-probe", None)
    with pytest.raises(AssertionError, match="altlive-probe"):
        try:
            next(gen)
        except StopIteration:
            pass


@pytest.mark.parametrize("meta_body", ["{ not json", "{}", '{"stable_pid": ""}'])
def test_an_unusable_stamp_fails_closed(tmp_path, monkeypatch, meta_body):
    """Unreadable, stampless, or empty — every unprovable case flags rather than
    exempts. An exemption that widened on a corrupt file would be a hole any
    leak could fall through by writing garbage."""
    hub = tmp_path / "coordinator-sessions"
    hub.mkdir()
    monkeypatch.setattr(cc_conftest, "_LIVE_HUB", str(hub))
    monkeypatch.setenv("CLAUDE_PID", "20204")
    gen = cc_conftest._no_new_live_session_hub_entries.__wrapped__()
    next(gen)
    d = hub / "sess-1"
    d.mkdir()
    (d / "meta.json").write_text(meta_body, encoding="utf-8")
    with pytest.raises(AssertionError, match="sess-1"):
        try:
            next(gen)
        except StopIteration:
            pass


def test_no_claude_pid_in_our_env_fails_closed(tmp_path, monkeypatch):
    """Without our own `CLAUDE_PID` there is nothing to compare against, so the
    guard must behave exactly as it did before this exemption existed rather
    than exempting everything."""
    hub = tmp_path / "coordinator-sessions"
    hub.mkdir()
    monkeypatch.setattr(cc_conftest, "_LIVE_HUB", str(hub))
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    gen = cc_conftest._no_new_live_session_hub_entries.__wrapped__()
    next(gen)
    _mint(hub, "c7-cold-fwd-probe", "19100")
    with pytest.raises(AssertionError, match="c7-cold-fwd-probe"):
        try:
            next(gen)
        except StopIteration:
            pass
