"""A restart backoff must not shield a successor server that is stalled re-importing.

Drives `decide` with a fake clock against a `tmp_path` runtime dir.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from coordinator_core.warm import hang_restart

pytestmark = pytest.mark.complete

T0 = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(hang_restart, "_progress_last", None)
    monkeypatch.setattr(hang_restart.os, "getpid", lambda: 200)


def _seed_restart(svc: Path, pid: int) -> None:
    stamp = time.strftime(hang_restart._LINE_STAMP_FMT, time.gmtime(T0))
    (svc / hang_restart.HANG_RESTART_LOG_FILENAME).write_text(
        f"{stamp} pid={pid} action=restart count=3 window_s=10 backoff_s=0 dumps=\n", encoding="utf-8"
    )


def _log(svc: Path) -> str:
    return (svc / hang_restart.HANG_RESTART_LOG_FILENAME).read_text(encoding="utf-8")


def test_stalled_successor_restarts_inside_backoff(tmp_path: Path) -> None:
    _seed_restart(tmp_path, 100)
    verdict = hang_restart.decide(tmp_path, server_pid=200, source="t", now=T0 + 5)
    assert verdict == "restart"
    assert "pid=200 action=restart reason=stalled-successor" in _log(tmp_path)


def test_same_pid_still_backs_off(tmp_path: Path) -> None:
    _seed_restart(tmp_path, 100)
    assert hang_restart.decide(tmp_path, server_pid=100, source="t", now=T0 + 5) == "suppressed"
    assert "reason=backoff" in _log(tmp_path)


def test_successor_that_progressed_still_backs_off(tmp_path: Path) -> None:
    _seed_restart(tmp_path, 100)
    hang_restart.note_progress()
    assert hang_restart.decide(tmp_path, server_pid=200, source="t", now=T0 + 5) == "suppressed"
    assert "reason=backoff" in _log(tmp_path)


def test_hold_still_shields_a_stalled_successor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_restart(tmp_path, 100)
    monkeypatch.setattr(hang_restart, "_hold_reason", lambda svc, now: "publish-hold")
    assert hang_restart.decide(tmp_path, server_pid=200, source="t", now=T0 + 5) == "suppressed"
    assert "reason=publish-hold" in _log(tmp_path)


def test_foreign_pid_is_not_judged_by_this_process_progress(tmp_path: Path) -> None:
    _seed_restart(tmp_path, 100)
    assert hang_restart.decide(tmp_path, server_pid=300, source="t", now=T0 + 5) == "suppressed"
    assert "reason=backoff" in _log(tmp_path)
