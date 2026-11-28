"""An ask spooled while a request is in flight drains when that request completes, not on a later request.

Ordering is pinned by call order and file mtimes, never wall clock.
"""

from __future__ import annotations

import json
import os

from coordinator_core.ask_spool import record
from coordinator_core.warm import server


class _IO:
    def __init__(self, line: bytes, events: list):
        self._line = line
        self._events = events

    def readline(self) -> bytes:
        return self._line

    def write(self, data: bytes) -> None:
        self._events.append("response")

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _VersionState:
    server_sha = "abc"

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return False


def _spool(repo, name: str, mtime: float) -> None:
    path = record.entry_path(str(repo), name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{}")
    os.utime(path, (mtime, mtime))


def _serve(repo, trigger, events: list, during_dispatch=lambda: None) -> None:
    line = (json.dumps({"jsonrpc": "2.0", "id": 1, "method": "noop", "params": {}, "_engine_token": "t",
                        "_origin_worktree": str(repo)}) + "\n").encode()

    def dispatch(msg, **kw):
        events.append("dispatch")
        during_dispatch()
        return {"jsonrpc": "2.0", "id": 1, "result": "ok"}

    def request_start(root):
        events.append("request")
        trigger.maybe_drain(root)

    def request_end(root):
        events.append("completion-rearm")
        trigger.maybe_drain(root, spooled_after_last_drain=True)

    server._handle_connection(
        _IO(line, events), version_state=_VersionState(), server_sha="abc", close_listener=lambda: None,
        drain=lambda: None, in_flight=server.InFlightCounter(), dispatch=dispatch,
        record_served_repo=request_start, rearm_spool_drain=request_end, engine_root=repo,
    )


def _arm(monkeypatch, events: list) -> None:
    monkeypatch.setattr(server, "_run_ask_spool_drain", lambda root: events.append("drain"))
    monkeypatch.setattr(server, "_spawn_background", lambda fn, *a: fn(*a))


def test_ask_spooled_mid_request_drains_after_the_response_without_a_later_request(tmp_path, monkeypatch):
    events: list = []
    _arm(monkeypatch, events)
    trigger = server._DrainTrigger()
    _serve(tmp_path, trigger, events)
    events.clear()
    _serve(tmp_path, trigger, events, during_dispatch=lambda: _spool(tmp_path, "late", server.time.time() + 5))
    assert events == ["request", "dispatch", "response", "completion-rearm", "drain"]


def test_completion_does_not_redrain_an_entry_older_than_the_last_drain(tmp_path, monkeypatch):
    events: list = []
    _arm(monkeypatch, events)
    trigger = server._DrainTrigger()
    _serve(tmp_path, trigger, events)
    events.clear()
    _serve(tmp_path, trigger, events, during_dispatch=lambda: _spool(tmp_path, "failed", server.time.time() - 5))
    assert "drain" not in events[events.index("completion-rearm"):]


def test_completion_starts_no_drain_before_any_drain_ran(tmp_path, monkeypatch):
    events: list = []
    monkeypatch.setattr(server, "_run_ask_spool_drain", lambda root: events.append("drain"))
    held: list = []
    monkeypatch.setattr(server, "_spawn_background", lambda fn, *a: held.append((fn, a)))
    trigger = server._DrainTrigger()
    _spool(tmp_path, "pending", server.time.time() + 5)
    trigger.maybe_drain(tmp_path, spooled_after_last_drain=True)
    assert held == []


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _hung_trigger(tmp_path, monkeypatch):
    held: list = []
    monkeypatch.setattr(server, "_spawn_background", lambda fn, *a: held.append((fn, a)))
    monkeypatch.setattr(server, "_run_ask_spool_drain", lambda root: None)
    clock = _Clock()
    trigger = server._DrainTrigger(clock=clock)
    trigger.maybe_drain(tmp_path)
    _spool(tmp_path, "late", clock.now + 5)
    return trigger, clock, held


def test_stuck_bound_is_not_below_the_claim_staleness():
    from coordinator_core.ask_spool import drain

    assert server._DRAIN_STUCK_BOUND_S >= drain.STALE_CLAIM_S


def test_a_drain_hung_past_the_bound_is_replaced_once_per_bound_period(tmp_path, monkeypatch):
    trigger, clock, held = _hung_trigger(tmp_path, monkeypatch)
    clock.now += server._DRAIN_STUCK_BOUND_S - 1
    trigger.maybe_drain(tmp_path)
    assert len(held) == 1
    clock.now += 2
    trigger.maybe_drain(tmp_path)
    assert len(held) == 2
    trigger.maybe_drain(tmp_path)
    clock.now += server._DRAIN_STUCK_BOUND_S - 3
    trigger.maybe_drain(tmp_path)
    assert len(held) == 2
    clock.now += 4
    trigger.maybe_drain(tmp_path)
    assert len(held) == 3


def test_a_hung_drain_with_nothing_pending_is_not_replaced(tmp_path, monkeypatch):
    held: list = []
    monkeypatch.setattr(server, "_spawn_background", lambda fn, *a: held.append((fn, a)))
    clock = _Clock()
    trigger = server._DrainTrigger(clock=clock)
    trigger.maybe_drain(tmp_path)
    clock.now += server._DRAIN_STUCK_BOUND_S + 1
    trigger.maybe_drain(tmp_path)
    assert len(held) == 1


def test_the_hung_drain_finishing_late_leaves_its_replacement_running(tmp_path, monkeypatch):
    trigger, clock, held = _hung_trigger(tmp_path, monkeypatch)
    clock.now += server._DRAIN_STUCK_BOUND_S + 1
    trigger.maybe_drain(tmp_path)
    (old_fn, old_args), _replacement = held
    old_fn(*old_args)
    assert str(tmp_path) in trigger._running
    held[1][0](*held[1][1])
    assert str(tmp_path) not in trigger._running


def test_completion_never_doubles_a_running_drain(tmp_path, monkeypatch):
    held: list = []
    monkeypatch.setattr(server, "_run_ask_spool_drain", lambda root: None)
    monkeypatch.setattr(server, "_spawn_background", lambda fn, *a: held.append((fn, a)))
    trigger = server._DrainTrigger()
    trigger.maybe_drain(tmp_path)
    _spool(tmp_path, "late", server.time.time() + 5)
    trigger.maybe_drain(tmp_path, spooled_after_last_drain=True)
    assert len(held) == 1
