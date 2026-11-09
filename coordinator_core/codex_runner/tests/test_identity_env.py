"""Independent native provider launches cannot impersonate their parent EM."""
import pytest
from coordinator_core.codex_runner import review, execute, agy, grok
from coordinator_core.codex_runner.identity_env import independent_child_env, provider_identity_env
from coordinator_core.session import core
from coordinator_core.warm.entry_seam import per_request_state

SID = "11111111-1111-4111-8111-111111111111"
PEER = "22222222-2222-4222-8222-222222222222"
OWN = ('CODEX_THREAD_ID','ANTIGRAVITY_CONVERSATION_ID','COORDINATOR_AGENT_SESSION',
       'COORDINATOR_SESSION_ID','CLAUDE_SESSION_ID','CLAUDE_CODE_SESSION_ID','CLAUDE_PID')
LABELS = ('COORDINATOR_WORKFLOW_AGENT_TYPE','COORDINATOR_WORKFLOW_AGENT_LABEL',
          'COORDINATOR_AGENT_TYPE','COORDINATOR_AGENT_LABEL')


@pytest.mark.parametrize('builder,vendor,role',[(review._build_env,'codex','reviewer'),
    (agy._build_env,'agy','reviewer'),(grok._build_env,'grok','reviewer')])
def test_provider_builders_keep_allowlists_but_not_parent_identity(monkeypatch,builder,vendor,role):
    monkeypatch.setattr(core,'attributable_session_id',lambda cwd=None:SID)
    source={key:PEER for key in OWN+LABELS}
    source.update(PATH='/bin',HOME='/home',SYSTEMROOT='windows',COORDINATOR_AGENT_ROLE='em',
                  COORDINATOR_AGENT_VENDOR='claude',COORDINATOR_AGENT_MODEL='parent-opus',
                  OPENAI_API_KEY='no-leak',GEMINI_API_KEY='no-leak',UNRELATED_SECRET='no-leak')
    out=builder(source,windows=True)
    assert all(key not in out for key in OWN+LABELS)
    assert out['COORDINATOR_PARENT_SESSION_ID']==SID
    assert out['COORDINATOR_AGENT_VENDOR']==out['COORDINATOR_AGENT_PROVIDER']==vendor
    assert out['COORDINATOR_AGENT_ROLE']==role and 'COORDINATOR_AGENT_MODEL' not in out
    assert out['SYSTEMROOT']=='windows' and out['PATH']=='/bin'
    assert not any(key in out for key in ('OPENAI_API_KEY','GEMINI_API_KEY','UNRELATED_SECRET'))


def test_execute_is_worker_and_api_key_opt_in_stays_explicit(monkeypatch):
    monkeypatch.setattr(core,'attributable_session_id',lambda cwd=None:SID)
    monkeypatch.setenv('OPENAI_API_KEY','explicit-paid-key')
    monkeypatch.setenv('CODEX_THREAD_ID',PEER)
    monkeypatch.setenv('COORDINATOR_AGENT_MODEL','parent-model')
    assert 'OPENAI_API_KEY' not in execute._child_env(False)
    out=execute._child_env(True)
    assert out['OPENAI_API_KEY']=='explicit-paid-key'
    assert out['COORDINATOR_AGENT_ROLE']=='worker' and out['COORDINATOR_AGENT_VENDOR']=='codex'
    assert out['COORDINATOR_PARENT_SESSION_ID']==SID
    assert all(key not in out for key in OWN+('COORDINATOR_AGENT_MODEL',))
    assert agy._build_env({},api_key='explicit-agy-key')['GEMINI_API_KEY']=='explicit-agy-key'


def test_unresolved_caller_never_uses_ambient_peer_as_parent(monkeypatch):
    monkeypatch.setattr(core,'attributable_session_id',lambda cwd=None:'')
    out=provider_identity_env({}, {'CODEX_THREAD_ID':PEER,'COORDINATOR_AGENT_SESSION':PEER})
    assert all(key not in out for key in OWN)
    assert 'COORDINATOR_PARENT_SESSION_ID' not in out


def test_warm_daemon_em_is_not_independent_child_or_parent(monkeypatch):
    monkeypatch.setenv('COORDINATOR_AGENT_ROLE','em')
    monkeypatch.setenv('COORDINATOR_AGENT_SESSION',PEER)
    monkeypatch.setenv('COORDINATOR_WORKFLOW_AGENT_LABEL','daemon-label')
    with per_request_state(session_id=SID,warm_served=True,isolated=False,
                           env={'COORDINATOR_AGENT_ROLE':'workflow-agent','COORDINATOR_AGENT_SESSION':SID}):
        out=review._build_env({'PATH':'/bin','COORDINATOR_AGENT_SESSION':PEER})
    assert out['COORDINATOR_PARENT_SESSION_ID']==SID
    assert out['COORDINATOR_AGENT_ROLE']=='reviewer'
    assert all(key not in out for key in OWN+LABELS)


def test_final_helper_preserves_existing_scoped_delegation_without_minting():
    out=independent_child_env({'COORDINATOR_ALLOW_FIRE_TOKEN':'assigned-existing-token',
                              'COORDINATOR_AGENT_ROLE':'em'},provider='codex',role='worker',parent=SID)
    assert out['COORDINATOR_ALLOW_FIRE_TOKEN']=='assigned-existing-token'
    assert out['COORDINATOR_AGENT_ROLE']=='worker'
    assert 'COORDINATOR_ALLOW_FIRE_TOKEN' not in independent_child_env({},provider='codex',role='worker')
