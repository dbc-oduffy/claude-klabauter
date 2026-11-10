"""Kernel-authenticated callers for an executor-owned private Workflow engine.

The protected roster is evidence written by the executor, never by a worker.
This gate narrows transport admission; existing operation/receipt guards still run.
"""
from __future__ import annotations

import json
import hashlib
from functools import lru_cache
import math
import os
from pathlib import Path
import re
import sys
import uuid

from coordinator_core.session_hub_path import hub_for_common

PRIVATE_ROSTER_ENV = 'COORDINATOR_WARM_PRIVATE_ROSTER'
MAX_ROSTER_BYTES = 128 * 1024
MAX_WORKERS = 64
MAX_ANCESTRY = 16
OWNER_METHODS = frozenset(('ping',))
DEFAULT_ENTRYPOINT = 'coordinator-invoke'  # existing native door default, auth only


class PrivatePeerRefused(ValueError):
    """The private endpoint cannot authenticate this request."""


def _refuse(reason):
    raise PrivatePeerRefused(reason)


def _pid(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _refuse('invalid process identity')
    return value


def _birth(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        _refuse('invalid process birth')
    return value


def _same_path(a, b):
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


def _within(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError, TypeError):
        return False


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _refuse('duplicate roster field')
        result[key] = value
    return result


def load_roster(path):
    """Read one bounded atomic roster snapshot from protected Git metadata."""
    path = Path(path)
    if not path.is_absolute():
        _refuse('roster path must be absolute')
    # Reparse paths could redirect a protected name to a worker-writable file.
    for part in (path, *path.parents):
        info = part.lstat()
        if part.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            _refuse('roster path contains a reparse point')
    with path.open('rb') as stream:
        data = stream.read(MAX_ROSTER_BYTES + 1)
    if len(data) > MAX_ROSTER_BYTES:
        _refuse('roster exceeds bound')
    row = json.loads(data, object_pairs_hook=_no_duplicate_keys)
    if not isinstance(row, dict) or row.get('schema_version') != 1:
        _refuse('unsupported roster schema')
    if not isinstance(row.get('fire_id'), str) or re.fullmatch('[0-9a-f]{32}', row['fire_id']) is None:
        _refuse('invalid roster fire handle')
    for name in ('repo_root', 'engine_root'):
        if not isinstance(row.get(name), str) or not Path(row[name]).is_absolute():
            _refuse('roster roots must be absolute')
    expected_home = hub_for_common(Path(row['repo_root']) / '.git') / 'workflow-fires' / 'headless' / row['fire_id']
    if not _same_path(path, expected_home / 'worker-bindings.json'):
        _refuse('roster is not the assigned fire binding record')
    if not isinstance(row.get('engine_token'), str) or not row['engine_token']:
        _refuse('missing engine generation')
    owner = row.get('owner')
    if not isinstance(owner, dict):
        _refuse('missing owner')
    _pid(owner.get('pid')); _birth(owner.get('birth'))
    parent = row.get('parent_session_id')
    if not isinstance(parent, str) or str(uuid.UUID(parent)) != parent:
        _refuse('owner requires canonical broker session')
    for name, pattern in (('source_sha', '[0-9a-f]{40}'), ('script_sha256', '[0-9a-f]{64}'), ('native_door_sha256', '[0-9a-f]{64}')):
        if not isinstance(row.get(name), str) or re.fullmatch(pattern, row[name]) is None:
            _refuse('missing pinned source/script digest')
    if not isinstance(row.get('private_bin'), str) or not Path(row['private_bin']).is_absolute() or not _same_path(row['private_bin'], expected_home / 'private-bin'):
        _refuse('private aliases must be in protected repository metadata')
    workers = row.get('workers')
    if not isinstance(workers, list) or len(workers) > MAX_WORKERS:
        _refuse('invalid worker roster')
    seen = set()
    ready_uuids = set()
    for worker in workers:
        if not isinstance(worker, dict):
            _refuse('invalid worker row')
        key = (_pid(worker.get('pid')), _birth(worker.get('birth')))
        if key in seen:
            _refuse('ambiguous worker process')
        seen.add(key)
        if worker.get('state') not in ('pending', 'ready', 'ended'):
            _refuse('invalid worker state')
        if worker.get('backend') != 'codex' or worker.get('role') != 'workflow-agent':
            _refuse('invalid worker authority')
        if worker.get('state') == 'ready':
            value = worker.get('native_uuid')
            if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                _refuse('ready worker lacks observed canonical UUID')
            if value == parent or value in ready_uuids:
                _refuse('ready native UUID is duplicated or names broker parent')
            ready_uuids.add(value)
            if not isinstance(worker.get('request_id'), (str, int)) or isinstance(worker['request_id'], bool) or worker['request_id'] == '':
                _refuse('ready worker lacks request identity')
            if not isinstance(worker.get('exe'), str) or not Path(worker['exe']).is_absolute():
                _refuse('ready worker lacks native executable evidence')
            if not isinstance(worker.get('exe_sha256'), str) or re.fullmatch('[0-9a-f]{64}', worker['exe_sha256']) is None:
                _refuse('ready worker lacks native executable digest')
    allowed = row.get('allowed_cli')
    if not isinstance(allowed, list) or not allowed or len(allowed) > 512 or any(
        not isinstance(name, str) or re.fullmatch('[a-z0-9][a-z0-9_-]*', name) is None for name in allowed
    ):
        _refuse('invalid CLI restriction')
    return row


def _validate_assignment(row):
    """Read the existing fire registry, assigned script and real engine stamp.

    This is transport admission evidence, not a replacement delegation token.
    The normal operation and receipt validators still run after admission.
    """
    # Same canonical registry path and fields as workflow_fire.fire/authority.
    record_path = hub_for_common(Path(row['repo_root']) / '.git') / 'workflow-fires' / (row['fire_id'] + '.json')
    with record_path.open('rb') as stream:
        data = stream.read(MAX_ROSTER_BYTES + 1)
    if len(data) > MAX_ROSTER_BYTES:
        _refuse('assigned fire record exceeds bound')
    record = json.loads(data, object_pairs_hook=_no_duplicate_keys)
    if not isinstance(record, dict) or record.get('fire_id') != row['fire_id']:
        _refuse('assigned fire record missing or mismatched')
    if record.get('state') not in ('pending', 'running') or record.get('executor') != 'headless' or record.get('backend') != 'codex':
        _refuse('assigned fire is not active headless Codex')
    if record.get('pid') != row['owner']['pid'] or abs(_birth(record.get('pid_birth')) - row['owner']['birth']) > 0.000001:
        _refuse('assigned fire owner process differs')
    if record.get('caller_session_id') != row['parent_session_id']:
        _refuse('assigned fire caller differs')
    if not isinstance(record.get('repo_root'), str) or not _same_path(record['repo_root'], row['repo_root']):
        _refuse('assigned fire repository differs')
    if record.get('sha256') != row['script_sha256'] or record.get('script_sha256') != row['script_sha256']:
        _refuse('assigned fire script digest differs')
    if record.get('source_sha') != row['source_sha']:
        _refuse('assigned fire source pin differs')
    if record.get('engine_root') and not _same_path(record['engine_root'], row['engine_root']):
        _refuse('assigned fire engine affinity differs')
    script_name = record.get('script_path')
    if not isinstance(script_name, str) or not Path(script_name).is_absolute() or not _within(script_name, row['repo_root']):
        _refuse('assigned script is outside assigned repository')
    digest = hashlib.sha256()
    with open(script_name, 'rb') as stream:
        total = 0
        while True:
            block = stream.read(1024 * 1024)
            if not block: break
            total += len(block)
            if total > 16 * 1024 * 1024:
                _refuse('assigned script exceeds verification bound')
            digest.update(block)
    if digest.hexdigest() != row['script_sha256']:
        _refuse('assigned script bytes changed')
    with (Path(row['engine_root']) / 'coordinator_core/_engine_stamp').open('rb') as stream:
        stamp = stream.read(256)
        if stream.read(1): _refuse('engine stamp exceeds bound')
    if stamp.decode('ascii').strip() != 'sha:' + row['source_sha']:
        _refuse('actual engine stamp differs from assigned source pin')
    return record


def kernel_client_pid(stream):
    """GetNamedPipeClientProcessId takes the accepted server pipe HANDLE."""
    if sys.platform != 'win32':
        _refuse('private peer authentication requires Windows named pipes')
    import ctypes
    from ctypes import wintypes
    import msvcrt
    handle = msvcrt.get_osfhandle(stream.fileno())
    fn = ctypes.WinDLL('kernel32', use_last_error=True).GetNamedPipeClientProcessId
    fn.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG))
    fn.restype = wintypes.BOOL
    pid = wintypes.ULONG()
    if not fn(handle, ctypes.byref(pid)):
        raise ctypes.WinError(ctypes.get_last_error())
    return _pid(pid.value)


def _process(pid):
    import psutil
    return psutil.Process(pid)


def _kernel_api():
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    api.OpenProcess.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    api.CloseHandle.restype = wintypes.BOOL
    return api


def kernel_live_pid(pid):
    """Query only: a process is live iff its kernel object is nonsignaled.

    psutil can retain a terminated Windows process while another handle keeps
    its object visible. A zero-time kernel wait distinguishes that from live.
    No process rights to terminate, modify or suspend are requested.
    """
    if sys.platform != 'win32' or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    api = handle = None
    live = False
    try:
        api = _kernel_api()
        handle = api.OpenProcess(0x00100000 | 0x1000, False, pid)
        if handle:
            live = api.WaitForSingleObject(handle, 0) == 0x102
    except Exception:
        live = False
    finally:
        if handle:
            try:
                if not api.CloseHandle(handle): live = False
            except Exception:
                live = False
    return live


def _matches(proc, record):
    try:
        if proc.pid != record['pid'] or not kernel_live_pid(proc.pid):
            return False
        fresh = _process(proc.pid)
        return (abs(proc.create_time() - record['birth']) <= 0.000001
                and abs(fresh.create_time() - record['birth']) <= 0.000001
                and fresh.is_running())
    except Exception:
        return False


@lru_cache(maxsize=16)
def _digest_executable(path, size, modified):
    if size > 512 * 1024 * 1024:
        _refuse('native executable exceeds verification bound')
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for _ in range((size // (1024 * 1024)) + 1):
            block = stream.read(1024 * 1024)
            if not block: break
            digest.update(block)
        if stream.read(1): _refuse('native executable changed size')
    after = os.stat(path)
    if after.st_size != size or after.st_mtime_ns != modified:
        _refuse('native executable changed during verification')
    return digest.hexdigest()


def _verified_image(path, expected):
    info = os.stat(path)
    if _digest_executable(str(path), info.st_size, info.st_mtime_ns) != expected:
        _refuse('observed executable digest changed')


def authorize(stream, message, *, engine_root=None):
    """Authenticate before dispatch; supplied transport-peer fields are ignored.

    An unset private roster leaves the existing shared server unchanged. Any
    missing/unreadable proof in private mode refuses; there is no cold fallback.
    """
    path = os.environ.get(PRIVATE_ROSTER_ENV)
    if not path:
        return None
    try:
        if sys.platform != 'win32':
            _refuse('private peer authentication requires Windows named pipes')
        row = load_roster(path)
        _validate_assignment(row)
        if engine_root is None or not _same_path(row['engine_root'], engine_root):
            _refuse('private engine root mismatch')
        if message.get('_engine_token') != row['engine_token']:
            _refuse('private engine generation mismatch')
        if not _matches(_process(row['owner']['pid']), row['owner']):
            _refuse('private executor owner is no longer live')
        peer = _process(kernel_client_pid(stream))
        if _matches(peer, row['owner']):
            if message.get('method') not in OWNER_METHODS:
                _refuse('owner transport is limited to private control')
            return {'kind': 'owner', 'pid': peer.pid, 'session_id': row['parent_session_id']}
        method, params = message.get('method'), message.get('params')
        if method != 'invoke.from_argv' or not isinstance(params, dict):
            _refuse('workers require named CLI invocation')
        worker = None
        proc = peer
        for _ in range(MAX_ANCESTRY):
            matches = [item for item in row['workers'] if _matches(proc, item)]
            if matches:
                worker = matches[0]
                break
            if _matches(proc, row['owner']):
                break  # owner descendants are not automatically workers
            proc = proc.parent()
            if proc is None:
                break
        if worker is None or worker.get('state') != 'ready':
            _refuse('peer has no live ready observed worker binding')
        if not _same_path(proc.exe(), worker['exe']):
            _refuse('observed worker executable changed')
        _verified_image(worker['exe'], worker['exe_sha256'])
        peer_birth = peer.create_time()
        caller = message.get('_caller')
        if not isinstance(caller, dict) or caller.get('session_id') != worker['native_uuid']:
            _refuse('caller session differs from observed native worker')
        if str(caller.get('pid')) != str(peer.pid):
            _refuse('caller PID differs from kernel peer')
        if not _within(caller.get('cwd'), row['repo_root']) or not _within(params.get('cwd'), row['repo_root']):
            _refuse('caller repository mismatch')
        if message.get('_origin_worktree') and not _same_path(message['_origin_worktree'], row['repo_root']):
            _refuse('request repository mismatch')
        env = message.get('_env', {})
        if not isinstance(env, dict):
            _refuse('invalid caller environment')
        for key in ('CODEX_THREAD_ID', 'COORDINATOR_SESSION_ID', 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_SESSION_ID', 'ANTIGRAVITY_CONVERSATION_ID', 'COORDINATOR_AGENT_SESSION'):
            if env.get(key) and env[key] != worker['native_uuid']:
                _refuse('forged environment session')
        if env.get('COORDINATOR_AGENT_ROLE') not in (None, '', 'workflow-agent'):
            _refuse('worker role promotion refused')
        if env.get('COORDINATOR_AGENT_PROVIDER') not in (None, '', 'codex'):
            _refuse('worker provider conflict')
        if env.get('COORDINATOR_AGENT_VENDOR') not in (None, '', 'codex'):
            _refuse('worker vendor conflict')
        if env.get('COORDINATOR_PARENT_SESSION_ID') not in (None, '', row['parent_session_id']):
            _refuse('worker broker lineage conflict')
        cli = params.get('entrypoint', DEFAULT_ENTRYPOINT)
        committed = Path(__file__).resolve().parents[1] / 'ops/warm_entrypoint_allowlist.json'
        allowlist = json.loads(committed.read_text(encoding='utf-8'))['entrypoints']
        if cli not in row['allowed_cli'] or cli not in allowlist:
            _refuse('CLI is not allowed by roster and committed source')
        expected_alias = Path(row['private_bin']) / (cli + '.exe')
        if not _same_path(peer.exe(), expected_alias):
            _refuse('kernel peer is not the assigned private CLI alias')
        _verified_image(expected_alias, row['native_door_sha256'])
        argv = params.get('argv')
        if not isinstance(argv, list) or any(not isinstance(arg, str) for arg in argv):
            _refuse('invalid CLI arguments')
        for index, arg in enumerate(argv):
            target = arg.split('=', 1)[1] if arg.startswith('--repo=') else argv[index + 1] if arg == '--repo' and index + 1 < len(argv) else None
            if target is not None and not _same_path(target, row['repo_root']):
                _refuse('CLI repository override refused')
        # Recheck births after inspecting the snapshot to close PID reuse races.
        if not _matches(_process(peer.pid), {'pid': peer.pid, 'birth': peer_birth}) or not _matches(_process(worker['pid']), worker):
            _refuse('peer or worker ended during authentication')
        # Canonicalize identity consumed by caller_context.merge_env_axis. Never
        # allow missing worker role to inherit the private server's ambient EM.
        canonical_env = dict(env)
        canonical_env.update(CODEX_THREAD_ID=worker['native_uuid'],
            COORDINATOR_SESSION_ID=worker['native_uuid'], COORDINATOR_AGENT_ROLE='workflow-agent',
            COORDINATOR_AGENT_VENDOR='codex', COORDINATOR_AGENT_PROVIDER='codex', COORDINATOR_PARENT_SESSION_ID=row['parent_session_id'])
        message['_env'] = canonical_env
        message['_caller'] = {**caller, 'session_id': worker['native_uuid'], 'pid': str(peer.pid), 'agent_id': None}
        # Construct only from the kernel-verified protected native worker,
        # never from the short-lived CLI alias peer or wire/environment fields.
        from coordinator_core.session.native_actor import VerifiedCodexExecActor
        actor = VerifiedCodexExecActor(
            session_id=worker['native_uuid'], repo_root=str(Path(row['repo_root']).resolve()),
            pid=worker['pid'], start_epoch=worker['birth'],
            exe=str(Path(worker['exe']).resolve()), exe_sha256=worker['exe_sha256'],
            parent_session_id=row['parent_session_id'], fire_id=row['fire_id'],
            request_id=worker['request_id'],
        )
        return {'verified_native_actor': actor, 'kind': 'worker', 'pid': peer.pid, 'birth': peer_birth,
            'session_id': worker['native_uuid'], 'role': 'workflow-agent', 'backend': 'codex',
            'request_id': worker['request_id'], 'fire_id': row['fire_id']}
    except PrivatePeerRefused:
        raise
    except Exception as exc:
        raise PrivatePeerRefused('private peer proof unavailable') from exc
