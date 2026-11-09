"""codex_runner.workflow -- generic prompt/schema execution for Workflow stages.

The Codex launcher and subscription guard are shared with codex_runner. Explicit
Workflow tiers use the shared provider catalogue; no model option means the native
configured default. Caller identity is lineage, never a child's own session ID.
"""
from __future__ import annotations

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

from coordinator_core.codex_runner.resolve import preflight_auth, resolve_codex
from coordinator_core.codex_runner.review import _unattended_env
from coordinator_core.bash_guards import _heavy_command_class as capacity
from coordinator_core.group_em import admission_post, slot_grant, slot_wait
from coordinator_core.agent_pipe.model_tiers import model_for_tier
from coordinator_core.ops.workflow_fire.receipt import FIRE_TOKEN_ENV
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
    # The shared provider helper preserves caller identity for ordinary helper
    # subprocesses. A fresh native agent is a new session, so scrub AFTER that
    # helper (it can reinsert the bound caller even when its input was scrubbed).
    for key in _OWN_IDENTITY_ENV:
        env.pop(key, None)
    if source.get("COORDINATOR_PARENT_SESSION_ID"):
        env["COORDINATOR_PARENT_SESSION_ID"] = source["COORDINATOR_PARENT_SESSION_ID"]
    if source.get(FIRE_TOKEN_ENV):
        env[FIRE_TOKEN_ENV] = source[FIRE_TOKEN_ENV]
    # The executor does not own shared daemon lifecycle. CLI calls run cold and
    # must never start/respawn a warm service as an implicit side effect.
    env['COORDINATOR_WARM'] = '0'
    # Native operation doors validate this stamped build using their normal
    # bootstrap ladder; never silently dispatch back into another live clone.
    from coordinator_core.warm.engine_root import current_engine_clone
    env['COORDINATOR_ENGINE_ROOT'] = str(current_engine_clone())
    # Native tools/hooks need these Windows and coordinator runtime locations.
    for key in ("TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "COMSPEC",
                "COORDINATOR_SETTINGS_HOME", "MACHINE_LOCAL_REGISTRY_DIR"):
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
                  request_id=None, fire_id=None, on_native_thread=None):
    """Reserve checked memory for this worker until its actual child exits."""
    deadline = time.monotonic() + timeout_s

    def remaining() -> float:
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise AgentFailed("Workflow agent deadline exhausted during admission or execution")
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
        )
        posted = admission_post.post("START", sid, pids=[proc.pid], run=key,
                                     declared_peak_mb=actual_peak_mb)
        if not posted.get("delivered"):
            raise AgentFailed(f"Workflow START admission delivery failed: {posted.get('reason', 'unknown')}")
        if on_spawn:
            on_spawn(proc.pid)
        if on_native_thread is not None:
            facts = _native_child_facts(proc, repo_root, sid, fire_id, request_id, argv)
            observer = _NativeThreadObserver(artifact_dir / 'stdout.jsonl', facts, on_native_thread, proc.kill)
            observer.start()
        proc.communicate(prompt.encode("utf-8"), timeout=remaining())
        if observer is not None:
            observer.finish()
        return proc
    except BaseException:
        if proc is not None:
            _cleanup(proc.kill)
            _cleanup(proc.communicate)
        raise
    finally:
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


def _persona(agent_type: str | None) -> str:
    if not agent_type or agent_type == "general-purpose":
        return ""
    if not isinstance(agent_type, str) or not re.fullmatch(r"coordinator:[a-z0-9][a-z0-9-]*", agent_type):
        raise AgentFailed(f"unsupported agentType: {agent_type!r}")
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


def _event_provenance(path: Path, backend: str) -> dict:
    """Observe actual CLI output only; requested models and parent IDs are not evidence."""
    result: dict = {
        "native_thread_id": "unknown", "native_turn_id": "unknown",
        "native_total_usage": "unknown", "model": "unknown", "effort": "unknown",
        "usage": "unknown", "observed_turn_usage_total": "unknown",
    }
    usages = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return result
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        identity = event.get("thread_id") if kind == "thread.started" else event.get("session_id") if backend == "claude" else None
        if isinstance(identity, str):
            try:
                result["native_thread_id"] = str(uuid.UUID(identity))
            except ValueError:
                pass
        if kind in ("thread.started", "turn.started", "session.configured", "system"):
            payload = event.get("session") if isinstance(event.get("session"), dict) else event
            for target, names in (("model", ("model",)), ("effort", ("reasoning_effort", "model_reasoning_effort", "effort"))):
                for name in names:
                    value = payload.get(name)
                    if isinstance(value, str) and value.strip():
                        result[target] = value
                        break
        if (kind == "turn.completed" or backend == "claude") and isinstance(event.get("usage"), dict):
            usages.append(event["usage"])
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
    if backend not in ('codex', 'claude') or not isinstance(events, list) or not events:
        return {'status': 'incomplete', 'reason': 'native usage events unavailable'}
    cached_key, written_key = ('cached_input_tokens', 'cache_write_input_tokens') if backend == 'codex' else (
        'cache_read_input_tokens', 'cache_creation_input_tokens')
    totals = dict.fromkeys(('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'), 0)
    for usage in events:
        keys = ('input_tokens', 'output_tokens', cached_key, written_key)
        if not isinstance(usage, dict) or any(
            isinstance(usage.get(key), bool) or not isinstance(usage.get(key), int) or usage[key] < 0
            for key in keys
        ):
            return {'status': 'incomplete', 'reason': 'native usage counters missing or invalid'}
        uncached = usage['input_tokens'] - usage[cached_key] if backend == 'codex' else usage['input_tokens']
        if uncached < 0:
            return {'status': 'incomplete', 'reason': 'native cache count exceeds input'}
        totals['input_tokens'] += uncached
        # Native output_tokens already includes reasoning tokens.
        totals['output_tokens'] += usage['output_tokens']
        totals['cache_read_input_tokens'] += usage[cached_key]
        totals['cache_creation_input_tokens'] += usage[written_key]
    stable_request = f'{fire}:{request}'
    payload = {
        'session_id': child, 'parent_session_id': parent, 'request_id': stable_request,
        'provider': backend, 'model': None if receipt.get('model') == 'unknown' else receipt.get('model'),
        'effort': None if receipt.get('effort') == 'unknown' else receipt.get('effort'),
        'service_tier': None, 'usage_format': 'coordinator-canonical-v1', 'usage': totals,
    }
    # Only native evidence can join canonical and native accounting streams.
    if isinstance(receipt.get('native_turn_id'), str) and receipt['native_turn_id'] != 'unknown':
        payload['native_turn_id'] = receipt['native_turn_id']
    if isinstance(receipt.get('native_total_usage'), dict):
        payload['native_total_usage'] = receipt['native_total_usage']
    event = {'type': 'coordinator_usage', 'timestamp': datetime.now(timezone.utc).isoformat(), 'payload': payload}
    try:
        path = (settings_home() / 'state' / 'codex-usage' / f'{parent}.jsonl').resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        with held_lock(path, timeout=2.0):
            with path.open('a+', encoding='utf-8', newline='\n') as stream:
                stream.seek(0)
                for line in stream:
                    if not line.endswith('\n'):
                        raise ValueError('usage ledger has an incomplete final line')
                    previous = json.loads(line)
                    previous_payload = previous.get('payload', {}) if isinstance(previous, dict) else {}
                    if isinstance(previous, dict) and previous.get('type') == 'coordinator_usage' and previous_payload.get('request_id') == stable_request:
                        return {'status': 'duplicate', 'path': str(path), 'request_id': stable_request}
                stream.write(json.dumps(event, ensure_ascii=False) + '\n')
                stream.flush()
        return {'status': 'appended', 'path': str(path), 'request_id': stable_request}
    except Exception as exc:
        return {'status': 'failed', 'reason': f'{type(exc).__name__}: {exc}'}


def _save_process(artifact_dir: Path, proc, backend: str, options: dict, requested_model,
                  *, broker_root_session_id=None, broker_request_id=None, broker_fire_id=None) -> None:
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
    receipt['canonical_usage'] = _append_canonical_usage(receipt)
    private_audit = artifact_dir / 'private-cli.json'
    if private_audit.exists():
        receipt['private_cli'] = _private_json(private_audit, 1024*1024)
    (artifact_dir / "process.json").write_text(json.dumps(receipt), encoding="utf-8")


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
) -> Any:
    """Return the schema-validated agent value; persist stdout/stderr and result.

    The caller gives each parallel agent its own artifact directory. This call
    blocks only its worker thread. Failure raises AgentFailed; it never becomes
    a success-shaped value that a Workflow could mistake for completed work.
    """
    options = {} if options is None else options
    if not isinstance(prompt, str) or not isinstance(options, dict):
        raise AgentFailed("agent requires a string prompt and object options")
    if timeout_s <= 0 or timeout_s > 86400:
        raise AgentFailed("agent timeout must be within (0, 86400] seconds")
    repo_root = (Path(repo_root).absolute() if backend == 'codex' and os.name == 'nt'
                 else Path(repo_root).resolve())
    if not repo_root.is_dir():
        raise AgentFailed(f"agent cwd does not exist: {repo_root}")
    schema = options.get("schema")
    if schema is not None:
        try:
            jsonschema.validators.validator_for(schema).check_schema(schema)
        except (jsonschema.SchemaError, TypeError, AttributeError) as exc:
            raise AgentFailed(f"invalid agent schema: {exc}") from exc
    persona = _persona(options.get("agentType"))
    full_prompt = (f"Agent definition ({options['agentType']}):\n{persona}\n\n" if persona else "") + prompt
    if os.name == 'nt':
        full_prompt = (
            'Native execution platform: Windows. Run supplied commands with PowerShell. '
            'For a quoted executable use the PowerShell call operator &, followed by the quoted path. '
            'Do not wrap a command in cmd.exe /c, use POSIX parameter expansion, or add literal backslash-escaped quotes. '
            'Use Python or native PowerShell for file operations. Preserve all operation arguments and JSON payloads.\n\n'
        ) + full_prompt
    native_schema = _codex_schema(schema) if schema is not None else None
    if backend == "codex" and schema is not None and native_schema is None:
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
        preflight_auth(env=env, requested=False)
        native_cli = resolve_codex(env=env)
        # Resolve the real native binary before private PATH can affect lookup.
        if private_settings is not None:
            env.update(private_settings[0])
            (artifact_dir / 'private-cli.json').write_text(json.dumps(private_settings[2]), encoding='utf-8')
        argv = [*native_cli, "exec", "--json", "--sandbox", "workspace-write",
                "--ephemeral", "-o", str(last)]
        if schema is not None:
            schema_path = artifact_dir / "schema.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            if native_schema is not None:
                argv += ["--output-schema", str(schema_path)]
        model = _codex_model(options)
        if model is not None:
            argv += ["--model", model]
        effort = _codex_effort(options, model, env)
        if effort is not None:
            argv += ["-c", "model_reasoning_effort=" + json.dumps(effort)]
        argv += ["-"]
    elif backend == "claude":
        executable = shutil.which("claude", path=env.get("PATH"))
        if not executable:
            raise AgentFailed("claude executable unavailable")
        argv = [executable, "-p", "--output-format", "json"]
        if schema is not None:
            argv += ["--json-schema", json.dumps(schema)]
        if options.get("model"):
            argv += ["--model", str(options["model"])]
    else:
        raise AgentFailed(f"unknown agent backend: {backend}")
    try:
        with (artifact_dir / "stdout.jsonl").open("wb") as out, (artifact_dir / "stderr.txt").open("wb") as err:
            proc = _run_admitted(
                argv, full_prompt, env=env, repo_root=repo_root, artifact_dir=artifact_dir,
                backend=backend, options=options, parent_session_id=parent_session_id,
                timeout_s=timeout_s, on_spawn=on_spawn, stdout=out, stderr=err,
                request_id=request_id, fire_id=fire_id, on_native_thread=on_native_thread,
            )
    except subprocess.TimeoutExpired as exc:
        raise AgentFailed(f"{backend} agent timed out after {timeout_s}s") from exc
    except OSError as exc:
        raise AgentFailed(f"{backend} spawn failed: {exc}") from exc
    _save_process(artifact_dir, proc, backend, options, model if backend == "codex" else options.get("model"),
                  broker_root_session_id=parent_session_id, broker_request_id=request_id,
                  broker_fire_id=fire_id)
    if proc.returncode != 0:
        raise AgentFailed(f"{backend} agent exited {proc.returncode}; see {artifact_dir / 'stderr.txt'}")
    try:
        if backend == "claude":
            envelope = json.loads((artifact_dir / "stdout.jsonl").read_text(encoding="utf-8"))
            if envelope.get("is_error"):
                raise AgentFailed(f"claude reported an error: {envelope.get('result', '')}")
            value = envelope.get("structured_output") if schema is not None else envelope.get("result")
            if schema is not None and value is None:
                value = json.loads(envelope["result"])
        else:
            text = last.read_text(encoding="utf-8")
            value = json.loads(text) if schema is not None else text
        if schema is not None:
            jsonschema.validate(value, schema)
    except (OSError, ValueError, KeyError, jsonschema.ValidationError) as exc:
        raise AgentFailed(f"{backend} returned no schema-valid result: {exc}") from exc
    (artifact_dir / "result.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return value
