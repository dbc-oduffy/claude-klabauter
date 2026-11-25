import pytest

from coordinator_core.ops.workflow_fire import authority
from coordinator_core.session import agent_role, core
from coordinator_core.session.caller_env import bind_caller_env
from pathlib import Path


@pytest.mark.parametrize('role', ['worker', 'reviewer', 'workflow-agent', 'unknown'])
def test_non_em_submit_is_recorded_not_refused(monkeypatch: pytest.MonkeyPatch, role) -> None:
    from coordinator_core import guard_advisory_counter
    fired = []
    monkeypatch.setattr(guard_advisory_counter, 'record_advisory_fire', lambda *a: fired.append(a))
    monkeypatch.setattr(authority, 'caller_principal', lambda *a: authority.Principal('actual', role, {}))
    assert authority.require_submission_em('actual', '.') is None
    assert fired == [('submit-engine-ask-non-em', 'actual', '.')]
    assert authority.require_submission_em('other', '.') is not None


def test_em_submit_records_no_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core import guard_advisory_counter
    fired = []
    monkeypatch.setattr(guard_advisory_counter, 'record_advisory_fire', lambda *a: fired.append(a))
    monkeypatch.setattr(authority, 'caller_principal', lambda *a: authority.Principal('actual', 'em', {}))
    assert authority.require_submission_em('actual', '.') is None
    assert fired == []


def test_root_codex_exec_registered_em_is_admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.session import external_registry
    sid = '00000000-0000-4000-8000-000000000002'
    payload = {'hook_event_name': 'SessionStart', 'session_id': sid, 'source': 'startup'}
    role = agent_role.classify(payload, {'CODEX_THREAD_ID': sid}, **agent_role.observed_cli_launch(['codex', 'exec', '-']))
    monkeypatch.setattr(external_registry, 'lookup', lambda s: {'session_id': sid, 'role': role})
    monkeypatch.setattr(authority, 'caller_principal',
                        lambda *a: authority.Principal(sid, agent_role.role_for_caller({}, sid), {}))
    assert role == 'em'
    assert authority.require_submission_em(sid, '.') is None


def test_verified_em_cannot_impersonate_another_submission_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(authority, 'caller_principal', lambda *a: authority.Principal('actual', 'em', {}))
    assert authority.require_submission_em('other', '.') is not None
    assert authority.require_submission_em('actual', '.') is None


def test_worker_cannot_select_legacy_claude_to_evade_fire_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(authority, 'caller_principal', lambda *a: authority.Principal('worker', 'worker', {}))
    with pytest.raises(ValueError, match='verified EM'):
        authority.require_fire_authority('unused.mjs', None, cwd=None, native=False, backend='claude')


def test_unknown_native_caller_is_not_promoted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(authority, 'caller_principal', lambda *a: authority.Principal('', 'unknown', {}))
    with pytest.raises(ValueError, match='unknown'):
        authority.require_fire_authority('unused.mjs', None, cwd=None, native=True, backend='codex')


def test_actual_carried_context_beats_daemon_em_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('COORDINATOR_AGENT_ROLE', 'em')
    monkeypatch.setattr(core, 'attributable_session_id', lambda *a: 'actual-worker')
    observed = []
    monkeypatch.setattr(agent_role, 'role_for_caller',
                        lambda env, sid: observed.append((dict(env), sid)) or env.get('COORDINATOR_AGENT_ROLE', 'unknown'))
    with core.warm_served_request(), bind_caller_env({'COORDINATOR_AGENT_ROLE': 'worker'}):
        assert authority.caller_principal().role == 'worker'
    assert observed == [({'COORDINATOR_AGENT_ROLE': 'worker'}, 'actual-worker')]


def test_missing_new_warm_context_does_not_borrow_daemon_role(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('COORDINATOR_AGENT_ROLE', 'em')
    monkeypatch.setattr(core, 'attributable_session_id', lambda *a: '')
    with core.warm_served_request():
        caller = authority.caller_principal()
    assert caller.role == 'unknown' and caller.env == {}


def _headless(monkeypatch, tmp_path, family, extra_env=None, token_env: bool = True, role: str = 'worker') -> None:
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
def test_grant_verified_headless_em_fires(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, family) -> None:
    _headless(monkeypatch, tmp_path, family)
    assert _fire().session_id == 'headless-' + family


@pytest.mark.parametrize('family', ['codex', 'agy', 'grok'])
def test_grant_verified_headless_em_is_its_own_fire_record_caller(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, family) -> None:
    _headless(monkeypatch, tmp_path, family, {'COORDINATOR_PARENT_SESSION_ID': 'someone-else'})
    assert authority.fire_record_caller_session_id(_fire()) == 'headless-' + family


def test_unverified_principal_inherits_parent_as_fire_record_caller() -> None:
    worker = authority.Principal('w', 'worker', {'COORDINATOR_PARENT_SESSION_ID': 'parent'})
    assert authority.fire_record_caller_session_id(worker) == 'parent'
    assert authority.fire_record_caller_session_id(authority.Principal('w', 'worker', {})) is None


@pytest.mark.parametrize('family', ['codex', 'agy', 'grok'])
def test_unverified_headless_session_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, family) -> None:
    _headless(monkeypatch, tmp_path, family, token_env=False)
    with pytest.raises(ValueError, match='verified EM'):
        _fire()


def test_wrong_token_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from coordinator_core.ops.workflow_fire.headless_em_grant import HEADLESS_EM_TOKEN_ENV
    _headless(monkeypatch, tmp_path, 'codex', {HEADLESS_EM_TOKEN_ENV: 'forged'})
    with pytest.raises(ValueError, match='verified EM'):
        _fire()


@pytest.mark.parametrize('marker', [{'COORDINATOR_AGENT_WORKER': '1'}, {'COORDINATOR_AGENT_ROLE': 'worker'},
                                    {'COORDINATOR_AGENT_ROLE': 'reviewer'}])
def test_worker_marker_is_refused_despite_valid_grant(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, marker) -> None:
    _headless(monkeypatch, tmp_path, 'codex', marker)
    with pytest.raises(ValueError, match='verified EM'):
        _fire()


_LEGACY_SID = '00000000-0000-4000-8000-000000000009'


def _legacy_row(monkeypatch, row, argv, env=None, sid=_LEGACY_SID) -> None:
    from coordinator_core.session import external_registry
    import psutil
    monkeypatch.setattr(external_registry, 'lookup', lambda s: row)
    monkeypatch.setattr(psutil, 'Process', lambda pid: type('P', (), {'cmdline': lambda self: argv})())
    monkeypatch.setattr(authority, 'caller_principal', lambda *a: authority.Principal(sid, 'unknown', env or {}))


_EM_ARGV = ['node', 'codex.js']


def test_roleless_live_row_on_an_observed_em_host_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    _legacy_row(monkeypatch, {'session_id': _LEGACY_SID, 'pid': 7}, _EM_ARGV)
    caller = authority.require_fire_authority('unused.mjs', None, cwd=None, native=True, backend='codex',
                                              submitted_session_id=_LEGACY_SID)
    assert caller.role == 'em' and caller.session_id == _LEGACY_SID


@pytest.mark.parametrize('row,argv,env', [
    ({'session_id': _LEGACY_SID, 'pid': 7, 'role': 'unknown'}, _EM_ARGV, {}),   # recorded role is never reclassified
    ({'session_id': _LEGACY_SID, 'pid': 7}, ['node', 'codex.js', 'exec', '-'], {'COORDINATOR_PARENT_SESSION_ID': 'p'}),  # engine-issued worker
    ({'session_id': _LEGACY_SID, 'pid': 7}, _EM_ARGV, {'COORDINATOR_AGENT_WORKER': '1'}),
    (None, _EM_ARGV, {}),  # no registry row: a caller string alone grants nothing
])
def test_unverified_family_em_caller_is_still_refused(monkeypatch: pytest.MonkeyPatch, row, argv, env) -> None:
    _legacy_row(monkeypatch, row, argv, env)
    with pytest.raises(ValueError, match='verified EM'):
        authority.require_fire_authority('unused.mjs', None, cwd=None, native=True, backend='codex',
                                         submitted_session_id=_LEGACY_SID)


def test_submitted_session_that_is_not_the_invoker_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(authority, 'caller_principal', lambda *a: authority.Principal('actual', 'em', {}))
    with pytest.raises(ValueError, match='must match'):
        authority.require_fire_authority('unused.mjs', None, cwd=None, native=True, backend='codex',
                                         submitted_session_id='someone-elses-em')


@pytest.fixture
def live_claude_em(monkeypatch: pytest.MonkeyPatch) -> str:
    from types import SimpleNamespace
    from coordinator_core.session import external_registry, harness_registry
    sid = '00000000-0000-4000-8000-0000000000c2'
    monkeypatch.setattr(core, 'attributable_session_id', lambda *a: sid)
    monkeypatch.setattr(external_registry, 'lookup', lambda s: None)
    monkeypatch.setattr(harness_registry, 'lookup',
                        lambda s: SimpleNamespace(pid=4242, start_epoch=1.0) if s == sid else None)
    monkeypatch.setattr(core, 'stable_pid_alive', lambda pid, **kw: True)
    return sid


def test_claude_em_fires_native_codex_headless(live_claude_em) -> None:
    with core.warm_served_request(), bind_caller_env({}):
        principal = authority.require_fire_authority('unused.mjs', None, cwd=None, native=True, backend='codex')
    assert principal.role == 'em' and principal.session_id == live_claude_em
    assert authority.fire_record_caller_session_id(principal) == live_claude_em


@pytest.mark.parametrize('env', [{'COORDINATOR_AGENT_WORKER': '1'}, {'COORDINATOR_AGENT_ROLE': 'worker'}])
def test_worker_marked_claude_session_is_still_refused(live_claude_em, env) -> None:
    with core.warm_served_request(), bind_caller_env(env):
        with pytest.raises(ValueError, match='verified EM'):
            authority.require_fire_authority('unused.mjs', None, cwd=None, native=True, backend='codex')


@pytest.mark.parametrize('env,expected', [
    ({'COORDINATOR_AGENT_WORKER': 'yes'}, True),
    ({'COORDINATOR_AGENT_ROLE': 'reviewer'}, True),
    ({'COORDINATOR_AGENT_ROLE': 'em'}, False),
    ({}, False),
])
def test_refusal_surface_shares_the_role_source_marker_set(env, expected) -> None:
    assert authority._explicit_worker_marker(env) is expected is agent_role.explicit_worker_marker(env)


_AGY_SID = '5f30ae34-9887-4080-8d72-e61aaac0cedb'


@pytest.fixture
def agy_leg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An interactive agy conversation with a leg record but no registry row."""
    import json
    import psutil
    from types import SimpleNamespace
    monkeypatch.setenv('COORDINATOR_SETTINGS_HOME', str(tmp_path))
    for var in ('COORDINATOR_AGENT_WORKER', 'COORDINATOR_AGENT_ROLE'):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(core, 'attributable_session_id', lambda *a: _AGY_SID)
    monkeypatch.setattr(core, 'stable_pid_alive', lambda *a, **k: True)
    monkeypatch.setattr(core, 'ensure_session', lambda *a, **k: None)
    (tmp_path / 'work').mkdir()
    legs = tmp_path / 'unimatrix' / 'agy-legs'
    legs.mkdir(parents=True)
    (legs / f'{_AGY_SID}.json').write_text(
        json.dumps({'conversation': _AGY_SID, 'agy_pid': 501, 'cwd': str(tmp_path / 'work')}), encoding='utf-8')

    def host(name, argv):
        proc = SimpleNamespace(pid=501, name=lambda: name, cmdline=lambda: list(argv), create_time=lambda: 1.0)
        monkeypatch.setattr(psutil, 'Process', lambda p: proc)
    return host


def _agy_fire(env):
    with core.warm_served_request(), bind_caller_env(env):
        return authority.require_fire_authority('unused.mjs', None, cwd=None, native=True, backend='codex')


@pytest.mark.parametrize('name,argv', [
    ('agy.exe', ['agy.exe', '--dangerously-skip-permissions']),
    ('agy', ['agy', '--dangerously-skip-permissions']),
])
def test_interactive_agy_session_without_a_registry_row_fires_as_em(agy_leg, name, argv) -> None:
    agy_leg(name, argv)
    principal = _agy_fire({'ANTIGRAVITY_CONVERSATION_ID': _AGY_SID})
    assert principal.role == 'em' and principal.session_id == _AGY_SID
    assert authority.fire_record_caller_session_id(principal) == _AGY_SID


@pytest.mark.parametrize('name,argv,env', [
    ('agy', ['agy', '-p', 'task'], {'ANTIGRAVITY_CONVERSATION_ID': _AGY_SID}),                 # headless run
    ('agy', ['agy'], {}),                                                                       # session id not carried as agy's own
    ('agy', ['agy'], {'ANTIGRAVITY_CONVERSATION_ID': '00000000-0000-4000-8000-000000000001'}),  # another conversation
    ('agy', ['agy'], {'ANTIGRAVITY_CONVERSATION_ID': _AGY_SID, 'COORDINATOR_AGENT_WORKER': '1'}),
    ('agy', ['agy'], {'ANTIGRAVITY_CONVERSATION_ID': _AGY_SID, 'COORDINATOR_PARENT_SESSION_ID': 'p'}),
])
def test_agy_caller_without_verified_interactive_evidence_is_still_refused(agy_leg, name, argv, env) -> None:
    agy_leg(name, argv)
    with pytest.raises(ValueError, match='verified EM'):
        _agy_fire(env)


_GROK_SID = '01a21334-b3fa-70b0-b317-abd3a378a142'


@pytest.fixture
def grok_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An interactive grok session listed in active_sessions.json with no host-sessions row."""
    import json
    import psutil
    from types import SimpleNamespace
    home = tmp_path / 'grok-home'
    home.mkdir()
    monkeypatch.setenv('COORDINATOR_SETTINGS_HOME', str(tmp_path))
    monkeypatch.setenv('GROK_HOME', str(home))
    for var in ('COORDINATOR_AGENT_WORKER', 'COORDINATOR_AGENT_ROLE'):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(core, 'attributable_session_id', lambda *a: _GROK_SID)
    monkeypatch.setattr(core, 'stable_pid_alive', lambda *a, **k: True)
    monkeypatch.setattr(core, 'ensure_session', lambda *a, **k: None)
    (tmp_path / 'work').mkdir()
    (home / 'active_sessions.json').write_text(
        json.dumps([{'session_id': _GROK_SID, 'pid': 601, 'cwd': str(tmp_path / 'work')}]), encoding='utf-8')

    def host(name, argv, running=True):
        proc = SimpleNamespace(pid=601, name=lambda: name, cmdline=lambda: list(argv), create_time=lambda: 1.0,
                               is_running=lambda: running)
        monkeypatch.setattr(psutil, 'Process', lambda p: proc)
    return host


def _grok_fire(env):
    with core.warm_served_request(), bind_caller_env(env):
        return authority.require_fire_authority('unused.mjs', None, cwd=None, native=True, backend='codex')


@pytest.mark.parametrize('name,argv', [
    ('grok', ['/opt/grok/bin/grok', '--dangerously-skip-permissions', '--model', 'grok-4']),
    ('grok.exe', ['grok.exe', '--dangerously-skip-permissions']),
])
def test_interactive_grok_session_without_a_registry_row_fires_as_em(grok_session, name, argv) -> None:
    grok_session(name, argv)
    principal = _grok_fire({'GROK_SESSION_ID': _GROK_SID})
    assert principal.role == 'em' and principal.session_id == _GROK_SID
    assert authority.fire_record_caller_session_id(principal) == _GROK_SID


@pytest.mark.parametrize('name,argv,env,running', [
    ('grok', ['grok', '-p', 'task'], {'GROK_SESSION_ID': _GROK_SID}, True),                    # headless run
    ('grok', ['grok'], {}, True),                                                              # session id not carried as grok's own
    ('grok', ['grok'], {'GROK_SESSION_ID': '00000000-0000-4000-8000-000000000001'}, True),     # another session
    ('grok', ['grok'], {'GROK_SESSION_ID': _GROK_SID, 'COORDINATOR_AGENT_WORKER': '1'}, True),
    ('grok', ['grok'], {'GROK_SESSION_ID': _GROK_SID, 'COORDINATOR_PARENT_SESSION_ID': 'p'}, True),
    ('grok', ['grok'], {'GROK_SESSION_ID': _GROK_SID}, False),                                 # dead process
    ('bash', ['bash'], {'GROK_SESSION_ID': _GROK_SID}, True),                                  # pid is not a grok host
])
def test_grok_caller_without_verified_interactive_evidence_is_still_refused(grok_session, name, argv, env, running) -> None:
    grok_session(name, argv, running)
    with pytest.raises(ValueError, match='verified EM'):
        _grok_fire(env)


def test_token_forwarded_via_caller_env_reaches_fire_authority(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
