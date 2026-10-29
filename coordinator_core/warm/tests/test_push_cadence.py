
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


@pytest.fixture(autouse=True)
def _author_profile(monkeypatch):
    monkeypatch.setattr(push_cadence, "machine_profile", lambda: "author")

def test_on_idle_tick_seeds_first_tick_then_waits_for_interval():
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


def _all_ahead(monkeypatch):
    monkeypatch.setattr(push_cadence, "is_ahead", lambda root: True)


def test_every_default_ceiling_repo_is_admitted_whatever_the_clock_reads(tmp_path, monkeypatch):
    swept = []
    monkeypatch.setattr(push_cadence, "_sweep_one", lambda root: swept.append(root.name))
    _all_ahead(monkeypatch)
    repos = [_repo(tmp_path, "a"), _repo(tmp_path, "b"), _repo(tmp_path, "c")]
    clock_values = iter([0.0, 10.0, 999.0])

    push_cadence.sweep_repos(
        repos, total_ceiling_secs=50.0, clock=lambda: next(clock_values, 999.0)
    )

    assert sorted(swept) == ["a", "b", "c"]


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
        acted = []
        unconfirmed = []

    def _fake_push_outstanding(root, **kwargs):
        unserved_marker.append(root)
        return _Outcome()

    monkeypatch.setattr(push_cadence, "push_outstanding", _fake_push_outstanding)
    _all_ahead(monkeypatch)

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
            acted = []
            unconfirmed = []

        return _Outcome()

    monkeypatch.setattr(push_cadence, "push_outstanding", _fake_push_outstanding)

    push_cadence._sweep_one(repo)

    assert len(calls) == 1
    assert calls[0].get("budget_secs") == push_mod.CADENCE_PUSH_RETRY_BUDGET_SECS
    assert calls[0].get("budget_secs") != push_mod.PUSH_RETRY_BUDGET_SECS


def test_a_push_longer_than_the_sweep_ceiling_still_starts_for_every_ahead_repo(
    tmp_path, monkeypatch
):
    swept = []
    monkeypatch.setattr(push_cadence, "_sweep_one", lambda root: swept.append(root.name))
    _all_ahead(monkeypatch)
    repos = [_repo(tmp_path, n) for n in ("a", "b", "c", "d")]

    push_cadence.sweep_repos(
        repos, total_ceiling_secs=10.0, per_repo_budget_secs=6.0, clock=lambda: 0.0
    )

    assert sorted(swept) == ["a", "b", "c", "d"]


def test_an_unreached_extended_repo_keeps_its_old_key_and_leads_the_next_tick(
    tmp_path, monkeypatch
):
    swept = []
    monkeypatch.setattr(push_cadence, "_sweep_one", lambda root: swept.append(root.name))
    _all_ahead(monkeypatch)
    keys = {"a": 1.0, "b": 2.0, "c": 3.0}
    monkeypatch.setattr(push_cadence, "since_pushed_key", lambda root: keys[root.name])
    monkeypatch.setattr(
        push_cadence,
        "resolve_push_ceiling",
        lambda root, *, default_secs: 100.0 if root.name in ("a", "b") else default_secs,
    )
    repos = [_repo(tmp_path, n) for n in ("a", "b", "c")]

    push_cadence.sweep_repos(repos, allow_extended_first=True, clock=lambda: 0.0)
    assert sorted(swept) == ["a", "c"]  # "b" is the second extended repo
    swept.clear()
    keys["a"] = 9.0  # a's push landed, so the remote just heard from it
    push_cadence.sweep_repos(repos, allow_extended_first=True, clock=lambda: 0.0)
    assert sorted(swept) == ["b", "c"]


def test_default_push_ceiling_fits_inside_both_sweep_ceilings():
    # Admission no longer consults the sweep ceiling; the occupancy bound holds
    # only while a default-ceiling push fits inside it.
    assert push_cadence.CADENCE_PUSH_RETRY_BUDGET_SECS <= push_cadence.EXIT_SWEEP_CEILING_SECS
    assert push_cadence.EXIT_SWEEP_CEILING_SECS <= push_cadence.SWEEP_TOTAL_CEILING_SECS


def test_exit_sweep_ceiling_secs_stays_tighter_than_the_idle_ceiling():
    assert push_cadence.EXIT_SWEEP_CEILING_SECS < push_cadence.SWEEP_TOTAL_CEILING_SECS


def test_sweep_lock_hold_secs_is_keyed_to_the_largest_per_repo_ceiling():
    """`_SWEEP_LOCK_HOLD_SECS` must cover the largest push a sweep can admit --
    a repo configured up to `PUSH_CEILING_MAX_SECS` -- plus a bounded margin, so
    the lock never expires under a push still in flight."""
    assert push_cadence._SWEEP_LOCK_HOLD_SECS == push_cadence.PUSH_CEILING_MAX_SECS + 10.0


# ---------------------------------------------------------------------------
# P052-C6 (docs/plans/2026-09-10-push-cadence-hang-detection-over-elapsed-
# timeout.md): the mechanical relationship guard pinning C5's arm-B
# derivation of the four enumerated constants -- a docstring alone does not
# discharge AC6, a test asserting the exact formula does. Arm B was
# selected (docs/research/2026-09-10-git-push-progress-stall-measurement.md):
# `CADENCE_PUSH_RETRY_BUDGET_SECS` is RETAINED as the fallback rather than
# retired, and `SWEEP_TOTAL_CEILING_SECS`/`EXIT_SWEEP_CEILING_SECS` are both
# re-derived off it (this module's own docstrings for each constant name
# the exact offsets pinned here).
# ---------------------------------------------------------------------------


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
        acted = []
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


def _fresh_branch_repo(tmp_path, *, head="a" * 40, remote_ref_sha="a" * 40, branch_upstream=False):
    repo = tmp_path / "repo"
    (repo / ".git" / "refs" / "remotes" / "origin").mkdir(parents=True)
    (repo / ".git" / "refs" / "remotes" / "origin" / "main").write_text(remote_ref_sha + "\n")
    if branch_upstream:
        d = repo / ".git" / "refs" / "remotes" / "origin" / "work" / "x"
        d.mkdir(parents=True)
        (d / "day").write_text(head + "\n")
    return repo


def test_sweep_is_silent_noop_on_fresh_branch_with_no_upstream(tmp_path, monkeypatch):
    repo = _fresh_branch_repo(tmp_path)
    monkeypatch.setattr(push_cadence, "machine_profile", lambda: "author")
    monkeypatch.setattr(push_cadence, "head_branch", lambda root: "work/x/day")
    monkeypatch.setattr(push_cadence, "head_sha", lambda root: "a" * 40)
    calls = []
    monkeypatch.setattr(push_cadence, "push_outstanding", lambda root, **kw: calls.append(root))
    push_cadence._sweep_one(repo)
    assert calls == []


def test_sweep_still_pushes_branch_with_new_commits_and_no_upstream(tmp_path, monkeypatch):
    repo = _fresh_branch_repo(tmp_path, remote_ref_sha="b" * 40)
    monkeypatch.setattr(push_cadence, "machine_profile", lambda: "author")
    monkeypatch.setattr(push_cadence, "head_branch", lambda root: "work/x/day")
    monkeypatch.setattr(push_cadence, "head_sha", lambda root: "a" * 40)

    class _Ok:
        failed = False
        unconfirmed = False
        acted = []

    calls = []
    monkeypatch.setattr(push_cadence, "push_outstanding", lambda root, **kw: calls.append(root) or _Ok())
    push_cadence._sweep_one(repo)
    assert len(calls) == 1


def test_sweep_does_not_run_on_consumer_profile(tmp_path, monkeypatch):
    repo = _fresh_branch_repo(tmp_path, remote_ref_sha="b" * 40)
    monkeypatch.setattr(push_cadence, "machine_profile", lambda: "consumer")
    calls = []
    monkeypatch.setattr(push_cadence, "push_outstanding", lambda root, **kw: calls.append(root))
    push_cadence._sweep_one(repo)
    assert calls == []


_LOCK_ERR = (
    "error: cannot lock ref 'refs/remotes/origin/work/x/day': Unable to create "
    "'/r/.git/refs/remotes/origin/work/x/day.lock': File exists."
)


def _ref_lock_sweep(tmp_path, monkeypatch, *, reaped):
    repo = _repo(tmp_path)
    monkeypatch.setattr(push_cadence, "head_branch", lambda root: "work/x/day")
    monkeypatch.setattr(push_cadence, "log_failure", lambda *a, **kw: None)

    class _Fail:
        failed = [_LOCK_ERR]
        unconfirmed = []
        acted = []
        attempts = 1

    class _Ok:
        failed = []
        unconfirmed = []
        acted = ["push"]
        attempts = 1

    pushes = []

    def _push(root, **kw):
        pushes.append(1)
        return _Fail() if len(pushes) == 1 or not reaped else _Ok()

    reap_calls = []
    monkeypatch.setattr(push_cadence, "push_outstanding", _push)
    import coordinator_core.ops.reap_stale_locks as rsl

    monkeypatch.setattr(
        rsl, "reap_ref_lock", lambda root, p: reap_calls.append(p) or reaped
    )
    return push_cadence._sweep_one(repo), pushes, reap_calls


def test_ref_lock_failure_reaps_named_lock_and_retries_once(tmp_path, monkeypatch):
    result, pushes, reap_calls = _ref_lock_sweep(tmp_path, monkeypatch, reaped=True)
    assert result == "pushed"
    assert len(pushes) == 2
    assert reap_calls == ["/r/.git/refs/remotes/origin/work/x/day.lock"]


def test_ref_lock_failure_left_alone_when_reaper_refuses(tmp_path, monkeypatch):
    result, pushes, _ = _ref_lock_sweep(tmp_path, monkeypatch, reaped=False)
    assert result == "failed"
    assert len(pushes) == 1


def test_non_lock_failure_never_calls_the_reaper(tmp_path, monkeypatch):
    repo = _repo(tmp_path)

    class _Fail:
        failed = ["git push: non-fast-forward"]
        unconfirmed = []
        attempts = 1

    pushes = []
    monkeypatch.setattr(push_cadence, "push_outstanding", lambda r, **kw: pushes.append(1) or _Fail())
    monkeypatch.setattr(push_cadence, "log_failure", lambda *a, **kw: None)
    import coordinator_core.ops.reap_stale_locks as rsl

    monkeypatch.setattr(rsl, "reap_ref_lock", lambda *a: pytest.fail("reaper called"))
    assert push_cadence._sweep_one(repo) == "failed"
    assert len(pushes) == 1
