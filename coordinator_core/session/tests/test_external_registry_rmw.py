"""Registry RMW races preserve native task/transcript/end evidence."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from coordinator_core.session import external_registry as registry

SID = "00000000-0000-4000-8000-000000000001"

@pytest.fixture
def live(tmp_path, monkeypatch):
    from coordinator_core.session import core
    monkeypatch.setattr(core, 'stable_pid_alive', lambda *a,**k:True)
    monkeypatch.setattr(core, 'ensure_session', lambda *a,**k:None)
    import psutil
    host=SimpleNamespace(pid=123, name=lambda:'codex.exe',create_time=lambda:456,cmdline=lambda:['codex','app-server'])
    monkeypatch.setattr(psutil,'Process',lambda pid:SimpleNamespace(parents=lambda:[host]))
    monkeypatch.setenv('COORDINATOR_AGENT_ROLE','unknown')
    row={'schema_version':1,'session_id':SID,'provider':'codex','pid':123,'start_epoch':456,
         'cwd':str(tmp_path),'name':'legacy-native-name','transcript_path':str(tmp_path/'rollout.jsonl')}
    (tmp_path/f'{SID}.json').write_text(json.dumps(row))
    return tmp_path


def read(directory):
    return json.loads((directory/f'{SID}.json').read_text())


def test_original_task_title_is_idempotent_and_birth_verified(live,monkeypatch):
    from coordinator_core.session import core
    first=registry.preserve_task_title(SID,'Original task',live)
    assert first['original_task_title']=='Original task'
    assert registry.preserve_task_title(SID,'alias + cost',live)['original_task_title']=='Original task'
    assert read(live)['name']=='legacy-native-name'
    monkeypatch.setattr(core,'stable_pid_alive',lambda *a,**k:False)
    assert registry.preserve_task_title(SID,'Reused PID',live) is None
    assert read(live)['original_task_title']=='Original task'


def test_end_and_title_rmw_do_not_erase_either_record(live,monkeypatch):
    original=registry.atomic_write_bytes
    title_writing=threading.Event(); release=threading.Event(); end_started=threading.Event()
    def delayed(path,data):
        if threading.current_thread().name.startswith('title'):
            title_writing.set()
            assert release.wait(0.15)
        original(path,data)
    monkeypatch.setattr(registry,'atomic_write_bytes',delayed)
    errors=[]
    def title():
        try:registry.preserve_task_title(SID,'Concurrent original',live)
        except Exception as exc:errors.append(exc)
    def end():
        end_started.set()
        try:registry.unregister(SID,live)
        except Exception as exc:errors.append(exc)
    one=threading.Thread(target=title,name='title-writer');one.start()
    assert title_writing.wait(0.15)
    two=threading.Thread(target=end);two.start()
    assert end_started.wait(0.15)
    release.set();one.join(0.5);two.join(0.5)
    assert not one.is_alive() and not two.is_alive() and not errors
    row=read(live)
    assert row['original_task_title']=='Concurrent original' and row['ended_at']>0
    assert row['transcript_path']==str(live/'rollout.jsonl')
    assert registry.preserve_task_title(SID,'Ended thread',live) is None


def test_late_start_cannot_erase_newer_end_but_later_resume_can(live,monkeypatch):
    row=read(live);row.update(ended_at=20,original_task_title='Original')
    (live/f'{SID}.json').write_text(json.dumps(row))
    payload={'session_id':SID,'cwd':str(live),'hook_event_name':'SessionStart','source':'resume'}
    monkeypatch.setattr(registry.time,'time',lambda:10)
    stale=registry.register_current(payload,live)
    assert stale['ended_at']==20 and stale['original_task_title']=='Original'
    monkeypatch.setattr(registry.time,'time',lambda:30)
    resumed=registry.register_current(payload,live)
    assert 'ended_at' not in resumed and resumed['original_task_title']=='Original'
    assert resumed['transcript_path']==str(live/'rollout.jsonl')


def test_alias_allocates_once_then_stays_immutable_across_repo_resume(live,monkeypatch):
    monkeypatch.setattr(registry.socket,'gethostname',lambda:'MACHINE-A.long.example')
    payload={'session_id':SID,'cwd':str(live),'hook_event_name':'SessionStart','source':'startup'}
    first=registry.register_current(payload,live)
    assert first['address_version']==1 and first['name'].endswith('-codex-machine-a-long-1')
    assert first['box']=='MACHINE-A.long.example' and len(first['name'])<=64
    counter=(live/'.alias-sequence').read_bytes()
    elsewhere=live/'other repo';elsewhere.mkdir()
    monkeypatch.setattr(registry.socket,'gethostname',lambda:'different-host')
    again=registry.register_current({**payload,'cwd':str(elsewhere),'source':'resume'},live)
    assert again['name']==first['name'] and again['box']==first['box']
    assert (live/'.alias-sequence').read_bytes()==counter


def test_alias_counter_reset_cannot_reuse_ended_row_address(live,monkeypatch):
    monkeypatch.setattr(registry.socket,'gethostname',lambda:'machine-a')
    payload={'session_id':SID,'cwd':str(live)}
    first=registry.register_current(payload,live)
    registry.unregister(SID,live)
    (live/'.alias-sequence').write_text('0')
    second=registry.register_current({**payload,'session_id':'00000000-0000-4000-8000-000000000002'},live)
    assert first['name']!=second['name'] and second['name'].endswith('-2')


def test_parallel_alias_allocation_uses_one_global_sequence(live,monkeypatch):
    monkeypatch.setattr(registry.socket,'gethostname',lambda:'machine-a')
    barrier=threading.Barrier(2)
    def start(number):
        barrier.wait(timeout=0.2)
        return registry.register_current({'session_id':f'00000000-0000-4000-8000-{number:012d}','cwd':str(live)},live)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows=list(pool.map(start,[2,3]))
    assert len({row['name'] for row in rows})==2
    assert sorted(int(row['name'].rsplit('-',1)[1]) for row in rows)==[1,2]


@pytest.mark.parametrize('child_fields', [
    {'hook_event_name':'SubagentStart'},
    {'hook_event_name':'SubagentStop'},
    {'hook_event_name':'SessionStart','agent_id':'native-child'},
])
def test_constructor_cannot_replace_parent_binding_from_subagent_payload(live,child_fields):
    path=live/f'{SID}.json'
    before=path.read_bytes()
    with pytest.raises(ValueError,match='parent thread binding'):
        registry.register_current({'session_id':SID,'cwd':str(live),**child_fields},live)
    assert path.read_bytes()==before
    assert not (live/'.alias-sequence').exists()


@pytest.mark.parametrize('child_fields', [
    {'hook_event_name':'SubagentStart'},
    {'hook_event_name':'SubagentStop'},
    {'hook_event_name':'SessionEnd','agent_id':'native-child'},
])
def test_session_end_cannot_tombstone_parent_from_subagent_payload(live,child_fields):
    path=live/f'{SID}.json'
    before=path.read_bytes()
    with pytest.raises(ValueError,match='parent thread binding'):
        registry.unregister(SID,live,payload={'session_id':SID,**child_fields})
    assert path.read_bytes()==before and not registry.is_ended(SID,live)


def test_real_thread_session_end_with_payload_still_tombstones(live):
    registry.unregister(SID,live,payload={'hook_event_name':'SessionEnd','session_id':SID})
    assert registry.is_ended(SID,live)


def test_module_session_end_door_preserves_subagent_evidence(live,monkeypatch,capsys):
    import io
    before=(live/f'{SID}.json').read_bytes()
    monkeypatch.setattr(registry,'registry_dir',lambda directory=None:live)
    monkeypatch.setattr(registry.sys,'stdin',io.StringIO(json.dumps({
        'session_id':SID,'hook_event_name':'SubagentStop','agent_id':'native-child',
    })))
    assert registry.main(['--event','SessionEnd'])==0
    assert (live/f'{SID}.json').read_bytes()==before
    assert 'parent thread binding' in capsys.readouterr().err


@pytest.mark.parametrize('argv,expected',[
    (['codex','-m','model','-c','a=1','-c','b=2','exec','-'],'worker'),
    (['codex','--model','exec'],'em'), (['codex','--profile','main','review'],'reviewer')])
def test_registry_reuses_observed_cli_audience_for_native_start(live,monkeypatch,argv,expected):
    import psutil
    host=psutil.Process(0).parents()[0]
    host.cmdline=lambda:argv
    for name in ('COORDINATOR_AGENT_ROLE','COORDINATOR_AGENT_WORKER','COORDINATOR_JOB_MODE'):
        monkeypatch.delenv(name,raising=False)
    row=registry.register_current({'session_id':SID,'cwd':str(live),'hook_event_name':'SessionStart','source':'startup'},live)
    assert row['role']==expected


def test_session_start_payload_model_is_observation_and_survives_end(live,monkeypatch):
    monkeypatch.setenv('COORDINATOR_AGENT_MODEL','parent-requested-model')
    payload={'session_id':SID,'cwd':str(live),'hook_event_name':'SessionStart',
             'source':'startup','model':'actual-active-model','turn_id':'actual-turn'}
    row=registry.register_current(payload,live)
    assert row['session_start_model']=='actual-active-model'
    assert row['session_start_model_evidence']=={'source':'session_start_payload',
        'observed_at':row['observed_at'],'session_source':'startup','turn_id':'actual-turn'}
    registry.unregister(SID,live)
    ended=read(live)
    assert ended['session_start_model']==row['session_start_model']
    assert ended['session_start_model_evidence']==row['session_start_model_evidence']


def test_resume_missing_model_removes_old_observation_not_refreshes_it(live):
    payload={'session_id':SID,'cwd':str(live),'hook_event_name':'SessionStart','source':'startup'}
    registry.register_current({**payload,'model':'before-resume','turn_id':'before-turn'},live)
    resumed=registry.register_current({**payload,'source':'resume'},live)
    assert 'session_start_model' not in resumed and 'session_start_model_evidence' not in resumed
    changed=registry.register_current({**payload,'source':'resume','model':'changed-model'},live)
    assert changed['session_start_model']=='changed-model'
    assert changed['session_start_model_evidence']=={'source':'session_start_payload','observed_at':changed['observed_at'],'session_source':'resume'}


@pytest.mark.parametrize('event,model',[('SessionStart','  '),('SessionStart',None),('SessionStart',{}),('OtherEvent','claimed-model')])
def test_unsupplied_or_non_start_model_never_promotes_requested_setting(live,monkeypatch,event,model):
    monkeypatch.setenv('COORDINATOR_AGENT_MODEL','parent-model')
    import psutil
    psutil.Process(0).parents()[0].cmdline=lambda:['codex','--model','argv-requested-model']
    row=registry.register_current({'session_id':SID,'cwd':str(live),'hook_event_name':event,'model':model},live)
    assert 'session_start_model' not in row and 'session_start_model_evidence' not in row


def test_session_start_model_conflict_is_historical_and_monotonic(live):
    payload={'session_id':SID,'cwd':str(live),'hook_event_name':'SessionStart','source':'startup'}
    first=registry.register_current({**payload,'model':'model-a'},live)
    assert first['session_start_model_initial']=='model-a'
    assert not first.get('session_start_model_conflict')
    missing=registry.register_current({**payload,'source':'resume'},live)
    assert missing['session_start_model_initial']=='model-a'
    assert 'session_start_model' not in missing and 'session_start_model_evidence' not in missing
    changed=registry.register_current({**payload,'source':'compact','model':'model-b'},live)
    assert changed['session_start_model_conflict'] is True
    assert changed['session_start_model_evidence']['session_source']=='compact'
    back=registry.register_current({**payload,'source':'resume','model':'model-a'},live)
    assert back['session_start_model_conflict'] is True
    assert back['session_start_model_initial']=='model-a' and back['session_start_model']=='model-a'
    absent=registry.register_current({**payload,'source':'resume'},live)
    assert absent['session_start_model_conflict'] is True
    assert 'session_start_model' not in absent and 'session_start_model_evidence' not in absent
