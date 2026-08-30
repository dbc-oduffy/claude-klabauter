"""Tests for coordinator_core.warm.push_cadence.

Spec backlink: docs/plans/2026-08-30-who-pushes-and-when.md § C4

Four legs, each pinned to fail if its own trigger stops firing (chunk body):
  (1) the idle tick fires a sweep at the interval and not before
  (2) idle demotion fires a final sweep via the shared `_run_tail`
  (3) a sweep never blocks demotion / never extends server lifetime
  (4) a second concurrent sweeper on the same repo declines

Plus: drains a pending record it finds, touches only served repos, and
feeds the failure detector on a declined/failed push.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.warm import idle, lifecycle, push_cadence, server


@pytest.fixture(autouse=True)
def _reset_state():
    push_cadence.reset_cadence_for_test()
    lifecycle.reset_shutdown_guard_for_test()
    lifecycle.reset_final_sweep_hook_for_test()
    idle.reset_idle_clock_for_test()
    yield
    push_cadence.reset_cadence_for_test()
    lifecycle.reset_shutdown_guard_for_test()
    lifecycle.reset_final_sweep_hook_for_test()
    idle.reset_idle_clock_for_test()


def _repo(tmp_path, name="repo"):
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    return root


class _FakeVersionState:
    def __init__(self, *, skewed: bool = False, server_sha: str = "deadbeef"):
        self._skewed = skewed
        self.server_sha = server_sha

    def is_skewed(self, client_token: str) -> bool:
        return self._skewed


def _make_context():
    return server._ServerContext(name="test", sid="sid", version_state=_FakeVersionState())


# ---------------------------------------------------------------------------
# Leg 1 -- on_idle_tick fires at the interval, not before
# ---------------------------------------------------------------------------


def test_on_idle_tick_does_not_sweep_before_interval_elapses():
    fake_now = [0.0]
    calls = []

    def _fake_sweep(repos, **kwargs):
        calls.append(list(repos))

    ran_first = push_cadence.on_idle_tick(
        served_repos=lambda: ["r"],
        clock=lambda: fake_now[0],
        interval_secs=600.0,
        sweep_fn=_fake_sweep,
    )
    assert ran_first is False
    assert calls == []

    fake_now[0] = 300.0
    ran_second = push_cadence.on_idle_tick(
        served_repos=lambda: ["r"],
        clock=lambda: fake_now[0],
        interval_secs=600.0,
        sweep_fn=_fake_sweep,
    )
    assert ran_second is False
    assert calls == []


def test_on_idle_tick_fires_once_interval_has_elapsed():
    fake_now = [0.0]
    calls = []

    def _fake_sweep(repos, **kwargs):
        calls.append(list(repos))

    push_cadence.on_idle_tick(
        served_repos=lambda: ["r"],
        clock=lambda: fake_now[0],
        interval_secs=600.0,
        sweep_fn=_fake_sweep,
    )
    fake_now[0] = 601.0
    ran = push_cadence.on_idle_tick(
        served_repos=lambda: ["r"],
        clock=lambda: fake_now[0],
        interval_secs=600.0,
        sweep_fn=_fake_sweep,
    )
    assert ran is True
    assert calls == [["r"]]


def test_push_cadence_interval_is_strictly_under_idle_deadline():
    assert push_cadence.PUSH_CADENCE_INTERVAL_SECS < idle.DEFAULT_IDLE_MINUTES * 60.0


# ---------------------------------------------------------------------------
# Leg 2 -- idle demotion fires a final sweep via the SHARED _run_tail, not
# merely skew eviction.
# ---------------------------------------------------------------------------


def test_begin_shutdown_runs_registered_final_sweep_hook():
    calls = []
    lifecycle.set_final_sweep_hook(lambda: calls.append("swept"))

    order = []
    lifecycle.begin_shutdown(
        close_listener=lambda: order.append("close"),
        in_flight_count=lambda: 0,
        ctx_shutdown=lambda: order.append("ctx_shutdown"),
        exit_fn=lambda code: order.append(("exit_fn", code)),
    )

    assert calls == ["swept"]
    # ctx_shutdown -> sweep -> exit_fn, in that order.
    assert order.index("ctx_shutdown") < order.index(("exit_fn", 0))


def test_drain_and_exit_also_runs_the_final_sweep_hook():
    calls = []
    lifecycle.set_final_sweep_hook(lambda: calls.append("swept"))

    lifecycle.drain_and_exit(
        in_flight_count=lambda: 0,
        ctx_shutdown=lambda: None,
        exit_fn=lambda code: None,
    )

    assert calls == ["swept"]


def test_no_hook_registered_is_a_silent_no_op():
    # No set_final_sweep_hook call -- must not raise.
    result = lifecycle.begin_shutdown(
        close_listener=lambda: None,
        in_flight_count=lambda: 0,
        ctx_shutdown=lambda: None,
        exit_fn=lambda code: None,
    )
    assert result is True


def test_final_sweep_hook_exception_never_blocks_exit_fn():
    order = []

    def _raising_hook():
        raise RuntimeError("sweep blew up")

    lifecycle.set_final_sweep_hook(_raising_hook)
    lifecycle.begin_shutdown(
        close_listener=lambda: None,
        in_flight_count=lambda: 0,
        ctx_shutdown=lambda: None,
        exit_fn=lambda code: order.append(code),
    )
    assert order == [0]


# ---------------------------------------------------------------------------
# Leg 3 -- a sweep never blocks demotion / never extends server lifetime.
# ---------------------------------------------------------------------------


def test_idle_tick_runs_cadence_only_when_should_demote_is_false(monkeypatch):
    """`_ServerContext._idle_tick` must never let a cadence sweep run on the
    SAME tick that actually demotes -- by the time `demote_if_idle` returns
    True in production, `os._exit` has already fired inside `begin_shutdown`
    and the process is gone. This test pins that ordering with a fake
    `exit_fn` standing in for `os._exit`.
    """
    ctx = _make_context()
    sweep_calls = []
    monkeypatch.setattr(
        push_cadence, "on_idle_tick", lambda **kwargs: sweep_calls.append(1) or True
    )
    monkeypatch.setattr(idle, "should_demote", lambda **kwargs: True)

    def _fake_demote_if_idle(**kwargs):
        # Mirrors idle.demote_if_idle's own contract on a True verdict: it
        # calls begin_shutdown, which (in production) ends the process via
        # os._exit before control ever returns to _idle_tick -- simulated
        # here by raising, since a real os._exit is not observable from a
        # test.
        raise SystemExit(0)

    monkeypatch.setattr(idle, "demote_if_idle", _fake_demote_if_idle)

    with pytest.raises(SystemExit):
        ctx._idle_tick()
    assert sweep_calls == []


def test_sweep_total_ceiling_stops_taking_new_repos(tmp_path):
    swept = []

    def _fake_sweep_one(repo_root, *, per_repo_deadline):
        swept.append(repo_root)

    # First value establishes the deadline (0.0 + total_ceiling_secs); the
    # second is checked before repo 1 (still under ceiling, proceeds); the
    # third is checked before repo 2 (past ceiling, breaks without ever
    # touching repo 2 or repo 3).
    clock_values = iter([0.0, 10.0, 999.0])

    def _clock():
        return next(clock_values, 999.0)

    import coordinator_core.warm.push_cadence as pc

    orig_sweep_one = pc._sweep_one
    try:
        pc._sweep_one = _fake_sweep_one
        repos = [_repo(tmp_path, "a"), _repo(tmp_path, "b"), _repo(tmp_path, "c")]
        pc.sweep_repos(repos, total_ceiling_secs=50.0, clock=_clock)
    finally:
        pc._sweep_one = orig_sweep_one

    assert len(swept) == 1


# ---------------------------------------------------------------------------
# Leg 4 -- a second concurrent sweeper on the same repo declines.
# ---------------------------------------------------------------------------


def test_second_concurrent_sweeper_declines(tmp_path):
    repo = _repo(tmp_path)
    # holder_pid must be a genuinely LIVE pid for the staleness check to
    # treat this as an in-window live holder -- this test process's own pid
    # stands in for "some resident generation currently mid-sweep".
    live_pid = os_getpid()
    first = push_cadence._acquire_sweep_lock(repo, now=1000.0, pid=live_pid)
    assert first is True

    second = push_cadence._acquire_sweep_lock(repo, now=1000.5, pid=live_pid + 1)
    assert second is False


def test_stale_holder_is_taken_over(tmp_path):
    repo = _repo(tmp_path)
    lock_path = push_cadence._sweep_lock_path(repo)
    lock_path.write_text(
        json.dumps({"holder_pid": 999999999, "hold_until": 1.0}), encoding="utf-8"
    )

    acquired = push_cadence._acquire_sweep_lock(repo, now=2.0, pid=os_getpid())
    assert acquired is True


def os_getpid():
    import os

    return os.getpid()


def test_release_only_removes_own_record(tmp_path):
    repo = _repo(tmp_path)
    assert push_cadence._acquire_sweep_lock(repo, now=1.0, pid=111) is True
    push_cadence._release_sweep_lock(repo, pid=222)
    assert push_cadence._sweep_lock_path(repo).exists()
    push_cadence._release_sweep_lock(repo, pid=111)
    assert not push_cadence._sweep_lock_path(repo).exists()


# ---------------------------------------------------------------------------
# Drains a pending record it finds; touches only served repos.
# ---------------------------------------------------------------------------


def test_sweep_one_drains_before_pushing(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    calls = []
    monkeypatch.setattr(
        push_cadence, "drain_pending_push", lambda root: calls.append(("drain", root))
    )

    class _Outcome:
        failed = []
        unconfirmed = []

    monkeypatch.setattr(
        push_cadence,
        "push_outstanding",
        lambda root: calls.append(("push", root)) or _Outcome(),
    )

    push_cadence._sweep_one(repo, per_repo_deadline=12.0)

    assert [c[0] for c in calls] == ["drain", "push"]


def test_sweep_repos_touches_only_the_served_set(tmp_path, monkeypatch):
    served = _repo(tmp_path, "served")
    unserved_marker = []

    monkeypatch.setattr(push_cadence, "drain_pending_push", lambda root: None)

    class _Outcome:
        failed = []
        unconfirmed = []

    def _fake_push_outstanding(root):
        unserved_marker.append(root)
        return _Outcome()

    monkeypatch.setattr(push_cadence, "push_outstanding", _fake_push_outstanding)

    push_cadence.sweep_repos([served])

    assert unserved_marker == [served]


def test_server_context_served_repos_reflects_only_recorded_repos(tmp_path):
    ctx = _make_context()
    assert ctx.served_repos() == []
    repo_a = tmp_path / "a"
    ctx.record_served_repo(repo_a)
    assert ctx.served_repos() == [repo_a]
    # Recording the same repo again must not duplicate it.
    ctx.record_served_repo(repo_a)
    assert ctx.served_repos() == [repo_a]


# ---------------------------------------------------------------------------
# A declined/failed push records a row through auto_push.log_failure.
# ---------------------------------------------------------------------------


def test_failed_push_feeds_the_failure_detector(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(push_cadence, "drain_pending_push", lambda root: None)
    monkeypatch.setattr(push_cadence, "head_branch", lambda root: "work/x/2026-08-30")

    class _FailedOutcome:
        failed = ["git push: non-fast-forward"]
        unconfirmed = []

    monkeypatch.setattr(push_cadence, "push_outstanding", lambda root: _FailedOutcome())

    logged = []
    monkeypatch.setattr(
        push_cadence,
        "log_failure",
        lambda repo_root, branch, route, err_class, attempts, first_err, stderr_text: logged.append(
            (repo_root, branch, route, err_class, first_err)
        ),
    )

    push_cadence._sweep_one(repo, per_repo_deadline=12.0)

    assert len(logged) == 1
    repo_root, branch, route, err_class, first_err = logged[0]
    assert branch == "work/x/2026-08-30"
    assert route == "cadence-sweep"
    assert err_class == "sweep-failed"
    assert "non-fast-forward" in first_err


def test_successful_push_does_not_feed_the_failure_detector(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(push_cadence, "drain_pending_push", lambda root: None)

    class _OkOutcome:
        failed = []
        unconfirmed = []

    monkeypatch.setattr(push_cadence, "push_outstanding", lambda root: _OkOutcome())

    logged = []
    monkeypatch.setattr(
        push_cadence,
        "log_failure",
        lambda *a, **kw: logged.append(1),
    )

    push_cadence._sweep_one(repo, per_repo_deadline=12.0)

    assert logged == []
