"""Native and Claude executors use the same immutable-digest spawn arbitration."""
from concurrent.futures import ThreadPoolExecutor
import hashlib

import pytest

from coordinator_core.ops.workflow_fire import fire, headless, op


@pytest.fixture
def native(tmp_path, monkeypatch):
    from coordinator_core.ops.workflow_fire import authority
    monkeypatch.setattr(authority, 'caller_principal', lambda *a: authority.Principal('verified-test-em', 'em', {}))
    script = tmp_path / 'native.mjs'
    script.write_text('export const meta={}; return {};', encoding='utf-8')
    registry = tmp_path / 'fires'
    monkeypatch.setattr(fire, '_resolve_target_repo', lambda *a: str(tmp_path))
    monkeypatch.setattr(fire, '_registry_dir', lambda *a: registry)
    monkeypatch.setattr(fire, 'resolve_plugin_dir', lambda: str(tmp_path))
    monkeypatch.setattr(fire, '_publish_lag_message', lambda *a: None)
    monkeypatch.setattr(fire, '_pid_alive', lambda pid: True)
    monkeypatch.setattr(fire, 'build_fire_env', lambda: {})
    monkeypatch.setattr(fire.time, 'sleep', lambda *a: None)
    calls = []
    class Child:
        pid = 424242
        def __init__(self, *args, **kwargs):
            calls.append(kwargs)
        def poll(self):
            return None
    monkeypatch.setattr(headless.subprocess, 'Popen', Child)
    return script, hashlib.sha256(script.read_bytes()).hexdigest(), registry, calls


def test_native_expected_digest_returns_existing_handle(native):
    script, digest, _, calls = native
    first = headless.start(str(script), expected=digest)
    second = headless.start(str(script), expected=digest)
    assert first['fire_id'] == second['fire_id']
    assert second['already_fired'] and len(calls) == 1
    assert first['sha256'] == digest
    assert first['execution_started'] is None  # only the worker can prove it passed the gate


def test_concurrent_native_digest_calls_share_one_spawn(native):
    script, digest, _, calls = native
    with ThreadPoolExecutor(max_workers=2) as pool:
        handles = list(pool.map(lambda _: headless.start(str(script), expected=digest), range(2)))
    assert handles[0]['fire_id'] == handles[1]['fire_id']
    assert len(calls) == 1


@pytest.mark.parametrize('first_kind', ['headless', 'claude'])
def test_both_executor_kinds_share_the_same_reservation(native, first_kind):
    script, digest, _, calls = native
    starters = [headless.start, fire.fire_workflow]
    if first_kind == 'claude':
        starters.reverse()
    first = starters[0](str(script), expected=digest)
    second = starters[1](str(script), expected=digest)
    assert first['fire_id'] == second['fire_id']
    assert second['already_fired'] and len(calls) == 1


def test_native_started_terminal_failure_is_not_retryable(native):
    script, digest, registry, calls = native
    first = headless.start(str(script), expected=digest)
    fire._write_record(fire._record_path(registry, first['fire_id']), {
        **first, 'state': 'exited', 'exit_code': 7, 'execution_started': True,
    })
    retry = headless.start(str(script), expected=digest)
    assert retry['already_fired'] and retry['fire_id'] == first['fire_id']
    assert len(calls) == 1


def test_native_known_failure_before_start_gate_is_retryable(native, monkeypatch):
    script, digest, registry, calls = native
    def reject(*args):
        raise OSError('receipt refused before spawn')
    monkeypatch.setattr(headless, 'stamp_fire_ownership', reject)
    with pytest.raises(OSError):
        headless.start(str(script), expected=digest)
    previous = fire._read_record(next(registry.glob('*.json')))
    assert previous['execution_started'] is False
    monkeypatch.setattr(headless, 'stamp_fire_ownership', lambda *a: None)
    retry = headless.start(str(script), expected=digest)
    assert retry['fire_id'] != previous['fire_id'] and len(calls) == 1


def test_native_expired_reservation_is_unknown_not_restarted(native):
    script, digest, registry, calls = native
    fire._write_record(fire._record_path(registry, 'lost-spawner'), {
        'fire_id': 'lost-spawner', 'state': 'pending', 'reserved_at': 0,
        'script_path': str(script), 'sha256': digest,
    })
    retry = headless.start(str(script), expected=digest)
    assert retry['already_fired'] and retry['execution_started'] is None
    assert not calls


def test_native_cap_arbitration_never_delivers_notifications(native, monkeypatch):
    from coordinator_core.agent_pipe import bridge
    script, digest, registry, _ = native
    fire._write_record(fire._record_path(registry, 'old'), {
        'fire_id': 'old', 'state': 'exited', 'notify_to': 'offline-peer',
    })
    monkeypatch.setattr(bridge, 'route', lambda *a: pytest.fail('transport under cap lock'))
    assert headless.start(str(script), expected=digest)['state'] == 'running'


def test_native_wire_expected_is_forwarded_to_shared_arbitration(native, monkeypatch):
    script, digest, _, _ = native
    captured = []
    monkeypatch.setattr(headless, 'start', lambda *a, **kw: captured.append(kw) or {'fire_id': 'captured'})
    op._workflow_fire({'script_path': str(script), 'expected': digest, 'executor': 'headless'})
    assert captured[0]['expected'] == digest


def test_native_duplicate_can_attach_late_notification_target(native):
    script, digest, registry, calls = native
    first = op._workflow_fire({'script_path': str(script), 'expected': digest, 'executor': 'headless'})
    second = op._workflow_fire({'script_path': str(script), 'expected': digest, 'executor': 'headless',
                               'notify_to': 'late-peer'})
    assert second['fire_id'] == first['fire_id']
    assert second['already_fired'] and second['notify_to'] == 'late-peer'
    assert len(calls) == 1
    persisted = fire._read_record(fire._record_path(registry, first['fire_id']))
    assert persisted['notify_to'] == 'late-peer'
    assert persisted['sha256'] == digest


def test_explicit_codex_backend_never_falls_through_to_claude(native, monkeypatch):
    script, digest, _, _ = native
    captured = []
    monkeypatch.setattr(headless, 'start', lambda *a, **kw: captured.append(kw) or {'fire_id': 'native'})
    monkeypatch.setattr(fire, 'fire_workflow', lambda *a, **kw: pytest.fail('Codex request must not spend Claude credit'))
    reply = op._workflow_fire({'script_path': str(script), 'expected': digest, 'backend': 'codex'})
    assert reply['fire_id'] == 'native' and captured[0]['backend'] == 'codex'


def test_contradictory_codex_backend_refuses_before_claude_spawn(native, monkeypatch):
    script, digest, _, _ = native
    monkeypatch.setattr(fire, 'fire_workflow', lambda *a, **kw: pytest.fail('must refuse before spawn'))
    with pytest.raises(ValueError, match='cannot use the Claude executor'):
        op._workflow_fire({'script_path': str(script), 'expected': digest, 'backend': 'codex', 'executor': 'claude'})


@pytest.mark.parametrize('failure', [ValueError, OSError])
def test_malformed_private_contract_releases_reservation_before_any_worker(native, monkeypatch, failure):
    import json
    from coordinator_core.ops.workflow_fire import finalizer
    script, digest, registry, calls = native
    monkeypatch.setattr(headless, 'validate_private_engine_config',
                        lambda *a, **kw: {'shared_engine_root': str(script.parent)})
    monkeypatch.setattr(headless, '_private_source_sha', lambda: 'a'*40)

    def malformed(*args):
        raise failure('malformed private terminal contract')

    monkeypatch.setattr(finalizer, 'snapshot_contract', malformed)
    with pytest.raises(failure, match='malformed private terminal contract'):
        headless.start(str(script), expected=digest, private_engine={'fixture': True})
    assert calls == []
    records = list(registry.glob('*.json'))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record['state'] == 'exited' and record['exit_code'] == 1
    assert record['outcome'] == 'failed' and record['execution_started'] is False
    assert 'malformed private terminal contract' in record['error']
    assert fire.count_live_fires(str(script.parent), notify=False) == 0
