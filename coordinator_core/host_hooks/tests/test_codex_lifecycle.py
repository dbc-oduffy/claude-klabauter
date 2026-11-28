import json
import sys
import pytest
from coordinator_core.host_hooks import codex_lifecycle as life
from pathlib import Path
SID = "00000000-0000-4000-8000-000000000001"

@pytest.fixture(autouse=True)
def warm_door(monkeypatch: pytest.MonkeyPatch):
    """Every test starts with the warm door unreachable; a test sets `.response` to serve it."""
    from coordinator_core.warm import client

    class Door:
        response = None

        def __init__(self) -> None:
            self.calls: list = []

    door = Door()

    def dial(msg, **kwargs):
        door.calls.append((msg, kwargs))
        return door.response

    monkeypatch.setattr(client, 'try_warm_dispatch_no_spawn', dial)
    return door


@pytest.fixture
def em(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(life.external_registry, 'lookup', lambda *args: {'session_id': SID})
    monkeypatch.setattr(life, 'role_for_caller', lambda *args:'em')


def test_stop_reuses_real_turn_intent_handler_without_claude_transcript(monkeypatch: pytest.MonkeyPatch, em, tmp_path: Path) -> None:
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
def test_stop_never_loops_or_promotes_child(monkeypatch: pytest.MonkeyPatch, em, change) -> None:
    from coordinator_core.hooks import guard_turn_intent as guard
    def forbidden(*args):raise AssertionError('must not invoke EM guard')
    monkeypatch.setattr(guard,'_handler',forbidden)
    assert life.handle({'hook_event_name':'Stop','session_id':SID,**change},{}) == {}


class _Pipe:
    def __init__(self) -> None:
        self.data = ''
        self.closed = False

    def write(self, text: str) -> None:
        self.data += text

    def close(self) -> None:
        self.closed = True


def _capture_popen(monkeypatch: pytest.MonkeyPatch) -> list:
    spawned: list = []

    class Proc:
        def __init__(self, argv, **kwargs) -> None:
            self.stdin = _Pipe()
            spawned.append((argv, kwargs, self.stdin))

    monkeypatch.setattr(life.subprocess, 'Popen', Proc)
    return spawned


@pytest.mark.parametrize('event',['Stop','SubagentStop','PostCompact'])
def test_status_refresh_is_detached_with_exact_native_payload_and_never_imported_inline(monkeypatch: pytest.MonkeyPatch,event) -> None:
    monkeypatch.delitem(sys.modules,'coordinator_core.codex_status.hook',raising=False)
    spawned=_capture_popen(monkeypatch)
    monkeypatch.setattr(life,'role_for_caller',lambda *args:'unknown')
    payload={'hook_event_name':event,'session_id':SID,'agent_id':'opaque-child','agent_transcript_path':'explicit-child'}
    result=life.handle(payload,{})
    assert result.get('systemMessage') if event=='PostCompact' else result=={}
    (argv,kwargs,pipe),=spawned
    assert argv[1:]==['-m','coordinator_core.codex_status.hook','--event',event]
    assert json.loads(pipe.data)=={'session_id':SID,'agent_transcript_path':'explicit-child'} and pipe.closed
    assert kwargs['stdout'] is life.subprocess.DEVNULL and kwargs['stdin'] is life.subprocess.PIPE
    assert ('creationflags' in kwargs) or kwargs.get('start_new_session') is True
    assert str(Path(life.__file__).resolve().parents[2]) in kwargs['env']['PYTHONPATH']
    assert 'coordinator_core.codex_status.hook' not in sys.modules


def test_detach_kwargs_allocate_no_console_so_no_conhost_is_spawned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(life.os, 'name', 'nt')
    flags = life._detach_kwargs()['creationflags']
    assert flags & 0x8 and flags & 0x200 and not flags & 0x08000000
    monkeypatch.setattr(life.os, 'name', 'posix')
    assert life._detach_kwargs() == {'start_new_session': True}


def test_status_refresh_forwards_only_consumed_keys_so_a_huge_message_never_fills_the_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned=_capture_popen(monkeypatch)
    big='x'*(1<<20)
    payload={'session_id':SID,'cwd':'C:/r','transcript_path':'C:/t','last_assistant_message':big,'tool_response':big}
    assert life.refresh_status_detached(payload,'Stop') is True
    (_,_,pipe),=spawned
    assert json.loads(pipe.data)=={'session_id':SID,'cwd':'C:/r','transcript_path':'C:/t'}
    assert len(pipe.data)<1024


@pytest.mark.parametrize('payload', [
    {'hook_event_name':'PostCompact','session_id':SID},
    {'hook_event_name':'PreCompact','session_id':SID},
    {'hook_event_name':'SubagentStop','session_id':SID},
    {'hook_event_name':'Stop','session_id':SID},
    {'hook_event_name':'Stop','session_id':SID,'last_assistant_message':'done','stop_hook_active':True},
])
def test_events_that_never_act_on_the_role_skip_the_registry_lookup(monkeypatch: pytest.MonkeyPatch, payload) -> None:
    _capture_popen(monkeypatch)
    def forbidden(*args):raise AssertionError('role lookup must not run')
    monkeypatch.setattr(life,'role_for_caller',forbidden)
    life.handle(payload,{})


def test_stop_with_a_message_still_resolves_the_role(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture_popen(monkeypatch)
    calls=[]
    monkeypatch.setattr(life.external_registry,'lookup',lambda *args:{'session_id':SID})
    monkeypatch.setattr(life,'role_for_caller',lambda *args:calls.append(args) or 'unknown')
    assert life.handle({'hook_event_name':'Stop','session_id':SID,'last_assistant_message':'hi'},{})=={}
    assert len(calls)==1


def test_unregistered_codex_session_is_unknown_without_the_harness_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture_popen(monkeypatch)
    monkeypatch.setattr(life.external_registry,'lookup',lambda *args:None)
    def forbidden(*args):raise AssertionError('harness scan must not run for a Codex session')
    monkeypatch.setattr(life,'role_for_caller',forbidden)
    assert life._codex_role({},SID)=='unknown'
    assert life.handle({'hook_event_name':'Stop','session_id':SID,'last_assistant_message':'hi'},{})=={}


def test_registered_codex_session_takes_the_full_role_rule_and_worker_marker_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(life.external_registry,'lookup',lambda *args:{'session_id':SID,'role':'em'})
    monkeypatch.setattr(life,'role_for_caller',lambda *args:'em')
    assert life._codex_role({},SID)=='em'
    assert life._codex_role({'COORDINATOR_AGENT_WORKER':'1'},SID)=='worker'


def test_precompact_never_spawns_a_status_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned=_capture_popen(monkeypatch)
    monkeypatch.setattr(life,'role_for_caller',lambda *args:'unknown')
    assert 'systemMessage' in life.handle({'hook_event_name':'PreCompact','session_id':SID},{})
    assert spawned==[]


def test_failed_refresh_spawn_still_delivers_the_hook_output(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args, **kwargs):
        raise OSError('no process')
    monkeypatch.setattr(life.subprocess,'Popen',refuse)
    result=life.handle({'hook_event_name':'PostCompact','session_id':SID},{})
    assert life.refresh_status_detached({'session_id':SID},'Stop') is False
    assert 'systemMessage' in result


def test_compaction_uses_documented_warning_channel_not_fake_steering(em) -> None:
    result=life.handle({'hook_event_name':'PreCompact','session_id':SID},{})
    assert 'systemMessage' in result and 'hookSpecificOutput' not in result
    assert result.get('continue') is not False


WARM_OK = {'jsonrpc': '2.0', 'id': 1, 'result': {'accepted': True}}


@pytest.mark.parametrize('event', ['Stop', 'SubagentStop', 'PostCompact'])
def test_status_refresh_is_one_warm_op_call_and_spawns_nothing_when_served(monkeypatch: pytest.MonkeyPatch, warm_door, event) -> None:
    spawned = _capture_popen(monkeypatch)
    warm_door.response = WARM_OK
    payload = {'hook_event_name': event, 'session_id': SID, 'cwd': 'C:/r', 'transcript_path': 'C:/t', 'last_assistant_message': 'x' * 4096}
    life.handle(payload, {})
    (msg, kwargs), = warm_door.calls
    assert msg['method'] == 'codex_status.refresh' and msg['_origin_worktree'] == 'C:/r'
    assert msg['params'] == {'event': event, 'session_id': SID, 'cwd': 'C:/r', 'transcript_path': 'C:/t'}
    assert kwargs['hook_path'] is True and kwargs['read_deadline_secs'] <= 0.15
    assert spawned == []


@pytest.mark.parametrize('response', [None, {'jsonrpc': '2.0', 'id': 1, 'error': {'code': -32602, 'message': 'bad'}}])
def test_unreachable_or_refusing_warm_door_falls_back_to_the_detached_child_once(monkeypatch: pytest.MonkeyPatch, warm_door, response) -> None:
    spawned = _capture_popen(monkeypatch)
    warm_door.response = response
    life.handle({'hook_event_name': 'Stop', 'session_id': SID, 'cwd': 'C:/r'}, {})
    assert len(warm_door.calls) == 1 and len(spawned) == 1


def test_payload_without_a_repository_never_dials_the_warm_door(monkeypatch: pytest.MonkeyPatch, warm_door) -> None:
    spawned = _capture_popen(monkeypatch)
    life.handle({'hook_event_name': 'Stop', 'session_id': SID}, {})
    assert warm_door.calls == [] and len(spawned) == 1


def test_warm_dial_never_outlasts_its_wall(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    from coordinator_core.warm import client
    release = threading.Event()
    monkeypatch.setattr(client, 'try_warm_dispatch_no_spawn', lambda msg, **kw: release.wait(5) and WARM_OK)
    monkeypatch.setattr(life, 'WARM_DIAL_BUDGET_SECS', 0.05)
    try:
        assert life.refresh_status_warm({'session_id': SID, 'cwd': 'C:/r'}, 'Stop') is False
    finally:
        release.set()


class _InlineThread:
    def __init__(self, target, args=(), **kwargs) -> None:
        self.target, self.args = target, args

    def start(self) -> None:
        self.target(*self.args)


def test_refresh_op_acks_and_runs_the_hook_refresh_with_the_consumed_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    from coordinator_core.codex_status import hook
    from coordinator_core.ops import codex_status as op
    seen = []
    monkeypatch.setattr(hook, 'handle', lambda payload, event: seen.append((payload, event)) or {'updated': True})
    monkeypatch.setattr(threading, 'Thread', _InlineThread)
    params = {'event': 'Stop', 'session_id': SID, 'cwd': 'C:/r', 'transcript_path': 'C:/t'}
    assert op.refresh(params) == {'accepted': True}
    assert seen == [({'session_id': SID, 'cwd': 'C:/r', 'transcript_path': 'C:/t'}, 'Stop')]


def test_refresh_op_reports_a_real_failure_and_stays_quiet_on_a_benign_skip(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    import threading
    from coordinator_core.codex_status import hook
    from coordinator_core.ops import codex_status as op
    monkeypatch.setattr(threading, 'Thread', _InlineThread)
    params = {'event': 'Stop', 'session_id': SID, 'cwd': 'C:/r'}
    monkeypatch.setattr(hook, 'handle', lambda payload, event: {'updated': False, 'reason': 'unchanged'})
    op.refresh(params)
    assert capsys.readouterr().err == ''
    monkeypatch.setattr(hook, 'handle', lambda payload, event: {'updated': False, 'reason': 'OSError: proxy down'})
    op.refresh(params)
    assert 'OSError: proxy down' in capsys.readouterr().err


@pytest.mark.parametrize('params', [{'event': 'SessionStart', 'session_id': SID, 'cwd': 'C:/r'}, {'event': 'Stop', 'session_id': SID, 'cwd': 'C:/r', 'extra': 1}])
def test_refresh_op_refuses_an_unknown_event_or_field(params) -> None:
    from coordinator_core.ipc import CallerFacingValidationError
    from coordinator_core.ops import codex_status as op
    with pytest.raises(CallerFacingValidationError):
        op.refresh(params)


def test_hook_forwarded_keys_equal_the_ops_accepted_keys() -> None:
    from coordinator_core.ops import codex_status as op
    assert life._STATUS_KEYS == op.REFRESH_PAYLOAD_KEYS


def test_refresh_op_is_registered_in_the_cold_map_and_classified() -> None:
    from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
    from coordinator_core.ops._registry_map import OP_MODULE_MAP
    assert OP_MODULE_MAP['codex_status.refresh'] == 'coordinator_core.ops.codex_status'
    assert OP_CLASSIFICATION['codex_status.refresh'] is OpClass.MUTATING


def test_refresh_worker_failure_is_recorded_and_releases_its_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.codex_status import hook
    from coordinator_core.ops import codex_status as op
    seen = []
    monkeypatch.setattr(hook, 'handle', lambda payload, event: (_ for _ in ()).throw(RuntimeError('boom')))
    monkeypatch.setattr(hook, 'record_result', lambda sid, event, result: seen.append((sid, event, result)))
    assert op._refresh_slots.acquire(blocking=False)
    op._refresh_worker({'session_id': SID}, 'Stop')
    assert seen == [(SID, 'Stop', {'updated': False, 'reason': 'RuntimeError: boom'})]
    assert op._refresh_slots.acquire(blocking=False)
    op._refresh_slots.release()


def test_registry_row_for_another_session_is_unknown_without_role_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(life.external_registry, 'lookup', lambda *args: {'session_id': 'someone-else'})
    def forbidden(*args): raise AssertionError('role rule must not run for a mismatched row')
    monkeypatch.setattr(life, 'role_for_caller', forbidden)
    assert life._codex_role({}, SID) == 'unknown'


def test_refresh_op_over_its_concurrency_cap_acks_busy_and_starts_no_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    from coordinator_core.ops import codex_status as op
    started = []
    monkeypatch.setattr(threading, 'Thread', lambda **kw: started.append(kw) or _InlineThread(lambda: None))
    held = 0
    while op._refresh_slots.acquire(blocking=False):
        held += 1
    try:
        assert op.refresh({'event': 'Stop', 'session_id': SID, 'cwd': 'C:/r'}) == {'accepted': True, 'skipped': 'busy'}
        assert started == []
    finally:
        for _ in range(held):
            op._refresh_slots.release()
