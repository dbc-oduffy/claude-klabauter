"""Snapshots consume only their exact engine-owned workflow usage journal."""
import json
import pytest
from coordinator_core.codex_status.__main__ import snapshot
from pathlib import Path

ROOT = "01a1b63c-040a-76f2-a9b9-929d9a0d0098"
CHILD = "01a1b63c-040a-76f2-a9b9-929d9a0d0099"
OTHER = "01a1b63c-040a-76f2-a9b9-929d9a0d0100"


def usage(request):
    return {'type':'coordinator_usage','payload':{'session_id':CHILD,'parent_session_id':ROOT,
            'request_id':request,'provider':'codex','model':'gpt-6.1-sol',
            'usage_format':'coordinator-canonical-v1',
            'usage':{'input_tokens':500,'cache_read_input_tokens':500,'cache_creation_input_tokens':0,'output_tokens':100}}}


def test_owned_journal_is_automatic_exact_and_idempotent(tmp_path: Path,monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('COORDINATOR_SETTINGS_HOME',str(tmp_path))
    directory=tmp_path/'state'/'codex-usage';directory.mkdir(parents=True)
    journal=directory/f'{ROOT}.jsonl'
    journal.write_text(json.dumps(usage('actual-workflow-request'))+'\n')
    # Another root is never crawled or joined based on directory adjacency.
    (directory/f'{OTHER}.jsonl').write_text(json.dumps(usage('unrelated-root-file'))+'\n')
    explicit=tmp_path/'explicit.jsonl';explicit.write_text(json.dumps(usage('explicit-request'))+'\n')
    params={'session_id':ROOT,'event_journals':[str(explicit),str(journal)]}
    first=snapshot(params)
    second=snapshot(params)
    assert first['priced_events']==second['priced_events']==2
    assert first['usd']==second['usd']==pytest.approx(.00410)
    assert first['coverage']['workflows'] is False
    cache=json.loads((tmp_path/'state'/'codex-status'/f'{ROOT}.json').read_text())
    assert cache['threads'][CHILD]['parent']==ROOT
    assert str((directory/f'{OTHER}.jsonl').resolve()) not in cache['files']
    assert list(cache['files']).count(str(journal.resolve()))==1


def test_absent_owned_journal_preserves_unknown_workflow_coverage(tmp_path: Path,monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('COORDINATOR_SETTINGS_HOME',str(tmp_path))
    value=snapshot({'session_id':ROOT})
    assert value['coverage']['workflows'] is False
    assert value['priced_events']==0
    assert not (tmp_path/'state'/'codex-usage').exists()


def execution(request, phase, **extra):
    return {'type': 'coordinator_execution', 'payload': {
        'parent_session_id': ROOT, 'request_id': request, 'phase': phase, **extra}}


def test_expected_workflow_needs_terminal_priced_usage(tmp_path, monkeypatch):
    monkeypatch.setenv('COORDINATOR_SETTINGS_HOME', str(tmp_path))
    directory = tmp_path / 'state' / 'codex-usage'
    directory.mkdir(parents=True)
    journal = directory / f'{ROOT}.jsonl'
    rows = [execution('fire:1', 'started'), usage('fire:1')]
    journal.write_text(''.join(json.dumps(e) + '\n' for e in rows))
    first = snapshot({'session_id': ROOT})
    assert not first['coverage']['workflows']
    assert first['execution_inventory']['missing_terminal'] == ['fire:1']
    with journal.open('a') as out:
        out.write(json.dumps(execution('fire:1', 'completed', session_id=CHILD,
            usage_status='appended', usage_request_ids=['fire:1'])) + '\n')
    second = snapshot({'session_id': ROOT})
    assert second['coverage']['workflows']
    assert second['execution_inventory']['expected'] == 1
    assert second['priced_events'] == 1
    assert snapshot({'session_id': ROOT})['usd'] == second['usd']


def test_completed_workflow_does_not_require_native_subagent_registration(tmp_path, monkeypatch):
    from coordinator_core.codex_status.tests.test_status import metadata, context, usage as native_usage, write
    monkeypatch.setenv('COORDINATOR_SETTINGS_HOME', str(tmp_path))
    registry = tmp_path / 'host-sessions' / 'codex'
    registry.mkdir(parents=True)
    path = tmp_path / 'root.jsonl'
    write(path, [metadata(ROOT), context('root'), native_usage(1000, 500, 100)])
    (registry / f'{ROOT}.json').write_text(json.dumps({'schema_version': 1, 'provider': 'codex',
        'session_id': ROOT, 'transcript_path': str(path)}))
    journal = tmp_path / 'state' / 'codex-usage' / f'{ROOT}.jsonl'
    journal.parent.mkdir(parents=True)
    rows = [execution('fire:1', 'started'), usage('fire:1'),
            execution('fire:1', 'completed', session_id=CHILD,
                      usage_status='appended', usage_request_ids=['fire:1'])]
    journal.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    value = snapshot({'session_id': ROOT})
    assert value['coverage']['subagents'] and value['coverage']['workflows']
    assert value['threads'] == 2


def test_unpriced_or_missing_workflow_receipt_is_incomplete(tmp_path, monkeypatch):
    monkeypatch.setenv('COORDINATOR_SETTINGS_HOME', str(tmp_path))
    directory = tmp_path / 'state' / 'codex-usage'
    directory.mkdir(parents=True)
    event = usage('fire:1')
    event['payload']['model'] = 'unknown'
    rows = [execution('fire:1', 'started'), event,
            execution('fire:1', 'completed', session_id=CHILD,
                      usage_status='appended', usage_request_ids=['fire:1'])]
    (directory / f'{ROOT}.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in rows))
    value = snapshot({'session_id': ROOT})
    assert not value['coverage']['workflows']
    assert value['unpriced_events'] == 1


def test_descendant_owned_journals_are_discovered_on_later_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv('COORDINATOR_SETTINGS_HOME', str(tmp_path))
    directory = tmp_path / 'state' / 'codex-usage'
    directory.mkdir(parents=True)
    (directory / f'{ROOT}.jsonl').write_text(json.dumps(usage('child')) + '\n')
    nested = usage('nested')
    nested['payload'].update(session_id=OTHER, parent_session_id=CHILD)
    (directory / f'{CHILD}.jsonl').write_text(json.dumps(nested) + '\n')
    first = snapshot({'session_id': ROOT})
    assert not first['backlog_complete']
    assert not first['coverage']['workflows']
    value = snapshot({'session_id': ROOT})
    assert value['threads'] == 3 and value['priced_events'] == 2


def test_default_snapshot_reconciles_registered_closed_and_resumed_children(tmp_path, monkeypatch):
    from coordinator_core.codex_status.tests.test_status import metadata, context, usage as native_usage, write
    settings, codex = tmp_path / 'settings', tmp_path / 'native'
    monkeypatch.setenv('COORDINATOR_SETTINGS_HOME', str(settings))
    monkeypatch.setenv('CODEX_HOME', str(codex))
    registry = settings / 'host-sessions' / 'codex'
    registry.mkdir(parents=True)
    codex.mkdir()
    records = {}
    for sid, parent in [(ROOT, None), (CHILD, ROOT), (OTHER, CHILD)]:
        path = codex / f'{sid}.jsonl'
        write(path, [metadata(sid, parent), context(sid), native_usage(1000, 500, 100)])
        record = {'schema_version': 1, 'provider': 'codex', 'session_id': sid,
                  'parent_session_id': parent, 'transcript_path': str(path), 'ended_at': 1.0}
        records[sid] = record
        (registry / f'{sid}.json').write_text(json.dumps(record))
    value = snapshot({'session_id': ROOT})
    assert value['coverage']['subagents'] and value['threads'] == 3
    records[CHILD].pop('ended_at')
    (registry / f'{CHILD}.json').write_text(json.dumps(records[CHILD]))
    assert not snapshot({'session_id': ROOT})['coverage']['subagents']
    records[CHILD]['ended_at'] = 2.0
    (registry / f'{CHILD}.json').write_text(json.dumps(records[CHILD]))
    Path(records[OTHER]['transcript_path']).unlink()
    missing = snapshot({'session_id': ROOT})
    assert not missing['coverage']['subagents'] and not missing['backlog_complete']
    assert missing['usd'] == value['usd']


def test_default_snapshot_uses_isolated_homes_and_versioned_raw_catalog(tmp_path, monkeypatch):
    from coordinator_core.codex_status.tests.test_status import metadata, context, usage as native_usage, write
    default, native, settings = (tmp_path / name for name in ('default', 'native', 'settings'))
    default.mkdir()
    sentinel = default / 'sentinel'
    sentinel.write_text('unchanged')
    monkeypatch.setattr(Path, 'home', lambda: default)
    monkeypatch.setenv('CODEX_HOME', str(native))
    monkeypatch.setenv('COORDINATOR_SETTINGS_HOME', str(settings))
    monkeypatch.setenv('CODEX_THREAD_ID', ROOT)
    monkeypatch.delenv('COORDINATOR_SESSION_ID', raising=False)
    native.mkdir()
    registry = settings / 'host-sessions' / 'codex'
    registry.mkdir(parents=True)
    path = native / 'session.jsonl'
    meta = metadata(ROOT)
    meta['payload']['cli_version'] = '0.171.0'
    observed = native_usage(1000, 500, 100)
    observed['payload']['info']['model_context_window'] = 258400
    write(path, [meta, context('turn', 'gpt-6.2-astra'), observed])
    (registry / f'{ROOT}.json').write_text(json.dumps({'schema_version': 1,
        'provider': 'codex', 'session_id': ROOT, 'transcript_path': str(path)}))
    (native / 'models_cache.json').write_text(json.dumps({'client_version': '0.171.0',
        'models': [{'slug': 'gpt-6.2-astra', 'context_window': 272000,
                    'effective_context_window_percent': 95}]}))
    # Neither swapped home contains the other home's source of truth.
    (settings / 'models_cache.json').write_text('invalid catalog')
    value = snapshot({})
    assert value['auto_compact_token_limit'] == 244800
    assert value['context_window'] == 258400
    assert value['usd'] == pytest.approx(.0082)
    assert value['unpriced_events'] == 0
    assert value['coverage']['subagents']
    assert not value['coverage']['workflows']
    assert snapshot({})['usd'] == value['usd']
    assert list(default.iterdir()) == [sentinel]
    assert sentinel.read_text() == 'unchanged'
    assert not (native / 'state').exists()
    assert not (settings / 'session.jsonl').exists()


def test_new_rate_reprices_consumed_workflow_journal_once(tmp_path, monkeypatch):
    from coordinator_core.usage.price_table import PRICES
    monkeypatch.setenv('COORDINATOR_SETTINGS_HOME', str(tmp_path))
    journal = tmp_path / 'state' / 'codex-usage' / f'{ROOT}.jsonl'
    journal.parent.mkdir(parents=True)
    event = usage('astra-workflow')
    event['payload']['model'] = 'gpt-6.2-astra'
    journal.write_text(json.dumps(event) + '\n')
    price = PRICES['gpt-6.2-astra']
    with monkeypatch.context() as context:
        context.delitem(PRICES, 'gpt-6.2-astra')
        assert snapshot({'session_id': ROOT})['unpriced_events'] == 1
    assert PRICES['gpt-6.2-astra'] == price
    value = snapshot({'session_id': ROOT})
    assert value['unpriced_events'] == 0
    assert value['priced_events'] == 1
    assert value['usd'] == pytest.approx(.0082)
    assert snapshot({'session_id': ROOT})['usd'] == value['usd']


def test_registered_root_does_not_prove_unregistered_child_completion(tmp_path, monkeypatch):
    from coordinator_core.codex_status.tests.test_status import metadata, context, usage as native_usage, write
    monkeypatch.setenv('COORDINATOR_SETTINGS_HOME', str(tmp_path))
    registry = tmp_path / 'host-sessions' / 'codex'
    registry.mkdir(parents=True)
    path = tmp_path / 'root.jsonl'
    write(path, [metadata(ROOT), context('root'), native_usage(1000, 500, 100)])
    (registry / f'{ROOT}.json').write_text(json.dumps({'schema_version': 1, 'provider': 'codex',
        'session_id': ROOT, 'transcript_path': str(path)}))
    journal = tmp_path / 'state' / 'codex-usage' / f'{ROOT}.jsonl'
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps(usage('untracked-child')) + '\n')
    value = snapshot({'session_id': ROOT})
    assert value['threads'] == 2
    assert not value['coverage']['subagents']
    assert not value['coverage']['workflows']
