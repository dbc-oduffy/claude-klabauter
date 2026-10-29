"""Disk saturation over the seven hot-path sites: each returns inside its budget with a named outcome.

A saturated disk is modelled by blocking the IO primitives under a sandbox on an event (the
probe's ``_install_stall``); no real disk thrash and no path outside ``tmp_path``. Every case
asserts the caller returns inside ``BUDGET_S`` while the primitive is still blocked, and the
site's named miss outcome.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path

import pytest

from coordinator_core import bounded_io
from coordinator_core.tests import hot_path_stall_probe as probe

BUDGET_S = 0.25


@pytest.fixture
def saturated(tmp_path, monkeypatch):
    """Yield ``(sandbox, sites, gate)`` with every IO primitive under the sandbox blocked until teardown."""
    sandbox = Path(os.path.realpath(tmp_path)) / "sbx"
    sandbox.mkdir()
    sites = dict(probe._build_sites(sandbox, monkeypatch.setattr))
    gate = {"ev": threading.Event()}
    undo = probe._install_stall(str(sandbox), gate)
    try:
        yield sandbox, sites, gate
    finally:
        gate["ev"].set()
        undo()
        deadline = time.monotonic() + 5
        while (bounded_io._inflight or bounded_io._stalled) and time.monotonic() < deadline:
            time.sleep(0.01)


def _timed(call):
    t0 = time.perf_counter()
    try:
        return call(), time.perf_counter() - t0
    except BaseException as exc:  # noqa: BLE001 - the outcome under test may be a raise
        return exc, time.perf_counter() - t0


def _drop_case(saturated, capsys, name):
    _, sites, _ = saturated
    result, elapsed = _timed(sites[name])
    assert elapsed < BUDGET_S, f"{name} held the caller {elapsed:.3f}s"
    assert result is None, f"{name} returned {result!r}, not the dropped outcome"
    assert "bounded_io: stalled" in capsys.readouterr().err


def test_em_turn_count_save_state_drops(saturated, capsys):
    _drop_case(saturated, capsys, "em_turn_count.save_state")
    sandbox = saturated[0]
    assert not (sandbox / "s" / "state.json").exists()


def test_registry_fallback_counter_drops(saturated, capsys):
    _drop_case(saturated, capsys, "registry_fallback_counter.record_registry_fallback")


def test_guard_advisory_fire_drops(saturated, capsys):
    _drop_case(saturated, capsys, "guard_advisory_counter.record_advisory_fire")


def test_guard_deny_fire_drops(saturated, capsys):
    _drop_case(saturated, capsys, "guard_advisory_counter.record_deny_fire")


def test_handoff_twin_scan_fails_closed(saturated):
    from coordinator_core.handoff_creation_guard import HandoffTwinScanStalled

    _, sites, _ = saturated
    result, elapsed = _timed(sites["handoff_creation_guard.find_archived_twin_by_handoff_id"])
    assert elapsed < BUDGET_S, f"twin scan held the caller {elapsed:.3f}s"
    assert isinstance(result, HandoffTwinScanStalled)


def test_prep_gate_dr_scan_skips_the_check(saturated, capsys):
    _, sites, _ = saturated
    result, elapsed = _timed(sites["prep_gate._new_dr_number_pinned"])
    assert elapsed < BUDGET_S, f"DR scan held the caller {elapsed:.3f}s"
    assert result is None
    assert "DR number check skipped" in capsys.readouterr().err


def test_sibling_release_read_miss_is_named(saturated):
    sandbox, sites, _ = saturated
    result, elapsed = _timed(sites["run_close.sibling_release"])
    assert elapsed < BUDGET_S, f"sibling_release held the caller {elapsed:.3f}s"
    assert isinstance(result, dict)
    assert result["status_write"] == "deadline-missed" and result["phase"] == "read"
    assert result["run_id"] == probe.SIBLING_RUN_ID
    assert result["released"] == [] and result["blocked"] == []
    assert Path(result["path"]).is_file()


def _stall_mkstemp(monkeypatch, sandbox):
    """Block only the write leg (``tempfile.mkstemp``, the frame of the live 09:05Z wedge)."""
    ev = threading.Event()
    real = tempfile.mkstemp

    def blocked(*a, **k):
        if str(k.get("dir", "")).startswith(str(sandbox)):
            ev.wait()
        return real(*a, **k)

    monkeypatch.setattr(tempfile, "mkstemp", blocked)
    return ev


def test_sibling_release_write_miss_is_named_and_reconcilable(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import run_close

    sandbox = Path(os.path.realpath(tmp_path)) / "sbx"
    sandbox.mkdir()
    dict(probe._build_sites(sandbox, monkeypatch.setattr))
    ev = _stall_mkstemp(monkeypatch, sandbox)
    try:
        result, elapsed = _timed(lambda: run_close.sibling_release(sandbox, probe.SIBLING_RUN_ID, None))
    finally:
        ev.set()
    assert elapsed < BUDGET_S, f"sibling_release write held the caller {elapsed:.3f}s"
    assert result["status_write"] == "deadline-missed" and result["phase"] == "write"
    assert result["released"] == [] and result["blocked"] == []
    assert Path(result["path"]).is_file()


def test_land_run_halt_returns_the_named_outcome(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import land_run_halt

    sandbox = Path(os.path.realpath(tmp_path)) / "sbx"
    sandbox.mkdir()
    dict(probe._build_sites(sandbox, monkeypatch.setattr))
    monkeypatch.setattr(land_run_halt, "_land", lambda *_a, **_k: {"committed": True, "sha": "abc"})
    ev = _stall_mkstemp(monkeypatch, sandbox)
    params = {land_run_halt.HALTED_RECORD_PARAM: {"halted": "x"}, "run_id": probe.SIBLING_RUN_ID}
    try:
        reply, elapsed = _timed(lambda: land_run_halt.land(sandbox, sandbox, params))
    finally:
        ev.set()
    assert elapsed < BUDGET_S, f"land held the caller {elapsed:.3f}s"
    assert reply["committed"] is True
    siblings = reply["siblings"]
    assert siblings["status_write"] == "deadline-missed" and siblings["run_id"] == probe.SIBLING_RUN_ID
    assert siblings["released"] == [] and siblings["blocked"] == []


def test_healthy_twin_scan_of_the_1757_record_archive_holds_the_budget(tmp_path):
    from coordinator_core.handoff_creation_guard import find_archived_twin_by_handoff_id

    sandbox = Path(os.path.realpath(tmp_path)) / "sbx"
    probe.build_archive(sandbox)
    cpu0 = time.process_time()
    assert find_archived_twin_by_handoff_id("absent-id", sandbox) is None
    assert time.process_time() - cpu0 < BUDGET_S
