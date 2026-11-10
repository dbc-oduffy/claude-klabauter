"""Accounting uses actual exec event fields, keeping broker lineage separate."""
import json
import os
import subprocess
import sys
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.codex_runner import workflow


















@pytest.fixture(autouse=True)
def scratch_usage_home(tmp_path, monkeypatch):
    monkeypatch.setenv('COORDINATOR_SETTINGS_HOME', str(tmp_path / 'settings'))
    monkeypatch.setenv('COORDINATOR_LOCK_ROOT', str(tmp_path / 'locks'))


def test_recorded_native_exec_shape_has_thread_and_cache_usage_only(tmp_path):
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


def test_identical_unidentified_turn_counters_are_not_guessed_duplicates(tmp_path):
    path = tmp_path / "stdout.jsonl"
    event = {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 5}}
    path.write_text(json.dumps(event) + '\n' + json.dumps(event), encoding="utf-8")
    observed = workflow._event_provenance(path, "codex")
    assert observed["observed_turn_usage_total"] == {"input_tokens": 20, "cached_input_tokens": 10}
    assert len(observed["usage_events"]) == 2
    assert observed["native_total_usage"] == "unknown"


def test_process_receipt_distinguishes_broker_lineage_from_native_identity(tmp_path):
    (tmp_path / "stdout.jsonl").write_text('', encoding="utf-8")
    workflow._save_process(tmp_path, SimpleNamespace(pid=12, returncode=0), "codex", {}, "requested",
        broker_root_session_id="broker-root", broker_request_id=7, broker_fire_id="fire-a")
    receipt = json.loads((tmp_path / "process.json").read_text())
    assert receipt["broker_root_session_id"] == "broker-root"
    assert receipt["broker_request_id"] == 7 and receipt["broker_fire_id"] == "fire-a"
    assert receipt["native_thread_id"] == "unknown"
    assert "native_parent_id" not in receipt
    assert receipt["model"] == "unknown" and receipt["requested_model"] == "requested"


def test_process_receipt_uses_recorded_admission_root_when_not_explicit(tmp_path):
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


def test_canonical_usage_subtracts_cached_and_does_not_double_reasoning(tmp_path):
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


def test_save_process_twice_produces_one_usage_event(tmp_path):
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
def test_incomplete_native_counters_are_not_fabricated(tmp_path, missing):
    receipt = complete_receipt()
    del receipt['usage_events'][0][missing]
    result = workflow._append_canonical_usage(receipt)
    assert result['status'] == 'incomplete'
    assert not (tmp_path / 'settings' / 'state' / 'codex-usage').exists()


def test_canonical_append_failure_is_recorded_without_worker_failure(tmp_path, monkeypatch):
    def refused(*args, **kwargs):
        raise OSError('test lock unavailable')
    monkeypatch.setattr(workflow, 'held_lock', refused)
    result = workflow._append_canonical_usage(complete_receipt())
    assert result['status'] == 'failed' and 'test lock unavailable' in result['reason']


def test_claude_counters_are_already_uncached_and_keep_cache_writes(tmp_path):
    receipt = complete_receipt()
    receipt['backend'] = 'claude'
    receipt['usage_events'] = [{'input_tokens': 40, 'output_tokens': 12,
                              'cache_read_input_tokens': 60, 'cache_creation_input_tokens': 8}]
    result = workflow._append_canonical_usage(receipt)
    from pathlib import Path
    payload = json.loads(Path(result['path']).read_text())['payload']
    assert payload['usage'] == receipt['usage_events'][0]
    assert payload['provider'] == 'claude'


def test_invalid_native_cache_total_is_incomplete(tmp_path):
    receipt = complete_receipt()
    receipt['usage_events'][0]['cached_input_tokens'] = 101
    result = workflow._append_canonical_usage(receipt)
    assert result['status'] == 'incomplete'
    assert 'exceeds input' in result['reason']


def test_kernel_lock_deduplicates_across_real_processes(tmp_path):
    script = ('import json; from coordinator_core.codex_runner.workflow import _append_canonical_usage; '
              f'print(json.dumps(_append_canonical_usage({complete_receipt()!r})))')
    processes = [subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE,
                 stderr=subprocess.PIPE, text=True, env=dict(os.environ)) for _ in range(3)]
    results = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stderr
            results.append(json.loads(stdout))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
    assert sorted(row['status'] for row in results) == ['appended', 'duplicate', 'duplicate']


def test_observer_partial_repeated_events_preserve_actual_child_uuid(tmp_path):
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
def test_observer_invalid_or_missing_identity_fails(tmp_path, payload):
    path=tmp_path/'events.jsonl';path.write_bytes(payload)
    aborted=[];observer=workflow._NativeThreadObserver(path, {}, lambda _: None, lambda: aborted.append(True))
    observer.start()
    with pytest.raises(workflow.AgentFailed, match='identity observation failed'):
        observer.finish()
    assert aborted


def test_observer_conflicting_uuid_fails(tmp_path):
    path=tmp_path/'events.jsonl'
    path.write_text('\n'.join(json.dumps({'type':'thread.started','thread_id':sid}) for sid in
        ['01a1b826-0012-72a0-a4d0-eae0ae828f4c','01a1b826-0012-72a0-a4d0-eae0ae828f4d'])+'\n')
    calls=[];observer=workflow._NativeThreadObserver(path, {}, calls.append, lambda: None);observer.start()
    with pytest.raises(workflow.AgentFailed, match='conflicting'):
        observer.finish()
    assert len(calls)==1


def test_observer_callback_failure_is_worker_failure(tmp_path):
    path=tmp_path/'events.jsonl';path.write_text(json.dumps({'type':'thread.started','thread_id':'01a1b826-0012-72a0-a4d0-eae0ae828f4c'})+'\n')
    def refused(_): raise RuntimeError('binding refused')
    observer=workflow._NativeThreadObserver(path, {}, refused, lambda: None);observer.start()
    with pytest.raises(workflow.AgentFailed, match='binding refused'):
        observer.finish()


def test_observer_discovery_cap_does_not_limit_later_total_output(tmp_path):
    path=tmp_path/'events.jsonl'
    path.write_text(json.dumps({'type':'thread.started','thread_id':'01a1b826-0012-72a0-a4d0-eae0ae828f4c'})+'\n'+
        (json.dumps({'type':'item.completed','content':'x'*10000})+'\n')*110)
    calls=[];observer=workflow._NativeThreadObserver(path, {}, calls.append, lambda: None);observer.start();observer.finish()
    assert len(calls)==1


def test_observer_protected_artifacts_only(tmp_path):
    (tmp_path/'.git').mkdir()
    workflow._protected_observer_path(tmp_path, tmp_path/'.git/private-fire/step1')
    for unsafe in [tmp_path/'workspace-artifacts', tmp_path/'.git', tmp_path/'other/../.git/private']:
        with pytest.raises(workflow.AgentFailed):workflow._protected_observer_path(tmp_path,unsafe)
    (tmp_path/'.git/private-file').write_text('x')
    with pytest.raises(workflow.AgentFailed):workflow._protected_observer_path(tmp_path,tmp_path/'.git/private-file/step')


def test_observer_rejects_existing_reparse_ancestor(tmp_path, monkeypatch):
    (tmp_path/'.git').mkdir()
    original=Path.lstat
    def fake(path,*args,**kwargs):
        info=original(path,*args,**kwargs)
        if path==tmp_path/'.git':return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(Path,'lstat',fake)
    with pytest.raises(workflow.AgentFailed,match='unsafe ancestor'):
        workflow._protected_observer_path(tmp_path,tmp_path/'.git/private/step')


def test_native_child_facts_hashes_exact_expected_executable(tmp_path, monkeypatch):
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


def test_native_child_facts_rejects_same_basename_different_binary(tmp_path,monkeypatch):
    import psutil
    expected=tmp_path/'trusted/codex.exe';expected.parent.mkdir();expected.write_bytes(b'trusted')
    actual=tmp_path/'untrusted/codex.exe';actual.parent.mkdir();actual.write_bytes(b'untrusted')
    child=SimpleNamespace(create_time=lambda:123.5,cmdline=lambda:[str(actual),'exec'],exe=lambda:str(actual),
        name=lambda:'codex.exe',ppid=os.getpid)
    monkeypatch.setattr(psutil,'Process',lambda _:child)
    with pytest.raises(workflow.AgentFailed,match='differs from trusted'):
        workflow._native_child_facts(SimpleNamespace(pid=73,poll=lambda:None),tmp_path,'parent','fire',4,[str(expected),'exec'])


_SCHEMA = {'type': 'object', 'properties': {'a': {'type': 'integer'}}, 'required': ['a']}


def _stub_family(monkeypatch, tmp_path, *, stdout_text, returncode=0):
    calls = []

    def fake_run(argv, prompt, *, stdout, stderr, **kwargs):
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


def test_grok_stage_spawns_resolved_binary_and_parses_envelope(tmp_path, monkeypatch):
    calls = _stub_family(monkeypatch, tmp_path, stdout_text=json.dumps({'text': '{"a": 1}', 'stopReason': 'end_turn'}, indent=2))
    assert _run(tmp_path, 'grok') == {'a': 1}
    argv, stdin = calls[0]
    assert argv[0] == '/bin/grok-x' and stdin == ''
    assert argv[argv.index('--permission-mode') + 1] == 'acceptEdits'
    assert not {'--always-approve', '--dangerously-skip-permissions', '--yolo'} & set(argv)
    assert '"a"' in (tmp_path / 'art' / 'prompt.txt').read_text() and 'JSON Schema' in (tmp_path / 'art' / 'prompt.txt').read_text()


def test_agy_stage_spawns_resolved_binary_and_parses_result_event(tmp_path, monkeypatch):
    stream = '\n'.join(json.dumps(e) for e in [
        {'event': 'step_update', 'step_update': {}},
        {'event': 'result', 'result': {'status': 'SUCCESS', 'response': '```json\n{"a": 2}\n```'}}])
    calls = _stub_family(monkeypatch, tmp_path, stdout_text=stream)
    assert _run(tmp_path, 'agy') == {'a': 2}
    argv, stdin = calls[0]
    assert argv[0] == '/bin/agy-x'
    assert argv[argv.index('--mode') + 1] == 'accept-edits' and '--dangerously-skip-permissions' not in argv
    assert json.loads(stdin)['event'] == 'user' and 'do it' in json.loads(stdin)['message']['content']


def test_family_text_result_without_schema_and_empty_agy_response_fails(tmp_path, monkeypatch):
    _stub_family(monkeypatch, tmp_path, stdout_text=json.dumps({'text': 'plain'}))
    assert _run(tmp_path, 'grok', schema=None) == 'plain'
    _stub_family(monkeypatch, tmp_path, stdout_text=json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'response': ''}}))
    with pytest.raises(workflow.AgentFailed, match='no schema-valid result'):
        _run(tmp_path, 'agy', schema=None)


@pytest.mark.parametrize('backend', ['grok', 'agy'])
def test_family_schema_violation_and_nonzero_exit_fail(tmp_path, monkeypatch, backend):
    body = json.dumps({'text': '{"a": "x"}'}) if backend == 'grok' else json.dumps(
        {'event': 'result', 'result': {'status': 'SUCCESS', 'response': '{"a": "x"}'}})
    _stub_family(monkeypatch, tmp_path, stdout_text=body)
    with pytest.raises(workflow.AgentFailed, match='no schema-valid result'):
        _run(tmp_path, backend)
    _stub_family(monkeypatch, tmp_path, stdout_text=body, returncode=3)
    with pytest.raises(workflow.AgentFailed, match='exited 3'):
        _run(tmp_path, backend)


@pytest.mark.parametrize('backend', ['grok', 'agy'])
def test_family_missing_binary_raises_agent_failed(tmp_path, monkeypatch, backend):
    from coordinator_core.codex_runner.agy import AgyUnavailable
    from coordinator_core.codex_runner.grok import GrokUnavailable
    _stub_family(monkeypatch, tmp_path, stdout_text='')
    def gone(): raise (GrokUnavailable if backend == 'grok' else AgyUnavailable)('not on PATH')
    monkeypatch.setattr(workflow, 'resolve_' + backend, gone)
    with pytest.raises(workflow.AgentFailed, match='executable unavailable'):
        _run(tmp_path, backend)


def test_unknown_backend_still_refused(tmp_path):
    with pytest.raises(workflow.AgentFailed, match='unknown agent backend: gemini'):
        _run(tmp_path, 'gemini')


def test_family_receipt_is_written_without_crashing_provenance(tmp_path, monkeypatch):
    _stub_family(monkeypatch, tmp_path, stdout_text=json.dumps({'text': '{"a": 1}'}))
    _run(tmp_path, 'grok')
    receipt = json.loads((tmp_path / 'art' / 'process.json').read_text())
    assert receipt['backend'] == 'grok' and receipt['canonical_usage']['status'] == 'incomplete'


def test_builtin_workspace_policy_has_no_custom_permission_selector():
    import inspect
    source=inspect.getsource(workflow.run_agent)
    assert '\"--sandbox\", \"workspace-write\"' in source
    assert 'default_permissions' not in source
    assert '_workspace_permissions' not in source
