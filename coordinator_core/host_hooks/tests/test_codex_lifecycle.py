import sys
import types
import pytest
from coordinator_core.host_hooks import codex_lifecycle as life
SID = "00000000-0000-4000-8000-000000000001"

@pytest.fixture
def em(monkeypatch):
    monkeypatch.setattr(life, 'role_for_caller', lambda *args:'em')


def test_stop_reuses_real_turn_intent_handler_without_claude_transcript(monkeypatch, em, tmp_path):
    from coordinator_core.hooks import guard_turn_intent as guard
    captured = []
    monkeypatch.setattr(guard, '_turn_intent_step', lambda payload,text: captured.append((payload,text)) or True)
    monkeypatch.setattr(guard, 'apply_guard_level', lambda name,verdict:verdict)
    payload = {'hook_event_name':'Stop','session_id':SID,'cwd':str(tmp_path), 'last_assistant_message':'I will run the failing test next.', 'transcript_path':'never-parse-native-as-claude'}
    result = life.handle(payload,{})
    assert result['decision']=='block' and result['reason']
    assert captured[0][1] == payload['last_assistant_message']
    assert 'transcript_path' not in captured[0][0]


@pytest.mark.parametrize('change', [{'stop_hook_active':True}, {'agent_id':'child'}, {'last_assistant_message':None}])
def test_stop_never_loops_or_promotes_child(monkeypatch, em, change):
    from coordinator_core.hooks import guard_turn_intent as guard
    def forbidden(*args):raise AssertionError('must not invoke EM guard')
    monkeypatch.setattr(guard,'_handler',forbidden)
    assert life.handle({'hook_event_name':'Stop','session_id':SID,**change},{}) == {}


@pytest.mark.parametrize('event',['Stop','SubagentStop','PostCompact'])
def test_status_refresh_receives_exact_native_payload_without_ending_parent(monkeypatch,event):
    seen=[]
    module=types.ModuleType('coordinator_core.codex_status.hook')
    module.handle=lambda payload,event:seen.append((payload,event))
    monkeypatch.setitem(sys.modules,'coordinator_core.codex_status.hook',module)
    monkeypatch.setattr(life,'role_for_caller',lambda *args:'unknown')
    payload={'hook_event_name':event,'session_id':SID,'agent_id':'opaque-child','agent_transcript_path':'explicit-child'}
    life.handle(payload,{})
    assert seen==[(payload,event)]


def test_compaction_uses_documented_warning_channel_not_fake_steering(em):
    result=life.handle({'hook_event_name':'PreCompact','session_id':SID},{})
    assert 'systemMessage' in result and 'hookSpecificOutput' not in result
    assert result.get('continue') is not False
