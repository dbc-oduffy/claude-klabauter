import threading
import time

import pytest

from coordinator_core import dispatch_abandon
from coordinator_core.bash_guards import leg_attribution as la
from coordinator_core.hooks import _preuse_phases as pp
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.telemetry import op_row_fields


def _run(chain, door_s=None, monkeypatch=None):
    if door_s is not None:
        monkeypatch.setattr(pp, "_door_deadline_s", lambda: door_s)
    token = op_row_fields.begin()
    try:
        out = pp.run_phased("hooks.preuse_x_dispatch", {}, chain)
    finally:
        fields = op_row_fields.drain(token)
    return out, fields


def test_sleep_phase_named_by_wall_with_near_zero_cpu():
    def chain(_params):
        with la.leg("fast"):
            pass
        with la.leg("stall"):
            time.sleep(0.12)
        return {"ok": 1}

    out, fields = _run(chain)
    assert out == {"ok": 1}
    assert fields["slow_phase"] == "stall"
    top = fields["spans"][0]
    assert top["wall_ms"] >= 110
    assert top["cpu_ms"] < 30


def test_attribution_and_deadline_unbound_after_run(monkeypatch):
    _run(lambda p: None, door_s=5.0, monkeypatch=monkeypatch)
    assert not la.active()
    assert pp._HOOK_DEADLINE.get() is None
    assert pp.past_deadline() is False


def test_unbound_is_not_past_deadline():
    assert pp.past_deadline() is False


def test_bound_dispatch_deadline_in_past_trips():
    tok = dispatch_abandon.DEADLINE.set(time.monotonic() - 1.0)
    try:
        assert pp.past_deadline() is True
    finally:
        dispatch_abandon.DEADLINE.reset(tok)


def test_far_dispatch_deadline_does_not_trip():
    tok = dispatch_abandon.DEADLINE.set(time.monotonic() + 30.0)
    try:
        assert pp.past_deadline() is False
    finally:
        dispatch_abandon.DEADLINE.reset(tok)


def test_hook_deadline_binds_even_when_dispatch_deadline_is_30s_out(monkeypatch):
    seen = {}

    def chain(_params):
        seen["before"] = pp.past_deadline()
        time.sleep(0.15)
        seen["after"] = pp.past_deadline()
        return None

    tok = dispatch_abandon.DEADLINE.set(time.monotonic() + 30.0)
    try:
        _run(chain, door_s=dispatch_abandon.REPLY_MARGIN_SECS + 0.1, monkeypatch=monkeypatch)
    finally:
        dispatch_abandon.DEADLINE.reset(tok)
    assert seen == {"before": False, "after": True}


def test_abandoned_trips():
    ev = threading.Event()
    tok = dispatch_abandon.ACTIVE.set(ev)
    try:
        assert pp.past_deadline() is False
        ev.set()
        assert pp.past_deadline() is True
    finally:
        dispatch_abandon.ACTIVE.reset(tok)


def test_deadline_stop_fails_open_and_runs_no_further_phase(monkeypatch):
    ran = []

    def chain(_params):
        for name in ("one", "two", "three"):
            pp.check_deadline(name)
            ran.append(name)
            with la.leg(name):
                time.sleep(0.06)
        return {"verdict": "deny"}

    out, fields = _run(chain, door_s=dispatch_abandon.REPLY_MARGIN_SECS + 0.1, monkeypatch=monkeypatch)
    assert ran == ["one", "two"]
    assert out == no_advisory()
    assert fields["deadline_stop"] == "three"


def test_check_deadline_raises_only_when_past():
    pp.check_deadline("p")
    tok = dispatch_abandon.DEADLINE.set(time.monotonic() - 1.0)
    try:
        with pytest.raises(pp.DeadlineStop) as ei:
            pp.check_deadline("p")
        assert ei.value.phase == "p"
    finally:
        dispatch_abandon.DEADLINE.reset(tok)


def test_door_deadline_is_the_hook_constant():
    from coordinator_core.hooks.submit_engine_ask import _DOOR_HOOK_READ_DEADLINE_S

    pp._door_deadline_s.cache_clear()
    assert pp._door_deadline_s() == _DOOR_HOOK_READ_DEADLINE_S


@pytest.mark.deliberate_wall_clock(reason="bounds the door deadline a blocked phase must not outwait; blocked time burns no CPU")
def test_blocked_phase_returns_by_deadline_and_chain_stops_after(monkeypatch):
    release = threading.Event()
    after = []

    def chain(_params):
        release.wait(3.0)
        try:
            pp.check_deadline("next_guard")
        except pp.DeadlineStop:
            after.append("stopped")
            raise
        after.append("ran-on")
        return {"ok": 1}

    t0 = time.monotonic()
    out, fields = _run(chain, door_s=0.4, monkeypatch=monkeypatch)
    elapsed = time.monotonic() - t0
    release.set()
    assert elapsed < 1.0
    assert out == no_advisory()
    assert fields["deadline_stop"] == "in_flight"
    deadline = time.monotonic() + 2.0
    while not after and time.monotonic() < deadline:
        time.sleep(0.01)
    assert after == ["stopped"]


def test_chain_finishing_inside_deadline_is_returned_whole(monkeypatch):
    out, fields = _run(lambda _p: {"ok": 1}, door_s=0.4, monkeypatch=monkeypatch)
    assert out == {"ok": 1}
    assert "deadline_stop" not in fields
