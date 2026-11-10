import pytest

from coordinator_core.ops.workflow_fire import authority
from coordinator_core.session import agent_role, core
from coordinator_core.session.caller_env import bind_caller_env


@pytest.mark.parametrize('role', ['worker', 'reviewer', 'workflow-agent', 'unknown'])
def test_non_em_submit_is_recorded_not_refused(monkeypatch, role):
    from coordinator_core import guard_advisory_counter
    fired = []
    monkeypatch.setattr(guard_advisory_counter, 'record_advisory_fire', lambda *a: fired.append(a))
    monkeypatch.setattr(authority, 'caller_principal', lambda *a: authority.Principal('actual', role, {}))
    assert authority.require_submission_em('actual', '.') is None
    assert fired == [('submit-engine-ask-non-em', 'actual', '.')]
    assert authority.require_submission_em('other', '.') is not None


def test_em_submit_records_no_advisory(monkeypatch):
    from coordinator_core import guard_advisory_counter
    fired = []
    monkeypatch.setattr(guard_advisory_counter, 'record_advisory_fire', lambda *a: fired.append(a))
    monkeypatch.setattr(authority, 'caller_principal', lambda *a: authority.Principal('actual', 'em', {}))
    assert authority.require_submission_em('actual', '.') is None
    assert fired == []


def test_root_codex_exec_registered_em_is_admitted(monkeypatch):
    from coordinator_core.session import external_registry
    sid = '00000000-0000-4000-8000-000000000002'
    payload = {'hook_event_name': 'SessionStart', 'session_id': sid, 'source': 'startup'}
    role = agent_role.classify(payload, {'CODEX_THREAD_ID': sid}, **agent_role.observed_cli_launch(['codex', 'exec', '-']))
    monkeypatch.setattr(external_registry, 'lookup', lambda s: {'session_id': sid, 'role': role})
    monkeypatch.setattr(authority, 'caller_principal',
                        lambda *a: authority.Principal(sid, agent_role.role_for_caller({}, sid), {}))
    assert role == 'em'
    assert authority.require_submission_em(sid, '.') is None


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
        assert authority.caller_principal().role == 'worker'
    assert observed == [({'COORDINATOR_AGENT_ROLE': 'worker'}, 'actual-worker')]


def test_missing_new_warm_context_does_not_borrow_daemon_role(monkeypatch):
    monkeypatch.setenv('COORDINATOR_AGENT_ROLE', 'em')
    monkeypatch.setattr(core, 'attributable_session_id', lambda *a: '')
    with core.warm_served_request():
        caller = authority.caller_principal()
    assert caller.role == 'unknown' and caller.env == {}


def _headless(monkeypatch, tmp_path, family, extra_env=None, token_env=True, role='worker'):
    from coordinator_core.ops.workflow_fire import headless_em_grant as grant
    monkeypatch.setenv('COORDINATOR_SETTINGS_HOME', str(tmp_path))
    sid = 'headless-' + family
    token = grant.write_headless_em_grant(sid, family)
    env = {'CODEX_THREAD_ID': sid} if family == 'codex' else {}
    if token_env:
        env[grant.HEADLESS_EM_TOKEN_ENV] = token
    env.update(extra_env or {})
    monkeypatch.setattr(authority, 'caller_principal', lambda *a: authority.Principal(sid, role, env))


def _fire():
    return authority.require_fire_authority('unused.mjs', None, cwd=None, native=True, backend='codex')


@pytest.mark.parametrize('family', ['codex', 'agy', 'grok'])
def test_grant_verified_headless_em_fires(monkeypatch, tmp_path, family):
    _headless(monkeypatch, tmp_path, family)
    assert _fire().session_id == 'headless-' + family


@pytest.mark.parametrize('family', ['codex', 'agy', 'grok'])
def test_grant_verified_headless_em_is_its_own_fire_record_caller(monkeypatch, tmp_path, family):
    _headless(monkeypatch, tmp_path, family, {'COORDINATOR_PARENT_SESSION_ID': 'someone-else'})
    assert authority.fire_record_caller_session_id(_fire()) == 'headless-' + family


def test_unverified_principal_inherits_parent_as_fire_record_caller():
    worker = authority.Principal('w', 'worker', {'COORDINATOR_PARENT_SESSION_ID': 'parent'})
    assert authority.fire_record_caller_session_id(worker) == 'parent'
    assert authority.fire_record_caller_session_id(authority.Principal('w', 'worker', {})) is None


@pytest.mark.parametrize('family', ['codex', 'agy', 'grok'])
def test_unverified_headless_session_is_refused(monkeypatch, tmp_path, family):
    _headless(monkeypatch, tmp_path, family, token_env=False)
    with pytest.raises(ValueError, match='verified EM'):
        _fire()


def test_wrong_token_is_refused(monkeypatch, tmp_path):
    from coordinator_core.ops.workflow_fire.headless_em_grant import HEADLESS_EM_TOKEN_ENV
    _headless(monkeypatch, tmp_path, 'codex', {HEADLESS_EM_TOKEN_ENV: 'forged'})
    with pytest.raises(ValueError, match='verified EM'):
        _fire()


@pytest.mark.parametrize('marker', [{'COORDINATOR_AGENT_WORKER': '1'}, {'COORDINATOR_AGENT_ROLE': 'worker'},
                                    {'COORDINATOR_AGENT_ROLE': 'reviewer'}])
def test_worker_marker_is_refused_despite_valid_grant(monkeypatch, tmp_path, marker):
    _headless(monkeypatch, tmp_path, 'codex', marker)
    with pytest.raises(ValueError, match='verified EM'):
        _fire()


def test_token_forwarded_via_caller_env_reaches_fire_authority(monkeypatch, tmp_path):
    from coordinator_core.ops.workflow_fire import headless_em_grant as grant
    monkeypatch.setenv('COORDINATOR_SETTINGS_HOME', str(tmp_path))
    sid = 'headless-forwarded'
    token = grant.write_headless_em_grant(sid, 'agy')
    monkeypatch.setattr(core, 'attributable_session_id', lambda *a: sid)
    monkeypatch.setattr(agent_role, 'role_for_caller', lambda env, s: 'unknown')
    with core.warm_served_request(), bind_caller_env({grant.HEADLESS_EM_TOKEN_ENV: token}):
        assert authority.require_fire_authority('unused.mjs', None, cwd=None, native=True,
                                                backend='codex').session_id == sid
    with core.warm_served_request(), bind_caller_env({grant.HEADLESS_EM_TOKEN_ENV: 'forged'}):
        with pytest.raises(ValueError, match='verified EM'):
            authority.require_fire_authority('unused.mjs', None, cwd=None, native=True, backend='codex')
