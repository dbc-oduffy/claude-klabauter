"""Executor-owned private warm engine; never discovers or stops a shared service.

This module must execute from the approved stamped private publication itself.
Construction validates evidence only. Provisioning and foreground service startup
are explicit, separately resource-authorized operations performed by start().
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid

MAX_WORKERS = 64
MAX_CLI = 512
MAX_ROSTER_BYTES = 128 * 1024
JOB_MEMORY_BYTES = 4 * 1024 ** 3
PRIVATE_ROSTER_ENV = 'COORDINATOR_WARM_PRIVATE_ROSTER'


def _module_engine_root():
    return Path(__file__).resolve().parents[3]


def _module_affinity(module, engine_root):
    path = _plain(module.__file__, directory=False)
    if not path.is_relative_to(engine_root / 'coordinator_core'):
        raise ValueError('private engine helper module was imported from another source root')


def _plain(path, *, directory=None):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('private engine paths must be absolute without parent traversal')
    for part in reversed((path, *path.parents)):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('private engine path contains a reparse point')
    if directory is True and not path.is_dir() or directory is False and not path.is_file():
        raise ValueError('private engine path has the wrong type')
    if os.path.normcase(str(path.resolve())) != os.path.normcase(str(path)):
        raise ValueError('private engine path is not canonical')
    return path


def _same(a, b):
    return os.path.normcase(str(Path(a))) == os.path.normcase(str(Path(b)))


def _digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('actual canonical session UUID required')
    return value


def _hex(value, length, label):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{' + str(length) + '}', value) is None:
        raise ValueError(label + ' is invalid')
    return value


def _process_facts(pid):
    import psutil
    if type(pid) is not int or not 0 < pid <= 0xffffffff:
        raise ValueError('actual DWORD process PID required')
    from coordinator_core.warm import workflow_peer
    _module_affinity(workflow_peer, _module_engine_root())
    live = getattr(workflow_peer, 'kernel_live_pid', None)
    if not callable(live):
        raise RuntimeError('private publication lacks kernel process liveness verification')
    if live(pid) is not True:
        raise ValueError('process is not kernel-live before identity observation')
    process = psutil.Process(pid)
    exe = _plain(process.exe(), directory=False)
    value = {'pid': pid, 'birth': process.create_time(), 'exe': str(exe), 'exe_sha256': _digest(exe)}
    if (live(pid) is not True or not process.is_running()
            or psutil.Process(pid).create_time() != value['birth']):
        raise ValueError('process ended during identity observation')
    return value


def _process_argv(pid):
    import psutil
    return psutil.Process(pid).cmdline()


def _creation_filetime(process):
    """Raw creation FILETIME from the owned Popen HANDLE, never a guessed PID."""
    if sys.platform != 'win32' or not hasattr(process, '_handle'):
        raise RuntimeError('private server identity requires an owned Windows process HANDLE')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    fn = kernel.GetProcessTimes
    fn.argtypes = (wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4))
    fn.restype = wintypes.BOOL
    created, ended, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
    if not fn(int(process._handle), ctypes.byref(created), ctypes.byref(ended),
              ctypes.byref(kernel_time), ctypes.byref(user_time)):
        raise ctypes.WinError(ctypes.get_last_error())
    return str((created.dwHighDateTime << 32) | created.dwLowDateTime)


def _endpoint_exists(endpoint):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    fn = kernel.WaitNamedPipeW
    fn.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
    fn.restype = wintypes.BOOL
    if fn(endpoint, 1):
        return True
    error = ctypes.get_last_error()
    if error in (2, 3):
        return False
    if error in (5, 121, 231):
        return True  # Existing/busy/inaccessible endpoint is never ours to reuse.
    raise ctypes.WinError(error)


def _pipe_server_pid(stream):
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    fn = kernel.GetNamedPipeServerProcessId
    fn.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG))
    fn.restype = wintypes.BOOL
    pid = wintypes.ULONG()
    if not fn(msvcrt.get_osfhandle(stream.fileno()), ctypes.byref(pid)):
        raise ctypes.WinError(ctypes.get_last_error())
    return pid.value


def _ping(endpoint, token, owner, repo_root, timeout, *, expected_process, expected_creation_ft):
    """Direct existing transport only: no client autostart, cold op or registry."""
    from coordinator_core.warm import client
    _module_affinity(client, _module_engine_root())
    stream = client._open_pipe(endpoint)
    waiting = None
    try:
        server_pid = _pipe_server_pid(stream)
        if server_pid != expected_process.pid:
            raise ValueError('private pipe kernel server PID differs from the owned child')
        if expected_process.poll() is not None or _creation_filetime(expected_process) != expected_creation_ft:
            raise ValueError('owned server creation witness changed before ping write')
        request = {'jsonrpc': '2.0', 'id': 'private-ready', 'method': 'ping', 'params': {},
                   '_engine_token': token, '_origin_worktree': str(repo_root),
                   '_caller': {'session_id': owner['parent_session_id'], 'pid': str(owner['pid']), 'cwd': str(repo_root)}}
        stream.write(json.dumps(request).encode('utf-8') + b'\n')
        stream.flush()
        waiting = client._PendingRead(stream)
        line = waiting.wait(timeout)
        if line is client._TIMED_OUT:
            raise TimeoutError('private engine ping did not answer within its readiness budget')
        reply = json.loads(line)
        if reply.get('id') != 'private-ready' or reply.get('error') or reply.get('result', {}).get('ok') is not True:
            raise ValueError('private engine ping was not acknowledged')
        return {'server_pid': server_pid, 'ping_ok': True}
    finally:
        # Buffered close can wait behind an abandoned read's lock. Never turn
        # a readiness deadline into an unbounded synchronous close.
        if waiting is not None and waiting._thread.is_alive():
            threading.Thread(target=stream.close, daemon=True).start()
        else:
            stream.close()


def _spawn(argv, **kwargs):
    from coordinator_core.diagnostics import contained_run
    _module_affinity(contained_run, _module_engine_root())
    return contained_run.spawn_in_capped_job(argv, active_process_limit=50, job_memory_limit_bytes=JOB_MEMORY_BYTES,
                                             popen_kwargs=kwargs)


class OwnedPrivateEngine:
    """One fire, one foreground server and one protected worker-binding roster.

    Integration supplies explicit private_engine_root/shared_engine_root/source_sha
    in the protected fire record. Existing fires without this opt-in are untouched.
    Callbacks are on_spawn(pid, request_id), on_native_thread(request_id, facts),
    and end_worker(request_id). Native facts use the backend's exact session_id,
    pid/start_epoch/argv/exe/exe_sha256/repo_root/parent_session_id/fire_id/
    request_id/role/evidence contract; there is no parent or field alias fallback.
    """

    def __init__(self, record_path, record, *, door_image, door_provenance):
        self._lock = threading.RLock()
        self._process = self._close_job = None
        self._outputs = []
        self._closed = self._attempted = self._ready = self._prepared = False
        self._sidecar_owned = False
        self._worker_argv = {}
        self.record = dict(record)
        self.fire_id = _hex(record.get('fire_id'), 32, 'fire handle')
        self.source_sha = _hex(record.get('source_sha'), 40, 'expected source SHA')
        self.script_sha256 = _hex(record.get('script_sha256'), 64, 'script digest')
        self.parent_session_id = _uuid(record.get('caller_session_id'))
        if (record.get('backend') != 'codex' or record.get('executor') != 'headless'
                or record.get('state') != 'running' or type(record.get('pid')) is not int
                or record['pid'] != os.getpid()):
            raise ValueError('private engine requires this active Codex headless process record')
        self.engine_root = _plain(_module_engine_root(), directory=True)
        if not _same(self.engine_root, _plain(record['private_engine_root'], directory=True)):
            raise ValueError('private publication differs from this module source')
        self.shared_engine_root = _plain(record['shared_engine_root'], directory=True)
        if _same(self.engine_root, self.shared_engine_root):
            raise ValueError('private source must differ from the explicit shared publication')
        stamp = _plain(self.engine_root / 'coordinator_core/_engine_stamp', directory=False).read_text().strip()
        if stamp not in (self.source_sha, 'sha:' + self.source_sha):
            raise ValueError('private publication stamp differs from approved source SHA')
        self.repo_root = _plain(record['repo_root'], directory=True)
        _plain(self.repo_root / '.git', directory=True)
        registry = self.repo_root / '.git/coordinator-sessions/workflow-fires'
        self.record_path = _plain(record_path, directory=False)
        if not _same(self.record_path, registry / (self.fire_id + '.json')):
            raise ValueError('fire record is outside its exact protected metadata path')
        self.artifact_dir = _plain(record['artifact_dir'], directory=True)
        if not _same(self.artifact_dir, registry / 'headless' / self.fire_id):
            raise ValueError('artifact directory is outside this fire protected metadata path')
        saved = json.loads(self.record_path.read_text(encoding='utf-8'))
        identity_keys = ('fire_id', 'source_sha', 'script_sha256', 'repo_root', 'artifact_dir',
                         'script_path', 'caller_session_id', 'private_engine_root', 'shared_engine_root',
                         'pid', 'pid_birth', 'backend', 'executor', 'state')
        if not isinstance(saved, dict) or any(saved.get(key) != record.get(key) for key in identity_keys):
            raise ValueError('supplied fire identity differs from its protected record')
        script = _plain(record['script_path'], directory=False)
        if not script.is_relative_to(self.repo_root) or _digest(script) != self.script_sha256:
            raise ValueError('fired script identity changed')
        from coordinator_core._settings_home import settings_home
        self.settings_home = _plain(settings_home(), directory=True)
        self.private_bin = self.artifact_dir / 'private-bin'
        self.bindings_path = self.artifact_dir / 'worker-bindings.json'
        self.sidecar_path = self.private_bin / 'door.private.json'
        self.door_image = _plain(door_image, directory=False)
        self.door_provenance = _plain(door_provenance, directory=False)
        self.image_sha256 = _digest(self.door_image)
        self._validate_provenance()
        catalog = json.loads(_plain(self.engine_root / 'coordinator_core/ops/warm_entrypoint_allowlist.json', directory=False).read_text())
        names = set(catalog['entrypoints']) & set(catalog['door_eligible_entrypoints'])
        if len(names) > MAX_CLI or any(not isinstance(name, str) for name in names):
            raise ValueError('committed native CLI catalog exceeds its bound')
        self.excluded_helpers = sorted(name for name in names if name.startswith('_'))
        self.allowed_cli = sorted(name for name in names if not name.startswith('_'))
        if not self.allowed_cli or any(re.fullmatch('[a-z0-9][a-z0-9_-]*', name) is None for name in self.allowed_cli):
            raise ValueError('committed native CLI catalog has invalid names')
        from coordinator_core.warm import election, skew
        _module_affinity(election, self.engine_root)
        _module_affinity(skew, self.engine_root)
        self.engine_token = skew.compute_client_token(repo_root=self.engine_root)
        if not isinstance(self.engine_token, str) or not self.engine_token or self.engine_token == 'unversioned':
            raise ValueError('private publication has no computed stamped generation token')
        self.endpoint = election.pipe_name(self.engine_token, engine_clone=self.engine_root)
        facts = _process_facts(os.getpid())
        birth = record.get('pid_birth')
        if (isinstance(birth, bool) or not isinstance(birth, (int, float)) or not math.isfinite(birth)
                or abs(birth-facts['birth']) > .000001):
            raise ValueError('active headless process birth differs from protected record')
        self.owner = {'pid': facts['pid'], 'birth': facts['birth'], 'parent_session_id': self.parent_session_id}
        self.roster = {'schema_version': 1, 'fire_id': self.fire_id, 'repo_root': str(self.repo_root),
            'engine_root': str(self.engine_root), 'engine_token': self.engine_token,
            'owner': {'pid': facts['pid'], 'birth': facts['birth']}, 'parent_session_id': self.parent_session_id,
            'source_sha': self.source_sha, 'script_sha256': self.script_sha256,
            'private_bin': str(self.private_bin), 'native_door_sha256': self.image_sha256,
            'allowed_cli': self.allowed_cli, 'workers': []}

    def resource_plan(self):
        """Actual copy cost for the owner's separate resource-GO request."""
        return {'alias_count': len(self.allowed_cli),
                'native_alias_bytes': len(self.allowed_cli) * self.door_image.stat().st_size,
                'job_memory_bytes': JOB_MEMORY_BYTES, 'active_process_limit': 50,
                'max_workers': MAX_WORKERS, 'max_roster_bytes': MAX_ROSTER_BYTES,
                'excluded_private_helpers': list(self.excluded_helpers)}

    def _validate_provenance(self):
        proof = json.loads(self.door_provenance.read_text(encoding='utf-8'))
        from coordinator_core.warm.door import build
        _module_affinity(build, self.engine_root)
        sources = {path.name: build.source_sha256(_plain(self.engine_root / 'coordinator_core/warm/door' / path.name,
                      directory=False)) for path in build.SOURCES}
        if proof.get('require_warm') is not True or proof.get('image_sha256') != self.image_sha256 or proof.get('sources') != sources:
            raise ValueError('native image lacks exact require-warm/source/image provenance')
        _hex(proof.get('rendered_source_sha256'), 64, 'native rendered-source digest')
        if not isinstance(proof.get('compile_flags'), list) or not proof['compile_flags'] or any(not isinstance(flag, str) for flag in proof['compile_flags']):
            raise ValueError('native image lacks compile flag provenance')
        if proof.get('door_c_sha256') != sources.get('door.c'):
            raise ValueError('native main-source digest contradicts the complete source inputs')
        if any(not isinstance(proof.get(name), str) or not proof[name] for name in ('compiler', 'compiler_version', 'engine_root', 'built_at')):
            raise ValueError('native image lacks required build provenance')
        if not Path(proof['engine_root']).is_absolute() or 'builder' not in proof or not isinstance(proof.get('command'), list) or not proof['command'] or any(not isinstance(value, str) for value in proof['command']):
            raise ValueError('native image lacks attributable build inputs')
        python_bin = proof.get('python_bin')
        python_path = build.expand_python_bin(python_bin) if isinstance(python_bin, str) else None
        if python_path is None or not python_path.is_absolute():
            raise ValueError('native image lacks its absolute Python render input')
        source_text = (self.engine_root / 'coordinator_core/warm/door/door.c').read_text(encoding='utf-8')
        rendered = build._render(source_text, python_path, Path(proof['engine_root']))
        rendered = '#define DOOR_PRIVATE_REQUIRED 1\n' + rendered
        if hashlib.sha256(rendered.encode('utf-8')).hexdigest() != proof['rendered_source_sha256']:
            raise ValueError('native require-warm rendered-source digest does not reproduce')

    def _atomic_json(self, path, value):
        _plain(path.parent, directory=True)
        if path.exists():
            _plain(path, directory=False)
        data = json.dumps(value, sort_keys=True).encode('utf-8') + b'\n'
        if path == self.bindings_path and len(data) > MAX_ROSTER_BYTES:
            raise ValueError('protected worker bindings exceed byte bound')
        temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('xb') as stream:
                stream.write(data)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _write_roster(self):
        self._atomic_json(self.bindings_path, self.roster)

    def _provision(self):
        _plain(self.artifact_dir, directory=True)
        if self.bindings_path.exists() or self.private_bin.exists():
            raise ValueError('private fire artifacts already exist; never reuse another instance')
        self.private_bin.mkdir()
        self._prepared = True
        self._validate_provenance()
        for name in self.allowed_cli:
            destination = self.private_bin / (name + '.exe')
            shutil.copy2(self.door_image, destination)
            if _digest(destination) != self.image_sha256:
                raise ValueError('private native alias image changed during copy')
        from coordinator_core.warm.door import build
        build.write_sidecar(self.private_bin / (self.allowed_cli[0] + '.exe'), self.engine_root)
        self._write_roster()

    def start(self, timeout_s=10):
        with self._lock:
            if self._closed or self._attempted:
                raise RuntimeError('private engine may start only once')
            self._attempted = True
            if sys.platform != 'win32':
                raise RuntimeError('private engine requires Windows kernel pipe authentication')
            if _endpoint_exists(self.endpoint):
                raise RuntimeError('private endpoint already exists; refusing reuse or shared-service mutation')
            if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) or not 0 < timeout_s <= 30:
                raise ValueError('private readiness timeout must be within 0..30 seconds')
            try:
                self._provision()
                env = dict(os.environ, COORDINATOR_ENGINE_ROOT=str(self.engine_root),
                    COORDINATOR_DOOR_ENGINE_ROOT=str(self.engine_root),
                    PYTHONPATH=str(self.engine_root), COORDINATOR_WARM='0',
                    COORDINATOR_WARM_LISTENER_AUTOSTART='0')
                env[PRIVATE_ROSTER_ENV] = str(self.bindings_path)
                for suffix in ('stdout', 'stderr'):
                    self._outputs.append((self.artifact_dir / ('private-server.' + suffix)).open('wb'))
                self._process, self._close_job = _spawn([sys.executable, '-m', 'coordinator_core.warm.server'],
                    cwd=self.engine_root, env=env, stdin=subprocess.DEVNULL,
                    stdout=self._outputs[0], stderr=self._outputs[1], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                process = self._process
                if type(process.pid) is not int or not 0 < process.pid <= 0xffffffff:
                    raise ValueError('owned server PID is invalid')
                creation = _creation_filetime(process)
                if re.fullmatch('[1-9][0-9]*', creation) is None:
                    raise ValueError('owned server creation FILETIME is invalid')
                deadline = time.monotonic() + timeout_s
                while True:
                    if process.poll() is not None:
                        raise RuntimeError('owned private server exited before readiness')
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('owned private server readiness deadline expired')
                    try:
                        proof = _ping(self.endpoint, self.engine_token, self.owner, self.repo_root, min(.25, remaining),
                                      expected_process=process, expected_creation_ft=creation)
                    except (FileNotFoundError, ConnectionRefusedError):
                        time.sleep(min(.05, max(0, deadline-time.monotonic())))
                        continue
                    if proof.get('server_pid') != process.pid or proof.get('ping_ok') is not True:
                        raise RuntimeError('private endpoint was won by a different process')
                    if process.poll() is not None or _creation_filetime(process) != creation:
                        raise RuntimeError('owned server identity changed during readiness')
                    break
                sidecar = {'schema_version': 1, 'fire_id': self.fire_id, 'engine_root': str(self.engine_root),
                    'engine_token': self.engine_token, 'settings_home': str(self.settings_home),
                    'repo_root': str(self.repo_root), 'bindings_path': str(self.bindings_path),
                    'server_pid': process.pid, 'server_creation_time_ft': creation,
                    'source_sha': self.source_sha, 'script_sha256': self.script_sha256}
                self._atomic_json(self.sidecar_path, sidecar)
                self._sidecar_owned = self._ready = True
                return dict(sidecar)
            except BaseException:
                self.close()
                raise

    def _worker(self, request_id):
        if isinstance(request_id, bool) or not isinstance(request_id, (str, int)) or request_id == '':
            raise ValueError('actual broker request identity required')
        matches = [row for row in self.roster['workers'] if type(row['request_id']) is type(request_id) and row['request_id'] == request_id]
        if len(matches) != 1:
            raise ValueError('request has no unique observed spawned worker')
        return matches[0]

    def on_spawn(self, pid, request_id):
        with self._lock:
            if not self._ready or self._closed:
                raise RuntimeError('private engine is not ready for worker binding')
            if isinstance(request_id, bool) or not isinstance(request_id, (str, int)) or request_id == '':
                raise ValueError('actual broker request identity required')
            if len(self.roster['workers']) >= MAX_WORKERS or any(row['request_id'] == request_id for row in self.roster['workers']):
                raise ValueError('worker request duplicate or worker bound exceeded')
            facts = _process_facts(pid)
            if pid == self.owner['pid'] or any(row['pid'] == pid and row['birth'] == facts['birth'] for row in self.roster['workers']):
                raise ValueError('worker process is ambiguous or is the owner')
            row = {**facts, 'request_id': request_id, 'state': 'pending', 'backend': 'codex', 'role': 'workflow-agent'}
            argv = _process_argv(pid)
            if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) for arg in argv):
                raise ValueError('spawned worker has no observed native argument vector')
            self._worker_argv[request_id] = list(argv)
            self.roster['workers'].append(row)
            self._write_roster()
            return dict(row)

    def on_native_thread(self, request_id, facts):
        with self._lock:
            row = self._worker(request_id)
            if self._closed or row['state'] == 'ended':
                return False
            if not isinstance(facts, dict):
                raise ValueError('observed native worker facts must be an object')
            native = _uuid(facts.get('session_id'))
            if native == self.parent_session_id or facts.get('pid') != row['pid']:
                raise ValueError('native worker identity conflicts with observed child')
            birth = facts.get('start_epoch')
            if (isinstance(birth, bool) or not isinstance(birth, (int, float)) or not math.isfinite(birth)
                    or abs(birth-row['birth']) > .000001):
                raise ValueError('native worker birth conflicts with pending process')
            if (facts.get('repo_root') != str(self.repo_root)
                    or facts.get('parent_session_id') != self.parent_session_id
                    or facts.get('fire_id') != self.fire_id or type(facts.get('request_id')) is not type(request_id)
                    or facts.get('request_id') != request_id
                    or facts.get('role') != 'workflow-agent'
                    or facts.get('evidence') != 'observed-native-exec-thread-event'):
                raise ValueError('native worker facts differ from the active fire/request authority')
            if (facts.get('argv') != self._worker_argv[request_id]
                    or facts.get('argv') != _process_argv(row['pid'])):
                raise ValueError('native worker argument vector differs from observed spawn')
            if any(other is not row and other.get('native_uuid') == native for other in self.roster['workers']):
                raise ValueError('native UUID is already bound to another worker request')
            current = _process_facts(row['pid'])
            if current != {name: row[name] for name in ('pid', 'birth', 'exe', 'exe_sha256')}:
                raise ValueError('worker process identity changed')
            if not _same(facts.get('exe', ''), current['exe']) or facts.get('exe_sha256') != current['exe_sha256']:
                raise ValueError('native executable evidence differs from the observed live process')
            if row['state'] == 'ready' and row['native_uuid'] != native:
                raise ValueError('ready worker native identity cannot be replaced')
            row.update(native_uuid=native, state='ready')
            self._write_roster()
            return True

    def end_worker(self, request_id):
        with self._lock:
            row = self._worker(request_id)
            row['state'] = 'ended'
            self._write_roster()

    def worker_env(self):
        if not self._ready or self._closed:
            raise RuntimeError('private worker environment requested before readiness')
        return {'COORDINATOR_ENGINE_ROOT': str(self.engine_root), 'COORDINATOR_WARM': '0',
                'COORDINATOR_DOOR_ENGINE_ROOT': str(self.engine_root),
                'COORDINATOR_WARM_LISTENER_AUTOSTART': '0',
                'PATH': str(self.private_bin) + os.pathsep + os.environ.get('PATH', '')}

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._ready = False
            try:
                if self._close_job is not None:
                    close, self._close_job = self._close_job, None
                    close()  # Only the owned Job Object; no pair_stop or global daemon action.
            finally:
                try:
                    if self._process is not None:
                        self._process.wait(timeout=2)
                finally:
                    for stream in self._outputs:
                        stream.close()
                    if self._sidecar_owned:
                        self.sidecar_path.unlink(missing_ok=True)
                    if self._prepared:
                        for row in self.roster['workers']:
                            row['state'] = 'ended'
                        self._write_roster()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.close()
