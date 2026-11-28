"""Accounting uses actual exec event fields, keeping broker lineage separate."""
import json
import os
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.codex_runner import workflow


















@pytest.fixture(autouse=True)
def scratch_usage_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('COORDINATOR_SETTINGS_HOME', str(tmp_path / 'settings'))
    monkeypatch.setenv('COORDINATOR_LOCK_ROOT', str(tmp_path / 'locks'))
    (tmp_path / 'codex-home').mkdir()
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex-home'))


def test_recorded_native_exec_shape_has_thread_and_cache_usage_only(tmp_path: Path) -> None:
    # Shape observed in the installed Windows exec smoke; no turn/model fields.
    path = tmp_path / "stdout.jsonl"
    path.write_text('\n'.join(json.dumps(event) for event in [
        {"type": "thread.started", "thread_id": "01a1b826-0012-72a0-a4d0-eae0ae828f4c"},
        {"type": "turn.started"},
        {"type": "turn.completed", "usage": {"input_tokens": 15568,
         "cached_input_tokens": 0, "cache_write_input_tokens": 0,
         "output_tokens": 15, "reasoning_output_tokens": 0}},
    ]), encoding="utf-8")
    observed = workflow._event_provenance(path, "codex")
    assert observed["native_thread_id"] == "01a1b826-0012-72a0-a4d0-eae0ae828f4c"
    assert observed["observed_turn_usage_total"] == observed["usage"]
    assert observed["usage"]["input_tokens"] == 15568
    for field in ("native_turn_id", "native_total_usage", "model", "effort"):
        assert observed[field] == "unknown"


@pytest.mark.parametrize('invalid', [None, 'identity', 'pid', 'parent', 'role', 'resume', 'conflict', 'unknown', 'reroute', 'oversize'])
def test_process_joins_only_same_worker_native_startup_model(tmp_path, invalid):
    sample = complete_receipt()
    child = sample['native_thread_id']
    events = [{'type': 'thread.started', 'thread_id': child},
              {'type': 'turn.completed', 'usage': sample['usage_events'][0]}]
    (tmp_path / 'stdout.jsonl').write_text('\n'.join(map(json.dumps, events)))
    record = {'schema_version': 1, 'provider': 'codex', 'session_id': child, 'pid': 12,
              'parent_session_id': sample['broker_root_session_id'], 'role': 'workflow-agent',
              'session_start_model': 'gpt-6-luna',
              'session_start_model_evidence': {'source': 'session_start_payload', 'session_source': 'startup'}}
    if invalid in ('identity', 'pid', 'parent', 'role', 'unknown'):
        key, value = {'identity': ('session_id', sample['broker_root_session_id']),
                      'pid': ('pid', 13), 'parent': ('parent_session_id', None),
                      'role': ('role', 'em'), 'unknown': ('session_start_model', 'unknown')}[invalid]
        record[key] = value
    elif invalid == 'resume':
        record['session_start_model_evidence']['session_source'] = 'resume'
    elif invalid == 'conflict':
        record['session_start_model_conflict'] = True
    elif invalid == 'reroute':
        record['session_start_model_initial'] = 'gpt-6.2-astra'
    elif invalid == 'oversize':
        record['padding'] = 'x' * (64 * 1024)
    directory = Path(os.environ['COORDINATOR_SETTINGS_HOME']) / 'host-sessions' / 'codex'
    directory.mkdir(parents=True)
    (directory / f'{child}.json').write_text(json.dumps(record))
    receipt = workflow._save_process(tmp_path, SimpleNamespace(pid=12, returncode=0), 'codex', {}, 'requested-astra',
        broker_root_session_id=sample['broker_root_session_id'], broker_request_id=1, broker_fire_id='fire-a')
    assert receipt['model'] == ('unknown' if invalid else 'gpt-6-luna')
    assert receipt['usage_observations'][0]['model'] == receipt['model']
    rows = [json.loads(line) for line in Path(receipt['canonical_usage']['path']).read_text().splitlines()]
    assert rows[0]['payload']['model'] == (None if invalid else 'gpt-6-luna')
    if not invalid:
        assert rows[0]['payload']['model_evidence']['source'] == 'session_start_payload'


def test_identical_unidentified_turn_counters_are_not_guessed_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "stdout.jsonl"
    event = {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 5}}
    path.write_text(json.dumps(event) + '\n' + json.dumps(event), encoding="utf-8")
    observed = workflow._event_provenance(path, "codex")
    assert observed["observed_turn_usage_total"] == {"input_tokens": 20, "cached_input_tokens": 10}
    assert len(observed["usage_events"]) == 2
    assert observed["native_total_usage"] == "unknown"


def test_process_receipt_distinguishes_broker_lineage_from_native_identity(tmp_path: Path) -> None:
    (tmp_path / "stdout.jsonl").write_text('', encoding="utf-8")
    workflow._save_process(tmp_path, SimpleNamespace(pid=12, returncode=0), "codex", {}, "requested",
        broker_root_session_id="broker-root", broker_request_id=7, broker_fire_id="fire-a")
    receipt = json.loads((tmp_path / "process.json").read_text())
    assert receipt["broker_root_session_id"] == "broker-root"
    assert receipt["broker_request_id"] == 7 and receipt["broker_fire_id"] == "fire-a"
    assert receipt["native_thread_id"] == "unknown"
    assert "native_parent_id" not in receipt
    assert receipt["model"] == "unknown" and receipt["requested_model"] == "requested"


def test_process_receipt_uses_recorded_admission_root_when_not_explicit(tmp_path: Path) -> None:
    (tmp_path / "admission.json").write_text(json.dumps({"session_id": "admitted-root"}))
    workflow._save_process(tmp_path, SimpleNamespace(pid=12, returncode=1), "codex", {}, None)
    receipt = json.loads((tmp_path / "process.json").read_text())
    assert receipt["broker_root_session_id"] == "admitted-root"
    assert receipt["broker_request_id"] is None


def complete_receipt():
    return {
        'native_thread_id': '01a1b826-0012-72a0-a4d0-eae0ae828f4c',
        'broker_root_session_id': '01a1b63b-acfb-78d1-86f2-a74ad6ca4572',
        'broker_fire_id': 'fire-a', 'broker_request_id': 1, 'backend': 'codex',
        'model': 'unknown', 'effort': 'unknown', 'native_turn_id': 'unknown',
        'native_total_usage': 'unknown',
        'usage_events': [{'input_tokens': 100, 'cached_input_tokens': 60,
                          'cache_write_input_tokens': 0, 'output_tokens': 12,
                          'reasoning_output_tokens': 5}],
    }


def test_canonical_usage_subtracts_cached_and_does_not_double_reasoning(tmp_path: Path) -> None:
    first = workflow._append_canonical_usage(complete_receipt())
    second = workflow._append_canonical_usage(complete_receipt())
    assert first['status'] == 'appended' and second['status'] == 'duplicate'
    from pathlib import Path
    events = [json.loads(line) for line in Path(first['path']).read_text().splitlines()]
    assert len(events) == 1
    payload = events[0]['payload']
    assert payload['usage'] == {'input_tokens': 40, 'output_tokens': 12,
        'cache_read_input_tokens': 60, 'cache_creation_input_tokens': 0}
    assert payload['session_id'] == complete_receipt()['native_thread_id']
    assert payload['parent_session_id'] == complete_receipt()['broker_root_session_id']
    assert payload['request_id'] == 'fire-a:1'
    assert payload['model'] is None and payload['effort'] is None and payload['service_tier'] is None
    assert 'native_turn_id' not in payload and 'native_total_usage' not in payload


def test_exec_thread_totals_resume_replay_and_fresh_child(tmp_path):
    receipt = complete_receipt()
    unit = dict(input_tokens=12400, cached_input_tokens=2000, cache_write_input_tokens=400, output_tokens=1000)
    receipt.update(usage_events=[unit], usage_observations=[{'model': 'gpt-6-astra'}])
    first = workflow._append_canonical_usage(receipt)
    assert first['status'] == 'appended'
    receipt.update(broker_request_id=2, usage_events=[{k: 2*v for k, v in unit.items()}])
    assert workflow._append_canonical_usage(receipt)['status'] == 'appended'
    assert workflow._append_canonical_usage(receipt)['status'] == 'duplicate'
    receipt.update(broker_request_id=3, usage_events=[unit])
    assert workflow._append_canonical_usage(receipt)['status'] == 'incomplete'
    receipt['native_thread_id'] = '00000000-0000-4000-8000-000000000001'
    assert workflow._append_canonical_usage(receipt)['status'] == 'appended'
    rows = [json.loads(line)['payload'] for line in Path(first['path']).read_text().splitlines()]
    assert len(rows) == 3
    assert all(r['usage'] == dict(input_tokens=10000, output_tokens=1000,
                                  cache_read_input_tokens=2000, cache_creation_input_tokens=400) for r in rows)


def test_long_cache_write_retention_survives_worker_journal_replay(tmp_path):
    from coordinator_core.codex_status.model import empty, ingest, summary

    receipt = complete_receipt()
    receipt.update(model='gpt-6.2-astra', service_tier='standard', usage_events=[dict(
        input_tokens=272000, cached_input_tokens=10000, cache_write_input_tokens=10000,
        ttl_12h_tokens=10000, output_tokens=1000)])
    recorded = workflow._append_canonical_usage(receipt)
    assert recorded['status'] == 'appended'
    assert workflow._append_canonical_usage(receipt)['status'] == 'duplicate'
    event = json.loads(Path(recorded['path']).read_text())
    assert event['payload']['usage']['ttl_12h_tokens'] == 10000
    state = empty(receipt['broker_root_session_id'])
    ingest(state, event, {})
    assert summary(state)['usd'] == pytest.approx(2.26)


def test_multiple_turns_keep_individual_tier_thresholds_and_service_tier(tmp_path):
    receipt = complete_receipt()
    receipt.update(model='gpt-6.1-sol', service_tier='priority')
    receipt['usage_events'] *= 2
    receipt['usage_events'] = [{**usage, 'input_tokens': 200000, 'cached_input_tokens': 60000}
                               for usage in receipt['usage_events']]
    result = workflow._append_canonical_usage(receipt)
    events = [json.loads(line) for line in Path(result['path']).read_text().splitlines()]
    assert len(events) == 2
    assert len({e['payload']['request_id'] for e in events}) == 2
    assert all(e['payload']['service_tier'] == 'priority' for e in events)
    assert all(e['payload']['usage']['input_tokens'] == 140000 for e in events)
    from coordinator_core.codex_status.model import empty, ingest, summary
    state = empty(receipt['broker_root_session_id'])
    for event in events:
        ingest(state, event, {})
    assert summary(state)['usd'] == pytest.approx(2 * (.28 + .006 + .00012) * 2)
    assert workflow._append_canonical_usage(receipt)['status'] == 'duplicate'


def test_event_provenance_retains_observed_service_tier(tmp_path):
    path = tmp_path / 'stdout.jsonl'
    path.write_text(json.dumps({'type': 'session.configured', 'session': {
        'model': 'gpt-6.1-sol', 'service_tier': 'ultrafast'}}))
    assert workflow._event_provenance(path, 'codex')['service_tier'] == 'ultrafast'


@pytest.mark.parametrize('usage_owner', [None, '00000000-0000-4000-8000-000000000002'])
def test_workflow_production_path_records_start_and_terminal_usage(tmp_path, monkeypatch, usage_owner):
    receipt = complete_receipt()
    events = [
        {'type': 'thread.started', 'thread_id': receipt['native_thread_id'], 'model': 'gpt-6.1-sol'},
        {'type': 'turn.completed', 'usage': receipt['usage_events'][0]}]
    def execute(argv, prompt, *, stdout, **kwargs):
        stdout.write(('\n'.join(map(json.dumps, events)) + '\n').encode())
        Path(argv[argv.index('-o') + 1]).write_text('done')
        return SimpleNamespace(pid=12, returncode=0)
    monkeypatch.setattr(workflow, '_run_admitted', execute)
    monkeypatch.setattr(workflow, 'resolve_codex', lambda **kw: ['/bin/codex-fixture'])
    monkeypatch.setattr(workflow, 'preflight_auth', lambda **kw: None)
    monkeypatch.setattr(workflow, 'shape_codex_child', lambda *a, **kw: None)
    from coordinator_core.warm import sandboxed_spawn
    monkeypatch.setattr(sandboxed_spawn, 'caller_is_sandboxed', lambda: False)
    parent = receipt['broker_root_session_id']
    assert workflow.run_agent('work', {}, repo_root=tmp_path, artifact_dir=tmp_path / 'art',
        parent_session_id=parent, usage_owner_session_id=usage_owner, fire_id='fire-a', request_id=1) == 'done'
    from coordinator_core.codex_status.__main__ import snapshot
    if usage_owner:
        # Another fire on the same launcher has no association with this native thread.
        workflow._record_execution(parent, 'unrelated-fire', 1, 'started')
        worker = json.loads((tmp_path / 'art' / 'process.json').read_bytes())
        assert worker['broker_root_session_id'] == parent
        assert worker['usage_owner_session_id'] == usage_owner
        assert snapshot({'session_id': parent})['priced_events'] == 0
    value = snapshot({'session_id': usage_owner or parent})
    assert value['coverage']['workflows']
    assert value['execution_inventory']['expected'] == 1
    assert value['priced_events'] == 1


def test_execution_telemetry_lock_failure_preserves_worker_result(monkeypatch):
    from coordinator_core.locked_write import LockTimeout
    def fail(*a, **kw):
        raise LockTimeout('locked')
    monkeypatch.setattr(workflow, 'held_lock', fail)
    receipt = complete_receipt()
    workflow._record_execution(receipt['broker_root_session_id'], 'fire', 1, 'started')


def test_resumed_request_preserves_first_turn_identity(tmp_path):
    receipt = complete_receipt()
    first = workflow._append_canonical_usage(receipt)
    receipt['usage_events'] *= 2
    second = workflow._append_canonical_usage(receipt)
    assert first['status'] == second['status'] == 'appended'
    events = [json.loads(line) for line in Path(second['path']).read_text().splitlines()]
    assert [e['payload']['request_id'] for e in events] == ['fire-a:1', 'fire-a:1:turn:1']


def test_conflicting_legacy_aggregate_does_not_append_duplicate_spend(tmp_path):
    receipt = complete_receipt()
    first = workflow._append_canonical_usage(receipt)
    receipt['usage_events'][0]['input_tokens'] += 10
    second = workflow._append_canonical_usage(receipt)
    assert second['status'] == 'failed' and 'different counters' in second['reason']
    assert len(Path(first['path']).read_text().splitlines()) == 1


def test_save_process_twice_produces_one_usage_event(tmp_path: Path) -> None:
    receipt = complete_receipt()
    events = [{'type': 'thread.started', 'thread_id': receipt['native_thread_id']},
              {'type': 'turn.completed', 'usage': receipt['usage_events'][0]}]
    (tmp_path / 'stdout.jsonl').write_text('\n'.join(map(json.dumps, events)))
    for _ in range(2):
        workflow._save_process(tmp_path, SimpleNamespace(pid=12, returncode=0), 'codex', {}, 'requested',
            broker_root_session_id=receipt['broker_root_session_id'], broker_request_id=1, broker_fire_id='fire-a')
    saved = json.loads((tmp_path / 'process.json').read_text())
    assert saved['canonical_usage']['status'] == 'duplicate'
    from pathlib import Path
    assert len(Path(saved['canonical_usage']['path']).read_text().splitlines()) == 1


@pytest.mark.parametrize('missing', ['input_tokens', 'output_tokens', 'cached_input_tokens', 'cache_write_input_tokens'])
def test_incomplete_native_counters_are_not_fabricated(tmp_path: Path, missing) -> None:
    receipt = complete_receipt()
    del receipt['usage_events'][0][missing]
    result = workflow._append_canonical_usage(receipt)
    assert result['status'] == 'incomplete'
    assert not (tmp_path / 'settings' / 'state' / 'codex-usage').exists()


def test_canonical_append_failure_is_recorded_without_worker_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(*args, **kwargs):
        raise OSError('test lock unavailable')
    monkeypatch.setattr(workflow, 'held_lock', refused)
    result = workflow._append_canonical_usage(complete_receipt())
    assert result['status'] == 'failed' and 'test lock unavailable' in result['reason']


def test_grok_worker_stream_joins_session_usage(tmp_path: Path) -> None:
    """A grok stage stream names sessionId and per-turn usage, not a codex thread."""
    child = "01a218c3-880e-7ca2-bff8-f613ba835762"
    parent = "297d177f-957c-4221-bb61-50d53d2a8fea"
    usage = {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 4,
             "cache_creation_input_tokens": 0, "reasoning_tokens": 1}
    path = tmp_path / "stdout.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in [
        {"type": "usage", "usage": usage},
        {"type": "end", "sessionId": child, "stopReason": "end_turn",
         "modelUsage": {"grok-4.7-build-fast": {
             "inputTokens": 10, "outputTokens": 2, "cacheReadInputTokens": 4,
             "cacheCreationInputTokens": 0, "modelCalls": 1}}},
    ]) + "\n", encoding="utf-8")
    observed = workflow._event_provenance(path, "grok")
    assert observed["native_thread_id"] == child
    assert observed["model"] == "grok-4.7-build-fast"
    assert observed["usage_events"] == [usage]
    receipt = {**observed, "backend": "grok", "broker_root_session_id": parent,
               "broker_fire_id": "fire-grok-attr", "broker_request_id": 1}
    result = workflow._append_canonical_usage(receipt)
    assert result["status"] == "appended"
    payload = json.loads(Path(result["path"]).read_text().splitlines()[-1])["payload"]
    assert payload["provider"] == "grok"
    assert payload["session_id"] == child
    assert payload["parent_session_id"] == parent
    assert payload["model"] == "grok-4.7-build-fast"
    assert payload["usage"] == {"input_tokens": 10, "output_tokens": 2,
                                 "cache_read_input_tokens": 4, "cache_creation_input_tokens": 0}


def test_claude_counters_are_already_uncached_and_keep_cache_writes(tmp_path: Path) -> None:
    receipt = complete_receipt()
    receipt['backend'] = 'claude'
    receipt['usage_events'] = [{'input_tokens': 40, 'output_tokens': 12,
                              'cache_read_input_tokens': 60, 'cache_creation_input_tokens': 8}]
    result = workflow._append_canonical_usage(receipt)
    from pathlib import Path
    payload = json.loads(Path(result['path']).read_text())['payload']
    assert payload['usage'] == receipt['usage_events'][0]
    assert payload['provider'] == 'claude'


def test_invalid_native_cache_total_is_incomplete(tmp_path: Path) -> None:
    receipt = complete_receipt()
    receipt['usage_events'][0]['cached_input_tokens'] = 101
    result = workflow._append_canonical_usage(receipt)
    assert result['status'] == 'incomplete'
    assert 'exceeds input' in result['reason']


def test_observer_partial_repeated_events_preserve_actual_child_uuid(tmp_path: Path) -> None:
    import time
    path = tmp_path / 'events.jsonl'; path.write_bytes(b'')
    sid = '01a1b826-0012-72a0-a4d0-eae0ae828f4c'
    calls = []; aborts = []
    facts = {'pid': 42, 'start_epoch': 3.0, 'parent_session_id': 'parent-only', 'fire_id': 'f', 'request_id': 1}
    observer = workflow._NativeThreadObserver(path, facts, calls.append, lambda: aborts.append(True))
    observer.start()
    event = json.dumps({'type': 'thread.started', 'thread_id': sid}).encode()
    with path.open('ab', buffering=0) as out:
        out.write(event[:20]); time.sleep(.03)
        assert calls == []
        out.write(event[20:] + b'\n' + event + b'\n')
    observer.finish()
    assert calls == [{**facts, 'session_id': sid}]
    assert not aborts


@pytest.mark.parametrize('payload', [b'not json\n', b'{"type":"thread.started","thread_id":"bad"}\n',
    b'{"type":"thread.started","thread_id":"01A1B826-0012-72A0-A4D0-EAE0AE828F4C"}\n',
    b'x'*65537+b'\n', b''], ids=['malformed-json', 'invalid-uuid', 'uppercase-uuid', 'oversized-line', 'missing'])
def test_observer_invalid_or_missing_identity_fails(tmp_path: Path, payload) -> None:
    path=tmp_path/'events.jsonl';path.write_bytes(payload)
    aborted=[];observer=workflow._NativeThreadObserver(path, {}, lambda _: None, lambda: aborted.append(True))
    observer.start()
    with pytest.raises(workflow.AgentFailed, match='identity observation failed'):
        observer.finish()
    assert aborted


def test_observer_conflicting_uuid_fails(tmp_path: Path) -> None:
    path=tmp_path/'events.jsonl'
    path.write_text('\n'.join(json.dumps({'type':'thread.started','thread_id':sid}) for sid in
        ['01a1b826-0012-72a0-a4d0-eae0ae828f4c','01a1b826-0012-72a0-a4d0-eae0ae828f4d'])+'\n')
    calls=[];observer=workflow._NativeThreadObserver(path, {}, calls.append, lambda: None);observer.start()
    with pytest.raises(workflow.AgentFailed, match='conflicting'):
        observer.finish()
    assert len(calls)==1


def test_observer_callback_failure_is_worker_failure(tmp_path: Path) -> None:
    path=tmp_path/'events.jsonl';path.write_text(json.dumps({'type':'thread.started','thread_id':'01a1b826-0012-72a0-a4d0-eae0ae828f4c'})+'\n')
    def refused(_): raise RuntimeError('binding refused')
    observer=workflow._NativeThreadObserver(path, {}, refused, lambda: None);observer.start()
    with pytest.raises(workflow.AgentFailed, match='binding refused'):
        observer.finish()


def test_observer_discovery_cap_does_not_limit_later_total_output(tmp_path: Path) -> None:
    path=tmp_path/'events.jsonl'
    path.write_text(json.dumps({'type':'thread.started','thread_id':'01a1b826-0012-72a0-a4d0-eae0ae828f4c'})+'\n'+
        (json.dumps({'type':'item.completed','content':'x'*10000})+'\n')*110)
    calls=[];observer=workflow._NativeThreadObserver(path, {}, calls.append, lambda: None);observer.start();observer.finish()
    assert len(calls)==1


def test_observer_protected_artifacts_only(tmp_path: Path) -> None:
    (tmp_path/'.git').mkdir()
    workflow._protected_observer_path(tmp_path, tmp_path/'.git/private-fire/step1')
    for unsafe in [tmp_path/'workspace-artifacts', tmp_path/'.git', tmp_path/'other/../.git/private']:
        with pytest.raises(workflow.AgentFailed):workflow._protected_observer_path(tmp_path,unsafe)
    (tmp_path/'.git/private-file').write_text('x')
    with pytest.raises(workflow.AgentFailed):workflow._protected_observer_path(tmp_path,tmp_path/'.git/private-file/step')


def test_observer_rejects_existing_reparse_ancestor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path/'.git').mkdir()
    original=Path.lstat
    def fake(path,*args,**kwargs):
        info=original(path,*args,**kwargs)
        if path==tmp_path/'.git':return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(Path,'lstat',fake)
    with pytest.raises(workflow.AgentFailed,match='unsafe ancestor'):
        workflow._protected_observer_path(tmp_path,tmp_path/'.git/private/step')


def test_native_child_facts_hashes_exact_expected_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Synthetic process metadata tests validation logic, not native execution.
    import psutil
    executable=tmp_path/'codex.exe';executable.write_bytes(b'trusted-native-binary-fixture')
    argv=[str(executable),'exec','--json']
    child=SimpleNamespace(create_time=lambda:123.5,cmdline=lambda:argv,exe=lambda:str(executable),
        name=lambda:'codex.exe',ppid=os.getpid)
    monkeypatch.setattr(psutil,'Process',lambda _:child)
    facts=workflow._native_child_facts(SimpleNamespace(pid=73,poll=lambda:None),tmp_path,'parent','fire',4,argv)
    assert facts['exe']==str(executable.resolve())
    assert facts['exe_sha256']==hashlib.sha256(executable.read_bytes()).hexdigest()
    assert facts['pid']==73 and facts['parent_session_id']=='parent'
    assert facts['role']=='workflow-agent'


def test_native_child_facts_rejects_same_basename_different_binary(tmp_path: Path,monkeypatch: pytest.MonkeyPatch) -> None:
    import psutil
    expected=tmp_path/'trusted/codex.exe';expected.parent.mkdir();expected.write_bytes(b'trusted')
    actual=tmp_path/'untrusted/codex.exe';actual.parent.mkdir();actual.write_bytes(b'untrusted')
    child=SimpleNamespace(create_time=lambda:123.5,cmdline=lambda:[str(actual),'exec'],exe=lambda:str(actual),
        name=lambda:'codex.exe',ppid=os.getpid)
    monkeypatch.setattr(psutil,'Process',lambda _:child)
    with pytest.raises(workflow.AgentFailed,match='differs from trusted'):
        workflow._native_child_facts(SimpleNamespace(pid=73,poll=lambda:None),tmp_path,'parent','fire',4,[str(expected),'exec'])


_SCHEMA = {'type': 'object', 'properties': {'a': {'type': 'integer'}}, 'required': ['a']}


def _stub_family(monkeypatch, tmp_path, *, stdout_text, returncode: int = 0):
    calls = []

    def fake_run(argv, prompt, *, stdout, stderr, **kwargs) -> SimpleNamespace:
        calls.append((list(argv), prompt))
        stdout.write(stdout_text.encode())
        return SimpleNamespace(pid=7, returncode=returncode)

    monkeypatch.setattr(workflow, '_run_admitted', fake_run)
    monkeypatch.setattr(workflow, 'resolve_grok', lambda: ['/bin/grok-x'])
    monkeypatch.setattr(workflow, 'resolve_agy', lambda: ['/bin/agy-x'])
    return calls


def _run(tmp_path, backend, schema=_SCHEMA):
    return workflow.run_agent('do it', {'schema': schema} if schema else {}, repo_root=tmp_path,
                              artifact_dir=tmp_path / 'art', backend=backend, parent_session_id='sid')


def test_grok_stage_spawns_resolved_binary_and_parses_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_family(monkeypatch, tmp_path, stdout_text=json.dumps({'text': '{"a": 1}', 'stopReason': 'end_turn'}, indent=2))
    assert _run(tmp_path, 'grok') == {'a': 1}
    argv, stdin = calls[0]
    assert argv[0] == '/bin/grok-x' and stdin == ''
    assert argv[argv.index('--permission-mode') + 1] == ('bypassPermissions' if os.name != 'nt' else 'acceptEdits')
    assert not {'--always-approve', '--dangerously-skip-permissions', '--yolo'} & set(argv)
    assert '"a"' in (tmp_path / 'art' / 'prompt.txt').read_text() and 'JSON Schema' in (tmp_path / 'art' / 'prompt.txt').read_text()


@pytest.mark.parametrize('text', [
    'Looking at it {not json} now.\n\n{"a": 3}',
    'Narration.\n```json\n{"a": 3}\n```',
])
def test_grok_narration_then_trailing_json_object_is_the_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text) -> None:
    _stub_family(monkeypatch, tmp_path, stdout_text=json.dumps({'text': text}))
    assert _run(tmp_path, 'grok') == {'a': 3}


def test_grok_narration_without_trailing_json_or_with_trailing_prose_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for text in ('only narration', '{"a": 3}\nand then more prose'):
        _stub_family(monkeypatch, tmp_path, stdout_text=json.dumps({'text': text}))
        with pytest.raises(workflow.AgentFailed, match='no schema-valid result'):
            _run(tmp_path, 'grok')


def test_agy_stage_spawns_resolved_binary_and_parses_result_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stream = '\n'.join(json.dumps(e) for e in [
        {'event': 'step_update', 'step_update': {}},
        {'event': 'result', 'result': {'status': 'SUCCESS', 'response': '```json\n{"a": 2}\n```'}}])
    calls = _stub_family(monkeypatch, tmp_path, stdout_text=stream)
    assert _run(tmp_path, 'agy') == {'a': 2}
    argv, stdin = calls[0]
    assert argv[0] == '/bin/agy-x'
    assert '--dangerously-skip-permissions' in argv and not {'--mode', '--settings', '--sandbox'} & set(argv)
    assert json.loads(stdin)['event'] == 'user' and 'do it' in json.loads(stdin)['message']['content']


def test_family_text_result_without_schema_and_empty_agy_response_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_family(monkeypatch, tmp_path, stdout_text=json.dumps({'text': 'plain'}))
    assert _run(tmp_path, 'grok', schema=None) == 'plain'
    _stub_family(monkeypatch, tmp_path, stdout_text=json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'response': ''}}))
    with pytest.raises(workflow.AgentFailed, match='no schema-valid result'):
        _run(tmp_path, 'agy', schema=None)


@pytest.mark.parametrize('backend', ['grok', 'agy'])
def test_family_schema_violation_and_nonzero_exit_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend) -> None:
    body = json.dumps({'text': '{"a": "x"}'}) if backend == 'grok' else json.dumps(
        {'event': 'result', 'result': {'status': 'SUCCESS', 'response': '{"a": "x"}'}})
    _stub_family(monkeypatch, tmp_path, stdout_text=body)
    with pytest.raises(workflow.AgentFailed, match='no schema-valid result'):
        _run(tmp_path, backend)
    _stub_family(monkeypatch, tmp_path, stdout_text=body, returncode=3)
    with pytest.raises(workflow.AgentFailed, match='exited 3') as failure:
        _run(tmp_path, backend)
    for name in ("stdout.jsonl", "stderr.txt"):
        artifact = tmp_path / "art" / name
        assert artifact.is_file() and str(artifact) in str(failure.value)


@pytest.mark.parametrize('backend', ['grok', 'agy'])
def test_family_missing_binary_raises_agent_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend) -> None:
    from coordinator_core.codex_runner.agy import AgyUnavailable
    from coordinator_core.codex_runner.grok import GrokUnavailable
    _stub_family(monkeypatch, tmp_path, stdout_text='')
    def gone(): raise (GrokUnavailable if backend == 'grok' else AgyUnavailable)('not on PATH')
    monkeypatch.setattr(workflow, 'resolve_' + backend, gone)
    with pytest.raises(workflow.AgentFailed, match='executable unavailable'):
        _run(tmp_path, backend)


def test_unknown_backend_still_refused(tmp_path: Path) -> None:
    with pytest.raises(workflow.AgentFailed, match='unknown agent backend: gemini'):
        _run(tmp_path, 'gemini')


def test_family_receipt_is_written_without_crashing_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_family(monkeypatch, tmp_path, stdout_text=json.dumps({'text': '{"a": 1}'}))
    _run(tmp_path, 'grok')
    receipt = json.loads((tmp_path / 'art' / 'process.json').read_text())
    assert receipt['backend'] == 'grok' and receipt['canonical_usage']['status'] == 'incomplete'


def test_builtin_workspace_policy_has_no_custom_permission_selector() -> None:
    import inspect
    source=inspect.getsource(workflow.run_agent)
    assert 'codex_family_stage_argv(' in source and 'workspace-write' not in source
    assert 'default_permissions' not in source
    assert '_workspace_permissions' not in source


def _stub_codex(monkeypatch, tmp_path, *, response='{"a": 1}', responses=None):
    calls = _stub_family(monkeypatch, tmp_path, stdout_text='')
    inner = workflow._run_admitted
    replies = iter(responses) if responses is not None else None

    def fake(argv, prompt, **kw):
        Path(argv[argv.index('-o') + 1]).write_text(
            next(replies) if replies is not None else response, encoding='utf-8',
        )
        return inner(argv, prompt, **kw)

    monkeypatch.setattr(workflow, '_run_admitted', fake)
    return calls


def test_host_selected_worker_policy(tmp_path, monkeypatch):
    repo = tmp_path / "product"
    repo.mkdir()
    policy = tmp_path / "policy.json"
    rules = {":root": "read", str(tmp_path): "deny", ":workspace_roots": "write", str(repo / ".git"): "write"}
    policy.write_text(json.dumps(rules))
    monkeypatch.setenv("COORDINATOR_CODEX_WORKER_POLICY_FILE", str(policy))
    monkeypatch.setenv("COORDINATOR_LOOPBACK_FIXTURE_URL", "http://127.0.0.1:8102/v1")
    calls = _stub_codex(monkeypatch, repo)
    monkeypatch.setattr(workflow, "preflight_auth", lambda **kw: None)
    monkeypatch.setattr(workflow, "resolve_codex", lambda **kw: ["codex"])
    _run(repo, "codex")
    argv = calls[0][0]
    assert 'default_permissions="coordinator_worker"' in argv
    assert 'approval_policy="never"' in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert "--ignore-user-config" in argv
    assert 'model_provider="local_fixture"' in argv
    assert 'model_providers.local_fixture.base_url="http://127.0.0.1:8102/v1"' in argv
    disabled = {arg for flag, arg in zip(argv, argv[1:]) if flag == "--disable"}
    assert {"multi_agent", "multi_agent_v2"} <= disabled
    assert 'web_search="disabled"' in argv
    assert str(repo / ".git") in next(arg for arg in argv if arg.startswith("permissions.coordinator_worker."))


@pytest.mark.parametrize("access", ["read", "write", "deny", None])
def test_restricted_shell_git_config_keeps_host_read_allocation(tmp_path, monkeypatch, access):
    repo = tmp_path / "product"
    repo.mkdir()
    config = tmp_path / "worker.gitconfig"
    config.write_text("[user]\n    name = Fixture\n")
    rules = {":root": "deny", ":workspace_roots": "write"}
    if access is not None:
        rules[str(config)] = access
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(rules))
    monkeypatch.setenv("COORDINATOR_CODEX_WORKER_GIT_CONFIG", str(config))
    if access != "read":
        with pytest.raises(workflow.AgentFailed, match="Git config"):
            workflow._restricted_worker_argv(["codex"], policy, repo, repo / "last.txt")
        return
    argv = workflow._restricted_worker_argv(["codex"], policy, repo, repo / "last.txt")
    assert "shell_environment_policy.set.GIT_CONFIG_GLOBAL=" + json.dumps(str(config)) in argv
    assert "--ignore-user-config" in argv


def test_policy_selects_its_allocated_git_config_without_environment_override(tmp_path, monkeypatch):
    repo = tmp_path / "product"
    repo.mkdir()
    policy = tmp_path / "executor.json"
    config = policy.with_suffix(".gitconfig")
    config.write_text("[user]\n    name = Local worker\n")
    rules = {":root": "deny", ":workspace_roots": "write"}
    monkeypatch.delenv("COORDINATOR_CODEX_WORKER_GIT_CONFIG", raising=False)
    policy.write_text(json.dumps(rules))
    argv = workflow._restricted_worker_argv(["codex"], policy, repo, repo / "last.txt")
    assert not any("GIT_CONFIG_GLOBAL" in value for value in argv)
    rules[str(config)] = "read"
    policy.write_text(json.dumps(rules))
    argv = workflow._restricted_worker_argv(["codex"], policy, repo, repo / "last.txt")
    assert "shell_environment_policy.set.GIT_CONFIG_GLOBAL=" + json.dumps(str(config)) in argv


@pytest.mark.parametrize("endpoint", ["https://example.org/v1", "http://127.0.0.1:99999/v1", ""])
def test_local_fixture_refuses_invalid_endpoint(endpoint):
    with pytest.raises(workflow.AgentFailed, match="loopback endpoint"):
        workflow._loopback_provider_argv(endpoint)


@pytest.mark.parametrize("value", [None, "", "{", "[]", '{":root":"write"}', '{":root":"read","typo":"deny"}'])
def test_unusable_selected_policy_refuses_before_spawn(tmp_path, monkeypatch, value):
    repo = tmp_path / "product"
    repo.mkdir()
    policy = tmp_path / "policy.json"
    if value is not None:
        policy.write_text(value)
    monkeypatch.setenv("COORDINATOR_CODEX_WORKER_POLICY_FILE", str(policy))
    calls = _stub_codex(monkeypatch, repo)
    monkeypatch.setattr(workflow, "preflight_auth", lambda **kw: None)
    monkeypatch.setattr(workflow, "resolve_codex", lambda **kw: ["codex"])
    with pytest.raises(workflow.AgentFailed, match="worker policy"):
        _run(repo, "codex")
    assert not calls


@pytest.mark.parametrize("annotated", [False, True])
def test_codex_gate_validates_json_rpc_result(tmp_path, monkeypatch, annotated):
    # Captured from the 7f8eb5 gate; fixture paths stand in for the original sizing and baton.
    result = {
        "arm": "m_plus", "halt": None, "baton": "state/handoffs/gate.md",
        "design_route": None, "acceptance": [], "sizing_path": "state/sizings/gate.yaml",
        "tshirt": "L", "route": "plan",
    }
    envelope = {"jsonrpc": "2.0", "id": 1, "result": result}
    if annotated:
        envelope["note"] = "Gate reply was relayed."
    calls = _stub_codex(monkeypatch, tmp_path, response=json.dumps(envelope))
    monkeypatch.setattr(workflow, 'preflight_auth', lambda **kw: None)
    monkeypatch.setattr(workflow, 'resolve_codex', lambda **kw: ['/bin/codex-x'])
    schema = {
        "type": "object",
        "properties": {"arm": {"const": "m_plus"}, "halt": {"type": "null"}},
        "required": ["arm", "halt"],
    }
    assert workflow.run_agent(
        'Gate fixture', {'label': 'gate', 'phase': 'gate', 'schema': schema},
        repo_root=tmp_path, artifact_dir=tmp_path / 'art', parent_session_id='sid',
    ) == result
    assert len(calls) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows worker prompt")
def test_windows_codex_gate_uses_native_command_with_original_payload(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit.ask_compose import _INVOKE

    calls = _stub_codex(monkeypatch, tmp_path, response='{"arm":"s","halt":null}')
    monkeypatch.setattr(workflow, "preflight_auth", lambda **kw: None)
    monkeypatch.setattr(workflow, "resolve_codex", lambda **kw: ["/bin/codex-x"])
    args = """dispatch.ask_gate '{"run_id":"ask-revised"}' --repo C:/dev/claude-klabauter"""
    assert workflow.run_agent(
        f"Run `{_INVOKE} {args}`",
        {"label": "gate", "phase": "gate", "schema": {"type": "object"}},
        repo_root=tmp_path, artifact_dir=tmp_path / "art", parent_session_id="sid",
    ) == {"arm": "s", "halt": None}
    assert f"Run `coordinator-invoke {args}`" in calls[0][1]
    assert _INVOKE not in calls[0][1]


@pytest.mark.parametrize(("initial", "repair", "arm", "error"), [
    pytest.param('{"arm":"m_plus" "halt":null}', '{"arm":"m_plus","halt":null}',
                 "m_plus", "Expecting ',' delimiter", id="1cade98f-missing-comma"),
    pytest.param('{"arm":"m_plus","halt":null},"acceptance":["keep this"]}',
                 '{"arm":"m_plus","halt":null}', None, "Extra data", id="56fbcaba-no-prefix"),
    pytest.param('{"arm":"s","halt":null}}', '{"arm":"s","halt":null}',
                 "s", "Extra data", id="a6e58670-extra-delimiter"),
    pytest.param(
        '{"jsonrpc":"2.0","id":1,"result":{"arm":"s" "halt":null,"acceptance":["keep"]}}',
        '{"arm":"s","halt":null,"acceptance":["keep"]}',
        "s", "Expecting ',' delimiter", id="d10d487a-malformed-wrapped-result",
    ),
    pytest.param(
        '{"jsonrpc":"2.0","id":1,"result":{"arm":"s" "halt":null},"acceptance":["keep"]}',
        '{"arm":"s","halt":null}',
        None, "Expecting ',' delimiter", id="d10d487a-do-not-drop-trailing-data",
    ),
    pytest.param('{"arm":"s" "halt":null}', '{"arm":"s" "halt":null}',
                 None, "Expecting ',' delimiter", id="retry-exhausted"),
])
def test_codex_gate_reasks_once_then_returns_a_typed_halt(tmp_path, monkeypatch, initial, repair, arm, error):
    repo = tmp_path / "product"
    repo.mkdir()
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({":root": "read", ":workspace_roots": "write"}))
    monkeypatch.setenv("COORDINATOR_CODEX_WORKER_POLICY_FILE", str(policy))
    calls = _stub_codex(monkeypatch, repo, responses=[initial, repair])
    monkeypatch.setattr(workflow, 'preflight_auth', lambda **kw: None)
    monkeypatch.setattr(workflow, 'resolve_codex', lambda **kw: ['/bin/codex-x'])
    schema = {
        "type": "object",
        "properties": {"arm": {"type": ["string", "null"]}, "halt": {"type": ["object", "null"]}},
        "required": ["arm", "halt"],
    }
    folder = tmp_path / 'art'
    value = workflow.run_agent(
        'Run the gate once', {'label': 'gate', 'phase': 'gate', 'schema': schema},
        repo_root=repo, artifact_dir=folder, parent_session_id='sid',
    )
    assert len(calls) == 2
    assert 'default_permissions="coordinator_worker"' in calls[0][0]
    retry_argv, retry_prompt = calls[1]
    assert retry_argv[retry_argv.index("--sandbox") + 1] == "read-only"
    assert {"shell_tool", "unified_exec", "plugins", "apps", "mcp_servers={}"} <= set(retry_argv)
    assert error in retry_prompt and 'Do not run any tools' in retry_prompt
    retry_data = json.loads(next(line for line in retry_prompt.splitlines() if line.startswith('{"diagnostic":')))
    assert retry_data["allowed_arm_values"] == ["xs", "s", "m_plus", "roadmap", None]
    assert error in retry_data["diagnostic"]
    assert 'Run the gate once' not in retry_prompt
    assert '--dangerously-bypass-approvals-and-sandbox' not in retry_argv
    assert (folder / "last.txt").read_text(encoding="utf-8") == initial
    assert (folder / "gate-reask" / "last.txt").read_text(encoding="utf-8") == repair
    assert json.loads((folder / "result.json").read_text(encoding="utf-8")) == value
    assert value["arm"] == arm
    if arm is None:
        assert value["halt"]["kind"] == "gate-reply-invalid"
    else:
        assert value["halt"] is None


_RELAY_RESULT = {
    "arm": "m_plus", "halt": None, "resume_rows": ["C0", "C4"],
    "resume_phases": {"reuse": [], "rerun": ["gate"], "verdicts": {}},
}


@pytest.mark.parametrize("trailer", [
    # 07a60db0 (ue-addon W4): dispatch.ask_gate's CLI trailer rode beside result.
    {"_execution": {"exit_code": 0, "params_file": "ask-gate.json"}},
    # d5c143c1 (rag c4821f): the relay appended an outer halt duplicating result.halt.
    {"halt": None},
    {"_execution": {"exit_code": 0}, "halt": None, "note": "relayed"},
])
def test_codex_gate_unwraps_relay_trailers_without_a_reask(tmp_path, monkeypatch, trailer):
    envelope = {"jsonrpc": "2.0", "id": 1, "result": _RELAY_RESULT, **trailer}
    calls = _stub_codex(monkeypatch, tmp_path, responses=[json.dumps(envelope)])
    monkeypatch.setattr(workflow, "preflight_auth", lambda **kw: None)
    monkeypatch.setattr(workflow, "resolve_codex", lambda **kw: ["/bin/codex-x"])
    schema = {"type": "object", "required": ["arm", "halt"]}
    assert workflow.run_agent(
        "Run gate", {"label": "gate", "phase": "gate", "schema": schema},
        repo_root=tmp_path, artifact_dir=tmp_path / "art", parent_session_id="sid",
    ) == _RELAY_RESULT
    assert len(calls) == 1


@pytest.mark.parametrize("envelope", [
    {"jsonrpc": "2.0", "id": 1, "result": {"arm": None, "halt": {"kind": "x"}}, "halt": None},
    {"jsonrpc": "2.0", "id": 1, "result": _RELAY_RESULT, "halt": {"kind": "x"}},
    {"jsonrpc": "2.0", "id": 1, "result": _RELAY_RESULT, "acceptance": ["dropped"]},
])
def test_gate_envelope_keeps_anything_it_cannot_drop_losslessly(envelope):
    assert workflow._gate_envelope_result(envelope) is envelope


def test_codex_gate_closes_only_missing_outer_brace(tmp_path, monkeypatch):
    result = {
        "arm": "s", "halt": None, "acceptance": {"route": "plan"},
        "manifest_handle": {"path": "manifest.json"}, "resume_phases": {"reuse": ["plan"]},
    }
    wrapped = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result})
    calls = _stub_codex(monkeypatch, tmp_path, responses=[wrapped[:-1]])
    monkeypatch.setattr(workflow, "preflight_auth", lambda **kw: None)
    monkeypatch.setattr(workflow, "resolve_codex", lambda **kw: ["/bin/codex-x"])
    schema = {"type": "object", "required": ["arm", "halt"]}
    assert workflow.run_agent(
        "Run gate", {"label": "gate", "phase": "gate", "schema": schema},
        repo_root=tmp_path, artifact_dir=tmp_path / "art", parent_session_id="sid",
    ) == result
    assert len(calls) == 1


@pytest.mark.parametrize(("label", "phase", "broken", "diagnostic"), [
    ("halt-commit", "commit", lambda reply: reply[:-1], "Expecting ',' delimiter"),
    ("plan-prep-gate", "plan", lambda reply: reply + "}", "Extra data"),
])
def test_codex_structured_stage_repairs_reply_without_repeating_operation(
    tmp_path, monkeypatch, label, phase, broken, diagnostic,
):
    repaired = '{"committed":true,"run_artifacts":{"withheld":{"halted.json":"halt-record-only"}}}'
    calls = _stub_codex(monkeypatch, tmp_path, responses=[broken(repaired), repaired])
    monkeypatch.setattr(workflow, "preflight_auth", lambda **kw: None)
    monkeypatch.setattr(workflow, "resolve_codex", lambda **kw: ["/bin/codex-x"])
    schema = {
        "type": "object", "required": ["committed", "run_artifacts"],
        "properties": {"committed": {"type": "boolean"}, "run_artifacts": {"type": "object"}},
    }
    assert workflow.run_agent(
        "Run operation once", {"label": label, "phase": phase, "schema": schema},
        repo_root=tmp_path, artifact_dir=tmp_path / "art", parent_session_id="sid",
    ) == json.loads(repaired)
    assert len(calls) == 2
    argv, prompt = calls[1]
    assert diagnostic in prompt
    assert "Run operation once" not in prompt
    assert argv[argv.index("--sandbox") + 1] == "read-only"


def _unelevated_home(tmp_path, monkeypatch):
    home = tmp_path / 'codex-home'
    (home / 'config.toml').write_text('[windows]\nsandbox = "unelevated"\n', encoding='utf-8')
    return home


def test_codex_stage_runs_when_unelevated_root_unproven_without_probing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.codex_runner import sandbox_preflight as sp
    _unelevated_home(tmp_path, monkeypatch)
    monkeypatch.setattr(sp, '_is_windows', lambda: True)
    monkeypatch.setattr(workflow, 'preflight_auth', lambda **kw: None)
    monkeypatch.setattr(workflow, 'resolve_codex', lambda **kw: ['/bin/codex-x'])
    from coordinator_core.codex_runner import codex_spawn_env
    monkeypatch.setattr(codex_spawn_env, 'check_pwsh_launchable', lambda env: None)
    monkeypatch.setattr(sp, 'check_workspace_write', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('probed')))
    calls = _stub_codex(monkeypatch, tmp_path)
    assert _run(tmp_path, 'codex') == {'a': 1}
    assert calls and not (tmp_path / sp.WARMUP_RELPATH).exists()


def test_codex_stage_argv_unchanged_without_unelevated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_codex(monkeypatch, tmp_path)
    from coordinator_core.codex_runner import sandbox_preflight as sp
    monkeypatch.setattr(sp, '_is_windows', lambda: True)
    monkeypatch.setattr(workflow, 'preflight_auth', lambda **kw: None)
    monkeypatch.setattr(workflow, 'resolve_codex', lambda **kw: ['/bin/codex-x'])
    from coordinator_core.codex_runner import codex_spawn_env
    monkeypatch.setattr(codex_spawn_env, 'check_pwsh_launchable', lambda env: None)
    assert _run(tmp_path, 'codex') == {'a': 1}
    argv = calls[0][0]
    assert argv[:7] == ['/bin/codex-x', 'exec', '--json', '--dangerously-bypass-approvals-and-sandbox', '--ephemeral', '-o', str(tmp_path / 'art' / 'last.txt')]
    assert '--sandbox' not in argv
    assert argv[6] == str(tmp_path / 'art' / 'last.txt') and argv[-1] == '-'
    assert not {'-c', '--config'} & set(argv)
    assert not (tmp_path / 'codex-home' / 'config.toml').exists()


def _family_model(tmp_path, monkeypatch, backend, events):
    _stub_family(monkeypatch, tmp_path, stdout_text='\n'.join(json.dumps(e) for e in events))
    _run(tmp_path, backend)
    return json.loads((tmp_path / 'art' / 'process.json').read_text())['model']


# Recorded agy 2026-11 stream-json (init/step_update/result) carries no model field; the
# model-bearing shapes below are synthesised for the CLI reporting one.
_AGY_RESULT = {'event': 'result', 'result': {'status': 'SUCCESS', 'response': '{"a": 2}'}}


@pytest.mark.parametrize('init', [
    {'event': 'init', 'conversation_id': 'c', 'init': {'cwd': '/x', 'model': 'gemini-4-argon-medium'}},
    {'event': 'init', 'conversation_id': 'c', 'model': 'gemini-4-argon-medium'}])
def test_agy_process_json_records_model_the_cli_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, init) -> None:
    assert _family_model(tmp_path, monkeypatch, 'agy', [init, _AGY_RESULT]) == 'gemini-4-argon-medium'


def test_agy_result_event_model_is_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    done = {'event': 'result', 'result': {'status': 'SUCCESS', 'response': '{"a": 2}', 'model': 'gemini-3.8-flash-low'}}
    assert _family_model(tmp_path, monkeypatch, 'agy', [{'event': 'init', 'init': {}}, done]) == 'gemini-3.8-flash-low'


def test_grok_process_json_records_model_the_cli_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _family_model(tmp_path, monkeypatch, 'grok', [{'text': '{"a": 1}', 'model': 'grok-build-1'}]) == 'grok-build-1'


@pytest.mark.parametrize('backend,events', [
    ('agy', [{'event': 'init', 'init': {'cwd': '/x'}}, _AGY_RESULT]),
    ('grok', [{'text': '{"a": 1}'}])])
def test_family_without_model_evidence_stays_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend, events) -> None:
    assert _family_model(tmp_path, monkeypatch, backend, events) == 'unknown'


def test_family_blank_or_nonstring_model_stays_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events = [{'event': 'init', 'init': {'model': '  '}}, {'event': 'init', 'model': 7}, _AGY_RESULT]
    assert _family_model(tmp_path, monkeypatch, 'agy', events) == 'unknown'


# Shapes recorded from real CLI runs 2026-11-10: grok --output-format json (indented envelope whose
# only model evidence is the modelUsage key) and agy stream-json (no model; the agy --log-file does).
_GROK_REAL = {'text': '{"a": 1}', 'stopReason': 'end_turn', 'sessionId': 's', 'usage': {'input_tokens': 5},
              'modelUsage': {'grok-4.6-build': {'inputTokens': 5, 'modelCalls': 1}}}
_AGY_REAL = [
    {'event': 'init', 'conversation_id': 'c', 'init': {'cwd': '/x', 'tools': ['run_command'], 'permission_mode': 'request-review'}},
    {'event': 'step_update', 'step_update': {'conversation_id': 'c', 'step_index': 2, 'state': 'DONE', 'step_type': 'agent_response'}},
    _AGY_RESULT]
_AGY_LOG = ('I1110 11:51:22.688106 1 printmode.go:208] Print mode: starting (promptLength=0, model="", conversationID="")\n'
            'I1110 11:51:22.691619 1 model_config_manager.go:375] Propagating selected model override to backend: label="Gemini 4 Argon (Low)"\n')


def test_grok_real_envelope_modelusage_key_is_the_recorded_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_family(monkeypatch, tmp_path, stdout_text=json.dumps(_GROK_REAL, indent=2))
    _run(tmp_path, 'grok')
    assert json.loads((tmp_path / 'art' / 'process.json').read_text())['model'] == 'grok-4.6-build'


def test_grok_multi_model_usage_records_the_most_called(tmp_path: Path) -> None:
    env = {'modelUsage': {'a': {'modelCalls': 1}, 'b': {'modelCalls': 4}}}
    (tmp_path / 's.jsonl').write_text(json.dumps(env), encoding='utf-8')
    assert workflow._event_provenance(tmp_path / 's.jsonl', 'grok')['model'] == 'b'


def test_agy_real_stream_model_comes_from_its_log_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_family(monkeypatch, tmp_path, stdout_text='\n'.join(json.dumps(e) for e in _AGY_REAL))
    inner = workflow._run_admitted

    def with_log(argv, prompt, **kw):
        Path(argv[argv.index('--log-file') + 1]).write_text(_AGY_LOG, encoding='utf-8')
        return inner(argv, prompt, **kw)

    monkeypatch.setattr(workflow, '_run_admitted', with_log)
    _run(tmp_path, 'agy')
    assert calls[0][0][calls[0][0].index('--log-file') + 1] == str(tmp_path / 'art' / 'agy.log')
    assert json.loads((tmp_path / 'art' / 'process.json').read_text())['model'] == 'Gemini 4 Argon (Low)'


def test_agy_without_log_model_line_stays_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _family_model(tmp_path, monkeypatch, 'agy', _AGY_REAL) == 'unknown'
    (tmp_path / 'art' / 'agy.log').write_text('model=""\n', encoding='utf-8')
    assert workflow._event_provenance(tmp_path / 'art' / 'stdout.jsonl', 'agy')['model'] == 'unknown'


def test_family_argv_agy_delegates_to_family_stage_argv_unrestricted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow, 'resolve_agy', lambda: ['/bin/agy-x'])
    argv = workflow._family_argv('agy', tmp_path)
    assert argv == workflow.family_stage_argv(['/bin/agy-x'], log_file=str(tmp_path / 'agy.log'))
    assert '--dangerously-skip-permissions' in argv
    assert not {'--mode', 'accept-edits', '--settings', '--sandbox'} & set(argv)
    assert argv[argv.index('--input-format') + 1] == 'stream-json'
    assert argv[argv.index('--output-format') + 1] == 'stream-json'



@pytest.mark.parametrize("outer_matches", [True, False])
def test_codex_gate_restores_malformed_duplicate_digest_from_engine_receipt(
    tmp_path, monkeypatch, outer_matches,
):
    from coordinator_core.ops.dispatch_emit import ask_stage_receipt, reply_sidecar

    run_id = "warp-2026-11-20-headless-gate-host-result-de0255-20261120T094343-34b2f2"
    manifest = tmp_path / "state" / "warp" / run_id / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"rows":[]}', encoding="ascii")
    import hashlib

    raw = manifest.read_bytes()
    authoritative = {"path": manifest.relative_to(tmp_path).as_posix(),
                     "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    ask_stage_receipt.write_gate(manifest.parent, authoritative)
    malformed = "84596cb9654fa419b0c338f474391ce7755f0e54b1819a36608"
    result = {
        "arm": "s", "halt": None,
        "manifest_handle": {**authoritative, "sha256": authoritative["sha256"] if outer_matches else "0" * 64},
        "stage_reply": {"manifest_handle": {**authoritative, "sha256": malformed}},
    }
    calls = _stub_codex(monkeypatch, tmp_path, responses=[json.dumps(result), "null"])
    monkeypatch.setattr(workflow, "preflight_auth", lambda **kw: None)
    monkeypatch.setattr(workflow, "resolve_codex", lambda **kw: ["/bin/codex-x"])
    schema = {"type": "object", "required": ["arm", "halt"], "properties": {
        "manifest_handle": reply_sidecar.HANDLE_SCHEMA,
        "stage_reply": reply_sidecar.STAGE_REPLY_SCHEMA,
    }}
    outcome = workflow.run_agent(
        "Run gate", {"label": "gate", "phase": "gate", "schema": schema, "gateManifestRunId": run_id},
        repo_root=tmp_path, artifact_dir=tmp_path / "art", parent_session_id="sid",
    )
    if not outer_matches:
        assert outcome["halt"]["kind"] == "gate-reply-invalid"
        return
    assert outcome["stage_reply"]["manifest_handle"] == authoritative
    assert outcome["manifest_handle"] == authoritative
    assert len(calls) == 1


_PLAIN_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "tail": {"type": "string"}},
    "required": ["summary", "tail"],
}


def _run_plain(tmp_path, monkeypatch, responses):
    calls = _stub_codex(monkeypatch, tmp_path, responses=responses)
    monkeypatch.setattr(workflow, "preflight_auth", lambda **kw: None)
    monkeypatch.setattr(workflow, "resolve_codex", lambda **kw: ["/bin/codex-x"])
    folder = tmp_path / "art"
    run = lambda: workflow.run_agent(  # noqa: E731
        "Do the stage", {"label": "halt-commit", "schema": _PLAIN_SCHEMA},
        repo_root=tmp_path, artifact_dir=folder, parent_session_id="sid",
    )
    return calls, folder, run


def _long_reply(body_char):
    # Shaped like the 8.7-8.9k witnesses: a long string field, then a trailing field.
    return '{"summary": "' + body_char + '", "tail": "done"}'


def test_unparseable_reply_persists_raw_then_fails_typed(tmp_path, monkeypatch):
    broken = '{"summary": "' + "x" * 8800 + '" "tail": "done"}'
    calls, folder, run = _run_plain(tmp_path, monkeypatch, [broken, broken])
    with pytest.raises(workflow.StructuredReplyInvalid) as info:
        run()
    assert len(calls) == 2  # one bounded re-ask, no more
    raw = folder / "raw-reply.txt"
    assert raw.read_text(encoding="utf-8") == broken
    message = str(info.value)
    assert message.startswith("structured_reply_invalid:")
    assert f"next_action: inspect the raw reply at {raw}" in message
    assert "could not be repaired" in message


@pytest.mark.parametrize("literal", ["line one\nline two\ttabbed " * 350, 'said "hello" and "bye" ' * 400])
def test_long_reply_with_literal_newlines_or_quotes_is_repaired_without_reask(tmp_path, monkeypatch, literal):
    reply = _long_reply(literal)
    assert 8000 < len(reply) < 12000
    calls, folder, run = _run_plain(tmp_path, monkeypatch, [reply])
    value = run()
    assert len(calls) == 1
    assert value == {"summary": literal, "tail": "done"}
    assert json.loads((folder / "result.json").read_text(encoding="utf-8")) == value


def test_reply_is_read_from_last_message_file_not_a_truncated_stream(tmp_path, monkeypatch):
    full = json.dumps({"summary": "s" * 9000, "tail": "done"})
    calls = _stub_family(monkeypatch, tmp_path, stdout_text=json.dumps(
        {"type": "item.completed", "item": {"type": "agent_message", "text": full[:4000]}}) + "\n")
    inner = workflow._run_admitted

    def fake(argv, prompt, **kw):
        Path(argv[argv.index("-o") + 1]).write_text(full, encoding="utf-8")
        return inner(argv, prompt, **kw)

    monkeypatch.setattr(workflow, "_run_admitted", fake)
    monkeypatch.setattr(workflow, "preflight_auth", lambda **kw: None)
    monkeypatch.setattr(workflow, "resolve_codex", lambda **kw: ["/bin/codex-x"])
    value = workflow.run_agent(
        "Do the stage", {"label": "halt-commit", "schema": _PLAIN_SCHEMA},
        repo_root=tmp_path, artifact_dir=tmp_path / "art", parent_session_id="sid",
    )
    assert value["summary"] == "s" * 9000 and len(calls) == 1


def test_only_commit_worker_receives_provisioned_repository_lock(tmp_path, monkeypatch):
    import subprocess
    from coordinator_core.ops.dispatch_emit.land_byte_gate import admit, sidecar_path

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setenv("COORDINATOR_LOCK_ROOT", str(tmp_path / "machine"))
    policy = tmp_path / "workers.json"
    policy.write_text(json.dumps({":root": "deny", str(repo): "write"}))
    with admit(repo, wait_secs=0):
        lock = sidecar_path(repo)
    monkeypatch.setenv("COORDINATOR_CODEX_LANDING_LOCK", str(lock))
    args = (["codex"], policy, repo, repo / "last")
    assert str(lock) in " ".join(workflow._restricted_worker_argv(*args, commit_worker=True))
    assert str(lock) not in " ".join(workflow._restricted_worker_argv(*args))
    monkeypatch.setenv("COORDINATOR_CODEX_LANDING_LOCK", str(policy))
    with pytest.raises(workflow.AgentFailed, match="repository sidecar"):
        workflow._restricted_worker_argv(*args, commit_worker=True)


@pytest.mark.parametrize("role,label,selected", [
    ("coordinator:test-runner", "overlay-verify", True),
    ("coordinator:test-runner", "verify-tests", False),
    ("coordinator:git-commit-agent", "overlay-verify", False),
])
def test_overlay_allocation_only_reaches_issued_verifier(tmp_path, monkeypatch, role, label, selected):
    repo = tmp_path / "repo"
    repo.mkdir()
    directory = tmp_path / "verifier-temp"
    directory.mkdir()
    base = tmp_path / "worker.json"
    base.write_text(json.dumps({":root": "deny", str(repo): "write"}))
    verifier = tmp_path / "verifier.json"
    verifier.write_text(json.dumps({":root": "deny", str(repo): "write", str(directory): "write"}))
    monkeypatch.setenv("COORDINATOR_CODEX_WORKER_POLICY_FILE", str(base))
    monkeypatch.setenv("COORDINATOR_CODEX_VERIFY_POLICY_FILE", str(verifier))
    monkeypatch.setenv("COORDINATOR_CODEX_VERIFY_TEMP", str(directory))
    definitions = tmp_path / "agents"
    definitions.mkdir()
    (definitions / (role.split(":")[1] + ".md")).write_text("Perform the issued stage.")
    monkeypatch.setattr("coordinator_core.content_root.read_content_root", lambda: str(tmp_path))
    calls = _stub_codex(monkeypatch, repo)
    monkeypatch.setattr(workflow, "preflight_auth", lambda **kw: None)
    monkeypatch.setattr(workflow, "resolve_codex", lambda **kw: ["codex"])
    workflow.run_agent("verify", {"agentType": role, "label": label, "schema": _SCHEMA},
                       repo_root=repo, artifact_dir=repo / "art", parent_session_id="sid")
    args = calls[0][0]
    assert (f"shell_environment_policy.set.TMPDIR={json.dumps(str(directory))}" in args) == selected
    assert (str(directory) in next(arg for arg in args if ".filesystem=" in arg)) == selected


@pytest.mark.skipif(not hasattr(os, 'pread'), reason='positional read required')
def test_native_identity_uses_child_descriptor_when_workspace_archive_disagrees(tmp_path):
    import tempfile

    archive = tmp_path / 'stdout.jsonl'
    real = '01a1b826-0012-72a0-a4d0-eae0ae828f4c'
    alternate = '01a1b826-0012-72a0-a4d0-eae0ae828f4d'
    def started(sid):
        return (json.dumps({'type': 'thread.started', 'thread_id': sid}) + '\n').encode()
    archive.write_bytes(started(alternate))
    calls = []
    with tempfile.TemporaryFile(dir=tmp_path) as capture:
        capture.write(started(real))
        capture.flush()
        observer = workflow._NativeThreadObserver(archive, {}, calls.append, lambda: None, capture=capture)
        observer.start()
        observer.finish()
        assert capture.tell() == len(started(real))
    assert calls == [{'session_id': real}]


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(workflow.sys.platform != "darwin", reason="Darwin Seatbelt verifier policy")
@pytest.mark.parametrize(("backend", "role", "restricted", "expected"), [
    ("codex", "test-runner", True, 19),
    ("codex", "test-runner", False, 10),
    ("codex", "executor", True, 10),
    ("agy", "test-runner", True, 10),
])
def test_worker_inherits_verifier_priority(monkeypatch, backend, role, restricted, expected):
    import subprocess
    monkeypatch.setattr(workflow.tenant_priority, "popen_kwargs", lambda _: {
        "preexec_fn": lambda: os.setpriority(os.PRIO_PROCESS, 0, 10)})
    if restricted:
        monkeypatch.setenv("COORDINATOR_CODEX_WORKER_POLICY_FILE", "selected")
    else:
        monkeypatch.delenv("COORDINATOR_CODEX_WORKER_POLICY_FILE", raising=False)
    argv = [workflow.sys.executable, "-c", "import os; print(os.getpriority(os.PRIO_PROCESS, 0))"]
    result = subprocess.check_output(argv, **workflow._worker_priority(argv, backend, "coordinator:" + role))
    assert int(result) == expected
