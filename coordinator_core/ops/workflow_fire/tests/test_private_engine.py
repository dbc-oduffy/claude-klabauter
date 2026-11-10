"""Fakes only: no warm server, native door, model or shared machine state."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from coordinator_core.session_hub_path import hub_for_common

# Always test the sibling candidate, including before source integration.
path = Path(__file__).parents[1] / 'private_engine.py'
spec = importlib.util.spec_from_file_location('private_engine_candidate', path)
pe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pe)

PARENT = '11111111-1111-4111-8111-111111111111'
CHILD = '22222222-2222-4222-8222-222222222222'
_REAL_PING = pe._ping
_REAL_PROCESS_FACTS = pe._process_facts


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    engine, shared, repo, settings = [tmp_path / name for name in ('publication', 'shared', 'repo', 'settings')]
    for path in (engine, shared, repo, settings):
        path.mkdir()
    (engine / 'coordinator_core').mkdir()
    (engine / 'coordinator_core/_engine_stamp').write_text('sha:' + 'a' * 40 + '\n')
    sources = engine / 'coordinator_core/warm/door'
    sources.mkdir(parents=True)
    from coordinator_core.warm.door import build
    hashes = {}
    for item in build.SOURCES:
        body = b'source\r\n' + item.name.encode()
        if item.name == 'door.c':
            body += b'\r\n__PYTHON_BIN_W__\r\n__BUILD_ENGINE_ROOT_W__\r\n'
        (sources / item.name).write_bytes(body)
        hashes[item.name] = hashlib.sha256((sources / item.name).read_bytes().replace(b'\r', b'')).hexdigest()
    catalog = engine / 'coordinator_core/ops/warm_entrypoint_allowlist.json'
    catalog.parent.mkdir()
    catalog.write_text(json.dumps({'entrypoints': ['coordinator-invoke', 'named-cli', '_private-helper', 'cold-only'],
                                   'door_eligible_entrypoints': ['coordinator-invoke', 'named-cli', '_private-helper']}))
    fire_id = 'b' * 32
    artifact = hub_for_common(repo / '.git') / 'workflow-fires/headless' / fire_id
    artifact.mkdir(parents=True)
    (repo / '.git').mkdir(exist_ok=True)
    script = repo / 'flow.mjs'
    script.write_text('export default async()=>{};')
    record_path = artifact.parents[1] / (fire_id + '.json')
    record = {'fire_id': fire_id, 'source_sha': 'a' * 40, 'script_sha256': pe._digest(script),
              'repo_root': str(repo), 'artifact_dir': str(artifact), 'script_path': str(script),
              'caller_session_id': PARENT, 'private_engine_root': str(engine), 'shared_engine_root': str(shared),
              'pid': os.getpid(), 'pid_birth': os.getpid()+.25, 'backend': 'codex', 'executor': 'headless', 'state': 'running'}
    record_path.write_text(json.dumps(record))
    image = tmp_path / 'door-require-warm.exe'
    image.write_bytes(b'fake image, never executable')
    proof_path = tmp_path / 'image.provenance.json'
    proof = {'require_warm': True, 'image_sha256': pe._digest(image), 'sources': hashes,
             'door_c_sha256': hashes['door.c'],
             'compiler': 'fake-compiler', 'compiler_version': 'not invoked', 'engine_root': str(engine),
             'built_at': '2026-11-08T02:00:00+00:00', 'builder': None,
             'command': ['python', '-m', 'coordinator_core.warm.door.build', '<engine_root>'],
             'python_bin': build.portable_python_bin(Path(sys.executable)), 'compile_flags': ['require-warm-test-flag']}
    rendered = '#define DOOR_PRIVATE_REQUIRED 1\n' + build._render(
        (sources / 'door.c').read_text(encoding='utf-8'), build.expand_python_bin(proof['python_bin']), Path(proof['engine_root']))
    proof['rendered_source_sha256'] = hashlib.sha256(rendered.encode('utf-8')).hexdigest()
    proof_path.write_text(json.dumps(proof))
    monkeypatch.setattr(pe, '_module_engine_root', lambda: engine)
    monkeypatch.setattr(pe, '_module_affinity', lambda module, root: None)
    from coordinator_core import _settings_home
    monkeypatch.setattr(_settings_home, 'settings_home', lambda: settings)
    from coordinator_core.warm import election, skew
    monkeypatch.setattr(skew, 'compute_client_token', lambda *, repo_root: 'actual-token-' + repo_root.name)
    monkeypatch.setattr(election, 'pipe_name', lambda token, *, engine_clone: 'private-endpoint-' + token)
    facts = lambda pid: {'pid': pid, 'birth': pid + .25, 'exe': str(image), 'exe_sha256': pe._digest(image)}
    monkeypatch.setattr(pe, '_process_facts', facts)
    monkeypatch.setattr(pe, '_process_argv', lambda pid: [str(image), 'exec', '--json'])
    monkeypatch.setattr(pe, '_endpoint_exists', lambda endpoint: False)
    process = SimpleNamespace(pid=34567, running=True, waits=[])
    process.poll = lambda: None if process.running else 0
    def wait(timeout):
        process.waits.append(timeout)
        return 0
    process.wait = wait
    closes, launches = [], []
    def spawn(argv, **kwargs):
        launches.append((argv, kwargs))
        def close():
            closes.append(process.pid)
            process.running = False
        return process, close
    monkeypatch.setattr(pe, '_spawn', spawn)
    monkeypatch.setattr(pe, '_creation_filetime', lambda proc: '133333333333333333')
    monkeypatch.setattr(pe, '_ping', lambda endpoint, token, owner, repo_root, timeout, **kwargs:
        {'server_pid': process.pid, 'ping_ok': True})
    # Windows CI is the actual target; POSIX fakes may still exercise orchestration.
    monkeypatch.setattr(pe.sys, 'platform', 'win32')
    def make():
        return pe.OwnedPrivateEngine(record_path, record, door_image=image, door_provenance=proof_path)
    def native_facts(pid=1234, request_id='request-a', session_id=CHILD):
        return {'session_id': session_id, 'pid': pid, 'start_epoch': pid+.25,
                'argv': pe._process_argv(pid), 'exe': str(image), 'exe_sha256': pe._digest(image),
                'repo_root': str(repo), 'parent_session_id': PARENT, 'fire_id': fire_id,
                'request_id': request_id, 'role': 'workflow-agent', 'evidence': 'observed-native-exec-thread-event'}
    return SimpleNamespace(**locals())


def test_validated_constructor_has_no_runtime_or_file_effects(prepared):
    p = prepared
    engine = p.make()
    assert not p.launches and not engine.private_bin.exists() and not engine.bindings_path.exists()
    assert engine.allowed_cli == ['coordinator-invoke', 'named-cli']
    assert engine.excluded_helpers == ['_private-helper']
    assert engine.resource_plan()['native_alias_bytes'] == 2 * p.image.stat().st_size


@pytest.mark.parametrize('change', ['unstamped', 'wrongstamp', 'same-shared', 'wrong-module', 'changed-script', 'bad-record'])
def test_source_and_protected_fire_identity_refuse(prepared, monkeypatch, change):
    p = prepared
    if change == 'unstamped': (p.engine / 'coordinator_core/_engine_stamp').unlink()
    if change == 'wrongstamp': (p.engine / 'coordinator_core/_engine_stamp').write_text('sha:' + 'f' * 40)
    if change == 'same-shared': p.record['shared_engine_root'] = str(p.engine)
    if change == 'wrong-module': monkeypatch.setattr(pe, '_module_engine_root', lambda: p.shared)
    if change == 'changed-script': p.script.write_text('changed')
    if change == 'bad-record': p.record_path.write_text('{}')
    with pytest.raises((ValueError, FileNotFoundError)):
        p.make()
    assert not p.launches


def test_stamped_private_publication_may_have_git_metadata(prepared):
    (prepared.engine / '.git').mkdir()
    assert prepared.make().engine_root == prepared.engine


@pytest.mark.parametrize('key,value', [('require_warm', False), ('require_warm', 1), ('image_sha256', '0' * 64),
    ('sources', {}), ('rendered_source_sha256', 'unknown'), ('compile_flags', None), ('command', [])])
def test_wrong_or_incomplete_native_provenance_refuses(prepared, key, value):
    p = prepared
    p.proof[key] = value
    p.proof_path.write_text(json.dumps(p.proof))
    with pytest.raises(ValueError):
        p.make()
    assert not p.launches


def test_reparse_git_metadata_refuses(prepared, monkeypatch):
    original = Path.lstat
    target = prepared.repo / '.git'
    def lstat(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == target:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
        return result
    monkeypatch.setattr(Path, 'lstat', lstat)
    with pytest.raises(ValueError, match='reparse'):
        prepared.make()


def test_nonordinary_git_and_wrong_artifact_path_refuse(prepared):
    p = prepared
    p.record['artifact_dir'] = str(p.repo)
    p.record_path.write_text(json.dumps(p.record))
    with pytest.raises(ValueError, match='artifact directory'):
        p.make()


def test_start_provisions_samehash_aliases_and_exact_sidecar_after_ping(prepared):
    p = prepared
    engine = p.make()
    sidecar = engine.start()
    assert sidecar['server_pid'] == p.process.pid
    assert sidecar['server_creation_time_ft'] == '133333333333333333'
    assert set(sidecar) == {'schema_version', 'fire_id', 'engine_root', 'engine_token', 'settings_home',
        'repo_root', 'bindings_path', 'server_pid', 'server_creation_time_ft', 'source_sha', 'script_sha256'}
    assert Path(sidecar['bindings_path']) == p.artifact / 'worker-bindings.json'
    for name in engine.allowed_cli:
        assert pe._digest(engine.private_bin / (name + '.exe')) == pe._digest(p.image)
    argv, kwargs = p.launches[0]
    assert argv == [sys.executable, '-m', 'coordinator_core.warm.server']
    assert kwargs['stdin'] == subprocess.DEVNULL
    assert kwargs['env'][pe.PRIVATE_ROSTER_ENV] == str(engine.bindings_path)
    assert kwargs['env']['COORDINATOR_WARM'] == '0'
    assert kwargs['env']['COORDINATOR_WARM_LISTENER_AUTOSTART'] == '0'
    assert kwargs['env']['COORDINATOR_DOOR_ENGINE_ROOT'] == str(engine.engine_root)
    assert engine.worker_env()['PATH'].split(os.pathsep)[0] == str(engine.private_bin)
    engine.close()
    engine.close()
    assert p.closes == [p.process.pid] and p.process.waits == [2]
    assert not engine.sidecar_path.exists()


def test_existing_endpoint_never_launches_reuses_or_stops_anything(prepared, monkeypatch):
    p = prepared
    monkeypatch.setattr(pe, '_endpoint_exists', lambda endpoint: True)
    engine = p.make()
    with pytest.raises(RuntimeError, match='already exists'):
        engine.start()
    engine.close()
    assert not p.launches and not p.closes
    assert not engine.private_bin.exists()


@pytest.mark.parametrize('failure', ['wrong-pid', 'wrong-birth', 'ping-refused'])
def test_readiness_mismatch_closes_only_owned_job_and_never_writes_ready_sidecar(prepared, monkeypatch, failure):
    p = prepared
    if failure == 'wrong-pid':
        monkeypatch.setattr(pe, '_ping', lambda *args, **kwargs: {'server_pid': 999, 'ping_ok': True})
    if failure == 'wrong-birth':
        births = iter(['133333333333333333', '144444444444444444'])
        monkeypatch.setattr(pe, '_creation_filetime', lambda proc: next(births))
    if failure == 'ping-refused':
        monkeypatch.setattr(pe, '_ping', lambda *args, **kwargs: {'server_pid': p.process.pid, 'ping_ok': False})
    engine = p.make()
    with pytest.raises(RuntimeError):
        engine.start()
    assert p.closes == [p.process.pid] and not engine.sidecar_path.exists()


def test_pending_ready_ended_and_late_callback_cannot_promote(prepared):
    p = prepared
    engine = p.make()
    engine.start()
    pending = engine.on_spawn(1234, 'request-a')
    assert pending['state'] == 'pending' and 'native_uuid' not in pending
    facts = p.native_facts()
    assert engine.on_native_thread('request-a', facts)
    saved = json.loads(engine.bindings_path.read_text())
    assert saved['parent_session_id'] == PARENT
    assert set(saved['owner']) == {'pid', 'birth'}
    assert saved['workers'][0]['state'] == 'ready'
    engine.end_worker('request-a')
    assert not engine.on_native_thread('request-a', facts)
    assert json.loads(engine.bindings_path.read_text())['workers'][0]['state'] == 'ended'
    engine.close()


@pytest.mark.parametrize('bad', ['parent-uuid', 'wrong-pid', 'wrong-exe', 'wrong-hash', 'pid-reused'])
def test_native_binding_requires_actual_child_process_evidence(prepared, monkeypatch, bad):
    engine = prepared.make()
    engine.start()
    engine.on_spawn(1234, 'a')
    facts = prepared.native_facts(request_id='a')
    if bad == 'parent-uuid': facts['session_id'] = PARENT
    if bad == 'wrong-pid': facts['pid'] = 4321
    if bad == 'wrong-exe': facts['exe'] = str(prepared.shared / 'not-observed.exe')
    if bad == 'wrong-hash': facts['exe_sha256'] = '0' * 64
    if bad == 'pid-reused':
        original = pe._process_facts
        monkeypatch.setattr(pe, '_process_facts', lambda pid: {**original(pid), 'birth': 999})
    with pytest.raises(ValueError):
        engine.on_native_thread('a', facts)
    assert json.loads(engine.bindings_path.read_text())['workers'][0]['state'] == 'pending'
    engine.close()


def test_concurrent_callbacks_are_atomic_and_catalog_worker_bounds_are_explicit(prepared):
    from concurrent.futures import ThreadPoolExecutor
    engine = prepared.make()
    engine.start()
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda n: engine.on_spawn(1000+n, str(n)), range(12)))
    rows = json.loads(engine.bindings_path.read_text())['workers']
    assert len(rows) == 12 and len({row['pid'] for row in rows}) == 12
    assert pe.MAX_WORKERS == 64 and pe.MAX_CLI == 512 and pe.MAX_ROSTER_BYTES == 128 * 1024
    engine.close()
    assert all(row['state'] == 'ended' for row in json.loads(engine.bindings_path.read_text())['workers'])


@pytest.mark.parametrize('field,value', [('backend','claude'), ('executor','other'), ('state','done'),
    ('pid',12345), ('pid_birth',99), ('pid_birth',True)])
def test_only_actual_active_headless_owner_record_can_initialize(prepared, field, value):
    p = prepared
    p.record[field] = value
    p.record_path.write_text(json.dumps(p.record))
    with pytest.raises(ValueError):
        p.make()
    assert not p.launches


@pytest.mark.parametrize('field,value', [('start_epoch',99), ('start_epoch',True), ('argv',[]),
    ('repo_root','C:/wrong-repo'), ('parent_session_id',CHILD), ('fire_id','f'*32), ('request_id','other'),
    ('role','root'), ('evidence','model-final-claim')])
def test_every_native_callback_authority_field_is_verified(prepared, field, value):
    engine = prepared.make()
    engine.start()
    engine.on_spawn(1234, 'request-a')
    facts = prepared.native_facts()
    facts[field] = value
    with pytest.raises(ValueError):
        engine.on_native_thread('request-a', facts)
    assert json.loads(engine.bindings_path.read_text())['workers'][0]['state'] == 'pending'
    engine.close()


def test_native_uuid_cannot_be_reused_by_a_second_request(prepared):
    engine = prepared.make()
    engine.start()
    engine.on_spawn(1234, 'request-a')
    engine.on_spawn(1235, 'request-b')
    engine.on_native_thread('request-a', prepared.native_facts())
    with pytest.raises(ValueError, match='already bound'):
        engine.on_native_thread('request-b', prepared.native_facts(1235, 'request-b'))
    engine.close()


def test_full_reviewed_catalog_is_not_silently_truncated(prepared):
    names = ['native-cli-' + str(i) for i in range(389)]
    prepared.catalog.write_text(json.dumps({'entrypoints': names, 'door_eligible_entrypoints': names}))
    engine = prepared.make()
    assert len(engine.allowed_cli) == 389
    assert engine.resource_plan()['alias_count'] == 389
    assert not engine.private_bin.exists()


def test_worker_count_refuses_at_64_without_mutating_existing_bindings(prepared):
    engine = prepared.make()
    engine.start()
    for index in range(64):
        engine.on_spawn(1000+index, str(index))
    before = engine.bindings_path.read_bytes()
    with pytest.raises(ValueError, match='bound exceeded'):
        engine.on_spawn(9999, 'too-many')
    assert engine.bindings_path.read_bytes() == before
    engine.close()


def test_mixed_source_helper_module_refuses(tmp_path):
    root = tmp_path / 'private'
    root.mkdir()
    unrelated = tmp_path / 'shared.py'
    unrelated.write_text('')
    with pytest.raises(ValueError, match='another source root'):
        pe._module_affinity(SimpleNamespace(__file__=str(unrelated)), root)


def test_cleanup_timeout_is_bounded_and_still_revokes_sidecar_and_bindings(prepared):
    engine = prepared.make()
    engine.start()
    engine.on_spawn(1234, 'request-a')
    def stuck(timeout):
        assert timeout == 2
        raise subprocess.TimeoutExpired('fake-owned-server', timeout)
    prepared.process.wait = stuck
    with pytest.raises(subprocess.TimeoutExpired):
        engine.close()
    assert prepared.closes == [prepared.process.pid]
    assert not engine.sidecar_path.exists()
    assert json.loads(engine.bindings_path.read_text())['workers'][0]['state'] == 'ended'


def test_worker_and_server_use_existing_listener_gate_and_never_enable_ambient_warmth(prepared, monkeypatch):
    from coordinator_core.warm import supervisor
    engine = prepared.make()
    engine.start()
    for env in (engine.worker_env(), prepared.launches[0][1]['env']):
        assert env['COORDINATOR_WARM'] == '0'
        assert env['COORDINATOR_WARM_LISTENER_AUTOSTART'] == '0'
        assert env['COORDINATOR_DOOR_ENGINE_ROOT'] == str(engine.engine_root)
        monkeypatch.setenv('COORDINATOR_WARM_LISTENER_AUTOSTART', env['COORDINATOR_WARM_LISTENER_AUTOSTART'])
        assert supervisor._listener_autostart_disabled()
        assert 'COORDINATOR_HTTP_AUTOSTART' not in engine.worker_env()
        assert 'COORDINATOR_WARM_HTTP_AUTOSTART' not in engine.worker_env()
    engine.close()


@pytest.mark.parametrize('mismatch', ['server-pid', 'creation-witness'])
def test_direct_ping_checks_owned_kernel_identity_before_first_write(prepared, monkeypatch, mismatch):
    from coordinator_core.warm import client
    stream = SimpleNamespace(writes=[], closed=False)
    def write(value): stream.writes.append(value)
    stream.write = write
    stream.flush = lambda: None
    stream.close = lambda: setattr(stream, 'closed', True)
    monkeypatch.setattr(client, '_open_pipe', lambda endpoint: stream)
    monkeypatch.setattr(pe, '_pipe_server_pid', lambda handle:
        99 if mismatch == 'server-pid' else prepared.process.pid)
    if mismatch == 'creation-witness':
        monkeypatch.setattr(pe, '_creation_filetime', lambda process: 'different')
    with pytest.raises(ValueError):
        _REAL_PING('exact-private-endpoint', 'exact-token', {'parent_session_id': PARENT, 'pid': os.getpid()},
                   prepared.repo, .1, expected_process=prepared.process,
                   expected_creation_ft='133333333333333333')
    assert stream.writes == [] and stream.closed


def test_unversioned_token_is_never_private_readiness_evidence(prepared, monkeypatch):
    from coordinator_core.warm import skew
    monkeypatch.setattr(skew, 'compute_client_token', lambda **kwargs: 'unversioned')
    with pytest.raises(ValueError, match='generation token'):
        prepared.make()


@pytest.mark.parametrize('change', ['wrong-digest', 'missing-python', 'relative-python', 'different-python',
                                   'different-build-root', 'double-prefix'])
def test_required_render_reproduction_refuses_incomplete_or_different_inputs(prepared, change):
    p = prepared
    if change == 'wrong-digest': p.proof['rendered_source_sha256'] = 'f' * 64
    if change == 'missing-python': del p.proof['python_bin']
    if change == 'relative-python': p.proof['python_bin'] = 'python.exe'
    if change == 'different-python': p.proof['python_bin'] = str(p.shared / 'different-python.exe')
    if change == 'different-build-root': p.proof['engine_root'] = str(p.shared)
    if change == 'double-prefix':
        from coordinator_core.warm.door import build
        rendered = '#define DOOR_PRIVATE_REQUIRED 1\n' * 2 + build._render(
            (p.sources / 'door.c').read_text(encoding='utf-8'),
            build.expand_python_bin(p.proof['python_bin']), Path(p.proof['engine_root']))
        p.proof['rendered_source_sha256'] = hashlib.sha256(rendered.encode('utf-8')).hexdigest()
    p.proof_path.write_text(json.dumps(p.proof))
    with pytest.raises(ValueError):
        p.make()
    assert not p.launches


@pytest.mark.parametrize('state', ['live', 'dead-before', 'dead-after', 'pid-reused'])
def test_process_snapshot_reuses_shared_kernel_liveness_and_rechecks_birth(prepared, monkeypatch, state):
    import psutil
    from coordinator_core.warm import workflow_peer
    calls, observations = [], []
    live_results = iter([False] if state == 'dead-before' else [True, False] if state == 'dead-after' else [True, True])
    def kernel_live(pid):
        calls.append(pid)
        return next(live_results)
    monkeypatch.setattr(workflow_peer, 'kernel_live_pid', kernel_live, raising=False)
    def process(pid):
        observations.append(pid)
        birth = 200.5 if state == 'pid-reused' and len(observations) > 1 else 100.5
        return SimpleNamespace(pid=pid, exe=lambda: str(prepared.image), create_time=lambda: birth,
                               is_running=lambda: True)  # Deliberately stale psutil liveness.
    monkeypatch.setattr(psutil, 'Process', process)
    if state == 'live':
        facts = _REAL_PROCESS_FACTS(1234)
        assert facts['pid'] == 1234 and facts['birth'] == 100.5
    else:
        with pytest.raises(ValueError, match='kernel-live|ended'):
            _REAL_PROCESS_FACTS(1234)
    assert calls == ([1234] if state == 'dead-before' else [1234, 1234])
    assert observations == ([] if state == 'dead-before' else [1234] if state == 'dead-after' else [1234, 1234])


def test_missing_kernel_liveness_capability_fails_closed_before_psutil_snapshot(prepared, monkeypatch):
    import psutil
    from coordinator_core.warm import workflow_peer
    monkeypatch.delattr(workflow_peer, 'kernel_live_pid', raising=False)
    monkeypatch.setattr(psutil, 'Process', lambda pid: pytest.fail('no process observation without kernel capability'))
    with pytest.raises(RuntimeError, match='lacks kernel'):
        _REAL_PROCESS_FACTS(1234)
