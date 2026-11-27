"""codex_runner.workflow -- generic prompt/schema execution for Workflow stages.

The Codex launcher and subscription guard are shared with codex_runner. Explicit
Workflow tiers use the shared provider catalogue; no model option means the native
configured default. Caller identity is lineage, never a child's own session ID.
"""
from __future__ import annotations

from contextlib import contextmanager
import sys
import tempfile
import json
import hashlib
import stat
import math
import os
import re
import shutil
import subprocess
import time
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import jsonschema

from coordinator_core.codex_runner.capture_provider import spawn_collector
from coordinator_core.codex_runner.agy import AgyUnavailable, resolve_agy
from coordinator_core.codex_runner.grok import GrokUnavailable, resolve_grok
from coordinator_core.codex_runner.codex_spawn_env import shape_codex_child, writeback_codex_auth
from coordinator_core.codex_runner.resolve import codex_family_stage_argv, preflight_auth, resolve_codex
from coordinator_core.codex_runner.sandbox_preflight import (
    SandboxSetupRefused,
)
from coordinator_core.codex_runner.review import (
    CREDENTIAL_CONTEXT_ENV,
    CREDENTIAL_CONTEXT_ENV_WINDOWS,
    USER_PROFILE_ENV,
    _unattended_env,
)
from coordinator_core import tenant_priority
from coordinator_core.bash_guards import _heavy_command_class as capacity
from coordinator_core.group_em import admission_post, slot_grant, slot_wait
from coordinator_core.agent_pipe.agy import family_stage_argv
from coordinator_core.agent_pipe.model_tiers import GROK_EFFORTS, model_for_tier
from coordinator_core.ops.workflow_fire.receipt import FIRE_TOKEN_ENV
from coordinator_core.ops.workflow_fire.headless_agents import fill_omitted_nullable, unwrap_rpc_envelope
from coordinator_core.ops.workflow_fire.worker_preamble import with_worker_preamble
from coordinator_core._settings_home import settings_home
from coordinator_core.locked_write import held_lock

DEFAULT_AGENT_PEAK_GIB = 1.0
_OWN_IDENTITY_ENV = (
    "CODEX_THREAD_ID", "ANTIGRAVITY_CONVERSATION_ID", "COORDINATOR_AGENT_SESSION",
    "COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID",
    "COORDINATOR_AGENT_MODEL", "COORDINATOR_AGENT_VENDOR", "COORDINATOR_AGENT_PROVIDER",
)


class AgentFailed(RuntimeError):
    """The worker failed, timed out, or returned data outside its schema."""


class StructuredReplyInvalid(AgentFailed):
    """A schema-bound reply stayed unparseable after local repair and one re-ask; the raw reply is on disk."""

    cause = "structured_reply_invalid"


class StageStalled(AgentFailed):
    """A Codex child's stdout and stderr stayed silent past the stall window."""


STALL_WINDOW_S = 900.0
STALL_RETRIES = 1
_STALL_POLL_S = 1.0


def _stream_bytes(*handles) -> int:
    return sum(os.fstat(h.fileno()).st_size for h in handles)


def _kill_descendants(pid: int) -> None:
    """Kill every descendant of `pid`; the child itself is killed by the caller's cleanup."""
    import psutil

    for child in psutil.Process(pid).children(recursive=True):
        try:
            child.kill()
        except psutil.Error:
            pass


def _codex_schema(schema: Any) -> Any:
    """Return the unchanged schema only when it already fits native strict output.

    Open maps and optional members are Workflow semantics, not adapter defaults.
    Never close a map or require an optional member just to satisfy the CLI.
    Unsupported schemas use prompt-constrained JSON and original local validation.
    """
    allowed = {"type", "properties", "required", "additionalProperties", "items",
               "enum", "const", "description", "title", "anyOf"}

    def supported(node: Any) -> bool:
        if not isinstance(node, dict) or not set(node).issubset(allowed):
            return False
        types = node.get("type")
        types = set(types) if isinstance(types, list) else {types} if isinstance(types, str) else set()
        if "object" in types or "properties" in node:
            properties = node.get("properties")
            if not isinstance(properties, dict) or node.get("additionalProperties") is not False:
                return False
            if set(node.get("required", [])) != set(properties):
                return False
            if not all(supported(value) for value in properties.values()):
                return False
        if "array" in types and ("items" not in node or not supported(node["items"])):
            return False
        if "anyOf" in node:
            alternatives = node["anyOf"]
            if not isinstance(alternatives, list) or not alternatives or not all(map(supported, alternatives)):
                return False
        elif not types:
            return False
        return True

    return schema if isinstance(schema, dict) and schema.get("type") == "object" and supported(schema) else None


def _environment() -> dict[str, str]:
    # Do not inherit API-key opt-ins, parent session identity or Claude nesting.
    source = dict(os.environ)
    source.pop("CODEX_RUNNER_ALLOW_API_KEY", None)
    source.pop("OPENAI_API_KEY", None)
    source.pop("CODEX_API_KEY", None)
    env = _unattended_env(source, requested=False)
    # Only the headless-em-launch session holds the EM token; a worker child never does.
    env.pop("COORDINATOR_ALLOW_HEADLESS_EM_TOKEN", None)
    # The shared provider helper preserves caller identity for ordinary helper
    # subprocesses. A fresh native agent is a new session, so scrub AFTER that
    # helper (it can reinsert the bound caller even when its input was scrubbed).
    for key in _OWN_IDENTITY_ENV:
        env.pop(key, None)
    if source.get("COORDINATOR_PARENT_SESSION_ID"):
        env["COORDINATOR_PARENT_SESSION_ID"] = source["COORDINATOR_PARENT_SESSION_ID"]
    if source.get(FIRE_TOKEN_ENV):
        env[FIRE_TOKEN_ENV] = source[FIRE_TOKEN_ENV]
    # Reach a live engine, never start one: warmth stays on (an inherited WARM=0
    # is dropped) while both autostart gates are pinned off.
    env.pop('COORDINATOR_WARM', None)
    env['COORDINATOR_WARM_LISTENER_AUTOSTART'] = '0'
    env['COORDINATOR_WARM_SERVER_AUTOSTART'] = '0'
    # Native operation doors validate this stamped build using their normal
    # bootstrap ladder; never silently dispatch back into another live clone.
    from coordinator_core.warm.engine_root import current_engine_clone
    env['COORDINATOR_ENGINE_ROOT'] = str(current_engine_clone())
    # The native door uses its own override ahead of the installed sidecar.
    env['COORDINATOR_DOOR_ENGINE_ROOT'] = env['COORDINATOR_ENGINE_ROOT']
    # Native tools/hooks need these Windows and coordinator runtime locations.
    for key in ("TEMP", "TMP", "COMSPEC", "PATHEXT", "SYSTEMROOT", "MACHINE_LOCAL_REGISTRY_DIR",
                *USER_PROFILE_ENV, *CREDENTIAL_CONTEXT_ENV, *CREDENTIAL_CONTEXT_ENV_WINDOWS):
        if source.get(key):
            env[key] = source[key]
    return env


def _cleanup(function, *args, **kwargs) -> None:
    try:
        function(*args, **kwargs)
    except Exception:
        # Run every cleanup leg, preserving the worker's primary failure.
        pass


def _protected_observer_path(repo_root: Path, artifact_dir: Path) -> None:
    """Only broker-private metadata can carry identity observation evidence."""
    root, artifacts = Path(repo_root).absolute(), Path(artifact_dir).absolute()
    if '..' in root.parts or '..' in artifacts.parts:
        raise AgentFailed('native identity observation rejects parent traversal')
    git = root / '.git'
    if not artifacts.is_relative_to(git) or artifacts == git:
        raise AgentFailed('native identity observation requires protected repo .git artifacts')
    for path in [*reversed(artifacts.parents), artifacts]:
        try:
            info = path.lstat()
        except FileNotFoundError:
            # run_agent creates missing artifact descendants after this check.
            if path.is_relative_to(git) and path != git:
                continue
            raise AgentFailed(f'native identity observation missing ancestor: {path}')
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise AgentFailed(f'native identity observation unsafe ancestor: {path}')
    if artifacts.resolve() != artifacts:
        raise AgentFailed('native identity observation artifacts redirect')


def _private_plain_path(value, *, directory: bool) -> Path:
    if not isinstance(value, (str, Path)):
        raise AgentFailed('private CLI requires explicit canonical paths')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise AgentFailed('private CLI path must be absolute without traversal')
    try:
        for item in [*reversed(path.parents), path]:
            info = item.lstat()
            expected_directory = item != path or directory
            if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                    or (not stat.S_ISDIR(info.st_mode) if expected_directory else not stat.S_ISREG(info.st_mode))):
                raise AgentFailed(f'private CLI unsafe path: {item}')
        if path.resolve() != path:
            raise AgentFailed('private CLI path is not canonical')
    except OSError as exc:
        raise AgentFailed(f'private CLI path unavailable: {path}: {exc}') from exc
    return path


def _private_json(path: Path, limit: int) -> dict:
    _private_plain_path(path, directory=False)
    try:
        with path.open('rb') as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise AgentFailed('private CLI metadata exceeds bounded size')
        value = json.loads(data)
    except (OSError, ValueError) as exc:
        raise AgentFailed(f'private CLI metadata unavailable: {exc}') from exc
    if not isinstance(value, dict):
        raise AgentFailed('private CLI metadata must be an object')
    return value


def _private_cli_settings(private_cli, *, repo_root, artifact_dir, parent_session_id, fire_id, request_id):
    """Validate an owner-supplied binding; model options never select this path."""
    import psutil
    keys = {'env', 'private_bin', 'allowed_cli', 'fire_id', 'request_id', 'bindings_path'}
    if not isinstance(private_cli, dict) or set(private_cli) != keys:
        raise AgentFailed('private CLI owner contract keys differ')
    env = private_cli['env']
    allowed_env = {'COORDINATOR_ENGINE_ROOT', 'COORDINATOR_DOOR_ENGINE_ROOT', 'COORDINATOR_WARM',
                   'COORDINATOR_WARM_LISTENER_AUTOSTART', 'PATH'}
    required_env = allowed_env - {'COORDINATOR_DOOR_ENGINE_ROOT'}
    if (not isinstance(env, dict) or not required_env <= set(env) <= allowed_env
            or any(not isinstance(value, str) or not value or '\x00' in value for value in env.values())
            or env['COORDINATOR_WARM'] != '0' or env['COORDINATOR_WARM_LISTENER_AUTOSTART'] != '0'):
        raise AgentFailed('private CLI environment violates the owner-only whitelist')
    if (not isinstance(fire_id, str) or re.fullmatch('[0-9a-f]{32}', fire_id) is None
            or private_cli['fire_id'] != fire_id
            or isinstance(request_id, bool) or not isinstance(request_id, (str, int)) or request_id == ''
            or type(private_cli['request_id']) is not type(request_id) or private_cli['request_id'] != request_id
            or not isinstance(parent_session_id, str) or not parent_session_id):
        raise AgentFailed('private CLI fire/request/parent does not match trusted caller context')
    private_bin = _private_plain_path(private_cli['private_bin'], directory=True)
    bindings_path = _private_plain_path(private_cli['bindings_path'], directory=False)
    _protected_observer_path(repo_root, private_bin)
    _protected_observer_path(repo_root, artifact_dir)
    if (private_bin.name != 'private-bin' or bindings_path != private_bin.parent / 'worker-bindings.json'
            or not Path(artifact_dir).absolute().is_relative_to(private_bin.parent)):
        raise AgentFailed('private CLI paths do not share this protected fire namespace')
    if env['PATH'].split(os.pathsep)[0] != str(private_bin):
        raise AgentFailed('private CLI PATH must start with its exact private-bin')
    engine = _private_plain_path(env['COORDINATOR_ENGINE_ROOT'], directory=True)
    if env.get('COORDINATOR_DOOR_ENGINE_ROOT', str(engine)) != str(engine):
        raise AgentFailed('private CLI door and engine roots differ')
    sidecar_path = private_bin / 'door.private.json'
    sidecar = _private_json(sidecar_path, 65536)
    sidecar_keys = {'schema_version', 'fire_id', 'engine_root', 'engine_token', 'settings_home', 'repo_root',
                    'bindings_path', 'server_pid', 'server_creation_time_ft', 'source_sha', 'script_sha256'}
    roster = _private_json(bindings_path, 128*1024)
    if set(sidecar) != sidecar_keys or type(sidecar['schema_version']) is not int or sidecar['schema_version'] != 1:
        raise AgentFailed('private CLI ready sidecar schema differs')
    if (type(sidecar['server_pid']) is not int or sidecar['server_pid'] <= 0
            or not isinstance(sidecar['server_creation_time_ft'], str)
            or re.fullmatch('[1-9][0-9]*', sidecar['server_creation_time_ft']) is None):
        raise AgentFailed('private CLI sidecar lacks owned server identity facts')
    _private_plain_path(sidecar['settings_home'], directory=True)
    expected = {'fire_id': fire_id, 'repo_root': str(repo_root), 'engine_root': str(engine)}
    if (any(sidecar.get(k) != v or roster.get(k) != v for k, v in expected.items())
            or sidecar['bindings_path'] != str(bindings_path) or roster.get('private_bin') != str(private_bin)
            or roster.get('parent_session_id') != parent_session_id
            or type(roster.get('schema_version')) is not int or roster['schema_version'] != 1):
        raise AgentFailed('private CLI sidecar/roster differs from trusted fire roots')
    for key, pattern in [('source_sha', '[0-9a-f]{40}'), ('script_sha256', '[0-9a-f]{64}')]:
        value = sidecar[key]
        if not isinstance(value, str) or not re.fullmatch(pattern, value) or roster.get(key) != value:
            raise AgentFailed('private CLI source/script facts differ')
    if not isinstance(sidecar['engine_token'], str) or not sidecar['engine_token'] or roster.get('engine_token') != sidecar['engine_token']:
        raise AgentFailed('private CLI engine token differs')
    owner = roster.get('owner')
    current_birth = psutil.Process(os.getpid()).create_time()
    if (not isinstance(owner, dict) or type(owner.get('pid')) is not int or owner['pid'] != os.getpid()
            or isinstance(owner.get('birth'), bool) or not isinstance(owner.get('birth'), (int, float))
            or not math.isfinite(owner['birth']) or abs(owner['birth']-current_birth) > .000001):
        raise AgentFailed('private CLI roster is not owned by this live supervisor')
    aliases = private_cli['allowed_cli']
    if (not isinstance(aliases, list) or not aliases or len(aliases) > 512
            or any(not isinstance(name, str) or re.fullmatch('[a-z0-9][a-z0-9_-]*', name) is None for name in aliases)
            or len(set(aliases)) != len(aliases)
            or roster.get('allowed_cli') != aliases):
        raise AgentFailed('private CLI alias catalog differs from protected roster')
    expected_hash = roster.get('native_door_sha256')
    if not isinstance(expected_hash, str) or not re.fullmatch('[0-9a-f]{64}', expected_hash):
        raise AgentFailed('private CLI roster has no exact native image digest')
    image_facts = []
    for name in aliases:
        image = _private_plain_path(private_bin / (name + '.exe'), directory=False)
        digest = hashlib.sha256()
        with image.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024*1024), b''):
                digest.update(chunk)
        if digest.hexdigest() != expected_hash:
            raise AgentFailed(f'private CLI alias image digest differs: {name}')
        image_facts.append({'name': name, 'path': str(image), 'sha256': expected_hash})
    audit = {'private_bin': str(private_bin), 'bindings_path': str(bindings_path), 'sidecar_path': str(sidecar_path),
             'fire_id': fire_id, 'request_id': request_id, 'engine_root': str(engine),
             'source_sha': sidecar['source_sha'], 'script_sha256': sidecar['script_sha256'],
             'images': image_facts, 'runtime_verified': False}
    instruction = ('\n\nFor logical coordinator CLI commands, use only the same-basename .exe under '
                   + str(private_bin) + '. Preserve every argument exactly. The read-only private-cli.json '
                   'artifact lists the allowed CLI catalog. Do not use installed absolute .py commands or '
                   'shared forwarding scripts, and do not rewrite arbitrary Python commands.\n')
    return dict(env), instruction, audit


def _native_child_facts(proc, repo_root, parent_session_id, fire_id, request_id, expected_argv) -> dict:
    import psutil
    from coordinator_core.session.agent_role import observed_cli_launch
    child = psutil.Process(proc.pid)
    birth, argv = child.create_time(), child.cmdline()
    executable = Path(child.exe()).resolve(strict=True)
    if not expected_argv or not Path(expected_argv[0]).is_absolute():
        raise AgentFailed('native identity observation requires explicit native executable path')
    expected = Path(expected_argv[0]).resolve(strict=True)
    if not executable.samefile(expected):
        raise AgentFailed('native identity executable differs from trusted launcher executable')
    executable_sha256 = hashlib.sha256(executable.read_bytes()).hexdigest()
    if (child.name().lower().removesuffix('.exe') != 'codex'
            or not observed_cli_launch(argv).get('exec_worker')
            or child.ppid() != os.getpid() or proc.poll() is not None
            or psutil.Process(proc.pid).create_time() != birth):
        raise AgentFailed('native identity observation requires own live Codex exec child')
    return {'pid': proc.pid, 'start_epoch': birth, 'argv': argv,
            'exe': str(executable), 'exe_sha256': executable_sha256,
            'repo_root': str(repo_root), 'parent_session_id': parent_session_id,
            'fire_id': fire_id, 'request_id': request_id, 'role': 'workflow-agent',
            'evidence': 'observed-native-exec-thread-event'}


class _NativeThreadObserver:
    """Bounded regular-file observer; does not manufacture lifecycle evidence."""
    def __init__(self, path, facts, callback, abort):
        self.path, self.facts, self.callback, self.abort = Path(path), facts, callback, abort
        self.stop = threading.Event()
        self.error = None
        self.join_attempted = False
        self.session_id = None
        self.thread = threading.Thread(target=self._watch, daemon=True, name='workflow-native-identity')

    def start(self):
        self.thread.start()

    def _watch(self):
        pending = b''
        total = events = 0
        try:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise AgentFailed('native identity stdout must be an ordinary regular file')
            with self.path.open('rb') as stream:
                while True:
                    chunk = stream.read(8192 if self.session_id is not None else min(8192, 1024*1024-total))
                    total += len(chunk)
                    pending += chunk
                    while b'\n' in pending:
                        line, pending = pending.split(b'\n', 1)
                        if len(line) > 65536:
                            raise AgentFailed('native identity event exceeds 64KiB')
                        if not line.strip():
                            continue
                        events += 1
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise AgentFailed('native identity event must be an object')
                        if event.get('type') == 'thread.started':
                            sid = event.get('thread_id')
                            if not isinstance(sid, str) or str(uuid.UUID(sid)) != sid:
                                raise AgentFailed('native identity event UUID is not canonical')
                            if self.session_id is not None and sid != self.session_id:
                                raise AgentFailed('conflicting native identity event UUIDs')
                            if self.session_id is None:
                                self.session_id = sid
                                self.callback({**self.facts, 'session_id': sid})
                        if events >= 100 and self.session_id is None:
                            raise AgentFailed('native identity event budget exhausted')
                    if len(pending) > 65536:
                        raise AgentFailed('native identity event exceeds 64KiB')
                    if total >= 1024*1024 and self.session_id is None:
                        raise AgentFailed('native identity byte budget exhausted')
                    if not chunk:
                        if self.stop.is_set():
                            if self.session_id is None:
                                raise AgentFailed('native identity thread event missing')
                            return
                        self.stop.wait(.02)
        except BaseException as exc:
            self.error = exc
            _cleanup(self.abort)

    def finish(self):
        self.stop.set()
        self.join_attempted = True
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise AgentFailed('native identity callback/observer did not stop within 2s')
        if self.error is not None:
            raise AgentFailed(f'native identity observation failed: {self.error}') from self.error


def _run_admitted(argv, prompt, *, env, repo_root, artifact_dir, backend,
                  options, parent_session_id, timeout_s, on_spawn, stdout, stderr,
                  request_id=None, fire_id=None, on_native_thread=None, stall_window_s=None):
    """Reserve checked memory for this worker until its actual child exits.

    A stall_window_s kills the child with StageStalled once its stdout and stderr
    have not grown for that long."""
    deadline = time.monotonic() + timeout_s

    def remaining() -> float:
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise AgentFailed(f"{backend} agent timed out after {timeout_s}s (deadline exhausted during admission or execution)")
        return budget

    sid = parent_session_id if parent_session_id is not None else (
        os.environ.get("COORDINATOR_SESSION_ID") or os.environ.get("CODEX_THREAD_ID")
    )
    if not isinstance(sid, str) or not sid.strip():
        raise AgentFailed("Workflow agent admission requires the real parent session ID")
    peak_mb = admission_post.valid_peak_mb(options.get("declaredPeakMb", DEFAULT_AGENT_PEAK_GIB * 1024))
    if not peak_mb:
        raise AgentFailed("declaredPeakMb must be a positive finite memory declaration")
    stable_key = f"{backend}-workflow"
    # Parallel broker threads have the same supervisor PID. A shared key would
    # let one sibling END release another's reservation, so reserve uniquely.
    key = f"{stable_key}-{uuid.uuid4().hex[:16]}"
    supervisor_pid = os.getpid()
    try:
        host = capacity._load_box_memory_host()
        measured = host.read_recorded_peak(host.admission_file(), stable_key)
        if measured is not None and (not math.isfinite(measured) or measured <= 0):
            raise ValueError("invalid recorded Workflow peak")
        default_peak = max(peak_mb / 1024, measured or 0)
        admitted = capacity.admit_before_spawn(
            key, default_peak, host=host, register_pid=supervisor_pid, session_id=sid,
            wait_kw={"timeout_s": min(remaining(), slot_wait.SLOT_WAIT_TIMEOUT_S)},
        )
    except Exception as exc:
        raise AgentFailed(f"Workflow memory admission unavailable: {type(exc).__name__}: {exc}") from exc
    if not admitted.admitted:
        _cleanup(capacity.release_spawn, key, supervisor_pid, host=host)
        raise AgentFailed(f"Workflow memory admission refused: {admitted.reason}")
    proc = None
    observer = None
    watchdog = None
    try:
        remaining()
        actual_peak_mb = admitted.peak_gib * 1024
        if actual_peak_mb > slot_wait.SLOT_THRESHOLD_MB:
            if not slot_grant.check_slot_grant(sid, key, peak_mb=actual_peak_mb)[0]:
                granted = slot_wait.request_and_wait(
                    sid, key, actual_peak_mb, timeout_s=min(remaining(), slot_wait.SLOT_WAIT_TIMEOUT_S)
                )
                if not granted.get("admitted"):
                    raise AgentFailed(f"Workflow slot admission refused: {granted.get('reason', 'unknown')}")
            # A grant can expire or cover less than the recorded memory peak.
            if not slot_grant.check_slot_grant(sid, key, peak_mb=actual_peak_mb)[0]:
                raise AgentFailed("Workflow requires a live Group EM grant covering its recorded peak")
        (artifact_dir / "admission.json").write_text(json.dumps({
            "session_id": sid, "run": key, "stable_key": stable_key,
            "declared_peak_mb": actual_peak_mb, "supervisor_pid": supervisor_pid,
        }), encoding="utf-8")
        env.pop("COORDINATOR_SESSION_ID", None)
        env["COORDINATOR_PARENT_SESSION_ID"] = sid
        remaining()  # A late grant must never start work after its broker deadline.
        proc = subprocess.Popen(
            argv, cwd=repo_root, env=env, stdin=subprocess.PIPE,
            stdout=stdout, stderr=stderr, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            **tenant_priority.popen_kwargs(argv),
        )
        posted = admission_post.post("START", sid, pids=[proc.pid], run=key,
                                     declared_peak_mb=actual_peak_mb)
        admission_post.record_undelivered(artifact_dir, {"phase": "START", **posted})
        if on_spawn:
            on_spawn(proc.pid)
        if on_native_thread is not None:
            facts = _native_child_facts(proc, repo_root, sid, fire_id, request_id, argv)
            observer = _NativeThreadObserver(artifact_dir / 'stdout.jsonl', facts, on_native_thread, proc.kill)
            observer.start()
        stalled = threading.Event()
        watchdog_done = threading.Event()
        if stall_window_s is not None:
            def _watch() -> None:
                seen, last_growth = -1, time.monotonic()
                while not watchdog_done.wait(_STALL_POLL_S):
                    try:
                        size = _stream_bytes(stdout, stderr)
                    except OSError:
                        return
                    if size != seen:
                        seen, last_growth = size, time.monotonic()
                    elif time.monotonic() - last_growth >= stall_window_s:
                        stalled.set()
                        _cleanup(_kill_descendants, proc.pid)
                        _cleanup(proc.kill)
                        return
            threading.Thread(target=_watch, name="stage-stall-watchdog", daemon=True).start()
        try:
            proc.communicate(prompt.encode("utf-8"), timeout=remaining())
        finally:
            watchdog_done.set()
        if stalled.is_set():
            raise StageStalled(f"{backend} stage silent for {stall_window_s}s; killed")
        if observer is not None:
            observer.finish()
        return proc
    except BaseException:
        if proc is not None:
            _cleanup(proc.kill)
            _cleanup(proc.communicate)
        raise
    finally:
        if watchdog is not None:
            watchdog.stop.set()
        if observer is not None:
            observer.stop.set()
            if not observer.join_attempted:
                observer.join_attempted = True
                observer.thread.join(timeout=2)
        if proc is not None:
            _cleanup(_save_process, artifact_dir, proc, backend, options,
                     argv[argv.index("--model") + 1] if "--model" in argv else None,
                     broker_root_session_id=sid, broker_request_id=request_id, broker_fire_id=fire_id)
            _cleanup(admission_post.post, "END", sid, pids=[proc.pid], run=key)
        _cleanup(capacity.release_spawn, key, supervisor_pid, host=host)


@contextmanager
def _provider_stream(env, backend, repo, stderr, fire_id, request_id, attempt, on_spawn):
    destination = os.environ.get("COORDINATOR_PROVIDER_EVIDENCE_DIR")
    if backend != "codex" or not destination:
        yield env, stderr, on_spawn
        return
    root = Path(destination)
    if not root.is_absolute() or not root.is_dir() or root.resolve().is_relative_to(repo):
        raise AgentFailed("Provider evidence requires an existing host directory outside the worker checkout")
    folder = Path(stderr.name).parent
    stage = folder.name if folder.parent.name == "agents" else f"{folder.parent.name}/{folder.name}"
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb") as reader, os.fdopen(write_fd, "wb") as writer, tempfile.NamedTemporaryFile(
        dir=root, prefix="worker-", suffix=".jsonl", delete=False
    ) as evidence:
        collector = None
        identity = {}

        def start_collector(pid):
            nonlocal collector
            identity.update(fire_id=fire_id, stage=stage, pid=pid)
            header = dict(identity, type="worker.capture", request_id=request_id, attempt=attempt,
                          usage_home=env.get("CODEX_HOME"), artifact_dir=str(folder))
            evidence.write((json.dumps(header) + "\n").encode())
            evidence.flush()
            collector = spawn_collector(identity, reader, evidence)
            if on_spawn:
                on_spawn(pid)

        try:
            yield dict(env, RUST_LOG="tungstenite::protocol=trace,codex_api::sse::responses=trace"), writer, start_collector
        finally:
            writer.close()
            if collector is not None:
                try:
                    collector.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    collector.terminate()
                    collector.wait()
                evidence.write((json.dumps(dict(identity, type="worker.capture_end", collector_exit=collector.returncode)) + "\n").encode())


def _persona(agent_type: str | None) -> str:
    if not agent_type or agent_type == "general-purpose":
        return ""
    if not isinstance(agent_type, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*:[a-z0-9][a-z0-9-]*", agent_type):
        raise AgentFailed(f"unsupported agentType: {agent_type!r}")
    if not agent_type.startswith("coordinator:"):
        # A domain persona (e.g. game-dev:staff-game-dev) is an installed plugin agent, not a content-root one.
        from coordinator_core.hooks.block_unenumerated_agent_type import plugin_agent_model

        _, found = plugin_agent_model(agent_type)
        if found is not None and found.is_file():
            return found.read_text(encoding="utf-8")
        raise AgentFailed(f"agent definition missing: {agent_type}")
    from coordinator_core.content_root import read_content_root

    root_value = read_content_root()
    if not root_value:
        raise AgentFailed(f"content root unresolved for {agent_type}")
    root = Path(root_value)
    basename = agent_type.split(":", 1)[1] + ".md"
    directories = (root / "coordinator" / "agents", root / "agents")
    from coordinator_core.hooks.block_unenumerated_agent_type import plugin_agent_model

    _, resolved = plugin_agent_model(agent_type)
    candidates = [folder / basename for folder in directories]
    if resolved is not None:
        # Share public plugin lookup without allowing a different installed
        # content root to silently replace this run's validated definitions.
        path = Path(resolved).resolve()
        if path.parent in {folder.resolve() for folder in directories}:
            candidates.insert(0, path)
    for path in candidates:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise AgentFailed(f"agent definition missing: {agent_type}")


def _codex_model(options: dict) -> str | None:
    explicit = options.get("codexModel")
    if explicit is not None:
        if not isinstance(explicit, str) or not explicit.strip():
            raise AgentFailed("codexModel must be a non-empty native model ID")
        return explicit
    tier = options.get("model")
    if tier is None:
        return None
    if not isinstance(tier, str) or not tier.strip():
        raise AgentFailed("model must be an explicit known Workflow tier")
    model = model_for_tier("codex", tier)
    if not model:
        raise AgentFailed(f"no verified Codex model mapping for Workflow tier {tier!r}")
    return model


def _codex_effort(options: dict, model: str | None, env: dict) -> str | None:
    effort = options.get("effort")
    if effort is None:
        return None
    if not isinstance(effort, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", effort):
        raise AgentFailed("effort must be a non-empty native reasoning effort name")
    home = env.get("CODEX_HOME")
    base = Path(home) if home else Path(env.get("HOME") or env.get("USERPROFILE") or Path.home()) / ".codex"
    try:
        catalog = json.loads((base / "models_cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        catalog = None
    models = catalog.get("models", []) if isinstance(catalog, dict) else []
    for row in models if isinstance(models, list) else []:
        if not isinstance(row, dict) or row.get("slug") != model:
            continue
        levels = row.get("supported_reasoning_levels")
        if isinstance(levels, list):
            supported = {item.get("effort") for item in levels if isinstance(item, dict)}
            if supported and effort not in supported:
                raise AgentFailed(f"reasoning effort {effort!r} is unsupported by catalogued model {model!r}")
        break
    # No matching local catalogue means the native CLI remains the authority.
    return effort


def _grok_model(options: dict) -> str | None:
    """Native Grok id for a Workflow tier, or an explicit grokModel. Omitted tier keeps the CLI default."""
    explicit = options.get("grokModel")
    if explicit is not None:
        if not isinstance(explicit, str) or not explicit.strip():
            raise AgentFailed("grokModel must be a non-empty native model ID")
        return explicit
    tier = options.get("model")
    if tier is None:
        return None
    if not isinstance(tier, str) or not tier.strip():
        raise AgentFailed("model must be an explicit known Workflow tier")
    model = model_for_tier("grok", tier)
    if not model:
        raise AgentFailed(f"no verified Grok model mapping for Workflow tier {tier!r}")
    return model


def _grok_effort(options: dict) -> str | None:
    """Pass a named reasoning effort through. Omitted effort is not invented."""
    effort = options.get("effort")
    if effort is None:
        return None
    if not isinstance(effort, str) or effort not in GROK_EFFORTS:
        raise AgentFailed(
            "effort must be a Grok reasoning effort: none, minimal, low, medium, high, xhigh, max"
        )
    return effort


def _family_event_model(event: dict) -> str | None:
    """Model a grok/agy event reports about itself, from the event or its init/result/session payload."""
    for holder in (event, event.get("init"), event.get("session"), event.get("result"), event.get("step_update")):
        if isinstance(holder, dict):
            for name in ("model", "model_id", "modelId"):
                value = holder.get(name)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


_AGY_LOG_MODEL = re.compile(r'selected model override to backend: label="([^"]+)"')


def _grok_envelope(raw: str) -> dict:
    """Normalize Grok's one-shot envelope or its native text chunks and terminal end event."""
    try:
        envelope = json.loads(raw)
    except ValueError:
        envelope = None
    if isinstance(envelope, dict) and "type" not in envelope:
        return envelope
    chunks = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "text" and isinstance(event.get("data"), str):
            chunks.append(event["data"])
        elif kind == "end":
            return {**event, "text": "".join(chunks) if chunks else None}
    raise ValueError("grok emitted no terminal end event")


def _grok_envelope_model(raw: str) -> str | None:
    """The modelUsage key with the most calls in Grok's final receipt."""
    try:
        usage = _grok_envelope(raw).get("modelUsage")
    except ValueError:
        return None
    if not isinstance(usage, dict):
        return None
    named = [(name.strip(), row) for name, row in usage.items() if isinstance(name, str) and name.strip()]
    if not named:
        return None
    def calls(item: tuple) -> int:
        count = item[1].get("modelCalls") if isinstance(item[1], dict) else None
        return count if isinstance(count, int) else 0

    return max(named, key=calls)[0]


def _agy_log_model(log: Path) -> str | None:
    """Model label agy's own --log-file reports selecting; its stream-json carries none."""
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found = _AGY_LOG_MODEL.findall(text)
    return found[-1].strip() or None if found else None


def _event_provenance(path: Path, backend: str) -> dict:
    """Observe actual CLI output only; requested models and parent IDs are not evidence."""
    result: dict = {
        "native_thread_id": "unknown", "native_turn_id": "unknown",
        "native_total_usage": "unknown", "model": "unknown", "effort": "unknown",
        "usage": "unknown", "observed_turn_usage_total": "unknown", "service_tier": None,
    }
    usages = []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result
    lines = raw.splitlines()
    if backend == "grok":
        result["model"] = _grok_envelope_model(raw) or "unknown"
    elif backend == "agy":
        result["model"] = _agy_log_model(path.with_name(_AGY_LOG_NAME)) or "unknown"
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if backend in _FAMILY_BACKENDS and result["model"] == "unknown":
            result["model"] = _family_event_model(event) or "unknown"
        kind = event.get("type")
        if kind == "thread.started":
            identity = event.get("thread_id")
        elif backend == "claude":
            identity = event.get("session_id")
        elif backend == "grok":
            identity = event.get("sessionId") or event.get("session_id")
        else:
            identity = None
        if isinstance(identity, str):
            try:
                result["native_thread_id"] = str(uuid.UUID(identity))
            except ValueError:
                pass
        if kind in ("thread.started", "turn.started", "session.configured", "system"):
            payload = event.get("session") if isinstance(event.get("session"), dict) else event
            for target, names in (("model", ("model",)), ("effort", ("reasoning_effort", "model_reasoning_effort", "effort")),
                                  ("service_tier", ("service_tier",))):
                for name in names:
                    value = payload.get(name)
                    if isinstance(value, str) and value.strip():
                        result[target] = value
                        break
        if (kind == "turn.completed" or backend == "claude" or (backend == "grok" and kind == "usage")) and isinstance(event.get("usage"), dict):
            usages.append(event["usage"])
            result.setdefault("usage_observations", []).append({
                "usage": event["usage"], "model": event.get("model", result["model"]),
                "effort": result["effort"], "service_tier": event.get("service_tier", result["service_tier"]),
                "native_turn_id": event.get("turn_id"), "native_total_usage": event.get("total_token_usage")})
    if usages:
        # Preserve the event shapes (including cache counters) and sum numeric
        # token counters across completed turns without inventing missing fields.
        totals: dict = {}
        for usage in usages:
            for key, value in usage.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                    totals[key] = totals.get(key, 0) + value
        result["usage"] = totals
        result["observed_turn_usage_total"] = totals
        result["usage_events"] = usages
    return result


def _join_startup_model(receipt: dict) -> None:
    """Bind model-free exec usage to the same worker's native startup observation."""
    if receipt.get('backend') != 'codex':
        return
    try:
        sid = str(uuid.UUID(receipt['native_thread_id']))
        path = settings_home() / 'host-sessions' / 'codex' / f'{sid}.json'
        with path.open('rb') as stream:
            raw = stream.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            return
        record = json.loads(raw)
        evidence = record.get('session_start_model_evidence', {})
        model = record.get('session_start_model')
        if (record.get('schema_version') != 1 or record.get('provider') != 'codex'
                or record.get('session_id') != sid or record.get('pid') != receipt.get('pid')
                or record.get('parent_session_id') != receipt.get('broker_root_session_id')
                or record.get('role') != 'workflow-agent'
                or evidence.get('source') != 'session_start_payload'
                or evidence.get('session_source') != 'startup'
                or record.get('session_start_model_conflict') is True
                or not isinstance(model, str) or not model.strip() or model == 'unknown'
                or record.get('session_start_model_initial', model) != model):
            return
        observations = receipt.get('usage_observations', [])
        if any(row.get('model') not in (None, 'unknown', model)
               for row in [receipt, *observations]):
            return
        # The process receipt and canonical accounting consumer retain the evidence source.
        receipt['model_evidence'] = {'session_id': sid, 'registry_path': str(path), **evidence}
        if receipt.get('model') in (None, 'unknown'):
            receipt['model'] = model
        for observation in observations:
            if observation.get('model') in (None, 'unknown'):
                observation['model'] = model
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return


def _append_canonical_usage(receipt: dict) -> dict:
    """Publish complete observed counters once per broker request under a kernel lock.

    This telemetry cannot change the worker's result. Missing native counters
    remain incomplete, rather than silently becoming zero-priced usage.
    """
    try:
        child = str(uuid.UUID(receipt['native_thread_id']))
        parent = str(uuid.UUID(receipt['broker_root_session_id']))
    except (ValueError, TypeError, KeyError, AttributeError):
        return {'status': 'incomplete', 'reason': 'actual child/root UUID unavailable'}
    fire, request = receipt.get('broker_fire_id'), receipt.get('broker_request_id')
    if not isinstance(fire, str) or not fire or isinstance(request, bool) or not isinstance(request, (str, int)) or request == '':
        return {'status': 'incomplete', 'reason': 'stable broker fire/request identity unavailable'}
    backend = receipt.get('backend')
    events = receipt.get('usage_events')
    if backend not in ('codex', 'claude', 'grok') or not isinstance(events, list) or not events:
        return {'status': 'incomplete', 'reason': 'native usage events unavailable'}
    cached_key, written_key = ('cached_input_tokens', 'cache_write_input_tokens') if backend == 'codex' else (
        'cache_read_input_tokens', 'cache_creation_input_tokens')
    records = []
    stable_request = f'{fire}:{request}'
    observations = receipt.get('usage_observations', [])
    for index, usage in enumerate(events):
        observation = observations[index] if index < len(observations) else receipt
        keys = ('input_tokens', 'output_tokens', cached_key, written_key)
        if not isinstance(usage, dict) or any(
            isinstance(usage.get(key), bool) or not isinstance(usage.get(key), int) or usage[key] < 0
            for key in keys
        ):
            return {'status': 'incomplete', 'reason': 'native usage counters missing or invalid'}
        uncached = usage['input_tokens'] - usage[cached_key] - usage[written_key] if backend == 'codex' else usage['input_tokens']
        if uncached < 0:
            return {'status': 'incomplete', 'reason': 'native cache count exceeds input'}
        totals = {'input_tokens': uncached, 'output_tokens': usage['output_tokens'],
                  'cache_read_input_tokens': usage[cached_key], 'cache_creation_input_tokens': usage[written_key]}
        payload = {
            'session_id': child, 'parent_session_id': parent,
            'request_id': stable_request if index == 0 else f'{stable_request}:turn:{index}',
            'provider': backend, 'model': None if observation.get('model') == 'unknown' else observation.get('model'),
            'effort': None if observation.get('effort') == 'unknown' else observation.get('effort'),
            'service_tier': observation.get('service_tier'), 'usage_format': 'coordinator-canonical-v1', 'usage': totals,
        }
        # Codex exec's successful completion reports the thread's lifetime counters.
        # Keep the observation alongside its charge so retries can compare the
        # original counters even after a later request has advanced the thread.
        if backend == 'codex' and observations and len(events) == 1:
            payload['exec_thread_total'] = totals.copy()
        if receipt.get('model_evidence'):
            payload['model_evidence'] = receipt['model_evidence']
        # Only native evidence can join canonical and native accounting streams.
        if isinstance(observation.get('native_turn_id'), str) and observation['native_turn_id'] != 'unknown':
            payload['native_turn_id'] = observation['native_turn_id']
        if isinstance(observation.get('native_total_usage'), dict):
            payload['native_total_usage'] = observation['native_total_usage']
        records.append({'type': 'coordinator_usage', 'timestamp': datetime.now(timezone.utc).isoformat(), 'payload': payload})
    try:
        path = (settings_home() / 'state' / 'codex-usage' / f'{parent}.jsonl').resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        with held_lock(path, timeout=0.05):
            with path.open('a+', encoding='utf-8', newline='\n') as stream:
                stream.seek(0)
                prior = stream.read(4 * 1024 * 1024 + 1)
                if len(prior) > 4 * 1024 * 1024:
                    raise ValueError('usage ledger exceeds bounded duplicate-check budget')
                seen = {}
                for line in prior.splitlines(keepends=True):
                    if not line.endswith('\n'):
                        raise ValueError('usage ledger has an incomplete final line')
                    previous = json.loads(line)
                    previous_payload = previous.get('payload', {}) if isinstance(previous, dict) else {}
                    if isinstance(previous, dict) and previous.get('type') == 'coordinator_usage':
                        seen[(previous_payload.get('session_id'), previous_payload.get('request_id'))] = previous_payload
                child_history = [p for (sid, _), p in seen.items() if sid == child]
                for event in records:
                    payload = event['payload']
                    prior_payload = seen.get((child, payload['request_id']))
                    original = payload.get('exec_thread_total', payload['usage'])
                    if prior_payload and prior_payload.get('exec_thread_total', prior_payload.get('usage')) != original:
                        raise ValueError('existing request has different counters; legacy aggregate or conflicting replay')
                    if not prior_payload and child_history and 'exec_thread_total' in payload:
                        previous_total = child_history[-1].get('exec_thread_total')
                        if not previous_total or any(original[k] < previous_total[k] for k in original):
                            return {'status': 'incomplete', 'reason': 'Codex thread has no comparable previous total'}
                        payload['usage'] = {k: value - previous_total[k] for k, value in original.items()}
                missing = [event for event in records if (child, event['payload']['request_id']) not in seen]
                for event in missing:
                    stream.write(json.dumps(event, ensure_ascii=False) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
        return {'status': 'appended' if missing else 'duplicate', 'path': str(path), 'request_id': stable_request,
                'request_ids': [event['payload']['request_id'] for event in records]}
    except Exception as exc:
        return {'status': 'failed', 'reason': f'{type(exc).__name__}: {exc}'}


def _record_execution(parent, fire, request, phase, *, receipt=None) -> None:
    """Persist expected execution and terminal accounting evidence in the parent's journal."""
    try:
        parent = str(uuid.UUID(parent))
        if not isinstance(fire, str) or not fire or isinstance(request, bool) or not isinstance(request, (str, int)):
            return
        payload = {'parent_session_id': parent, 'request_id': f'{fire}:{request}', 'phase': phase}
        if receipt is not None:
            usage = receipt.get('canonical_usage', {})
            payload.update(session_id=receipt.get('native_thread_id'), usage_status=usage.get('status'),
                           usage_request_ids=usage.get('request_ids', []))
        path = settings_home() / 'state' / 'codex-usage' / f'{parent}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        with held_lock(path, timeout=0.05):
            with path.open('a', encoding='utf-8', newline='\n') as stream:
                stream.write(json.dumps({'type': 'coordinator_execution', 'payload': payload}) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
    except Exception:
        return


_FAMILY_BACKENDS = ("grok", "agy")
_AGY_LOG_NAME = "agy.log"
_AGY_OK = frozenset({"success", "ok", "completed", "done"})
_FENCE = re.compile(r"^\s*```(?:json)?\s*\n(.*?)\n\s*```\s*$", re.DOTALL)


def _repair_json_text(text: str):
    """Parse a reply with literal control characters in strings and unescaped inner quotes.

    Trap: an inner quote is escaped only where the parser stops with a missing delimiter, one
    quote at a time and bounded, so structure outside strings is never rewritten.
    """
    for _ in range(text.count('"') + 1):
        try:
            return json.loads(text, strict=False)
        except json.JSONDecodeError as exc:
            if not exc.msg.startswith(("Expecting ',' delimiter", "Expecting ':' delimiter")):
                raise
            quote = text.rfind('"', 0, exc.pos)
            if quote < 0:
                raise
            text = text[:quote] + "\\" + text[quote:]
    raise ValueError("inner-quote repair exhausted")


def _trailing_json(text: str):
    """Parse a reply that is JSON, a fenced JSON block, or narration ending in a JSON object.

    Raises ValueError (as json.loads does) when no object ends the text.
    """
    stripped = text.strip()
    fence = _FENCE.match(stripped)
    try:
        return json.loads(fence.group(1) if fence else stripped)
    except ValueError as whole_error:
        decoder = json.JSONDecoder()
        start = stripped.find("{")
        while start != -1:
            try:
                value, end = decoder.raw_decode(stripped, start)
            except ValueError:
                value = None
            else:
                if isinstance(value, dict) and not stripped[end:].strip().strip("`").strip():
                    return value
            start = stripped.find("{", start + 1)
        raise whole_error


_OP_RELAY_AGENT_TYPE = "coordinator:op-relay"
_COMMIT_AGENT_TYPE = "coordinator:git-commit-agent"
# Children that run coordinator-invoke and nothing else get only this allow rule. The rule grammar is
# grok's; a rejected pattern surfaces as the named cancel error. Literals match ask_compose's.
_INVOKE_ONLY_AGENT_TYPES = frozenset({_OP_RELAY_AGENT_TYPE, _COMMIT_AGENT_TYPE})
_RELAY_ALLOW = "Bash(coordinator-invoke:*)"
# Trap: grok's headless allow gate never matches a command led by the `&` call operator (measured: every
# `--allow Bash...` rule still answers "permission required"), so grok is told the bare form; settings-home
# bin is on its PATH (_grok_child_path), which resolves sizing-assemble and coordinator-invoke by name.
# Codex's own exec policy rejects `Remove-Item -Recurse -Force` ("blocked by policy", no rule named) and the
# kill guard rejects a kill chosen by lookup; these two ops carry neither shape.
CODEX_CLEANUP_CLAUSE = (
    'To delete your own scratch directory under scratch/codex-tmp or scratch/tmp, run '
    "coordinator-invoke scratch.remove '{\"paths\":[\"<path>\"]}'; to stop a process you spawned, run "
    "coordinator-invoke proc.stop_own_child '{\"pid\":<pid>}'. Never Remove-Item -Recurse or a name or port lookup. "
)
_WINDOWS_CALL_FORM = {
    False: 'For a quoted executable use the PowerShell call operator &, followed by the quoted path. ',
    True: 'Run every command as a plain command: never lead with the & call operator or quote the executable; '
          'coordinator tools such as sizing-assemble and coordinator-invoke are on PATH, so run them by bare name. ',
}


_SETTINGS_HOME_INVOKE = '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/coordinator-invoke"'
_OP_ID = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")


def _operation_allow(options: dict) -> list[str]:
    """`--allowedTools` argv for a Claude child whose agent options declare one engine `op_id`; none when undeclared.

    Two rules admit `coordinator-invoke <op_id>` and nothing else: the bare command word and the quoted
    settings-home path ask_compose's `_INVOKE` emits (a prefix rule on the bare word never matches it).
    A malformed `op_id` fails the agent.
    """
    op_id = options.get("op_id")
    if op_id is None:
        return []
    if not isinstance(op_id, str) or not _OP_ID.fullmatch(op_id):
        raise AgentFailed(f"invalid op_id: {op_id!r}")
    return [flag for word in ("coordinator-invoke", _SETTINGS_HOME_INVOKE)
            for flag in ("--allowedTools", f"Bash({word} {op_id}:*)")]


def _agy_model(options: dict) -> str | None:
    """Native Agy id for a Workflow tier, or an explicit agyModel. Omitted tier keeps the CLI default."""
    explicit = options.get("agyModel")
    if explicit is not None:
        if not isinstance(explicit, str) or not explicit.strip():
            raise AgentFailed("agyModel must be a non-empty native model ID")
        return explicit
    tier = options.get("model")
    if tier is None:
        return None
    if not isinstance(tier, str) or not tier.strip():
        raise AgentFailed("model must be an explicit known Workflow tier")
    model = model_for_tier("agy", tier)
    if not model:
        raise AgentFailed(f"no verified Agy model mapping for Workflow tier {tier!r}")
    return model


def _agy_effort(options: dict) -> str | None:
    """Pass a named Agy reasoning effort through. Omitted effort is not invented."""
    effort = options.get("effort")
    if effort is None:
        return None
    if not isinstance(effort, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", effort):
        raise AgentFailed("effort must be a non-empty native reasoning effort name")
    return effort


def _family_argv(backend: str, artifact_dir: Path, agent_type: str | None = None,
                 model: str | None = None, effort: str | None = None) -> list[str]:
    """Write-capable argv for a grok/agy stage (prompt via file for grok, stdin for agy).

    Never carries grok.FORBIDDEN_FLAGS; the agy arm delegates to family_stage_argv (single builder)
    and runs unrestricted (--dangerously-skip-permissions, no sandbox, shell and file permission
    granted) per PM ruling. For agy, `model` and `effort` pass through `--model` / `--effort`
    matching Codex's separation of model and effort. A grok stage with a Workflow tier gets that
    tier's native model and, when set, its reasoning effort. Omitted model and effort stay with the
    CLI. Stages stay single-agent. A grok op-relay or commit-stage child gets only a
    coordinator-invoke allow rule.
    """
    try:
        if backend == "grok":
            # acceptEdits still prompts for a shell command, and a headless run cannot
            # ask. A general stage therefore uses bypassPermissions. An invoke-only
            # child keeps one coordinator-invoke rule: the absolute launcher on POSIX,
            # because the prompt rewrite expands ${COORDINATOR_SETTINGS_HOME:-...}/bin/<cli>
            # and a bare-name rule does not match that path.
            if agent_type in _INVOKE_ONLY_AGENT_TYPES:
                permission_mode = "acceptEdits"
                if os.name == "nt":
                    allow = _RELAY_ALLOW
                else:
                    launcher = (settings_home() / "bin" / "coordinator-invoke").as_posix()
                    allow = f"Bash({launcher}:*)"
            elif os.name == "nt":
                permission_mode = "acceptEdits"
                allow = "Bash"
            else:
                permission_mode = "bypassPermissions"
                allow = "Bash"
            output_format = "streaming-json" if agent_type == "coordinator:sizing-scout" else "json"
            argv = [*resolve_grok(), "--prompt-file", str(artifact_dir / "prompt.txt"),
                    "--output-format", output_format, "--permission-mode", permission_mode,
                    "--allow", allow, "--no-subagents", "--no-auto-update"]
            if model:
                argv += ["--model", model]
            if effort:
                argv += ["--reasoning-effort", effort]
            return argv
        return family_stage_argv(
            resolve_agy(),
            log_file=str(artifact_dir / _AGY_LOG_NAME),
            model=model,
            effort=effort,
        )
    except (GrokUnavailable, AgyUnavailable) as exc:
        raise AgentFailed(f"{backend} executable unavailable: {exc}") from exc


def _grok_child_path(env: dict[str, str]) -> str:
    """PATH for a headless grok child: settings-home bin first (bare coordinator-invoke), git's dir last.

    The engine's own PATH may lack git; Git for Windows' cmd dir is found by one stat, no spawn.
    """
    parts = [str(settings_home() / 'bin'), env.get('PATH')]
    git = shutil.which('git', path=env.get('PATH'))
    if git:
        return os.pathsep.join(filter(None, parts))
    roots = [os.environ.get(k) for k in ('ProgramFiles', 'ProgramW6432', 'ProgramFiles(x86)')]
    for root in filter(None, roots):
        cmd_dir = Path(root) / 'Git' / 'cmd'
        if (cmd_dir / 'git.exe').is_file():
            parts.append(str(cmd_dir))
            break
    return os.pathsep.join(filter(None, parts))


def _family_text(backend: str, stdout: Path) -> str:
    """Final response text from a Grok or Agy receipt."""
    raw = stdout.read_text(encoding="utf-8", errors="replace")
    if backend == "grok":
        text = _grok_envelope(raw).get("text")
        if not isinstance(text, str):
            raise ValueError("grok envelope has no text")
        return text
    result = None
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("event") == "result" and isinstance(event.get("result"), dict):
            result = event["result"]
    if result is None:
        raise ValueError("agy emitted no result event")
    status = str(result.get("status", "")).lower()
    if status not in _AGY_OK:
        raise ValueError(f"agy result status {status or 'missing'}: {result.get('error') or result.get('response')}")
    response = result.get("response")
    if not isinstance(response, str) or not response.strip():
        raise ValueError("agy result has no response (a tool permission was likely denied)")
    return response


def _grok_cancellation(stdout: Path, stderr: Path) -> str | None:
    """A named error when the grok envelope reports stopReason cancelled, else None."""
    try:
        envelope = _grok_envelope(stdout.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(envelope, dict) or str(envelope.get("stopReason", "")).lower() not in ("cancelled", "canceled"):
        return None
    try:
        reason = stderr.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        reason = ""
    return "grok run cancelled (stopReason cancelled): " + (reason[-1000:] or "no stderr reason")


def _save_process(artifact_dir: Path, proc, backend: str, options: dict, requested_model,
                  *, broker_root_session_id=None, broker_request_id=None, broker_fire_id=None) -> dict:
    if broker_root_session_id is None:
        try:
            admission = json.loads((artifact_dir / "admission.json").read_text(encoding="utf-8"))
            broker_root_session_id = admission.get("session_id") if isinstance(admission, dict) else None
        except (OSError, ValueError):
            pass
    receipt = {
        "pid": proc.pid, "exit_code": proc.returncode, "backend": backend,
        "label": options.get("label"), "requested_model": requested_model,
        "requested_effort": options.get("effort"),
        # Broker lineage is supplied by the caller, never claimed as a native
        # parent relationship or used as the child's own session identity.
        "broker_root_session_id": broker_root_session_id,
        "broker_request_id": broker_request_id, "broker_fire_id": broker_fire_id,
        **_event_provenance(artifact_dir / "stdout.jsonl", backend),
    }
    _join_startup_model(receipt)
    receipt['canonical_usage'] = _append_canonical_usage(receipt)
    private_audit = artifact_dir / 'private-cli.json'
    if private_audit.exists():
        receipt['private_cli'] = _private_json(private_audit, 1024*1024)
    (artifact_dir / "process.json").write_text(json.dumps(receipt), encoding="utf-8")
    return receipt


def _write_commit_params(repo_root: Path, prompt: str) -> None:
    """Write the run's terminal-commit params file from the raw prompt before a commit child spawns.

    Trap: params come from the composed prompt this runner holds, never from the child's output,
    and never from persona/schema text (callers pass the raw prompt). A backend whose tool hooks
    never fire has no other writer. Never raises and never blocks the spawn.
    """
    if not (repo_root / ".git").exists():
        return
    try:
        from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan

        path, leg = chan.materialize_from_prompt(repo_root, prompt)
        if path is None:
            return
        if leg is None:
            chan.clear_refusal(str(path))
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        chan.note_refusal(str(path), "runner: " + leg)
    except Exception:
        return


_GATE_REASK_SECONDS = 60
_GATE_REPLY_LIMIT = 128 * 1024
_GATE_SCALARS = re.compile(r'"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\b(?:true|false|null)\b')


def _gate_envelope_result(value: Any) -> Any:
    """Unwrap the JSON-RPC reply a gate relay echoes verbatim.

    Relays append sibling keys beside ``result``: ``note`` (prose), ``_execution`` (the CLI's
    exit-code/params-file trailer) and an outer ``halt: null``. Each is lossless to drop; the outer
    ``halt`` only when it is null and the result's own ``halt`` is null too, so a real halt is never
    masked. Any other key stays wrapped and fails validation.
    """
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        return value
    extra = set(value) - {"jsonrpc", "id", "result"}
    result = value.get("result")
    if "result" not in value or not extra <= {"note", "_execution", "halt"}:
        return value
    if (("note" in extra and not isinstance(value["note"], str))
            or ("_execution" in extra and not isinstance(value["_execution"], dict))
            or ("halt" in extra and not (
                value["halt"] is None and isinstance(result, dict) and result.get("halt") is None))):
        return value
    return result


def _bind_gate_manifest_digest(value: Any, repo_root: Path, run_id: Any) -> Any:
    """Repair a malformed duplicate digest after verifying the outer handle."""
    if not isinstance(value, dict) or not isinstance(run_id, str):
        return value
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id):
        return value
    stage = value.get("stage_reply")
    outer = value.get("manifest_handle")
    inner = stage.get("manifest_handle") if isinstance(stage, dict) else None
    from coordinator_core.ops.dispatch_emit import ask_stage_receipt, reply_sidecar
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    if not all(isinstance(handle, dict) and set(handle) == set(reply_sidecar.HANDLE_KEYS)
               for handle in (outer, inner)):
        return value
    root = repo_root.resolve()
    directory = record_dir(root, run_id)
    path = directory / ask_stage_receipt.MANIFEST_NAME
    if (not path.is_relative_to(root) or path.resolve() != path
            or (directory / ask_stage_receipt.RECEIPT_NAME).is_symlink()):
        return value
    expected = path.relative_to(root).as_posix()
    if any(handle["path"] != expected for handle in (outer, inner)):
        return value
    sealed = ask_stage_receipt.landed(directory, ask_stage_receipt.GATE_DIGEST)
    if sealed is None or sealed["path"] != expected or outer["sha256"] != sealed["sha256"]:
        return value
    if any(type(handle["bytes"]) is not int or handle["bytes"] != sealed["bytes"]
           for handle in (outer, inner)):
        return value
    digest = inner["sha256"]
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        return value
    return {**value, "stage_reply": {**stage, "manifest_handle": {**inner, "sha256": sealed["sha256"]}}}


_GATE_WRAPPER_PREFIX = re.compile(
    r'^\s*\{\s*"jsonrpc"\s*:\s*"2\.0"\s*,\s*"id"\s*:\s*'
    r'(?:-?\d+|"(?:[^"\\]|\\.)*"|null)\s*,\s*"result"\s*:\s*(?=\{)'
)


def _gate_reask_preserves_scalars(initial: str, retry: str) -> bool:
    expected = _GATE_SCALARS.findall(initial)
    if expected == _GATE_SCALARS.findall(retry):
        return True
    # A schema-bound repair may return the bare payload. Only disregard the
    # proven leading wrapper fields on a syntactically invalid JSON reply.
    try:
        json.loads(initial)
    except ValueError:
        wrapper = _GATE_WRAPPER_PREFIX.match(initial)
        if wrapper is not None:
            return _GATE_SCALARS.findall(initial[wrapper.end():]) == _GATE_SCALARS.findall(retry)
    return False


def _loopback_provider_argv(endpoint: str) -> list[str]:
    """Pin a credential-free local responder when the worker ignores user configuration."""
    port = re.fullmatch(r"http://127[.]0[.]0[.]1:([1-9][0-9]{0,4})/v1", endpoint)
    if not port or int(port[1]) > 65535:
        raise AgentFailed("local fixture requires an IPv4 loopback endpoint")
    return [
        "-c", 'model_provider="local_fixture"',
        "-c", 'model_providers.local_fixture.name="local_fixture"',
        "-c", 'model_providers.local_fixture.wire_api="responses"',
        "-c", "model_providers.local_fixture.base_url=" + json.dumps(endpoint),
    ]


def _budget_gateway_argv(capture_root: str | None, endpoint: str | None) -> list[str]:
    """Bind a capped native worker to the assessor's HTTP listener before admitting its spawn."""
    owner = os.environ.get("COORDINATOR_PAIR_BUDGET_ID")
    path = Path(capture_root) / "budget-gateway.json" if capture_root else None
    if owner is None and (path is None or not path.exists()):
        return []
    if not owner or path is None or not endpoint:
        raise AgentFailed("capped worker requires its allocation, evidence directory and gateway")
    import http.client
    from urllib.parse import urlsplit

    argv = _loopback_provider_argv(endpoint)
    try:
        binding = json.loads(path.read_bytes())
        if binding != {"endpoint": endpoint, "allocation": owner}:
            raise ValueError("gateway differs from issued allocation")
        connection = http.client.HTTPConnection("127.0.0.1", urlsplit(endpoint).port, timeout=0.5)
        try:
            connection.request("HEAD", "/cap-ready")
            response = connection.getresponse()
            if response.status != 204 or response.getheader("X-Allocation") != owner:
                raise ValueError("gateway did not confirm allocation")
        finally:
            connection.close()
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise AgentFailed(f"capped worker gateway unavailable: {exc}") from exc
    return [*argv, "-c", "model_providers.local_fixture.supports_websockets=false"]


def run_agent(
    prompt: str,
    options: dict[str, Any] | None,
    *,
    repo_root: Path,
    artifact_dir: Path,
    backend: str = "codex",
    timeout_s: float = 3600,
    on_spawn: Callable[[int], None] | None = None,
    parent_session_id: str | None = None,
    request_id: str | int | None = None,
    fire_id: str | None = None,
    on_native_thread: Callable[[dict], None] | None = None,
    private_cli: dict | None = None,
    run_id: str | None = None,
    _gate_reask: bool = False,
) -> Any:
    """Return the schema-validated agent value; persist stdout/stderr and result.

    The caller gives each parallel agent its own artifact directory. This call
    blocks only its worker thread. Failure raises AgentFailed; it never becomes
    a success-shaped value that a Workflow could mistake for completed work.
    """
    options = {} if options is None else options
    started = time.monotonic()
    if not isinstance(prompt, str) or not isinstance(options, dict):
        raise AgentFailed("agent requires a string prompt and object options")
    if timeout_s <= 0 or timeout_s > 86400:
        raise AgentFailed("agent timeout must be within (0, 86400] seconds")
    repo_root = (Path(repo_root).absolute() if backend == 'codex' and os.name == 'nt'
                 else Path(repo_root).resolve())
    if not repo_root.is_dir():
        raise AgentFailed(f"agent cwd does not exist: {repo_root}")
    schema = options.get("schema")
    codex_gate = (backend == "codex" and schema is not None
                  and options.get("label") == "gate" and options.get("phase") == "gate")
    codex_structured_reply = backend == "codex" and schema is not None
    if schema is not None:
        try:
            jsonschema.validators.validator_for(schema).check_schema(schema)
        except (jsonschema.SchemaError, TypeError, AttributeError) as exc:
            raise AgentFailed(f"invalid agent schema: {exc}") from exc
    if options.get("agentType") == _COMMIT_AGENT_TYPE:
        _write_commit_params(repo_root, prompt)
    persona = _persona(options.get("agentType"))
    if os.name == "nt":
        from coordinator_core.ops.dispatch_emit.invoke_form import POWERSHELL, render_invoke_for_shell

        prompt = render_invoke_for_shell(prompt, POWERSHELL)
    full_prompt = (f"Agent definition ({options['agentType']}):\n{persona}\n\n" if persona else "") + prompt
    if os.name == 'nt':
        full_prompt = (
            'Native execution platform: Windows. Run supplied commands with PowerShell. '
            + _WINDOWS_CALL_FORM[backend == 'grok'] +
            'Do not wrap a command in cmd.exe /c, use POSIX parameter expansion, or add literal backslash-escaped quotes. '
            'Run each coordinator-invoke as one plain command, coordinator-invoke <op> \'<json>\', by that bare name '
            '(it is on PATH): never its full path or the & operator, no Set-Location, '
            'variable assignment, pipeline or semicolon; the working directory is already set, so run no cd or '
            'Set-Location even where the task text asks for one. '
            'Use Python or native PowerShell for file operations. Preserve all operation arguments and JSON payloads.\n\n'
        ) + full_prompt
    if backend == "codex":
        full_prompt = (
            "Codex edit tool: if the assigned stage calls for a Write or Edit on a declared "
            "path, use apply_patch for that edit. The Claude tool names in the brief describe "
            "the action, not a requirement to discover those tools. Keep the same file scope "
            "and all write evidence requirements. Do not edit when the stage is read-only.\n\n"
        ) + full_prompt
    elif backend == "grok":
        full_prompt = (
            "Grok stage tools: search_replace or write for file edits; run_terminal_command for commands. "
            "Edit, Write, Bash, and Agent in the brief name the action. Subagents are off. "
            "coordinator-invoke only when the brief names that command.\n\n"
        ) + full_prompt
    full_prompt = with_worker_preamble(backend, full_prompt)
    if run_id:
        from coordinator_core.ops.dispatch_emit import engine_identity

        full_prompt += "\n\n" + engine_identity.PROMPT_LINE.format(run_id=run_id)
        on_spawn = engine_identity.spawn_recorder(
            repo_root, run_id, str(options.get("label") or Path(artifact_dir).name), on_spawn)
    native_schema = _codex_schema(schema) if schema is not None else None
    if schema is not None and (backend in _FAMILY_BACKENDS or (backend == "codex" and native_schema is None)):
        full_prompt += (
            "\n\nYour final response must be exactly one raw JSON value, with no Markdown fences or commentary. "
            "It must satisfy this exact JSON Schema. Preserve dynamic mapping entries and optional/nullable "
            "field semantics; do not replace argument objects with empty objects:\n"
            + json.dumps(schema, ensure_ascii=False)
        )
    if private_cli is not None and (backend != 'codex' or on_spawn is None or on_native_thread is None):
        raise AgentFailed('private CLI requires Codex and owner spawn/native identity callbacks')
    private_settings = None
    if private_cli is not None:
        private_settings = _private_cli_settings(private_cli, repo_root=repo_root, artifact_dir=artifact_dir,
            parent_session_id=parent_session_id, fire_id=fire_id, request_id=request_id)
        full_prompt += private_settings[1]
    if on_native_thread is not None:
        if backend != 'codex':
            raise AgentFailed('native identity callback requires Codex backend')
        _protected_observer_path(repo_root, artifact_dir)
    artifact_dir = Path(artifact_dir).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    if on_native_thread is not None:
        _protected_observer_path(repo_root, artifact_dir)
    (artifact_dir / "prompt.txt").write_text(full_prompt, encoding="utf-8")
    env = _environment()
    env['COORDINATOR_AGENT_PROVIDER'] = backend
    env['COORDINATOR_AGENT_VENDOR'] = backend
    env['COORDINATOR_AGENT_ROLE'] = 'workflow-agent'
    if backend == 'grok':
        env['PATH'] = _grok_child_path(env)
    for key, option in (("COORDINATOR_WORKFLOW_AGENT_TYPE", "agentType"), ("COORDINATOR_WORKFLOW_AGENT_LABEL", "label")):
        value = options.get(option)
        if isinstance(value, str) and value:
            env[key] = value
    last = artifact_dir / "last.txt"
    # A reused directory must not let stale successful output mask a failure.
    last.unlink(missing_ok=True)
    (artifact_dir / "process.json").unlink(missing_ok=True)
    (artifact_dir / 'private-cli.json').unlink(missing_ok=True)
    if backend == "codex":
        from coordinator_core.warm import sandboxed_spawn

        if sandboxed_spawn.caller_is_sandboxed():
            raise AgentFailed(
                "codex child would spawn nested in the codex sandbox without network; "
                "fire the run through the warm engine (engine_ask.submit)"
            )
        preflight_auth(env=env, requested=False)
        native_cli = resolve_codex(env=env)
        # Resolve the real native binary before private PATH can affect lookup.
        if private_settings is not None:
            env.update(private_settings[0])
            (artifact_dir / 'private-cli.json').write_text(json.dumps(private_settings[2]), encoding='utf-8')
        capture_root = os.environ.get("COORDINATOR_PROVIDER_EVIDENCE_DIR")
        worker_home_root = None
        if capture_root:
            root = Path(capture_root)
            policy_file = os.environ.get("COORDINATOR_CODEX_WORKER_POLICY_FILE")
            if not root.is_absolute() or not root.is_dir() or root.resolve().is_relative_to(repo_root) or not policy_file:
                raise AgentFailed("Persistent attribution requires a restricted worker and private evidence directory")
            rules = json.loads(Path(policy_file).read_bytes())
            if rules.get(str(root.resolve())) != "deny" or any(
                Path(path).is_absolute() and Path(path).resolve().is_relative_to(root.resolve()) and access != "deny"
                for path, access in rules.items()
            ):
                raise AgentFailed("Worker policy must deny the provider evidence directory and descendants")
            worker_home_root = Path(tempfile.mkdtemp(prefix="native-", dir=root))
        try:
            shape_codex_child(env, writable_root=repo_root, worker_home_root=worker_home_root)
        except SandboxSetupRefused as exc:
            raise AgentFailed(str(exc)) from exc
        if _gate_reask:
            argv = [
                *native_cli, "exec", "--json", "--ephemeral", "--ignore-user-config",
                "--sandbox", "read-only", "--disable", "shell_tool", "--disable", "unified_exec",
                "--disable", "plugins", "--disable", "apps", "-c", "mcp_servers={}", "-o", str(last),
            ]
        else:
            argv = codex_family_stage_argv(native_cli, "--ephemeral", "-o", str(last))
        if schema is not None:
            schema_path = artifact_dir / "schema.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            if native_schema is not None:
                argv += ["--output-schema", str(schema_path)]
        if worker_home_root is not None:
            argv.remove("--ephemeral")
        model = _codex_model(options)
        if model is not None:
            argv += ["--model", model]
        effort = _codex_effort(options, model, env)
        if effort is not None:
            argv += ["-c", "model_reasoning_effort=" + json.dumps(effort)]
        argv += ["-"]
        endpoint = os.environ.get("COORDINATOR_LOOPBACK_FIXTURE_URL")
        budget_argv = _budget_gateway_argv(capture_root, endpoint)
        if endpoint is not None:
            if "COORDINATOR_CODEX_WORKER_POLICY_FILE" not in os.environ:
                raise AgentFailed("local fixture requires a configured worker policy")
            argv += budget_argv or _loopback_provider_argv(endpoint)
    elif backend == "claude":
        executable = shutil.which("claude", path=env.get("PATH"))
        if not executable:
            raise AgentFailed("claude executable unavailable")
        argv = [executable, "-p", "--output-format", "json"]
        if schema is not None:
            argv += ["--json-schema", json.dumps(schema)]
        if options.get("model"):
            argv += ["--model", str(options["model"])]
        argv += _operation_allow(options)
    elif backend in _FAMILY_BACKENDS:
        family_model = _agy_model(options) if backend == "agy" else _grok_model(options) if backend == "grok" else None
        family_effort = _agy_effort(options) if backend == "agy" else _grok_effort(options) if backend == "grok" else None
        argv = _family_argv(backend, artifact_dir, options.get("agentType"), family_model, family_effort)
    else:
        raise AgentFailed(f"unknown agent backend: {backend}")
    stdin_text = full_prompt
    if backend == "agy":
        stdin_text = json.dumps({"event": "user", "message": {"content": full_prompt}}) + "\n"
    elif backend == "grok":
        stdin_text = ""
    stall_window_s = None
    if backend == "codex":
        try:
            stall_window_s = float(options.get("stallWindowS", STALL_WINDOW_S))
        except (TypeError, ValueError) as exc:
            raise AgentFailed(f"stallWindowS must be a number: {exc}") from exc
    started = time.monotonic()
    try:
        _record_execution(parent_session_id or os.environ.get('COORDINATOR_SESSION_ID') or os.environ.get('CODEX_THREAD_ID'),
                          fire_id, request_id, 'started')
        for attempt in range(STALL_RETRIES + 1):
            with (artifact_dir / "stdout.jsonl").open("wb") as out, (artifact_dir / "stderr.txt").open("wb") as err:
                try:
                    with _provider_stream(env, backend, repo_root, err, fire_id, request_id, attempt, on_spawn) as (child_env, child_err, child_spawn):
                        proc = _run_admitted(
                            argv, stdin_text, env=child_env, repo_root=repo_root, artifact_dir=artifact_dir,
                            backend=backend, options=options, parent_session_id=parent_session_id,
                            timeout_s=max(timeout_s - (time.monotonic() - started), 1e-3), on_spawn=child_spawn,
                            stdout=out, stderr=child_err, request_id=request_id, fire_id=fire_id,
                            on_native_thread=on_native_thread, stall_window_s=stall_window_s,
                        )
                    break
                except StageStalled as exc:
                    if attempt >= STALL_RETRIES:
                        # Receipt consumers read this file by name to tell a stall from a wall timeout.
                        (artifact_dir / "stage-idle-timeout.json").write_text(json.dumps({
                            "cause": "stage_idle_timeout", "idle_seconds": stall_window_s,
                            "attempts": attempt + 1, "detail": str(exc),
                        }), encoding="utf-8")
                        raise
    except subprocess.TimeoutExpired as exc:
        raise AgentFailed(f"{backend} agent timed out after {timeout_s}s") from exc
    except OSError as exc:
        raise AgentFailed(f"{backend} spawn failed: {exc}") from exc
    finally:
        if backend == "codex":
            writeback_codex_auth(env)
    requested_model = model if backend == "codex" else (family_model if backend in ("agy", "grok") and family_model is not None else options.get("model"))
    receipt = _save_process(artifact_dir, proc, backend, options, requested_model,
                  broker_root_session_id=parent_session_id, broker_request_id=request_id,
                  broker_fire_id=fire_id)
    _record_execution(receipt.get('broker_root_session_id'), fire_id, request_id, 'completed', receipt=receipt)
    if proc.returncode != 0:
        raise AgentFailed(f"{backend} agent exited {proc.returncode}; see {artifact_dir / 'stderr.txt'}")
    text = None
    try:
        if backend == "claude":
            envelope = json.loads((artifact_dir / "stdout.jsonl").read_text(encoding="utf-8"))
            if envelope.get("is_error"):
                raise AgentFailed(f"claude reported an error: {envelope.get('result', '')}")
            value = envelope.get("structured_output") if schema is not None else envelope.get("result")
            if schema is not None and value is None:
                value = json.loads(envelope["result"])
        elif backend in _FAMILY_BACKENDS:
            if backend == "grok":
                cancelled = _grok_cancellation(artifact_dir / "stdout.jsonl", artifact_dir / "stderr.txt")
                if cancelled:
                    raise AgentFailed(cancelled)
            text = _family_text(backend, artifact_dir / "stdout.jsonl")
            if schema is not None:
                value = _trailing_json(text)
            else:
                value = text
        else:
            text = last.read_text(encoding="utf-8")
            value = json.loads(text, strict=False) if schema is not None else text
            if codex_gate:
                value = _bind_gate_manifest_digest(
                    _gate_envelope_result(value), repo_root, options.get("gateManifestRunId"),
                )
        if schema is not None:
            if not codex_gate:
                value = fill_omitted_nullable(unwrap_rpc_envelope(value, schema), schema)
            jsonschema.validate(value, schema)
    except (OSError, ValueError, KeyError, jsonschema.ValidationError) as exc:
        if not codex_structured_reply or _gate_reask:
            raise AgentFailed(f"{backend} returned no schema-valid result: {exc}") from exc
        diagnostic = (exc.message if isinstance(exc, jsonschema.ValidationError) else str(exc))[:400]
        raw_path = None
        if text:
            # last.txt is unlinked on a reused directory; the journal needs a path that outlives it.
            raw_path = artifact_dir / "raw-reply.txt"
            raw_path.write_text(text, encoding="utf-8")
        if text and not codex_gate and isinstance(exc, json.JSONDecodeError):
            try:
                mended = fill_omitted_nullable(unwrap_rpc_envelope(_repair_json_text(text), schema), schema)
                jsonschema.validate(mended, schema)
            except (ValueError, jsonschema.ValidationError):
                pass
            else:
                (artifact_dir / "result.json").write_text(json.dumps(mended, ensure_ascii=False), encoding="utf-8")
                return mended
        value = {"arm": None, "halt": {"kind": "gate-reply-invalid", "reason": diagnostic}} if codex_gate else None
        if (codex_gate and isinstance(exc, json.JSONDecodeError)
                and exc.pos == len(text) and _GATE_WRAPPER_PREFIX.match(text)):
            try:
                closed = _bind_gate_manifest_digest(
                    _gate_envelope_result(json.loads(text + "}")),
                    repo_root, options.get("gateManifestRunId"),
                )
                jsonschema.validate(closed, schema)
            except (ValueError, jsonschema.ValidationError):
                pass
            else:
                (artifact_dir / "result.json").write_text(json.dumps(closed, ensure_ascii=False), encoding="utf-8")
                return closed
        remaining = min(_GATE_REASK_SECONDS, timeout_s - (time.monotonic() - started))
        if text and len(text) <= _GATE_REPLY_LIMIT and remaining > 0 and private_cli is None:
            from coordinator_core.ops.dispatch_emit.run_truth_contract import ARMS

            retry_dir = artifact_dir / ("gate-reask" if codex_gate else "reply-reask")
            context = {"diagnostic": diagnostic, "reply": text}
            if codex_gate:
                context["allowed_arm_values"] = [*ARMS, None]
            retry_data = json.dumps(
                context,
                ensure_ascii=False,
            )
            retry_prompt = (
                "Repair JSON syntax in the following data. Do not run any tools or repeat the operation. "
                "Preserve every key, value and its order, including trailing fields. "
                "Return the complete original reply with only JSON punctuation or whitespace repaired. "
                "If that is impossible, return null.\n"
                + retry_data
            )
            try:
                repaired = run_agent(
                    retry_prompt, {k: v for k, v in options.items() if k != "agentType"},
                    repo_root=repo_root, artifact_dir=retry_dir,
                    backend=backend, timeout_s=remaining, parent_session_id=parent_session_id,
                    request_id=f"{request_id}:gate-reask" if request_id is not None else None,
                    fire_id=fire_id, run_id=run_id, _gate_reask=True,
                )
                repaired_text = (retry_dir / "last.txt").read_text(encoding="utf-8")
                # A schema-valid prefix can still omit acceptance or manifest fields.
                if codex_gate and _gate_reask_preserves_scalars(text, repaired_text):
                    value = repaired
                elif not codex_gate and _GATE_SCALARS.findall(text) == _GATE_SCALARS.findall(repaired_text):
                    value = repaired
            except (AgentFailed, OSError):
                pass
        if value is None:
            next_action = f"; next_action: inspect the raw reply at {raw_path}" if raw_path else ""
            raise StructuredReplyInvalid(
                f"{StructuredReplyInvalid.cause}: {backend} returned no schema-valid result; "
                f"structured reply could not be repaired: {diagnostic}{next_action}"
            ) from exc
        jsonschema.validate(value, schema)
    (artifact_dir / "result.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return value
