import hashlib
import json
from pathlib import Path
import shutil

import pytest

from coordinator_core.ops.workflow_fire import headless

pytestmark = pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')


def record(tmp_path, script):
    source = tmp_path / 'run.mjs'
    source.write_text(script if script.startswith('export') else 'export const meta={};\n' + script, encoding='utf-8')
    artifacts = tmp_path / 'artifacts'
    artifacts.mkdir()
    (artifacts / 'start').touch()
    path = tmp_path / 'record.json'
    path.write_text(json.dumps({'fire_id': 'test', 'script_path': str(source),
        'script_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'repo_root': str(tmp_path), 'artifact_dir': str(artifacts), 'backend': 'codex',
        'journal_path': str(artifacts / 'journal.jsonl'), 'timeout_s': 10}), encoding='utf-8')
    return path


def test_real_runtime_parallel_journal_and_terminal_handoff(tmp_path: Path) -> None:
    path = record(tmp_path, "export const meta={}; await phase('Execute'); const r=await parallel([()=>agent('one',{label:'A'}),()=>agent('two',{label:'B'})]); return {next_action:{op:'dispatch.terminal_commit',params:{run_id:'test'}},r};")
    calls, landings = [], []
    def worker(prompt, options, **kwargs):
        calls.append((prompt, options, kwargs))
        return {'done': prompt}
    def land(value, script, repo):
        landings.append(value)
        return {'committed': True, 'sha': 'abc'}
    result = headless.execute(path, agent_runner=worker, terminal_runner=land)
    assert result['exit_code'] == 0
    assert result['landing']['committed']
    assert len(calls) == 2 and len(landings) == 1
    assert calls[0][2]['artifact_dir'] != calls[1][2]['artifact_dir']
    events = [json.loads(line) for line in Path(result['journal_path']).read_text().splitlines()]
    assert any(e['type'] == 'agent-response' for e in events)
    assert events[-1]['type'] == 'completed'


def test_exited_driver_records_started_child_workflow(tmp_path: Path) -> None:
    child = tmp_path / 'staged-run.workflow.mjs'
    child.write_text("export const meta={}; throw new Error('child halted');", encoding='utf-8')
    path = record(tmp_path, 'return await workflow(' + json.dumps({'scriptPath': str(child)}) + ');')

    result = headless.execute(path)

    assert result['state'] == 'exited'
    assert result['exit_code'] == 1
    assert json.loads(path.read_text(encoding='utf-8'))['child_workflows'] == [str(child)]


def test_agent_error_fails_workflow_without_landing(tmp_path: Path) -> None:
    path = record(tmp_path, "await agent('fail',{}); return {ok:true};")
    def worker(*args, **kwargs):
        raise RuntimeError('native worker failed')
    def land(*args) -> None:
        pytest.fail('failed workflow cannot land')
    result = headless.execute(path, agent_runner=worker, terminal_runner=land)
    assert result['exit_code'] == 1
    assert 'native worker failed' in result['error']


def test_script_drift_refused_before_agent(tmp_path: Path) -> None:
    path = record(tmp_path, "return {}; ")
    (tmp_path / 'run.mjs').write_text('return {changed:true};')
    result = headless.execute(path, agent_runner=lambda *a, **k: pytest.fail('drift'))
    assert result['exit_code'] == 1
    assert 'changed after fire' in result['error']


def test_terminal_refusal_is_not_success(tmp_path: Path) -> None:
    path = record(tmp_path, 'return {next_action:{op:"dispatch.terminal_commit",params:{}}};')
    result = headless.execute(path, terminal_runner=lambda *a: {'refused': 'ledger'})
    assert result['exit_code'] == 1
    assert result['landing']['refused'] == 'ledger'


def test_completion_delivery_is_durable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.agent_pipe import bridge
    from types import SimpleNamespace
    path = record(tmp_path, 'return {ok:true};')
    value = json.loads(path.read_text())
    value['notify_to'] = 'codex-parent'
    path.write_text(json.dumps(value))
    delivered = []
    monkeypatch.setattr(bridge, 'route', lambda message: delivered.append(message) or SimpleNamespace(status='queued'))
    result = headless.execute(path)
    assert result['notified_at'] and result['notify_status'] == 'queued'
    assert len(delivered) == 1
    assert json.loads(path.read_text())['notify_status'] == 'queued'


def test_source_operation_retains_unstamped_guard(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match='not a published engine'):
        headless._invoke_source('dispatch.emit', {'ask': 'unused'}, str(tmp_path), tmp_path / 'operation', 15)


def test_params_file_must_stay_inside_target_repo(tmp_path: Path) -> None:
    outside = tmp_path / 'outside.json'
    outside.write_text('{}')
    repo = tmp_path / 'repo'
    repo.mkdir()
    with pytest.raises(ValueError):
        headless._invoke_source('dispatch.emit', {'paramsFile': str(outside)}, str(repo), repo / 'operation', 15)


@pytest.mark.parametrize('value', [
    {'run_id': 'ask-native-proof', 'halted_by': {'halted': 'refusal', 'reason': 'sizing-assemble failed'},
     'review': {'receipt': 'review: 0 verdicts'}, 'commit': None,
     'next_action': {'kind': 'revise_and_refire', 'op': 'workflow.revise'}},
    {'run_id': 'ask-native-proof', 'next_action': {'kind': 'revise_and_refire', 'op': 'workflow.revise'}},
    {'status': 'emit_refused', 'error': 'POSIX command rejected Windows'},
    {'error': 'engine emit failed'},
    {'halted': 'ledger-check'},
    {'status': 'minted', 'run_id': 'unfinished'},
    {'stage_pending': {'handle': 'unresolved'}},
    {'schema': 'wake-digest', 'outcome': 'incomplete', 'completed': False},
    {'parent_id': 'fan', 'runs': [{'run_id': 'child', 'status': 'ok', 'result': {'halted': 'ledger'}}]},
    {'parent_id': 'fan', 'runs': [{'run_id': 'c1', 'status': 'ok', 'result': {}},
                                  {'run_id': 'c2', 'status': 'not_started', 'reason': 'emit leg threw'}]},
])
def test_returned_engine_failure_is_not_clean_exit(tmp_path: Path, value) -> None:
    path = record(tmp_path, 'return ' + json.dumps(value) + ';')
    result = headless.execute(path)
    assert result['exit_code'] == 1
    assert result['outcome'] == 'failed'
    assert result['result'] == value
    assert result['error']


@pytest.mark.parametrize('value', [
    {'ok': True, 'data': {'status': 'failed', 'error': 'a test fixture value'}},
    {'ok': True, 'error': 'descriptive schema property'},
    {'ok': True, 'run_id': 'task-data', 'error': 'descriptive schema property'},
    {'status': 'open', 'item': 'ordinary task data'},
    {'committed': False, 'nothing_to_commit': True},
    {'committed': False, 'already_landed': True, 'sha': 'abc'},
])
def test_normal_schema_and_successful_noop_are_preserved(tmp_path: Path, value) -> None:
    path = record(tmp_path, 'return ' + json.dumps(value) + ';')
    result = headless.execute(path, terminal_runner=lambda *a: value if 'committed' in value else None)
    assert result['exit_code'] == 0, result


@pytest.mark.parametrize('landing', [
    {'committed': False},
    {'committed': True, 'plan_status': 'not-stamped', 'plan_status_reason': 'criterion not met'},
    {'committed': True, 'criterion_status': 'indeterminate'},
])
def test_noncomplete_landing_cannot_report_success(tmp_path: Path, landing) -> None:
    path = record(tmp_path, 'return {next_action:{op:"dispatch.terminal_commit",params:{}}};')
    result = headless.execute(path, terminal_runner=lambda *a: landing)
    assert result['exit_code'] == 1
    assert result['landing'] == landing


def test_not_started_fan_child_is_a_failure_naming_its_leg_reason() -> None:
    reason = 'start leg: no run dir'
    failed = headless._result_failure(
        {'parent_id': 'p', 'runs': [{'run_id': 'c', 'status': 'not_started', 'reason': reason}]})
    assert failed and reason in failed
    mixed = {'parent_id': 'p', 'runs': [{'run_id': 'a', 'status': 'ok', 'result': {}},
                                        {'run_id': 'c', 'status': 'not_started', 'reason': reason}]}
    assert reason in (headless._result_failure(mixed) or '')
    ok_only = {'parent_id': 'p', 'runs': [{'run_id': 'c', 'status': 'ok', 'result': {}}]}
    assert headless._result_failure(ok_only) is None


def test_fan_child_unrelayed_terminal_action_is_not_clean(tmp_path: Path) -> None:
    value = {'parent_id': 'parent', 'runs': [{'run_id': 'child', 'status': 'ok', 'result': {
        'schema': 'wake-digest', 'outcome': 'completed', 'completed': True,
        'next_action': {'op': 'dispatch.land_run', 'params': {'run_id': 'child'}},
    }}]}
    path = record(tmp_path, 'return ' + json.dumps(value) + ';')
    result = headless.execute(path)
    assert result['exit_code'] == 1
    assert 'fan child terminal action has not been relayed' in result['error']
    assert result['result'] == value


def test_deadline_never_dispatches_queued_agents_after_runtime_dies(tmp_path: Path) -> None:
    import time
    path = record(tmp_path, "return await parallel(Array.from({length: 20}, (_, i) => () => agent(String(i))));")
    settings = json.loads(path.read_text())
    settings['timeout_s'] = .3
    path.write_text(json.dumps(settings))
    calls = []
    def worker(prompt, options, **kwargs):
        calls.append((prompt, kwargs['timeout_s']))
        time.sleep(.4)
        return {'done': prompt}
    before = time.monotonic()
    result = headless.execute(path, agent_runner=worker)
    assert result['exit_code'] == 1
    # The bound is the invariant: no agent past the deadline. Zero dispatched is legitimate
    # when interpreter start under load already spends the 0.3s runtime.
    assert len(calls) <= 8
    assert all(0 < timeout <= .3 for _, timeout in calls)
    assert time.monotonic() - before < 2


def test_cancelled_run_marker_stops_the_driver_before_the_next_agent(tmp_path: Path) -> None:
    from coordinator_core.ops.dispatch_emit import halted_run
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    (tmp_path / '.git').mkdir()
    run = 'warp-2026-11-15-aaaaaa-1-foo-20261115T000000-bbbbbb'
    run_dir = tmp_path / RECORD_DIR_ROOT / run
    path = record(tmp_path, "const a = await agent('first'); const b = await agent('second'); return {a, b};")
    settings = json.loads(path.read_text())
    settings['run_id'] = run
    path.write_text(json.dumps(settings))
    calls = []
    def worker(prompt, options, **kwargs):
        calls.append(prompt)
        halted_run.write_halt_marker(run_dir, halted='cancelled', sizing_rel='state/sizings/x.yaml')
        return {'done': prompt}
    result = headless.execute(path, agent_runner=worker)
    assert calls == ['first']
    assert result['exit_code'] == 1 and 'cancelled by workflow.cancel' in result['error']
    assert 'halted_runs' not in result
    assert halted_run.read_strict(run_dir)['halted'] == 'cancelled'


def test_unreleased_start_gate_persists_failed_record_and_notifies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = record(tmp_path, 'return {};')
    value = json.loads(path.read_text())
    value.update(timeout_s=.01, notify_to='parent')
    path.write_text(json.dumps(value))
    (tmp_path / 'artifacts' / 'start').unlink()
    deliveries = []
    from coordinator_core.agent_pipe import bridge
    from types import SimpleNamespace
    monkeypatch.setattr(bridge, 'route', lambda message: deliveries.append(message) or SimpleNamespace(status='queued'))
    result = headless.execute(path, agent_runner=lambda *a, **k: pytest.fail('gate must prevent work'))
    assert result['exit_code'] == 1 and result['state'] == 'exited'
    assert 'start gate' in result['error']
    assert json.loads(path.read_text())['notified_at']
    assert len(deliveries) == 1


@pytest.fixture
def local_worker_root(tmp_path, monkeypatch):
    from coordinator_core.warm import sandboxed_spawn

    monkeypatch.setattr(headless, '_worker_engine_root', lambda: tmp_path)
    monkeypatch.setattr(sandboxed_spawn, 'caller_is_sandboxed', lambda: False)


@pytest.mark.parametrize('pin_kind', ['matching', 'foreign', 'relative', 'unstamped'])
def test_worker_root_requires_matching_stamped_absolute_pin(tmp_path, monkeypatch, pin_kind):
    from coordinator_core.session import caller_env
    from coordinator_core.warm import engine_root

    running = tmp_path / 'running'
    running.mkdir()
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    pin = str(foreign) if pin_kind == 'foreign' else ('running' if pin_kind == 'relative' else str(running))
    monkeypatch.setattr(caller_env, 'effective_caller_env', lambda: {'COORDINATOR_DOOR_ENGINE_ROOT': pin})
    monkeypatch.setattr(engine_root, 'current_engine_clone', lambda: running)
    monkeypatch.setattr(engine_root, 'is_engine_root', lambda path: pin_kind != 'unstamped')
    if pin_kind == 'matching':
        assert headless._worker_engine_root() == running.resolve()
    else:
        with pytest.raises(headless.HeadlessEngineRootUnavailable, match='must match'):
            headless._worker_engine_root()


def test_start_receipt_failure_releases_reserved_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_worker_root) -> None:
    source = tmp_path / 'workflow.mjs'
    source.write_text('export const meta={}; return {};')
    registry = tmp_path / 'registry'
    monkeypatch.setattr(headless.fire, '_resolve_target_repo', lambda *a: str(tmp_path))
    monkeypatch.setattr(headless.fire, '_assert_script_under_repo_root', lambda *a: None)
    monkeypatch.setattr(headless.fire, '_registry_dir', lambda *a: registry)
    monkeypatch.setattr(headless.fire, '_acquire_cap_lock', lambda *a: None)
    monkeypatch.setattr(headless.fire, '_release_cap_lock', lambda *a: None)
    monkeypatch.setattr(headless.fire, 'count_live_fires', lambda *a, **k: 0)
    def fail(*a):
        raise OSError('receipt refused')
    monkeypatch.setattr(headless, 'stamp_fire_ownership', fail)
    with pytest.raises(OSError, match='receipt refused'):
        headless.start(str(source))
    records = list(registry.glob('*.json'))
    assert len(records) == 1
    value = json.loads(records[0].read_text())
    assert value['state'] == 'exited' and value['exit_code'] == 1
    assert value['outcome'] == 'failed' and 'receipt refused' in value['error']


@pytest.mark.parametrize('backend', ['codex', 'claude', 'grok', 'agy'])
def test_start_records_the_requested_backend_family(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend, local_worker_root) -> None:
    source = tmp_path / 'workflow.mjs'
    source.write_text('export const meta={}; return {};')
    registry = tmp_path / 'registry'
    monkeypatch.setattr(headless.fire, '_resolve_target_repo', lambda *a: str(tmp_path))
    monkeypatch.setattr(headless.fire, '_assert_script_under_repo_root', lambda *a: None)
    monkeypatch.setattr(headless.fire, '_registry_dir', lambda *a: registry)
    monkeypatch.setattr(headless.fire, '_acquire_cap_lock', lambda *a: None)
    monkeypatch.setattr(headless.fire, '_release_cap_lock', lambda *a: None)
    monkeypatch.setattr(headless.fire, 'count_live_fires', lambda *a, **k: 0)
    def stop(*a):
        raise OSError('stop after record')
    monkeypatch.setattr(headless, 'stamp_fire_ownership', stop)
    with pytest.raises(OSError, match='stop after record'):
        headless.start(str(source), backend=backend)
    assert json.loads(next(registry.glob('*.json')).read_text())['backend'] == backend


def test_start_refuses_an_unknown_backend_naming_the_set(tmp_path: Path) -> None:
    source = tmp_path / 'workflow.mjs'
    source.write_text('export const meta={}; return {};')
    with pytest.raises(ValueError, match=r'codex\|claude\|grok\|agy'):
        headless.start(str(source), backend='gemini')


def test_failed_gate_release_reaps_only_its_own_spawned_driver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_worker_root) -> None:
    from types import SimpleNamespace
    source = tmp_path / 'workflow.mjs'
    source.write_text('export const meta={}; return {};')
    registry = tmp_path / 'registry'
    monkeypatch.setattr(headless.fire, '_resolve_target_repo', lambda *a: str(tmp_path))
    monkeypatch.setattr(headless.fire, '_assert_script_under_repo_root', lambda *a: None)
    monkeypatch.setattr(headless.fire, '_registry_dir', lambda *a: registry)
    monkeypatch.setattr(headless.fire, '_acquire_cap_lock', lambda *a: None)
    monkeypatch.setattr(headless.fire, '_release_cap_lock', lambda *a: None)
    monkeypatch.setattr(headless.fire, 'count_live_fires', lambda *a, **k: 0)
    monkeypatch.setattr(headless.fire, 'build_fire_env', lambda: {})
    monkeypatch.setattr(headless, 'stamp_fire_ownership', lambda *a: None)
    reaped = []
    child = SimpleNamespace(pid=123, poll=lambda: None, kill=lambda: reaped.append('kill'), wait=lambda: reaped.append('wait'))
    monkeypatch.setattr(headless.subprocess, 'Popen', lambda *a, **k: child)
    touch = Path.touch
    def fail_gate(self, *args, **kwargs):
        if self.name == 'start':
            raise OSError('gate denied')
        return touch(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'touch', fail_gate)
    with pytest.raises(OSError, match='gate denied'):
        headless.start(str(source))
    assert reaped == ['kill', 'wait']
    value = json.loads(next(registry.glob('*.json')).read_text())
    assert value['state'] == 'exited' and value['exit_code'] == 1
    assert 'gate denied' in value['error']

@pytest.mark.parametrize("worker_failed", [False, True])
def test_headless_gate_journals_tool_output_and_separates_diagnostics(
    tmp_path: Path, worker_failed: bool,
) -> None:
    path = record(tmp_path, "return await agent('relay gate',{label:'gate',phase:'gate'});")
    output = '{"jsonrpc":"2.0","id":1,"result":{"arm":"m_plus","halt":null}}\n'
    output += "outside-landing walk exceeded 0.300s; open rows dispatch uncredited\n"
    seen = []

    def worker(prompt, options, *, artifact_dir, **kwargs):
        seen.append(prompt)
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "stdout.jsonl").write_text(json.dumps({
            "type": "item.completed",
            "item": {
                "type": "command_execution", "id": "item_0", "exit_code": 0,
                "command": "coordinator-invoke dispatch.ask_gate '{}'",
                "aggregated_output": output,
            },
        }) + "\n", encoding="utf-8")
        if worker_failed:
            raise RuntimeError("gate worker reply invalid")
        return {"arm": "m_plus", "halt": None}

    result = headless.execute(path, agent_runner=worker)
    events = [json.loads(line) for line in Path(result["journal_path"]).read_text().splitlines()]
    gate = next(event for event in events if event["type"] == "gate-command-output")
    assert gate["id"] and gate["tool_item_id"] == "item_0"
    assert gate["exit_code"] == 0 and gate["combined_output"] == output
    assert gate["verified"] is False
    assert "stderr diagnostics do not turn" in seen[0]
    assert result["exit_code"] == int(worker_failed)
    if worker_failed:
        assert "gate worker reply invalid" in result["error"]
