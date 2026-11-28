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


def _running(tmp_path, monkeypatch, fire_id, **fields):
    registry = tmp_path / 'registry'
    registry.mkdir(exist_ok=True)
    monkeypatch.setattr(fire, '_registry_dir', lambda cwd=None: registry)
    log = tmp_path / 'fire.log'
    log.write_text('', encoding='utf-8')
    record = {'fire_id': fire_id, 'pid': os.getpid(), 'state': 'running', 'exit_code': None,
              'log_path': str(log), 'log_size_bytes': 0, 'started_at': 1_000_000.0, **fields}
    fire._write_record(fire._record_path(registry, fire_id), record)
    return fire_id


def _own_birth():
    import psutil

    return psutil.Process().create_time()


def test_matching_birth_stays_live_and_holds_its_slot(tmp_path, monkeypatch):
    fid = _running(tmp_path, monkeypatch, 'a' * 32, pid_birth=_own_birth())
    assert fire.fire_status(fid)['state'] == 'running'
    assert fire.count_live_fires(notify=False) == 1


def test_reused_pid_with_a_different_birth_is_dead_and_frees_its_slot(tmp_path, monkeypatch):
    fid = _running(tmp_path, monkeypatch, 'b' * 32, pid_birth=_own_birth() - 100.0)
    assert fire.fire_status(fid)['state'] == 'exited'
    assert fire.count_live_fires(notify=False) == 0


@pytest.mark.parametrize('fields', [
    {'pid_birth': 1_000_000.0},
    {'pid_birth': None, 'pid_birth_denied': True},
    {'pid_birth': None},
])
def test_access_denied_current_birth_is_dead_and_frees_its_slot(tmp_path, monkeypatch, fields):
    fid = _running(tmp_path, monkeypatch, 'c' * 32, **fields)
    monkeypatch.setattr(fire, '_pid_birth', lambda pid: None)
    assert fire.fire_status(fid)['state'] == 'exited'
    assert fire.count_live_fires(notify=False) == 0


def test_access_denied_pid_probe_with_unreadable_birth_is_dead(tmp_path, monkeypatch):
    import psutil

    fid = _running(tmp_path, monkeypatch, 'd' * 32, pid_birth=_own_birth())
    monkeypatch.setattr(fire.sys, 'platform', 'linux')
    monkeypatch.setattr(psutil, 'Process', lambda pid: (_ for _ in ()).throw(psutil.AccessDenied(pid)))
    monkeypatch.setattr(fire, '_pid_birth', lambda pid: None)
    assert fire.fire_status(fid)['state'] == 'exited'
    assert fire.count_live_fires(notify=False) == 0


@pytest.mark.parametrize('pid', [None, 0, -1, True, '123'])
def test_invalid_pid_is_not_alive(pid):
    assert not fire._pid_alive(pid)
