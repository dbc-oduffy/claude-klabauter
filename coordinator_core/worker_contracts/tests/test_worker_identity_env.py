"""Inspect the actual supervised spawn environment without launching a child."""
import os
from types import SimpleNamespace
import pytest
from coordinator_core.worker_contracts import worker

OWN_IDS = ("CODEX_THREAD_ID", "ANTIGRAVITY_CONVERSATION_ID", "AGY_SESSION_ID",
           "COORDINATOR_AGENT_SESSION", "COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID",
           "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "COORDINATOR_WORKFLOW_AGENT_TYPE",
           "COORDINATOR_WORKFLOW_AGENT_LABEL", "COORDINATOR_AGENT_TYPE", "COORDINATOR_AGENT_LABEL")

@pytest.mark.parametrize('args,model', [([],None),(['--model','gpt-6.1-sol'],'gpt-6.1-sol'),
                                      (['--model=gpt-6.1-sol'],'gpt-6.1-sol')])
def test_supervised_worker_spawn_has_actual_role_vendor_and_no_parent_identity(tmp_path,monkeypatch,args,model):
    captured={}
    base={key:'parent-own-id' for key in OWN_IDS}
    base.update(COORDINATOR_AGENT_ROLE='em',COORDINATOR_AGENT_PROVIDER='claude',
                COORDINATOR_AGENT_VENDOR='claude',COORDINATOR_AGENT_MODEL='parent-opus',
                COORDINATOR_PARENT_SESSION_ID='unrelated-grandparent',PATH='keep')
    monkeypatch.setattr(worker,'_child_env',lambda requested:base)
    monkeypatch.setattr(worker.slot_grant,'check_slot_grant',lambda *a,**k:(True,{}))
    monkeypatch.setattr(worker.capacity,'admit_before_spawn',lambda *a,**k:SimpleNamespace(admitted=True,peak_gib=1))
    monkeypatch.setattr(worker.capacity,'release_spawn',lambda *a,**k:None)
    monkeypatch.setattr(worker.admission_post,'post',lambda *a,**k:{'delivered':True})
    monkeypatch.setattr(worker.roster,'write_record',lambda *a,**k:None)
    monkeypatch.setattr(worker.roster,'remove_record',lambda *a,**k:None)
    class Process:
        pid=123;returncode=0
        def communicate(self,prompt=None):return ('','')
    def fake(argv,**kwargs):
        captured.update(kwargs['env'])
        return Process()
    parent='00000000-0000-4000-8000-000000000001'
    assignment={'session_id':parent,'run':'assigned','declared_peak_mb':1024}
    assert worker.supervise(tmp_path,'assigned task',assignment,['codex',*args,'exec','-'],provider='codex',mode='worker',popen=fake)==0
    assert all(key not in captured for key in OWN_IDS)
    assert captured['COORDINATOR_PARENT_SESSION_ID']==parent
    assert captured['COORDINATOR_AGENT_ROLE']=='worker'
    assert captured['COORDINATOR_AGENT_PROVIDER']==captured['COORDINATOR_AGENT_VENDOR']=='codex'
    assert captured.get('COORDINATOR_AGENT_MODEL')==model
    assert captured['PATH'].split(os.pathsep)[0]=='keep' and base['COORDINATOR_AGENT_ROLE']=='em'
