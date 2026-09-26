
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


def test_idle_tick_runs_cadence_only_when_should_demote_is_false(monkeypatch):
    ctx = _make_context()
    sweep_calls = []
    monkeypatch.setattr(
        push_cadence, "on_idle_tick", lambda **kwargs: sweep_calls.append(1) or True
    )
    monkeypatch.setattr(idle, "should_demote", lambda **kwargs: True)

    def _fake_demote_if_idle(**kwargs):
        raise SystemExit(0)

    monkeypatch.setattr(idle, "demote_if_idle", _fake_demote_if_idle)

    with pytest.raises(SystemExit):
        ctx._idle_tick()
    assert sweep_calls == []


def test_sweep_total_ceiling_stops_taking_new_repos(tmp_path):
    swept = []

    def _fake_sweep_one(repo_root):
        swept.append(repo_root)

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


def test_second_concurrent_sweeper_declines(tmp_path):
    repo = _repo(tmp_path)
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


def test_sweep_repos_touches_only_the_served_set(tmp_path, monkeypatch):
    served = _repo(tmp_path, "served")
    unserved_marker = []


    class _Outcome:
        failed = []
        unconfirmed = []

    def _fake_push_outstanding(root, **kwargs):
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
    ctx.record_served_repo(repo_a)
    assert ctx.served_repos() == [repo_a]


def test_failed_push_feeds_the_failure_detector(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(push_cadence, "head_branch", lambda root: "work/x/2026-08-30")

    class _FailedOutcome:
        failed = ["git push: non-fast-forward"]
        unconfirmed = []
        attempts = 3

    monkeypatch.setattr(push_cadence, "push_outstanding", lambda root, **kw: _FailedOutcome())

    logged = []
    monkeypatch.setattr(
        push_cadence,
        "log_failure",
        lambda repo_root, branch, route, err_class, attempts, first_err, stderr_text, unconfirmed=None: logged.append(
            (repo_root, branch, route, err_class, attempts, first_err, unconfirmed)
        ),
    )

    push_cadence._sweep_one(repo)

    assert len(logged) == 1
    repo_root, branch, route, err_class, attempts, first_err, unconfirmed = logged[0]
    assert branch == "work/x/2026-08-30"
    assert route == "cadence-sweep"
    assert err_class == "sweep-failed"
    assert "non-fast-forward" in first_err
    assert attempts == 3
    assert unconfirmed is False


def test_sweep_feed_reports_the_outcomes_own_attempt_count(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(push_cadence, "head_branch", lambda root: "work/x/2026-08-30")

    seen = []
    monkeypatch.setattr(
        push_cadence,
        "log_failure",
        lambda *a, **kw: seen.append(a[4]),
    )

    for reported in (1, 2, 3, None):

        class _Unconfirmed:
            failed = []
            unconfirmed = ["git push: timed out"]
            attempts = reported

        monkeypatch.setattr(push_cadence, "push_outstanding", lambda root, o=_Unconfirmed, **kw: o())
        push_cadence._sweep_one(repo)

    assert seen == [1, 2, 3, None]


def test_sweep_one_passes_the_cadence_budget_not_the_interactive_one(tmp_path, monkeypatch):
    """`_sweep_one` must hand `push_outstanding` the cadence's OWN, smaller
    `CADENCE_PUSH_RETRY_BUDGET_SECS` -- never the interactive
    `PUSH_RETRY_BUDGET_SECS` every other `push_outstanding` caller defaults
    to. Pinned directly at the seam, not inferred from timing."""
    from coordinator_core.ops.ceremony import push as push_mod

    repo = _repo(tmp_path)
    calls = []

    def _fake_push_outstanding(root, **kwargs):
        calls.append(kwargs)

        class _Outcome:
            failed = []
            unconfirmed = []

        return _Outcome()

    monkeypatch.setattr(push_cadence, "push_outstanding", _fake_push_outstanding)

    push_cadence._sweep_one(repo)

    assert len(calls) == 1
    assert calls[0].get("budget_secs") == push_mod.CADENCE_PUSH_RETRY_BUDGET_SECS
    assert calls[0].get("budget_secs") != push_mod.PUSH_RETRY_BUDGET_SECS


def test_sweep_repos_refuses_to_start_a_repo_it_cannot_finish(tmp_path):
    swept = []

    def _fake_sweep_one(repo_root):
        swept.append(repo_root)

    clock_values = iter([0.0, 6.0])

    def _clock():
        return next(clock_values, 999.0)

    import coordinator_core.warm.push_cadence as pc

    orig_sweep_one = pc._sweep_one
    try:
        pc._sweep_one = _fake_sweep_one
        repos = [_repo(tmp_path, "a")]
        pc.sweep_repos(
            repos,
            total_ceiling_secs=10.0,
            per_repo_budget_secs=6.0,
            clock=_clock,
        )
    finally:
        pc._sweep_one = orig_sweep_one

    assert swept == []


def test_exit_sweep_ceiling_secs_stays_tighter_than_the_idle_ceiling():
    assert push_cadence.EXIT_SWEEP_CEILING_SECS < push_cadence.SWEEP_TOTAL_CEILING_SECS


def test_sweep_lock_hold_secs_is_keyed_to_the_cadence_budget():
    """`_SWEEP_LOCK_HOLD_SECS` must track `CADENCE_PUSH_RETRY_BUDGET_SECS`
    (the cadence path's own budget) -- not the interactive
    `PUSH_RETRY_BUDGET_SECS` this sweep never spends -- so the lock stays a
    bounded margin over the work it actually guards
    (overengineering-reviewer finding 5)."""
    assert push_cadence._SWEEP_LOCK_HOLD_SECS == push_cadence.CADENCE_PUSH_RETRY_BUDGET_SECS + 10.0


# `CADENCE_PUSH_RETRY_BUDGET_SECS` is RETAINED as the fallback rather than
# retired, and `SWEEP_TOTAL_CEILING_SECS`/`EXIT_SWEEP_CEILING_SECS` are both


def test_sweep_total_ceiling_secs_is_cadence_budget_plus_its_named_margin():
    """`SWEEP_TOTAL_CEILING_SECS` = `CADENCE_PUSH_RETRY_BUDGET_SECS` + 2.0 --
    the one-repo-per-tick sizing this module's own docstring derives (2.0s
    covering the sweep's own per-repo overhead beyond the push itself)."""
    assert (
        push_cadence.SWEEP_TOTAL_CEILING_SECS
        == push_cadence.CADENCE_PUSH_RETRY_BUDGET_SECS + 2.0
    )


def test_exit_sweep_ceiling_secs_is_cadence_budget_plus_its_named_margin():
    """`EXIT_SWEEP_CEILING_SECS` = `CADENCE_PUSH_RETRY_BUDGET_SECS` + 1.0 --
    strictly under `SWEEP_TOTAL_CEILING_SECS`'s own +2.0 margin, per that
    constant's own docstring (exit-path latency is the more sensitive of
    the two)."""
    assert (
        push_cadence.EXIT_SWEEP_CEILING_SECS
        == push_cadence.CADENCE_PUSH_RETRY_BUDGET_SECS + 1.0
    )


def test_cadence_push_retry_budget_secs_was_not_retired():
    """Arm B's negative spec: unlike arm A, `CADENCE_PUSH_RETRY_BUDGET_SECS`
    survives as the fallback bound rather than being retired outright, and
    every constant re-derived off it still imports it live rather than a
    frozen copy of its old value."""
    from coordinator_core.ops.ceremony.push import CADENCE_PUSH_RETRY_BUDGET_SECS

    assert push_cadence.CADENCE_PUSH_RETRY_BUDGET_SECS is CADENCE_PUSH_RETRY_BUDGET_SECS
    assert push_cadence.CADENCE_PUSH_RETRY_BUDGET_SECS > 0


def test_successful_push_does_not_feed_the_failure_detector(tmp_path, monkeypatch):
    repo = _repo(tmp_path)

    class _OkOutcome:
        failed = []
        unconfirmed = []
        attempts = None

    monkeypatch.setattr(push_cadence, "push_outstanding", lambda root, **kw: _OkOutcome())

    logged = []
    monkeypatch.setattr(
        push_cadence,
        "log_failure",
        lambda *a, **kw: logged.append(1),
    )

    push_cadence._sweep_one(repo)

    assert logged == []
