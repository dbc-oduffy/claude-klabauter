"""A Claude EM verified by its own host-session row is admitted for a headless fire; nothing else is."""
import json
import os
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from coordinator_core.ops.workflow_fire import authority
from coordinator_core.session import core, external_registry as reg
from coordinator_core.session.caller_env import bind_caller_env

SID = '217c73d7-2e34-4008-bb67-298d4f9c067a'
START = {'hook_event_name': 'SessionStart', 'source': 'startup', 'session_id': SID}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('COORDINATOR_SETTINGS_HOME', str(tmp_path))
    for key in ('COORDINATOR_AGENT_ROLE', 'COORDINATOR_AGENT_WORKER', 'COORDINATOR_PARENT_SESSION_ID'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(core, 'ensure_session', lambda *a, **k: None)
    return tmp_path


def _register(monkeypatch, tmp_path, *, host=None, payload=None):
    host = host or psutil.Process(os.getpid())
    monkeypatch.setattr(reg, '_claude_host', lambda: host)
    return reg.register_claude_current({**START, 'cwd': str(tmp_path), **(payload or {})})


def _fire(monkeypatch, env):
    monkeypatch.setattr(core, 'attributable_session_id', lambda cwd=None: SID)
    with bind_caller_env({'CLAUDE_CODE_SESSION_ID': SID, **env}):
        return authority.require_fire_authority('unused.mjs', None, cwd=None, native=True, backend='claude')


def test_row_lives_in_claude_registry_not_codex(home, monkeypatch):
    row = _register(monkeypatch, home)
    assert (home / 'host-sessions' / 'claude' / f'{SID}.json').is_file()
    assert not (home / 'host-sessions' / 'codex').exists()
    assert row['provider'] == 'claude' and row['role'] == 'em' and row['pid'] == os.getpid()
    assert reg.lookup_claude(SID)['role'] == 'em'
    assert reg.lookup(SID) is None  # the Codex path never sees a Claude row


def test_verified_claude_em_is_admitted_for_headless_fire(home, monkeypatch):
    _register(monkeypatch, home)
    assert _fire(monkeypatch, {}).role == 'em'


def test_claude_env_without_row_is_refused(home, monkeypatch):
    with pytest.raises(ValueError, match='caller role is unknown'):
        _fire(monkeypatch, {})


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_dead_pid_is_refused(home, monkeypatch):
    child = subprocess.Popen([sys.executable, '-c', 'pass'])
    host = psutil.Process(child.pid)
    _register(monkeypatch, home, host=host)
    child.wait()
    with pytest.raises(ValueError, match='caller role is unknown'):
        _fire(monkeypatch, {})


def test_birth_mismatch_is_refused(home, monkeypatch):
    _register(monkeypatch, home)
    path = home / 'host-sessions' / 'claude' / f'{SID}.json'
    row = json.loads(path.read_text())
    row['start_epoch'] -= 3600
    path.write_text(json.dumps(row))
    with pytest.raises(ValueError, match='caller role is unknown'):
        _fire(monkeypatch, {})


@pytest.mark.parametrize('env', [{'COORDINATOR_AGENT_WORKER': '1'}, {'COORDINATOR_AGENT_ROLE': 'worker'},
                                 {'COORDINATOR_AGENT_ROLE': 'reviewer'}])
def test_worker_marker_is_refused_even_with_em_row(home, monkeypatch, env):
    _register(monkeypatch, home)
    with pytest.raises(ValueError, match='verified EM'):
        _fire(monkeypatch, env)


def test_subagent_start_writes_no_binding_and_is_refused(home, monkeypatch):
    with pytest.raises(ValueError, match='subagent lifecycle'):
        _register(monkeypatch, home, payload={'agent_id': 'a1'})
    assert not (home / 'host-sessions' / 'claude').exists()
    with pytest.raises(ValueError, match='caller role is unknown'):
        _fire(monkeypatch, {})


def test_worker_env_at_start_records_worker_role(home, monkeypatch):
    monkeypatch.setenv('COORDINATOR_AGENT_WORKER', '1')
    assert _register(monkeypatch, home)['role'] == 'worker'


def test_no_claude_ancestor_writes_nothing(home, monkeypatch):
    monkeypatch.setattr(reg, '_claude_host', lambda: None)
    with pytest.raises(ValueError, match='no observed Claude ancestor'):
        reg.register_claude_current({**START, 'cwd': str(home)})
    assert not (home / 'host-sessions' / 'claude').exists()


def test_session_end_tombstone_revokes_admission(home, monkeypatch):
    _register(monkeypatch, home)
    reg.unregister_claude(SID)
    assert reg.lookup_claude(SID) is None
    with pytest.raises(ValueError, match='caller role is unknown'):
        _fire(monkeypatch, {})
