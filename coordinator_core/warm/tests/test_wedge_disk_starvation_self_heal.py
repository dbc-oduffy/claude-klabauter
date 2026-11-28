"""Under induced disk starvation the warm engine recovers: wedged server killed, stalled successor
killed again inside the backoff, and a guard hook still gets a real verdict.

Starvation is an in-process blocking loader seam (an Event held on the module import / backend
read), never an OS disk tool; all paths live under ``tmp_path``. The restart leg drives the real
``hang_restart.maybe_restart`` at its default threshold, window and backoff; the hook leg drives
the real forwarder ``POST /hook``.
"""

from __future__ import annotations

import importlib
import json
import os
import socket
import sys
import threading
import time
from http.client import HTTPConnection
from pathlib import Path

import pydantic  # noqa: F401 -- bind its lazy loader to the real import_module before any test patches it
import pytest

from coordinator_core import machine_profile
from coordinator_core.ops._registry_map import OP_MODULE_MAP
from coordinator_core.warm import hang_restart, server
from coordinator_core.warm import http_hook_forwarder as forwarder

HOLD_CEILING_SECS = 30
T0 = 1_800_000_000.0
FIRST_PID = 100
SUCCESSOR_PID = os.getpid()  # the stalled-successor gate only judges the process it runs in


class _StarvedDisk:
    """A loader/read seam that parks its caller until released, as a starved disk does."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def read(self) -> None:
        self.entered.set()
        self.release.wait(HOLD_CEILING_SECS)


@pytest.fixture(autouse=True)
def _fresh_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hang_restart, "_progress_last", None)
    monkeypatch.setattr(hang_restart, "_progress_excused_since", {})


def _write_dumps(stall_dir: Path, first_epoch: int, tag: str) -> None:
    """HANG_DUMP_THRESHOLD hung-stall records, one second apart, stamped from `first_epoch`."""
    stall_dir.mkdir(parents=True, exist_ok=True)
    for i in range(hang_restart.HANG_DUMP_THRESHOLD):
        stamp = time.strftime(hang_restart._STAMP_FMT, time.gmtime(first_epoch + i))
        (stall_dir / f"{stamp}-{tag}-{i}.txt").write_text("op=starved import_module\n", encoding="utf-8")


def _log(svc: Path) -> str:
    return (svc / hang_restart.HANG_RESTART_LOG_FILENAME).read_text(encoding="utf-8")


def _starved_server_generation(disk: _StarvedDisk) -> threading.Thread:
    """One server generation whose dispatch is parked in the starved loader."""
    t = threading.Thread(target=disk.read, daemon=True, name="starved-dispatch")
    t.start()
    assert disk.entered.wait(HOLD_CEILING_SECS)
    return t


def test_wedged_server_and_stalled_successor_are_both_killed_inside_the_backoff(tmp_path: Path) -> None:
    svc = tmp_path / "svc"
    stall_dir = svc / "stalls"
    killed: list = []
    disk = _StarvedDisk()
    try:
        _starved_server_generation(disk)
        _write_dumps(stall_dir, int(T0), "first")
        first = hang_restart.maybe_restart(
            stall_dir, server_pid=FIRST_PID, now=T0 + 10, exit_fn=killed.append
        )
        assert first == "restart"

        # The successor re-imports under the same starvation: it never completes an op.
        _write_dumps(stall_dir, int(T0) + 20, "successor")
        second = hang_restart.maybe_restart(
            stall_dir, server_pid=SUCCESSOR_PID, now=T0 + 30, exit_fn=killed.append
        )
    finally:
        disk.release.set()

    assert second == "restart"
    assert killed == [FIRST_PID, SUCCESSOR_PID]
    log = _log(svc)
    assert f"pid={FIRST_PID} action=restart " in log
    assert f"pid={SUCCESSOR_PID} action=restart reason={hang_restart.STALLED_SUCCESSOR_REASON}" in log
    assert 20 < hang_restart.RESTART_MIN_INTERVAL_SECS  # the second kill (T0+30) landed inside the backoff of the first (T0+10)


def test_a_successor_that_progressed_still_honours_the_backoff(tmp_path: Path) -> None:
    svc = tmp_path / "svc"
    stall_dir = svc / "stalls"
    killed: list = []
    _write_dumps(stall_dir, int(T0), "first")
    assert hang_restart.maybe_restart(stall_dir, server_pid=FIRST_PID, now=T0 + 10, exit_fn=killed.append) == "restart"

    hang_restart.note_progress()
    _write_dumps(stall_dir, int(T0) + 20, "successor")
    out = hang_restart.maybe_restart(stall_dir, server_pid=SUCCESSOR_PID, now=T0 + 30, exit_fn=killed.append)

    assert out == "suppressed"
    assert killed == [FIRST_PID]
    assert "reason=backoff" in _log(svc) or "reason=progress" in _log(svc)


def test_boot_preimport_leaves_dispatch_no_read_for_a_starved_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    server._prewarm_op_modules()
    disk = _StarvedDisk()
    real = importlib.import_module

    def starved(name, *a):
        disk.read()
        return real(name, *a)

    monkeypatch.setattr(importlib, "import_module", starved)
    monkeypatch.setattr(server.dispatch_ack, "begin_import_phase", lambda *a, **k: object())
    monkeypatch.setattr(server.dispatch_ack, "end_import_phase", lambda marker: True)
    try:
        for method in OP_MODULE_MAP:
            assert server._import_op_module({"method": method}, None, None) is True
        assert not disk.entered.is_set()

        method = next(iter(OP_MODULE_MAP))
        module = OP_MODULE_MAP[method]
        monkeypatch.delitem(sys.modules, module)
        disk.release.set()  # an unwarmed op does reach the loader seam
        server._import_op_module({"method": method}, None, None)
        assert disk.entered.is_set()
    finally:
        disk.release.set()
        monkeypatch.setitem(sys.modules, module, real(module))


def test_cold_chain_is_resident_before_a_starved_backend_needs_it() -> None:
    forwarder._preimport_cold_chain()
    assert "coordinator_core.warm.hook_http" in sys.modules
    assert forwarder._HEDGE_DEADLINE_SECS < forwarder._FORWARD_READ_TIMEOUT_SECS


def test_a_guard_hook_gets_a_real_verdict_through_the_forwarder_while_the_backend_is_starved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(machine_profile, "resolved_level", lambda _n, subagent=False: "floor")
    monkeypatch.delenv("COORDINATOR_OVERRIDE_NO_VERIFY", raising=False)
    monkeypatch.setattr(forwarder, "degrade_log_path", lambda: tmp_path / "degrade.jsonl")
    forwarder._preimport_cold_chain()

    stub = socket.socket()
    stub.bind(("127.0.0.1", 0))
    stub.listen(8)
    held: list = []

    def _accept_and_never_answer() -> None:
        while True:
            try:
                held.append(stub.accept()[0])
            except OSError:
                return

    threading.Thread(target=_accept_and_never_answer, daemon=True).start()
    stub_port = stub.getsockname()[1]
    monkeypatch.setattr(
        forwarder, "_resolve_backend", lambda _k: (("127.0.0.1", stub_port, "/hook", None), None, None)
    )
    hedge = 0.5
    monkeypatch.setattr(forwarder, "_HEDGE_DEADLINE_SECS", hedge)

    fwd = forwarder._ExclusiveServer(("127.0.0.1", 0), forwarder._ForwarderHandler)
    fwd.dial_counter = forwarder.DialCounter(tmp_path / "dial.json")
    threading.Thread(target=fwd.serve_forever, daemon=True).start()
    try:
        event = {
            "hook_event_name": "PreToolUse",
            "session_id": "s-starved",
            "cwd": "C:/Windows/Temp",
            "tool_name": "Bash",
            "tool_input": {"command": "git commit --no-verify -m x -- foo.py"},
            "env": {},
        }
        conn = HTTPConnection("127.0.0.1", fwd.server_address[1], timeout=20)
        started = time.monotonic()
        conn.request(
            "POST",
            "/hook",
            body=json.dumps(event),
            headers={"Content-Type": "application/json", forwarder.ROUTING_HEADER_NAME: "x"},
        )
        resp = conn.getresponse()
        data = json.loads(resp.read())
        elapsed = time.monotonic() - started
        conn.close()
    finally:
        fwd.shutdown()
        fwd.server_close()
        stub.close()
        for sock in held:
            sock.close()

    assert resp.status == 200
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert elapsed < forwarder._FORWARD_READ_TIMEOUT_SECS
    rows = [json.loads(x) for x in (tmp_path / "degrade.jsonl").read_text().splitlines()]
    assert any(
        r.get("rung") == forwarder.RUNG_COLD and r.get("cause") == forwarder.CAUSE_BACKEND_WEDGED for r in rows
    )
