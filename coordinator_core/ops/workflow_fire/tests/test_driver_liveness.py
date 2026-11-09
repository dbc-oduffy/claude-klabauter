"""Completion probes never spawn tasklist or wait for a live driver."""
import os
from types import SimpleNamespace

import pytest

from coordinator_core.ops.workflow_fire import fire


@pytest.mark.parametrize('state,expected', [(True, True), (False, False), (None, True)])
def test_windows_native_probe_never_spawns(monkeypatch, state, expected):
    from coordinator_core.ask_spool import record

    monkeypatch.setattr(fire.sys, 'platform', 'win32')
    monkeypatch.setattr(record, 'win_pid_state', lambda pid: state)
    monkeypatch.setattr(fire.subprocess, 'run', lambda *a, **k: pytest.fail('probe spawned'))
    assert fire._pid_alive(123) is expected


@pytest.mark.parametrize('status,expected', [('running', True), ('zombie', False)])
def test_posix_zombie_is_terminal_without_reaping(monkeypatch, status, expected):
    import psutil

    monkeypatch.setattr(fire.sys, 'platform', 'linux')
    monkeypatch.setattr(psutil, 'Process', lambda pid: SimpleNamespace(
        is_running=lambda: True, status=lambda: status))
    monkeypatch.setattr(fire.os, 'waitpid', lambda *a: pytest.fail('probe reaped'), raising=False)
    assert fire._pid_alive(123) is expected


def test_own_live_process_is_alive():
    assert fire._pid_alive(os.getpid())


@pytest.mark.parametrize('pid', [None, 0, -1, True, '123'])
def test_invalid_pid_is_not_alive(pid):
    assert not fire._pid_alive(pid)
