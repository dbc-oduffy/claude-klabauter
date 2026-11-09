"""Daemon identity never becomes role/delegation authority for another request."""
from coordinator_core.session.caller_env import carried_caller_env, effective_caller_env
from coordinator_core.warm.entry_seam import per_request_state


def test_warm_unisolated_uses_actual_packet_and_preserves_reentry(monkeypatch):
    monkeypatch.setenv('COORDINATOR_AGENT_ROLE', 'em')
    actual = {'COORDINATOR_AGENT_ROLE': 'workflow-agent', 'COORDINATOR_PARENT_SESSION_ID': 'parent'}
    with per_request_state(warm_served=True, isolated=False, env=actual):
        assert effective_caller_env() == actual
        with per_request_state(isolated=False):
            assert carried_caller_env() == actual
        from coordinator_core.warm.entry_seam import _reentry_scope
        with _reentry_scope():
            assert carried_caller_env() == actual
        copy = effective_caller_env()
        copy['COORDINATOR_AGENT_ROLE'] = 'em'
        assert carried_caller_env() == actual
    assert effective_caller_env()['COORDINATOR_AGENT_ROLE'] == 'em'


def test_new_missing_warm_request_clears_outer_authority(monkeypatch):
    monkeypatch.setenv('COORDINATOR_ALLOW_FIRE_TOKEN', 'daemon-not-caller')
    with per_request_state(warm_served=True, isolated=False, env={'COORDINATOR_AGENT_ROLE': 'em'}):
        with per_request_state(warm_served=True, isolated=False):
            assert effective_caller_env() == {}
        assert carried_caller_env() == {'COORDINATOR_AGENT_ROLE': 'em'}


def test_cold_explicit_packet_and_unbound_own_environment(monkeypatch):
    monkeypatch.setenv('COORDINATOR_AGENT_ROLE', 'worker')
    assert carried_caller_env() == {}
    assert effective_caller_env()['COORDINATOR_AGENT_ROLE'] == 'worker'
    with per_request_state(warm_served=False, isolated=False, session_id='00000000-0000-4000-8000-000000000001'):
        assert effective_caller_env()['COORDINATOR_AGENT_ROLE'] == 'worker'
    with per_request_state(warm_served=False, isolated=False, env={'COORDINATOR_AGENT_ROLE': 'reviewer'}):
        assert effective_caller_env() == {'COORDINATOR_AGENT_ROLE': 'reviewer'}
