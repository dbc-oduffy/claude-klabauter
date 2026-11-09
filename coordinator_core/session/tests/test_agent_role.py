import pytest
from coordinator_core.session.agent_role import classify
SID = "00000000-0000-4000-8000-000000000001"
BASE = {"hook_event_name":"SessionStart", "session_id":SID, "source":"startup"}

@pytest.mark.parametrize('env,observed,expected', [
    ({}, {}, 'unknown'),
    ({'COORDINATOR_JOB_MODE':'cron'}, {'native_main':True}, 'unknown'),
    ({'COORDINATOR_JOB_MODE':'interactive'}, {}, 'unknown'),
    ({'COORDINATOR_AGENT_ROLE':'em'}, {'native_main':True,'exec_worker':True}, 'worker'),
    ({'COORDINATOR_AGENT_ROLE':'em','COORDINATOR_AGENT_WORKER':'1'}, {'native_main':True}, 'worker'),
    ({'COORDINATOR_AGENT_ROLE':'invalid'}, {'native_main':True}, 'unknown'),
])
def test_unknown_or_unattended_launch_never_auto_promotes(env, observed, expected):
    assert classify(BASE, env, **observed) == expected


@pytest.mark.parametrize('explicit,registered,expected', [
    ('worker','em','worker'), ('workflow-agent','em','workflow-agent'),
    ('reviewer','em','reviewer'), ('unknown','em','unknown'),
    ('invalid','em','unknown'), ('em','worker','worker'),
    ('em',None,'unknown'), (None,'em','em'), (None,None,'unknown'),
])
def test_caller_authority_never_uses_parent_or_daemon_role(monkeypatch, explicit, registered, expected):
    from coordinator_core.session.agent_role import role_for_caller
    from coordinator_core.session import external_registry
    row = {'session_id':SID,'role':registered} if registered else None
    monkeypatch.setattr(external_registry,'lookup',lambda sid:row)
    env = {'COORDINATOR_AGENT_ROLE':explicit} if explicit else {}
    assert role_for_caller(env,SID)==expected


def test_legacy_worker_marker_overrides_registered_em(monkeypatch):
    from coordinator_core.session.agent_role import role_for_caller
    from coordinator_core.session import external_registry
    monkeypatch.setattr(external_registry,'lookup',lambda sid:{'session_id':SID,'role':'em'})
    assert role_for_caller({'COORDINATOR_AGENT_WORKER':'1','COORDINATOR_AGENT_ROLE':'em'},SID)=='worker'


def test_explicit_em_marker_alone_does_not_establish_native_main():
    from coordinator_core.session.agent_role import classify
    assert classify({"hook_event_name":"SessionStart", "source":"startup", "session_id":"e29b1cb1-ddaa-44fe-9918-ece1b81fd341"}, {"COORDINATOR_AGENT_ROLE":"em"}) == "unknown"


EXEC = ['codex', 'exec', '-']
CONTRACTS = [{'COORDINATOR_AGENT_ROLE': 'worker'}, {'COORDINATOR_AGENT_ROLE': 'reviewer'},
             {'COORDINATOR_PARENT_SESSION_ID': SID}, {'COORDINATOR_AGENT_ROLE': 'em'}]


def test_root_codex_exec_without_engine_contract_is_em():
    from coordinator_core.session.agent_role import observed_cli_launch
    assert classify(BASE, {'CODEX_THREAD_ID': SID}, **observed_cli_launch(EXEC)) == 'em'


def test_root_codex_exec_still_needs_a_session_start_payload_and_no_job_mode():
    from coordinator_core.session.agent_role import observed_cli_launch
    launch = observed_cli_launch(EXEC)
    assert classify({'hook_event_name': 'PreToolUse'}, {}, **launch) == 'unknown'
    assert classify({**BASE, 'session_id': 'x'}, {}, **launch) == 'unknown'
    assert classify(BASE, {'COORDINATOR_JOB_MODE': 'cron'}, **launch) == 'unknown'


@pytest.mark.parametrize('env', CONTRACTS)
def test_engine_spawned_codex_exec_child_stays_worker(env):
    from coordinator_core.session.agent_role import observed_cli_launch
    assert classify(BASE, env, **observed_cli_launch(EXEC)) == ('reviewer' if env.get('COORDINATOR_AGENT_ROLE') == 'reviewer' else 'worker')


def test_codex_review_child_stays_reviewer_and_subagent_stays_worker():
    from coordinator_core.session.agent_role import observed_cli_launch
    assert classify(BASE, {}, **observed_cli_launch(['codex', 'review'])) == 'reviewer'
    assert classify({**BASE, 'agent_id': 'a'}, {}, **observed_cli_launch(EXEC)) == 'worker'


def test_claude_em_classification_unchanged():
    assert classify(BASE, {}, native_main=True) == 'em'
    assert classify(BASE, {}) == 'unknown'


@pytest.mark.parametrize('argv,expected',[
    (['codex'],'em'), (['codex','app-server','--listen','stdio://'],'em'),
    (['codex','resume','--last'],'em'), (['codex','fork','--last'],'em'),
    (['codex','--model','exec'],'em'), (['codex','--model=review'],'em'),
    (['codex','-mexec','-c','reasoning_effort="high"','-a','never','e','task'],'worker'),
    (['codex','--model','gpt-6.1-sol','--config','reasoning_effort="high"','--sandbox','workspace-write','--cd','C:/repo','exec','-'],'worker'),
    (['codex','--profile','exec','review'],'reviewer'), (['codex','r'],'reviewer'),
    (['codex','--','exec'],'em'), (['codex','--','--new-flag'],'em'),
    (['codex','--new-flag','exec'],'unknown'), (['codex','--model'],'unknown'),
    (['codex','--image','one.png','exec'],'unknown'),
    (['codex','--help'],'unknown'), (['codex','exec-server'],'unknown'),
])
def test_observed_cli_audience_skips_global_values_and_handles_delimiter(argv,expected):
    from coordinator_core.session.agent_role import observed_cli_audience,observed_cli_launch
    assert observed_cli_audience(argv)==expected
    assert classify(BASE,{},**observed_cli_launch(argv))==('em' if expected=='worker' else expected)


def test_native_launch_uses_observed_full_command_line(monkeypatch):
    from types import SimpleNamespace
    import psutil
    from coordinator_core.session.agent_role import native_launch
    argv=['codex','-m','gpt-6.1-sol','-c','a=1','-c','b=2','--profile','native','exec','-']
    host=SimpleNamespace(name=lambda:'codex.exe',cmdline=lambda:argv)
    monkeypatch.setattr(psutil,'Process',lambda pid:SimpleNamespace(parents=lambda:[host]))
    assert classify(BASE,{},**native_launch())=='em'
    assert classify(BASE,{'COORDINATOR_PARENT_SESSION_ID':SID},**native_launch())=='worker'
