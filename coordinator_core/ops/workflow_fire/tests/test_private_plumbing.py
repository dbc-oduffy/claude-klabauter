"""Controller wiring tests; fake lifecycle only, never native acceptance."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from coordinator_core.ops.workflow_fire import headless
from coordinator_core.session_hub_path import hub_for_common


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='private native engine is Windows-only')


def spec(tmp_path):
    return {name: str(tmp_path / name) for name in ('door_image', 'door_provenance', 'shared_engine_root')}


@pytest.mark.parametrize('bad', [True, {}, {'door_image': 'relative'},
    {'door_image': 'C:/x', 'door_provenance': 'C:/y', 'shared_engine_root': 'C:/z', 'identity': 'em'}])
def test_private_configuration_rejects_partial_or_extra_fields(tmp_path, bad):
    with pytest.raises(ValueError):
        headless.validate_private_engine_config(bad)


def test_private_configuration_never_selects_claude(tmp_path):
    with pytest.raises(ValueError):
        headless.validate_private_engine_config(spec(tmp_path), backend='claude')


def make_record(tmp_path):
    from coordinator_core.ops.workflow_fire.finalizer import snapshot_contract
    script = tmp_path / 'run.mjs'
    script.write_text('export const meta={}; const value=await agent("fixture",{}); return {ok:true,value};')
    artifact = hub_for_common(tmp_path / '.git') / 'workflow-fires/headless' / ('a' * 32)
    artifact.mkdir(parents=True)
    (tmp_path / '.git').mkdir(exist_ok=True)
    (artifact / 'start').touch()
    path = tmp_path / 'record.json'
    path.write_text(json.dumps({'fire_id': 'a' * 32, 'script_path': str(script),
        'script_sha256': hashlib.sha256(script.read_bytes()).hexdigest(),
        'repo_root': str(tmp_path), 'artifact_dir': str(artifact), 'backend': 'codex',
        'journal_path': str(artifact / 'journal.jsonl'), 'timeout_s': 10,
        'caller_session_id': '11111111-1111-4111-8111-111111111111', 'private_engine': spec(tmp_path),
        'terminal_contract': snapshot_contract(tmp_path, script)}))
    return path


class FakePrivate:
    instances = []
    fail_cleanup = False
    fail_ready = False

    def __init__(self, path, record, **kwargs):
        self.events = ['construct']
        self.roster = {}
        self.private_bin = Path(record['artifact_dir']) / 'private-bin'
        self.bindings_path = Path(record['artifact_dir']) / 'worker-bindings.json'
        self.allowed_cli = ['sizing-assemble']
        self.instances.append(self)

    def resource_plan(self):
        return {'alias_count': 1, 'fixture': True}

    def start(self, timeout_s):
        self.events.append('ready')
        if self.fail_ready:
            raise RuntimeError('fixture not ready')
        return {'server_pid': 123, 'fixture': True}

    def worker_env(self):
        return {'COORDINATOR_WARM': '0'}

    def on_spawn(self, pid, request):
        self.events.append(('spawn', pid, request))

    def on_native_thread(self, request, facts):
        self.events.append(('native', request, facts['session_id']))

    def end_worker(self, request):
        self.events.append(('ended', request))

    def close(self):
        self.events.append('close')
        if self.fail_cleanup:
            raise TimeoutError('fixture cleanup incomplete')


@pytest.fixture(autouse=True)
def reset_fake():
    FakePrivate.instances = []
    FakePrivate.fail_cleanup = FakePrivate.fail_ready = False


def test_controller_routes_callbacks_and_keeps_parent_as_lineage(tmp_path):
    path = make_record(tmp_path)
    def worker(prompt, options, **kwargs):
        private = FakePrivate.instances[-1]
        assert private.events == ['construct', 'ready']
        assert kwargs['parent_session_id'] == '11111111-1111-4111-8111-111111111111'
        assert kwargs['private_cli']['request_id'] == kwargs['request_id']
        assert kwargs['private_cli']['fire_id'] == 'a' * 32
        kwargs['on_spawn'](321)
        kwargs['on_native_thread']({'session_id': '22222222-2222-4222-8222-222222222222'})
        return {'fixture': True}
    result = headless.execute(path, agent_runner=worker, private_engine_factory=FakePrivate)
    assert result['exit_code'] == 0, result
    events = FakePrivate.instances[-1].events
    assert events[2][0] == 'spawn' and events[3][0] == 'native' and events[4][0] == 'ended'
    assert events[-1] == 'close'
    assert result['private_engine_cleanup']['closed'] is True


def test_private_startup_failure_never_dispatches_a_worker(tmp_path):
    FakePrivate.fail_ready = True
    def worker(*args, **kwargs):
        pytest.fail('not-ready private engine must not start a model')
    result = headless.execute(make_record(tmp_path), agent_runner=worker, private_engine_factory=FakePrivate)
    assert result['exit_code'] == 1
    assert 'not ready' in result['error']
    assert FakePrivate.instances[-1].events[-1] == 'close'


@pytest.mark.parametrize('damage', ['missing', 'tampered'])
def test_invalid_preworker_contract_refuses_before_private_ready_or_worker(tmp_path, damage):
    path = make_record(tmp_path)
    record = json.loads(path.read_text())
    if damage == 'missing':
        record.pop('terminal_contract')
    else:
        record['terminal_contract']['files']['run.mjs'] = '0' * 64
    path.write_text(json.dumps(record))
    def worker(*args, **kwargs):
        pytest.fail('invalid authority must not dispatch a worker')
    result = headless.execute(path, agent_runner=worker, private_engine_factory=FakePrivate)
    assert result['exit_code'] == 1
    assert 'contract' in result['error'] or 'changed after fire' in result['error']
    assert not FakePrivate.instances


def test_cleanup_failure_cannot_be_reported_clean(tmp_path):
    FakePrivate.fail_cleanup = True
    result = headless.execute(make_record(tmp_path), agent_runner=lambda *a, **kw: {'fixture': True},
                              private_engine_factory=FakePrivate)
    assert result['exit_code'] == 1 and result['outcome'] == 'failed'
    assert result['private_engine_cleanup']['closed'] is False


def test_failed_worker_is_ended_before_private_cleanup(tmp_path):
    def worker(*args, **kwargs):
        kwargs['on_spawn'](321)
        raise RuntimeError('fixture worker failed after spawn')
    result = headless.execute(make_record(tmp_path), agent_runner=worker, private_engine_factory=FakePrivate)
    assert result['exit_code'] == 1
    events = FakePrivate.instances[-1].events
    assert events[-2][0] == 'ended' and events[-1] == 'close'


def test_delegated_worker_cannot_select_a_private_engine_build(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from coordinator_core.ops.workflow_fire import authority, op
    monkeypatch.setattr(authority, 'require_fire_authority', lambda *a, **kw: SimpleNamespace(
        role='workflow-agent', session_id='22222222-2222-4222-8222-222222222222',
        env={'COORDINATOR_PARENT_SESSION_ID': '11111111-1111-4111-8111-111111111111'}))
    monkeypatch.setattr(headless, 'start', lambda *a, **kw: pytest.fail('must not reserve a new fire'))
    with pytest.raises(PermissionError, match='only the EM'):
        op._workflow_fire({'script_path': str(tmp_path / 'fixture.mjs'), 'backend': 'codex',
            'executor': 'headless', 'private_engine': spec(tmp_path)}, tmp_path)


def test_submit_forwards_execution_configuration_without_putting_it_in_script_args(tmp_path, monkeypatch):
    from coordinator_core.hooks import _engine_ask_gates
    from coordinator_core.ops import engine_ask_submit
    from coordinator_core.ops.workflow_fire import op
    calls = []
    monkeypatch.setattr(_engine_ask_gates, 'submit_ask', lambda **kw: {
        'run_id': 'ask-fixture', 'script_path': str(tmp_path / 'run.mjs'), 'sha256': 'a' * 64})
    monkeypatch.setattr(op, '_workflow_fire', lambda params, repo: calls.append(params) or {'fire_id': 'b' * 32})
    result = engine_ask_submit._engine_ask_submit({'ask': 'fixture', 'ask_source': 'relayed',
        'session_id': '11111111-1111-4111-8111-111111111111', 'caller': 'fixture',
        'cwd': str(tmp_path), 'fire': True, 'private_engine': spec(tmp_path)})
    assert 'fire' in result
    assert calls[0]['private_engine'] == spec(tmp_path)
    assert 'args' not in calls[0]


def test_bad_private_configuration_is_refused_before_submission(tmp_path, monkeypatch):
    from coordinator_core.hooks import _engine_ask_gates
    from coordinator_core.ops import engine_ask_submit
    monkeypatch.setattr(_engine_ask_gates, 'submit_ask', lambda **kw: pytest.fail('must not mint a run'))
    result = engine_ask_submit._engine_ask_submit({'private_engine': {}, 'backend': 'codex'})
    assert result['refused'] == 'bad-request'
