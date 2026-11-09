"""Snapshots consume only their exact engine-owned workflow usage journal."""
import json
import pytest
from coordinator_core.codex_status.__main__ import snapshot

ROOT = "01a1b63c-040a-76f2-a9b9-929d9a0d0098"
CHILD = "01a1b63c-040a-76f2-a9b9-929d9a0d0099"
OTHER = "01a1b63c-040a-76f2-a9b9-929d9a0d0100"


def usage(request):
    return {'type':'coordinator_usage','payload':{'session_id':CHILD,'parent_session_id':ROOT,
            'request_id':request,'provider':'codex','model':'gpt-6.1-sol',
            'usage_format':'coordinator-canonical-v1',
            'usage':{'input_tokens':500,'cache_read_input_tokens':500,'cache_creation_input_tokens':0,'output_tokens':100}}}


def test_owned_journal_is_automatic_exact_and_idempotent(tmp_path,monkeypatch):
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


def test_absent_owned_journal_preserves_unknown_workflow_coverage(tmp_path,monkeypatch):
    monkeypatch.setenv('COORDINATOR_SETTINGS_HOME',str(tmp_path))
    value=snapshot({'session_id':ROOT})
    assert value['coverage']['workflows'] is False
    assert value['priced_events']==0
    assert not (tmp_path/'state'/'codex-usage').exists()
