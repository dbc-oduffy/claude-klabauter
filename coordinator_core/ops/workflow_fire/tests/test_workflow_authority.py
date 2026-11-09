import pytest

from coordinator_core.ops.workflow_fire import authority
from coordinator_core.session import agent_role, core
from coordinator_core.session.caller_env import bind_caller_env


@pytest.mark.parametrize('role', ['worker', 'reviewer', 'workflow-agent', 'unknown'])
def test_non_em_cannot_submit_root_ask(monkeypatch, role):
    monkeypatch.setattr(authority, 'caller_principal', lambda *a: authority.Principal('actual', role, {}))
    assert authority.require_submission_em('actual', '.') is not None


def test_verified_em_cannot_impersonate_another_submission_session(monkeypatch):
    monkeypatch.setattr(authority, 'caller_principal', lambda *a: authority.Principal('actual', 'em', {}))
    assert authority.require_submission_em('other', '.') is not None
    assert authority.require_submission_em('actual', '.') is None


def test_worker_cannot_select_legacy_claude_to_evade_fire_authority(monkeypatch):
    monkeypatch.setattr(authority, 'caller_principal', lambda *a: authority.Principal('worker', 'worker', {}))
    with pytest.raises(ValueError, match='verified EM'):
        authority.require_fire_authority('unused.mjs', None, cwd=None, native=False, backend='claude')


def test_unknown_native_caller_is_not_promoted(monkeypatch):
    monkeypatch.setattr(authority, 'caller_principal', lambda *a: authority.Principal('', 'unknown', {}))
    with pytest.raises(ValueError, match='unknown'):
        authority.require_fire_authority('unused.mjs', None, cwd=None, native=True, backend='codex')


def test_actual_carried_context_beats_daemon_em_marker(monkeypatch):
    monkeypatch.setenv('COORDINATOR_AGENT_ROLE', 'em')
    monkeypatch.setattr(core, 'attributable_session_id', lambda *a: 'actual-worker')
    observed = []
    monkeypatch.setattr(agent_role, 'role_for_caller',
                        lambda env, sid: observed.append((dict(env), sid)) or env.get('COORDINATOR_AGENT_ROLE', 'unknown'))
    with core.warm_served_request(), bind_caller_env({'COORDINATOR_AGENT_ROLE': 'worker'}):
        assert authority.require_submission_em('actual-worker', '.') is not None
    assert observed == [({'COORDINATOR_AGENT_ROLE': 'worker'}, 'actual-worker')]


def test_missing_new_warm_context_does_not_borrow_daemon_role(monkeypatch):
    monkeypatch.setenv('COORDINATOR_AGENT_ROLE', 'em')
    monkeypatch.setattr(core, 'attributable_session_id', lambda *a: '')
    with core.warm_served_request():
        caller = authority.caller_principal()
    assert caller.role == 'unknown' and caller.env == {}
