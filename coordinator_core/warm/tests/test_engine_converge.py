"""Background engine convergence: a clean behind clone is fast-forwarded and its
stamp changes; a dirty clone is refused with the reason recorded; scheduling
returns without running git."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from coordinator_core.warm import engine_converge, push_cadence

_REPO_LIB = Path(__file__).resolve().parents[3] / "coordinator" / "lib"


def test_a_convergence_pass_signals_no_process(tmp_path, monkeypatch):
    from coordinator_core.warm import pair_stop

    def _forbidden(*a, **k):
        raise AssertionError("convergence must not stop or reap any process")

    for name in ("stop_warm_pair", "reap_pairs_outside", "_terminate_pid", "_terminate_with_reason"):
        monkeypatch.setattr(pair_stop, name, _forbidden)
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)

    class _Result:
        ok = True
        fast_forwarded = True

    payload = engine_converge.run_convergence_pass(
        clone=clone, fast_forward=lambda c: _Result(), record_to=tmp_path / "rec.json"
    )
    assert payload["outcome"] == "fast_forwarded"


def test_non_git_clone_is_recorded_not_raised(tmp_path):
    payload = engine_converge.run_convergence_pass(
        clone=tmp_path / "nogit", fast_forward=lambda c: pytest.fail("no git"),
        record_to=tmp_path / "r.json",
    )
    assert payload["outcome"] == "not_a_git_clone"


def test_default_clone_resolves_through_skew(monkeypatch, tmp_path):
    from coordinator_core.warm import skew

    monkeypatch.setattr(skew, "_default_engine_clone", lambda: tmp_path / "resolved")
    payload = engine_converge.run_convergence_pass(record_to=tmp_path / "r.json")
    assert payload["clone"] == str(tmp_path / "resolved")


@pytest.fixture(autouse=True)
def _reset():
    engine_converge.reset_converge_for_test()
    push_cadence.reset_cadence_for_test()
    yield
    engine_converge.reset_converge_for_test()


def test_scheduling_returns_without_running_the_pass():
    gate = threading.Event()
    ran = threading.Event()

    def blocked_pass():
        ran.set()
        gate.wait(5)

    started: list = []
    try:
        ok = engine_converge.schedule_convergence(
            pass_fn=blocked_pass, start=lambda t: (started.append(t), t.start())
        )
        assert ok is True
        assert ran.wait(2)  # the pass is running on its own thread...
        # ...and a second schedule neither blocks on it nor starts another.
        assert engine_converge.schedule_convergence(pass_fn=blocked_pass) is False
    finally:
        gate.set()
        started[0].join(2)


def test_schedule_is_interval_gated_and_first_call_is_due():
    clock = [0.0]
    calls: list = []

    def run_inline(t):
        t.start()
        t.join(2)

    kwargs = dict(clock=lambda: clock[0], interval_secs=600.0, pass_fn=lambda: calls.append(1), start=run_inline)
    assert engine_converge.schedule_convergence(**kwargs) is True
    clock[0] = 599.0
    assert engine_converge.schedule_convergence(**kwargs) is False
    clock[0] = 601.0
    assert engine_converge.schedule_convergence(**kwargs) is True
    assert len(calls) == 2


def test_default_schedule_is_inert_under_pytest():
    assert engine_converge.schedule_convergence() is False


def test_idle_tick_schedules_convergence(monkeypatch):
    seen: list = []
    monkeypatch.setattr(engine_converge, "schedule_convergence", lambda: seen.append(1) or True)
    push_cadence.on_idle_tick(served_repos=lambda: [], sweep_fn=lambda *a, **k: None)
    assert seen == [1]


def test_idle_tick_survives_a_scheduler_failure(monkeypatch):
    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(engine_converge, "schedule_convergence", boom)
    assert push_cadence.on_idle_tick(served_repos=lambda: [], sweep_fn=lambda *a, **k: None) is False
