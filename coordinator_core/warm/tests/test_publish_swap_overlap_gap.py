"""A publish swap leaves no window where the old pair stops evaluating before its successor answers.

The production arm boots real pairs, so it sits in the `complete` tier; the request-handler cases
drive `_serve_line` with fakes and stay in the fast tier.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from coordinator_core.benchmarks import land_swap_hook_window as lw
from coordinator_core.warm import server, skew

_BODY = {"jsonrpc": "2.0", "id": "x", "method": "ping", "params": {}}


class _State:
    """`token_stale` is the generation-binding axis; `source_stale` the sticky axis-2 flag a
    publish copy raises on the old pair."""

    def __init__(self, *, token_stale: bool, source_stale: bool = False) -> None:
        self.token_stale = token_stale
        self.source_stale = source_stale

    def is_skewed(self, client_token: str) -> bool:
        return self.token_stale or self.source_stale

    def is_token_skewed(self, client_token: str) -> bool:
        return self.token_stale


def _serve(monkeypatch, state, *, window: bool, dispatch, method: str = "ping"):
    monkeypatch.setattr(server._engine_landing, "swap_in_progress", lambda root: window)
    written: list[bytes] = []
    events: list[str] = []
    server._serve_line(
        (json.dumps({**_BODY, "method": method, "_engine_token": "t"}) + "\n").encode(),
        write=written.append,
        version_state=state,
        server_sha="deadbeef",
        close_listener=lambda: events.append("close"),
        drain=lambda: events.append("drain"),
        release_in_flight=lambda: events.append("release"),
        record_exit=lambda reason, detail=None: events.append("exit-record"),
        dispatch=dispatch,
        engine_root=Path("."),
    )
    return [json.loads(w) for w in written], events


def _ok(msg, **_kw):
    return {"jsonrpc": "2.0", "id": msg["id"], "result": "evaluated"}


def test_swap_window_serves_a_request_bound_to_this_generation(monkeypatch) -> None:
    replies, events = _serve(monkeypatch, _State(token_stale=False), window=True, dispatch=_ok)
    assert replies[0]["result"] == "evaluated"
    assert events == ["release"]


def test_swap_window_ignores_the_source_axis_the_copy_itself_raises(monkeypatch) -> None:
    replies, events = _serve(
        monkeypatch, _State(token_stale=False, source_stale=True), window=True, dispatch=_ok
    )
    assert replies[0]["result"] == "evaluated"
    assert "close" not in events and "drain" not in events and "exit-record" not in events


def test_swap_window_still_refuses_a_request_bound_to_another_generation(monkeypatch) -> None:
    dispatched: list = []
    replies, events = _serve(
        monkeypatch,
        _State(token_stale=True),
        window=True,
        dispatch=lambda msg, **kw: dispatched.append(msg) or _ok(msg),
    )
    assert replies[0]["error"]["code"] == skew.ENGINE_SKEW
    assert dispatched == [] and events == ["release"]


def test_swap_window_refuses_when_evaluation_on_the_half_copied_tree_fails(monkeypatch) -> None:
    def _boom(msg, **_kw):
        raise ImportError("half-copied tree")

    replies, events = _serve(monkeypatch, _State(token_stale=False), window=True, dispatch=_boom)
    assert replies[0]["error"]["code"] == skew.ENGINE_SKEW
    assert "close" not in events and "drain" not in events


def test_publish_marker_alone_opens_the_swap_window(monkeypatch) -> None:
    monkeypatch.setattr(server.breadcrumb, "publish_marker_held", lambda svc: True)
    replies, events = _serve(
        monkeypatch, _State(token_stale=False, source_stale=True), window=False, dispatch=_ok
    )
    assert replies[0]["result"] == "evaluated"
    assert "close" not in events and "drain" not in events and "exit-record" not in events


def test_outside_a_swap_window_the_source_axis_still_evicts(monkeypatch) -> None:
    monkeypatch.setattr(server.breadcrumb, "publish_marker_held", lambda svc: False)
    replies, events = _serve(
        monkeypatch, _State(token_stale=False, source_stale=True), window=False, dispatch=_ok
    )
    assert replies[0]["error"]["code"] == skew.ENGINE_SKEW
    assert "close" in events and "drain" in events and "exit-record" in events


def _conftest_module():
    return next(
        m for m in list(sys.modules.values()) if hasattr(m, "ForeignProcessKill") and hasattr(m, "_is_own_process_tree")
    )


def _under(root: Path, pid: int) -> bool:
    """True iff process `pid` names a path under `root` on its command line or runs with it as
    working directory (a multiprocessing child carries only the cwd)."""
    import psutil

    try:
        proc = psutil.Process(pid)
        return any(str(root) in part for part in proc.cmdline()) or str(root) in proc.cwd()
    except psutil.Error:
        return False


@pytest.fixture
def stop_of_detached_pair_under_tmp_allowed(monkeypatch, tmp_path: Path):
    """The swap's production stop signals the detached pair it booted; the conftest tripwire
    refuses any pid outside the test's process tree, and a detached spawn leaves it. Opens the
    tripwire for processes whose command line names this test's tmp root only."""
    conftest = _conftest_module()
    real = conftest._is_own_process_tree
    monkeypatch.setattr(conftest, "_is_own_process_tree", lambda pid: real(pid) or _under(tmp_path, pid))
    return conftest


def test_tripwire_exemption_still_refuses_a_process_outside_the_tmp_root(
    stop_of_detached_pair_under_tmp_allowed, tmp_path: Path
) -> None:
    conftest = stop_of_detached_pair_under_tmp_allowed
    assert conftest._is_own_process_tree(os.getppid()) is False
    with pytest.raises(conftest.ForeignProcessKill):
        conftest._refuse_foreign(os.getppid(), "psutil.Process.terminate")


@pytest.mark.complete
@pytest.mark.spawns_process
def test_production_swap_evaluates_every_guard_and_bounds_the_handover_gap(
    stop_of_detached_pair_under_tmp_allowed, tmp_path: Path
) -> None:
    report = lw.run_production_swap(copy_secs=1.0, work_dir=tmp_path)

    assert report.requests > 0
    assert report.successor_answered_before_release is True
    assert report.unevaluated_guard_count == 0, report
    assert report.swap_window_fail_open_count == 0, report
    assert report.handover_gap_process_ms is not None, report
    assert report.handover_gap_process_ms <= lw.HANDOVER_GAP_PROCESS_MS_BUDGET, report
