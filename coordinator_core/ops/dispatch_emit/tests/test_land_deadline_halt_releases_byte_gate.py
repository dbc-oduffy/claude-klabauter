"""A land-deadline halt must leave the halt-commit of the same run admitted by the land byte gate.

Reproduces run ask-20261113T234117-5fab03: its halt-commit was refused `queued` because the gate was
held by the run's own label. One case per candidate path through `land_run._dispatch`; "red" means the
halt-commit is refused `queued` at HEAD (the `pending_fix` cases, which C3 turns green):

  (a) halted params carrying the run id only under `halted_by` admit with an empty label: RED.
      A top-level `run_id` (the digest shape) is admitted: green.
  (b) the first call's thread still inside the gate (abandoned dispatcher) when the halt-commit of
      the same run id arrives on another thread: green, the gate re-enters on the same label.
  (c) `_deadline_halt` running inside the gate: the gate is held for its span (pinned), and the
      same-run halt-commit is admitted meanwhile: green. After `_dispatch` returns the gate is free.
  (d) a halt-commit whose run id differs in form from the holder's label (warp id vs ask id) queues: RED.

A different run's label queues in every holder state, so none of these passes by an open gate.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from coordinator_core import dispatch_abandon, locked_write
from coordinator_core.ops.dispatch_emit import land_byte_gate, land_deadline, land_leg_record, land_resume, land_run

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ASK_ID = "ask-20261113T234117-5fab03"
WARP_ID = "warp-2026-11-13-land-legs-fit-budget-and-deadline-gate-release-122ce6-20261113T235115-ca442a"
OTHER_ID = "ask-20261113T000000-aaaaaa"
BUDGET = land_deadline.LAND_BUDGET_SECS
JOIN_SECS = 15


class _Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=str(root), capture_output=True, check=True, creationflags=_NO_WINDOW
    )
    real = locked_write.held_lock

    def anchored(target, **kw):
        return real(target, **{**kw, "anchor_root": root})

    monkeypatch.setattr(land_byte_gate, "held_lock", anchored)
    monkeypatch.setattr(land_leg_record, "write", lambda *a, **k: None)
    monkeypatch.setattr(land_resume, "enqueue", lambda *a, **k: False)
    return root


@pytest.fixture
def window(monkeypatch):
    """Bound the dispatcher window so a queued call times out inside ~0.8s."""
    deadline = time.monotonic() + dispatch_abandon.REPLY_MARGIN_SECS + 0.8
    monkeypatch.setattr(dispatch_abandon, "remaining", lambda: deadline - time.monotonic())


class _Calls:
    """Stubs `_dispatch_mode`: a product call overruns the land deadline after `before_deadline`;
    a halted call records that it ran."""

    def __init__(self, monkeypatch) -> None:
        self.clock = _Clock()
        monkeypatch.setattr(land_deadline, "_now", self.clock)
        self.inside = threading.Event()
        self.release = threading.Event()
        self.block_before_deadline = False
        self.halted_ran = []
        self.gate_held_in_halt = []
        monkeypatch.setattr(land_run, "_dispatch_mode", self._mode)

    def _mode(self, mode, root, repo_root, params):
        if mode == "halted":
            self.halted_ran.append(params)
            return {"committed": True}
        self.inside.set()
        if self.block_before_deadline:
            assert self.release.wait(JOIN_SECS)
        self.clock.now += BUDGET + 1.0
        land_deadline.check("land_run.dirty_scan")
        return {"committed": True}  # pragma: no cover

    def product(self, repo_root: Path, run_id: str) -> dict:
        return land_run._dispatch({"script_path": "s.py", "run_id": run_id}, repo_root / ".git")

    def halt_commit(self, repo_root: Path, run_id: str, *, top_level: bool = True) -> dict:
        halted_by = {"halted": "land-deadline", "reason": "land_run.dirty_scan"}
        params = {"halted_by": halted_by, **({"run_id": run_id} if top_level else {})}
        if not top_level:
            halted_by["run_id"] = run_id
        return land_run._dispatch(params, repo_root / ".git")


def _in_thread(fn, *args):
    out = {}

    def run():
        try:
            out["reply"] = fn(*args)
        except BaseException as exc:  # noqa: BLE001 -- surfaced by the asserting caller
            out["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, out


def _queued(reply: dict) -> bool:
    return reply.get("refused") == "queued"


def _gate_free(repo: Path) -> bool:
    try:
        with land_byte_gate.admit(repo, 0.5, holder_label=OTHER_ID):
            return True
    except land_byte_gate.LandAdmissionTimeout:
        return False


def test_halt_commit_after_the_deadline_halt_returned_is_admitted_and_the_gate_is_free(repo, monkeypatch):
    calls = _Calls(monkeypatch)
    first = calls.product(repo, ASK_ID)
    assert first.get("refused") != "queued" and "leg_costs" in first
    assert _gate_free(repo)
    reply = calls.halt_commit(repo, ASK_ID)
    assert not _queued(reply) and calls.halted_ran


def test_b_same_run_halt_commit_is_admitted_while_the_first_call_is_inside_the_gate(repo, monkeypatch, window):
    calls = _Calls(monkeypatch)
    calls.block_before_deadline = True
    thread, out = _in_thread(calls.product, repo, ASK_ID)
    try:
        assert calls.inside.wait(JOIN_SECS)
        reply = calls.halt_commit(repo, ASK_ID)
        assert not _queued(reply) and calls.halted_ran
    finally:
        calls.release.set()
        thread.join(JOIN_SECS)
    assert "error" not in out


def test_b_a_different_run_still_queues_while_the_first_call_is_inside_the_gate(repo, monkeypatch, window):
    calls = _Calls(monkeypatch)
    calls.block_before_deadline = True
    thread, out = _in_thread(calls.product, repo, ASK_ID)
    try:
        assert calls.inside.wait(JOIN_SECS)
        reply = calls.halt_commit(repo, OTHER_ID)
        assert _queued(reply) and not calls.halted_ran
    finally:
        calls.release.set()
        thread.join(JOIN_SECS)
    assert "error" not in out


def _hold_in_deadline_halt(calls, monkeypatch, repo, gate_probe):
    """Run a product call whose `_deadline_halt` blocks; returns (thread, out, release_event)."""
    release = threading.Event()
    entered = threading.Event()
    real = land_run._deadline_halt

    def slow_halt(root, params, exc):
        gate_probe.append(not _gate_free_fast(repo))
        entered.set()
        assert release.wait(JOIN_SECS)
        return real(root, params, exc)

    monkeypatch.setattr(land_run, "_deadline_halt", slow_halt)
    thread, out = _in_thread(calls.product, repo, ASK_ID)
    assert entered.wait(JOIN_SECS)
    return thread, out, release


def _gate_free_fast(repo: Path) -> bool:
    """Probed from a fresh thread: the holder's own context re-enters its gate and would read free."""

    def probe() -> bool:
        try:
            with land_byte_gate.admit(repo, 0.0, holder_label=OTHER_ID):
                return True
        except land_byte_gate.LandAdmissionTimeout:
            return False

    thread, out = _in_thread(probe)
    thread.join(JOIN_SECS)
    return out["reply"]


def test_c_gate_is_held_through_deadline_halt_and_the_same_run_halt_commit_is_admitted(repo, monkeypatch, window):
    calls = _Calls(monkeypatch)
    probe = []
    thread, out, release = _hold_in_deadline_halt(calls, monkeypatch, repo, probe)
    try:
        assert probe == [True]
        reply = calls.halt_commit(repo, ASK_ID)
        assert not _queued(reply) and calls.halted_ran
    finally:
        release.set()
        thread.join(JOIN_SECS)
    assert "error" not in out and _gate_free(repo)


def test_c_a_different_run_queues_while_deadline_halt_holds_the_gate(repo, monkeypatch, window):
    calls = _Calls(monkeypatch)
    thread, out, release = _hold_in_deadline_halt(calls, monkeypatch, repo, [])
    try:
        assert _queued(calls.halt_commit(repo, OTHER_ID))
    finally:
        release.set()
        thread.join(JOIN_SECS)
    assert "error" not in out


@pytest.mark.pending_fix
def test_a_halted_params_carrying_the_run_id_only_under_halted_by_are_admitted(repo, monkeypatch, window):
    calls = _Calls(monkeypatch)
    calls.block_before_deadline = True
    thread, out = _in_thread(calls.product, repo, ASK_ID)
    try:
        assert calls.inside.wait(JOIN_SECS)
        reply = calls.halt_commit(repo, ASK_ID, top_level=False)
        assert not _queued(reply)
    finally:
        calls.release.set()
        thread.join(JOIN_SECS)
    assert "error" not in out


@pytest.mark.pending_fix
def test_d_halt_commit_run_id_in_another_form_than_the_holder_label_is_admitted(repo, monkeypatch, window):
    calls = _Calls(monkeypatch)
    calls.block_before_deadline = True
    thread, out = _in_thread(calls.product, repo, ASK_ID)
    try:
        assert calls.inside.wait(JOIN_SECS)
        reply = calls.halt_commit(repo, WARP_ID)
        assert not _queued(reply)
    finally:
        calls.release.set()
        thread.join(JOIN_SECS)
    assert "error" not in out
