"""A headless fire's engineOp reaches the warm engine at its own stamp, and falls through cold, loudly, otherwise."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

from coordinator_core.ops.workflow_fire import headless
from coordinator_core.warm import breadcrumb, client, election, engine_root, skew

ASK = {'ask': 'unused'}


@pytest.fixture
def warm_env(monkeypatch):
    monkeypatch.setattr(client, 'is_warm_enabled', lambda: True)
    monkeypatch.setattr(skew, 'compute_client_token', lambda root=None: 'tok')

    def no_cold(*args, **kwargs):
        pytest.fail('cold_operation spawned')

    monkeypatch.setattr(headless.subprocess, 'run', no_cold)


def route(artifacts):
    return json.loads((artifacts / 'warm_route.json').read_text(encoding='utf-8'))


def test_warm_reply_is_used_inside_the_bar_without_a_cold_spawn(tmp_path, monkeypatch, warm_env):
    seen = []

    def served(msg, **kwargs):
        seen.append(msg)
        return {'jsonrpc': '2.0', 'id': 1, 'result': {'status': 'gated'}}

    monkeypatch.setattr(client, 'try_warm_dispatch', served)
    started = time.process_time()
    result = headless._invoke_source('dispatch.ask_gate', ASK, str(tmp_path), tmp_path / 'op', 15)
    assert time.process_time() - started < 0.5
    assert result == {'status': 'gated'}
    assert seen[0]['method'] == 'dispatch.ask_gate' and seen[0]['params'] == ASK
    assert seen[0]['_origin_worktree'] == str(tmp_path)
    assert route(tmp_path / 'op')['route'] == 'warm'
    assert json.loads((tmp_path / 'op' / 'response.json').read_text())['result'] == {'status': 'gated'}
    assert json.loads((tmp_path / 'op' / 'request.json').read_text())['op'] == 'dispatch.ask_gate'


def test_warm_error_envelope_raises_like_cold(tmp_path, monkeypatch, warm_env):
    monkeypatch.setattr(client, 'try_warm_dispatch',
                        lambda msg, **k: {'jsonrpc': '2.0', 'id': 1, 'error': {'code': -32004}})
    with pytest.raises(RuntimeError, match='-32004'):
        headless._invoke_source('dispatch.ask_gate', ASK, str(tmp_path), tmp_path / 'op', 15)


def test_warm_attempt_never_spawns_a_server(tmp_path, monkeypatch, warm_env):
    spawned = []
    monkeypatch.setattr(client, '_spawn_once', lambda: spawned.append(1))

    def missing(msg, **kwargs):
        client._spawn_once()
        return None

    monkeypatch.setattr(client, 'try_warm_dispatch', missing)
    reply = headless._warm_reply('dispatch.ask_gate', ASK, str(tmp_path), tmp_path)
    assert reply is None and spawned == []
    assert route(tmp_path)['reason'] == 'no_server'
    client._spawn_once()
    assert spawned == [1]


def cold_fallthrough(tmp_path, monkeypatch, reason, **patches):
    for name, value in patches.items():
        target = skew if name == 'token' else client
        monkeypatch.setattr(target, 'compute_client_token' if name == 'token' else name, value)
    ran = []

    def cold(command, **kwargs):
        ran.append(command)
        Path(command[-1]).write_text(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'via': 'cold'}}),
                                     encoding='utf-8')

    monkeypatch.setattr(headless.subprocess, 'run', cold)
    result = headless._invoke_source('dispatch.ask_gate', ASK, str(tmp_path), tmp_path / 'op', 15)
    assert result == {'via': 'cold'} and len(ran) == 1
    assert 'cold_operation' in ran[0][2]
    note = route(tmp_path / 'op')
    assert note['route'] == 'cold' and note['reason'].startswith(reason)


def test_no_server_falls_through_cold_with_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(client, 'is_warm_enabled', lambda: True)
    monkeypatch.setattr(skew, 'compute_client_token', lambda root=None: 'tok')

    def missing(msg, **kwargs):
        client._spawn_once()

    monkeypatch.setattr(client, 'try_warm_dispatch', missing)
    cold_fallthrough(tmp_path, monkeypatch, 'no_server')


def test_skew_or_silent_reply_falls_through_cold_with_reason(tmp_path, monkeypatch):
    """A None reply after a possible delivery still re-runs the op cold, once, with the reason on disk."""
    monkeypatch.setattr(client, 'is_warm_enabled', lambda: True)
    monkeypatch.setattr(skew, 'compute_client_token', lambda root=None: 'tok')
    monkeypatch.setattr(client, 'try_warm_dispatch', lambda msg, **k: None)
    cold_fallthrough(tmp_path, monkeypatch, 'no_warm_reply')


def test_unstamped_root_falls_through_cold_with_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(client, 'is_warm_enabled', lambda: True)

    def unstamped(root=None):
        raise skew.UnstampedEngineRootError('no stamp', root=tmp_path, root_exists=True)

    monkeypatch.setattr(client, 'try_warm_dispatch', lambda msg, **k: pytest.fail('asked an unstamped root'))
    cold_fallthrough(tmp_path, monkeypatch, 'unstamped_root', token=unstamped)


def test_warm_disabled_falls_through_cold_with_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(client, 'try_warm_dispatch', lambda msg, **k: pytest.fail('warm disabled'))
    cold_fallthrough(tmp_path, monkeypatch, 'warm_disabled', is_warm_enabled=lambda: False)


def test_client_endpoint_is_this_executors_engine_root():
    assert engine_root.current_engine_clone() == Path(headless.__file__).resolve().parents[3]


def test_warm_read_is_bounded_by_the_caller_timeout(tmp_path, monkeypatch, warm_env):
    seen = {}

    def served(msg, **kwargs):
        seen.update(kwargs)
        return {'jsonrpc': '2.0', 'id': 1, 'result': {}}

    monkeypatch.setattr(client, 'try_warm_dispatch', served)
    headless._invoke_source('dispatch.ask_gate', ASK, str(tmp_path), tmp_path / 'op', 7)
    assert seen == {'read_deadline_secs': 7}


# Mirrors _SERVER_CHILD in coordinator_core/benchmarks/tests/test_headless_fire_ask_gate_route.py: change both together.
_SERVER_CHILD = r'''
import os, sys
from pathlib import Path

repo, root = sys.argv[1], Path(sys.argv[2])
sys.path.insert(0, repo)
from coordinator_core.warm import breadcrumb, server, skew

token = skew.compute_client_token(root)


class _Version:
    server_sha = "headless-reach"
    last_skew_axes = ()

    def is_skewed(self, client_token):
        return False

    def is_token_skewed(self, client_token):
        return False


def _dispatch(self, msg, **_kw):
    return {"jsonrpc": "2.0", "id": msg["id"],
            "result": {"served_by": "warm", "method": msg["method"], "repo": msg.get("_origin_worktree")}}


server._ServerContext._pool_dispatch = _dispatch
on_windows = sys.platform == "win32"
elected = server._elect_windows_pipe(root, token) if on_windows else server._elect_unix_socket_endpoint(root, token)
ctx = server._ServerContext(name=elected.endpoint, sid=elected.identity, version_state=_Version(),
                            engine_root=root, listen_socket=elected.listen_socket, endpoint_path=elected.endpoint_path)
breadcrumb.write_breadcrumb(pipe=elected.endpoint, pid=os.getpid(),
                            stable_pid_start_epoch=server._self_stable_pid_start_epoch() or 0,
                            engine_sha=_Version.server_sha, engine_root=root,
                            transport=breadcrumb.TRANSPORT_PIPE if on_windows else breadcrumb.TRANSPORT_UNIX)
if elected.first_handle is not None:
    ctx.serve_forever(elected.first_handle)
else:
    ctx.serve_forever_unix(elected.listen_socket)
'''

_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def _stamped_root(base: Path, name: str) -> Path:
    root = base / name
    (root / 'coordinator_core' / 'warm').mkdir(parents=True)
    (root / '.git').mkdir()
    (root / 'coordinator_core' / '_engine_stamp').write_text('sha:headless-reach\n', encoding='utf-8')
    return root


def _point_everything_at(monkeypatch, root: Path) -> None:
    """This process's warm client and the headless executor both run from `root`."""
    monkeypatch.setattr(engine_root, 'current_engine_clone', lambda: root)
    monkeypatch.setattr(breadcrumb, '_default_engine_clone', lambda: root)
    monkeypatch.setattr(election, '_default_engine_clone', lambda: root)
    monkeypatch.setattr(client, '_engine_clone_root', lambda: root)
    monkeypatch.setattr(client, '_configured_root_is_foreign', lambda: False)
    monkeypatch.setattr(client, '_sidecar_keyed_clone', lambda: None)
    monkeypatch.setattr(headless, '_executor_root', lambda: root)


@pytest.fixture
def real_warm(tmp_path, monkeypatch, request):
    """A real warm server child at a stamped temp engine root, reached by the real client."""
    runtime = Path(tempfile.mkdtemp(prefix='hw-', dir=None if os.name == 'nt' else '/tmp'))
    request.addfinalizer(lambda: shutil.rmtree(runtime, ignore_errors=True))
    monkeypatch.setenv('COORDINATOR_WARM', '1')
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(runtime))
    root = _stamped_root(tmp_path, 'eng')
    _point_everything_at(monkeypatch, root)
    procs = []

    def boot():
        script = root / 'coordinator_core' / 'warm' / 'server.py'
        script.write_text(_SERVER_CHILD, encoding='utf-8')
        repo = str(Path(headless.__file__).resolve().parents[3])
        with open(root / 'server.log', 'wb') as log:
            proc = subprocess.Popen([sys.executable, str(script), repo, str(root)], stdout=log,
                                    stderr=subprocess.STDOUT, creationflags=_NO_WINDOW)
        procs.append(proc)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if breadcrumb.read_breadcrumb(root):
                return
            if proc.poll() is not None:
                pytest.fail(f'warm server child exited {proc.returncode}')
            time.sleep(0.1)
        pytest.fail('warm server child not ready within 30s')

    request.addfinalizer(lambda: [p.kill() for p in procs if p.poll() is None])
    return boot, root


def test_real_warm_server_answers_a_headless_ask_gate_inside_the_bar(tmp_path, monkeypatch, real_warm):
    boot, root = real_warm
    boot()
    monkeypatch.setattr(headless.subprocess, 'run', lambda *a, **k: pytest.fail('cold_operation spawned'))
    repo = tmp_path / 'target'
    repo.mkdir()
    started = time.process_time()
    result = headless._invoke_source('dispatch.ask_gate', ASK, str(repo), tmp_path / 'op', 15)
    process_secs = time.process_time() - started
    assert result == {'served_by': 'warm', 'method': 'dispatch.ask_gate', 'repo': str(repo)}
    note = route(tmp_path / 'op')
    assert note['route'] == 'warm' and note['reason'] is None and note['engine_root'] == str(root)
    assert note['token'] == skew.compute_client_token(root)
    assert process_secs < 0.5, f'client process time {process_secs * 1000:.0f}ms (server side is billed in test_headless_fire_ask_gate_route)'


def test_real_no_server_falls_through_cold_without_starting_one(tmp_path, monkeypatch, real_warm):
    _, root = real_warm
    spawned = []
    monkeypatch.setattr(client, '_spawn_once', lambda: spawned.append(1))
    cold_fallthrough(tmp_path, monkeypatch, 'no_server')
    assert spawned == [] and breadcrumb.read_breadcrumb(root) is None


def test_private_engine_root_never_reaches_the_shared_server(tmp_path, monkeypatch, real_warm):
    """The R1 private-engine shape: an executor at its own root, with the shared server's stamp, still goes cold.

    The endpoint is keyed on the clone path as well as the stamp token, so the private tree finds no server.
    """
    boot, shared = real_warm
    boot()
    private = _stamped_root(tmp_path, 'private')
    assert skew.compute_client_token(private) == skew.compute_client_token(shared)
    _point_everything_at(monkeypatch, private)
    spawned = []
    monkeypatch.setattr(client, '_spawn_once', lambda: spawned.append(1))
    cold_fallthrough(tmp_path, monkeypatch, 'no_server')
    assert spawned == []
