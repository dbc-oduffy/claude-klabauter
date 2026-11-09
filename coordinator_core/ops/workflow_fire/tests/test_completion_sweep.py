"""External submit/fire completes through the warm watchdog without caller polls."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from coordinator_core.agent_pipe import bridge
from coordinator_core.ops import engine_ask_submit
from coordinator_core.ops.workflow_fire import completion, fire, op


@pytest.fixture(autouse=True)
def fresh_watchdog(monkeypatch):
    monkeypatch.setattr(completion, '_next_tick', 0.0)
    monkeypatch.setattr(completion, '_active', {})
    monkeypatch.setattr(completion, '_repo_cursor', 0)
    monkeypatch.setattr(completion, '_scans', {})
    monkeypatch.setattr(completion, '_scan_stamps', {})


@pytest.mark.spawns_process
@pytest.mark.parametrize('recipient', ['codex-peer', 'agy-peer', 'grok-peer'])
def test_external_submit_fire_survives_observer_restart_and_delivers(tmp_path, monkeypatch, recipient):
    # No Workflow tool and no real model invocation: the real fire path owns a
    # short-lived Python driver, while shared submission gates supply its script.
    from coordinator_core.hooks import _engine_ask_gates
    from coordinator_core.ops.workflow_fire import authority
    monkeypatch.setattr(authority, 'caller_principal', lambda cwd: authority.Principal('test-session', 'em', {}))

    script = tmp_path / 'workflow.mjs'
    script.write_text('// emitted workflow\n', encoding='utf-8')
    driver = tmp_path / 'driver.py'
    driver.write_text("import time, json\ntime.sleep(0.55)\nprint(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'test-driver'}))\n", encoding='utf-8')
    monkeypatch.setattr(fire, '_resolve_target_repo', lambda script, cwd: str(tmp_path))
    monkeypatch.setattr(fire, '_registry_dir', lambda cwd=None: tmp_path / 'fires')
    monkeypatch.setattr(fire, 'resolve_plugin_dir', lambda: str(tmp_path))
    monkeypatch.setattr(fire, '_publish_lag_message', lambda cwd: None)
    monkeypatch.setattr(fire, 'build_fire_command', lambda *a, **kw: [sys.executable, str(driver)])
    monkeypatch.setattr(_engine_ask_gates, 'submit_ask', lambda **kw: {
        'run_id': 'run-test', 'script_path': str(script),
        'sha256': hashlib.sha256(script.read_bytes()).hexdigest(),
    })
    # Retain the handle for cleanup, but let the observer see terminal state
    # before reaping, including the POSIX zombie interval.
    children = []
    real_popen = subprocess.Popen
    def spawn(*a, **kw):
        child = real_popen(*a, **kw)
        children.append(child)
        return child
    monkeypatch.setattr(fire.subprocess, 'Popen', spawn)
    deliveries = []
    monkeypatch.setattr(bridge, 'route', lambda message: deliveries.append(message) or SimpleNamespace(status='queued'))
    with pytest.warns(UserWarning):
        reply = engine_ask_submit._engine_ask_submit({
            'ask': 'PM text', 'ask_source': 'pm', 'caller': recipient,
            'session_id': 'test-session', 'cwd': str(tmp_path), 'fire': True,
            'executor': 'claude', 'backend': 'claude', 'notify_to': recipient,
        }, tmp_path)
    assert reply['fire']['state'] == 'running'
    assert completion.on_idle_tick([tmp_path]) is True
    assert not deliveries
    # Observe terminal state before reaping, as a persistent warm parent does.
    # POSIX zombies must not retain a live workflow slot.
    deadline = time.monotonic() + 3
    while fire._pid_alive(children[0].pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not fire._pid_alive(children[0].pid)
    # A successor has no in-memory registration; disk recovery is sufficient.
    completion._scans.clear()
    completion._active.clear()
    completion._scan_stamps.clear()
    completion._next_tick = 0.0
    assert completion.on_idle_tick([tmp_path]) is False
    assert len(deliveries) == 1
    children[0].wait(timeout=3)
    assert deliveries[0].to_identity == recipient
    record = fire._read_record(fire._record_path(tmp_path / 'fires', reply['fire']['fire_id']))
    assert record['state'] == 'exited'
    assert record['notified_at'] and record['notify_status'] == 'queued'
    completion._next_tick = 0.0
    completion.on_idle_tick([tmp_path])
    assert len(deliveries) == 1


def test_watchdog_delivery_failure_retries_without_caller_status(tmp_path, monkeypatch):
    monkeypatch.setattr(fire, '_registry_dir', lambda cwd=None: tmp_path)
    path = tmp_path / 'fire.json'
    fire._write_record(path, {'fire_id':'fire', 'state':'exited', 'notify_to':'codex-peer'})
    monkeypatch.setattr(bridge, 'route', lambda msg: SimpleNamespace(status='unroutable', reason='offline'))
    assert completion.on_idle_tick([tmp_path])
    assert 'offline' in fire._read_record(path)['notify_error']
    monkeypatch.setattr(bridge, 'route', lambda msg: SimpleNamespace(status='queued'))
    completion._next_tick = 0.0
    assert not completion.on_idle_tick([tmp_path])
    assert fire._read_record(path)['notified_at']


def test_busy_kernel_lock_skips_delivery_without_changing_record(tmp_path, monkeypatch):
    from coordinator_core import locked_write
    from coordinator_core.ops.workflow_fire import notify
    monkeypatch.setattr(locked_write, '_machine_lock_dir', lambda: tmp_path / 'locks')
    path = tmp_path / 'fire.json'
    fire._write_record(path, {'fire_id':'fire', 'state':'exited', 'notify_to':'codex-peer'})
    record = fire._read_record(path)
    persisted = path.read_bytes()
    monkeypatch.setattr(bridge, 'route', lambda msg: pytest.fail('duplicate delivery'))
    # A second fd contends on the actual OS lock, even in this same process.
    # The notification refresh must return immediately and leave disk alone.
    with locked_write.held_lock(path.resolve(), timeout=0, holder_label='active notifier'):
        assert notify.refresh_record(path, record) == record
    assert path.read_bytes() == persisted


def test_deployed_watchdog_keeps_pending_completion_alive(monkeypatch):
    from coordinator_core.warm import server
    calls = []
    monkeypatch.setattr(completion, 'on_idle_tick', lambda repos: calls.append('completion') or True)
    monkeypatch.setattr(server.push_cadence, 'registry_sweep_repos', lambda repos: repos)
    monkeypatch.setattr(server.push_cadence, 'on_idle_tick', lambda **kw: None)
    monkeypatch.setattr(server.idle, 'mark_invocation', lambda: calls.append('keep-alive'))
    monkeypatch.setattr(server.idle, 'should_demote', lambda **kw: False)
    monkeypatch.setattr(server.idle, 'demote_if_idle', lambda **kw: calls.append('retirement'))
    monkeypatch.setattr(server.telemetry, 'record_worker_pool_depth', lambda **kw: None)
    monkeypatch.setattr(server.telemetry, 'flush_requests', lambda *a: None)
    context = SimpleNamespace(
        served_repos=lambda: [], _token_is_stale=lambda: False, _ladder_gone=None,
        telemetry=SimpleNamespace(served_count=lambda: 1),
        close_listener=lambda: None, drain_outstanding=lambda: 0,
        _ctx_shutdown=lambda: None, worker_pool_depth=lambda: 0, engine_root='test',
    )
    server._ServerContext._idle_tick(context)
    assert calls == ['completion', 'keep-alive', 'retirement']

def test_watchdog_bounds_active_deliveries_and_rotates(tmp_path, monkeypatch):
    monkeypatch.setattr(fire, '_registry_dir', lambda cwd=None: tmp_path)
    for index in range(3):
        fire._write_record(tmp_path / f'f{index}.json', {
            'fire_id':f'f{index}', 'state':'exited', 'notify_to':f'peer-{index}'})
    delivered = []
    monkeypatch.setattr(bridge, 'route', lambda msg: delivered.append(msg.to_identity) or SimpleNamespace(status='queued'))
    for tick in range(3):
        completion._next_tick = 0.0
        completion.on_idle_tick([tmp_path])
        assert len(delivered) == tick + 1
    assert set(delivered) == {'peer-0', 'peer-1', 'peer-2'}


def test_released_kernel_lock_rereads_owner_checkpoint_without_duplicate(tmp_path, monkeypatch):
    from coordinator_core import locked_write
    from coordinator_core.ops.workflow_fire import notify
    monkeypatch.setattr(locked_write, '_machine_lock_dir', lambda: tmp_path / 'locks')
    path = tmp_path / 'fire.json'
    fire._write_record(path, {'fire_id':'fire', 'state':'exited', 'notify_to':'codex-peer'})
    stale = fire._read_record(path)
    monkeypatch.setattr(bridge, 'route', lambda msg: pytest.fail('repeated accepted notice'))
    with locked_write.held_lock(path.resolve(), timeout=0, holder_label='owner checkpoint'):
        fire._write_record(path, {**stale, 'notified_at':'owner-accepted', 'notify_status':'queued'})
        assert not notify.refresh_record(path, stale).get('notified_at')
    # The stale caller snapshot cannot overwrite the owner's newer checkpoint.
    refreshed = notify.refresh_record(path, stale)
    assert refreshed['notified_at'] == 'owner-accepted'
    assert fire._read_record(path) == refreshed


def test_released_kernel_lock_retries_failed_delivery_with_stable_message_id(tmp_path, monkeypatch):
    from coordinator_core import locked_write
    from coordinator_core.ops.workflow_fire import notify
    monkeypatch.setattr(locked_write, '_machine_lock_dir', lambda: tmp_path / 'locks')
    path = tmp_path / 'fire.json'
    fire._write_record(path, {'fire_id':'fire', 'state':'exited', 'notify_to':'codex-peer'})
    stale = fire._read_record(path)
    calls = []
    def route(message):
        calls.append(message)
        return (SimpleNamespace(status='unroutable', reason='offline') if len(calls) == 1
                else SimpleNamespace(status='queued'))
    monkeypatch.setattr(bridge, 'route', route)
    with locked_write.held_lock(path.resolve(), timeout=0, holder_label='released before notice'):
        assert not notify.refresh_record(path, stale).get('notified_at')
        assert not calls
    first = notify.refresh_record(path, stale)
    assert not first.get('notified_at') and 'offline' in first['notify_error']
    second = notify.refresh_record(path, stale)
    assert second['notified_at'] and 'notify_error' not in second
    assert len(calls) == 2 and calls[0].id == calls[1].id
    assert notify.refresh_record(path, stale) == second
    assert len(calls) == 2

def test_completed_history_is_not_rescanned_until_directory_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(fire, '_registry_dir', lambda cwd=None: tmp_path)
    fire._write_record(tmp_path / 'done.json', {'fire_id':'done', 'state':'exited', 'notify_to':'peer', 'notified_at':'done'})
    real_read = fire._read_record
    reads = []
    monkeypatch.setattr(fire, '_read_record', lambda path: reads.append(path) or real_read(path))
    assert not completion.on_idle_tick([tmp_path], clock=lambda: 100)
    assert len(reads) == 1
    completion._next_tick = 0
    assert not completion.on_idle_tick([tmp_path], clock=lambda: 101)
    assert len(reads) == 1
    fire._write_record(tmp_path / 'new.json', {'fire_id':'new', 'state':'exited', 'notify_to':'peer'})
    monkeypatch.setattr(bridge, 'route', lambda msg: SimpleNamespace(status='queued'))
    completion._next_tick = 0
    completion.on_idle_tick([tmp_path], clock=lambda: 131)
    assert real_read(tmp_path / 'new.json')['notified_at']


def test_removed_repo_releases_discovery_iterator_and_stamp(tmp_path, monkeypatch):
    class Scan:
        closed = False
        def close(self):
            self.closed = True
    scan = Scan()
    completion._scans[tmp_path] = scan
    completion._scan_stamps[tmp_path] = 1
    assert not completion.on_idle_tick([])
    assert scan.closed and not completion._scans and not completion._scan_stamps


@pytest.mark.parametrize('record', [
    {'fire_id':'broken', 'state':'running', 'notify_to':'peer'},
    {'fire_id':'broken', 'state':'unknown', 'notify_to':'peer'},
    ['not-a-record'],
])
def test_malformed_history_does_not_keep_engine_alive(tmp_path, monkeypatch, record):
    monkeypatch.setattr(fire, '_registry_dir', lambda cwd=None: tmp_path)
    (tmp_path / 'broken.json').write_text(json.dumps(record), encoding='utf-8')
    assert not completion.on_idle_tick([tmp_path])
    assert not completion._active

def test_active_selection_visits_one_key_without_snapshot(tmp_path, monkeypatch):
    class Counted(dict):
        visits = 0
        def __iter__(self):
            for key in super().__iter__():
                self.visits += 1
                yield key
    active = Counted({tmp_path / f'{i}.json': None for i in range(1000)})
    monkeypatch.setattr(completion, '_active', active)
    monkeypatch.setattr(completion, 'refresh_and_notify', lambda path: {'notified_at':'done'})
    assert completion.on_idle_tick([])
    assert active.visits == 1
    assert len(active) == 999



def test_ended_recipient_retains_terminal_artifact_without_keepalive_and_resume_retries(tmp_path, monkeypatch):
    from coordinator_core.session import external_registry
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    registry = tmp_path / "fires"
    registry.mkdir()
    monkeypatch.setattr(fire, "_registry_dir", lambda cwd=None: registry)
    sid = "00000000-0000-4000-8000-000000000001"
    owned = external_registry.registry_dir()
    owned.mkdir(parents=True, exist_ok=True)
    (owned / f"{sid}.json").write_text(json.dumps({
        "schema_version":1, "provider":"codex", "session_id":sid,
    }), encoding="utf-8")
    external_registry.unregister(sid)
    path = registry / "fire.json"
    fire._write_record(path, {"fire_id":"fire", "state":"exited", "notify_to":sid})
    monkeypatch.setattr(bridge, "route", lambda msg: pytest.fail("closed thread notified"))
    assert not completion.on_idle_tick([tmp_path], clock=lambda: 100)
    assert path.is_file() and not fire._read_record(path).get("notified_at")
    # Resume clears terminal evidence; the persisted fire becomes discoverable
    # after the bounded rescan even when the fire directory itself is unchanged.
    import os
    import psutil
    from coordinator_core.session import core
    epoch = psutil.Process(os.getpid()).create_time()
    class Host:
        pid = os.getpid()
        def name(self):
            return "codex.exe"
        def create_time(self):
            return epoch
        def parents(self):
            return [self]
    monkeypatch.setattr(psutil, "Process", lambda pid: Host())
    monkeypatch.setattr(core, "ensure_session", lambda *a, **kw: "")
    external_registry.register_current({"session_id":sid, "cwd":str(tmp_path)})
    assert not external_registry.is_ended(sid)
    calls = []
    monkeypatch.setattr(bridge, "route", lambda msg: calls.append(msg) or SimpleNamespace(status="queued"))
    assert not completion.on_idle_tick([tmp_path], clock=lambda: 131)
    assert len(calls) == 1 and fire._read_record(path)["notified_at"]


def test_ended_recipient_does_not_stop_observing_running_driver(tmp_path, monkeypatch):
    from coordinator_core.session import external_registry
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    sid = "00000000-0000-4000-8000-000000000001"
    owned = external_registry.registry_dir()
    owned.mkdir(parents=True, exist_ok=True)
    (owned / f"{sid}.json").write_text(json.dumps({
        "schema_version":1, "provider":"codex", "session_id":sid,
    }), encoding="utf-8")
    external_registry.unregister(sid)
    assert completion._pending({"state":"running", "pid":1, "notify_to":sid})
    assert not completion._pending({"state":"exited", "notify_to":sid})


def test_ended_uuid_does_not_override_explicit_rebinding(tmp_path, monkeypatch):
    from coordinator_core.agent_pipe import roster
    from coordinator_core.session import external_registry
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    sid = "00000000-0000-4000-8000-000000000001"
    owned = external_registry.registry_dir()
    owned.mkdir(parents=True, exist_ok=True)
    (owned / f"{sid}.json").write_text(json.dumps({
        "schema_version":1, "provider":"codex", "session_id":sid,
    }), encoding="utf-8")
    external_registry.unregister(sid)
    roster.bind(sid, "claude", "replacement")
    assert not completion.recipient_ended(sid)


def test_unregistered_uuid_is_unknown_not_ended(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    sid = "00000000-0000-4000-8000-000000000001"
    assert not completion.recipient_ended(sid)
    assert completion._pending({"state":"exited", "notify_to":sid})



def test_unreadable_override_is_unknown_not_terminal(monkeypatch):
    from coordinator_core.agent_pipe import roster
    from coordinator_core.session import external_registry
    monkeypatch.setattr(external_registry, "is_ended", lambda target: True)
    def unreadable(target):
        raise ValueError("corrupt roster")
    monkeypatch.setattr(roster, "resolve", unreadable)
    assert not completion.recipient_ended("known-uuid")
