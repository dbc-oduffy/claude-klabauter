"""Harness-independent Workflow driver: durable journal, native agents and completion delivery."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import tempfile
import time
import uuid

from coordinator_core.ops.workflow_fire import fire
from coordinator_core.ops.workflow_fire.backends import check as check_backend
from coordinator_core.ops.workflow_fire.receipt import FIRE_TOKEN_ENV, stamp_fire_ownership
from coordinator_core.ops.workflow_fire.wait import DEFAULT_TIMEOUT_S


_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _save(path: Path, value: dict) -> None:
    temporary = path.with_name(f'{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp')
    temporary.write_text(json.dumps(value, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def _spawn_worker(command: list[str], *, cwd, env, stdout, popen=None, windows: bool | None = None) -> subprocess.Popen:
    """Start the worker outside the caller's job; a job that forbids breakaway gets one retry without it."""
    popen = subprocess.Popen if popen is None else popen
    windows = os.name == 'nt' if windows is None else windows
    kwargs: dict = {}
    base = 0
    if windows:
        base = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200)
        kwargs['creationflags'] = base | _CREATE_BREAKAWAY_FROM_JOB
    else:
        kwargs['start_new_session'] = True

    def spawn() -> subprocess.Popen:
        return popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.STDOUT, **kwargs)

    try:
        return spawn()
    except OSError:
        if not windows:
            raise
        kwargs['creationflags'] = base
        return spawn()


def _pid_birth(pid: int) -> float | None:
    import psutil

    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def validate_private_engine_config(value, *, backend: str = 'codex') -> dict | None:
    """Validate an EM-selected native build without starting a service."""
    if value is None:
        return None
    if backend != 'codex' or os.name != 'nt':
        raise ValueError('private native engines require the Windows Codex backend')
    fields = {'door_image', 'door_provenance', 'shared_engine_root'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('private_engine requires door_image, door_provenance and shared_engine_root')
    result = {}
    for name in fields:
        path = value[name]
        if not isinstance(path, str) or not path or not Path(path).is_absolute() or '..' in Path(path).parts:
            raise ValueError('private_engine paths must be absolute without parent traversal')
        result[name] = str(Path(path))
    return result


def _private_source_sha() -> str:
    stamp = Path(__file__).resolve().parents[3] / 'coordinator_core' / '_engine_stamp'
    value = stamp.read_text(encoding='utf-8').strip().removeprefix('sha:')
    if re.fullmatch('[0-9a-f]{40}', value) is None:
        raise ValueError('private native execution requires a genuine stamped publication')
    return value


class HeadlessEngineRootUnavailable(RuntimeError):
    """No published stamped engine root exists for a headless worker to run from."""


def _worker_engine_root() -> Path:
    """The published stamped engine root the worker imports from; never the caller checkout."""
    from coordinator_core.install.engine_root_for_install import resolve_engine_root_for_install
    from coordinator_core.warm.engine_root import current_engine_clone, is_engine_root

    from coordinator_core.session.caller_env import effective_caller_env

    # Inside the warm server os.environ is the server's, not the caller's; the pin rides the request.
    pin = effective_caller_env().get('COORDINATOR_DOOR_ENGINE_ROOT', '').strip()
    if pin:
        pinned_root = Path(pin).resolve()
        if not Path(pin).is_absolute() or pinned_root != current_engine_clone().resolve() or not is_engine_root(pinned_root):
            raise HeadlessEngineRootUnavailable(
                'headless Workflow engine pin must match the running stamped door engine'
            )
        return pinned_root
    resolved = resolve_engine_root_for_install()
    if resolved.kind == 'none' or resolved.root is None:
        raise HeadlessEngineRootUnavailable(
            'headless Workflow worker needs a published stamped engine root '
            f'(repos.claude_klabauter): {resolved.remediation}')
    return Path(resolved.root)


def start(script_path: str, *, cwd: str | None = None, notify_to: str | None = None,
          backend: str = 'codex', concurrency_cap: int = fire.DEFAULT_CONCURRENCY_CAP,
          timeout_s: int = DEFAULT_TIMEOUT_S, args: dict | None = None, run_id: str | None = None,
          caller_session_id: str | None = None, expected: str | None = None,
          private_engine: dict | None = None, refire: bool = False) -> dict:
    check_backend(backend)
    if type(concurrency_cap) is not int or concurrency_cap < 1:
        raise ValueError('concurrency_cap must be a positive integer')
    if type(timeout_s) is not int or not 1 <= timeout_s <= 86400:
        raise ValueError('timeout_s must be an integer within 1..86400')
    if args is not None and not isinstance(args, dict):
        raise ValueError('workflow args must be an object')
    private_spec = validate_private_engine_config(private_engine, backend=backend)
    private_sha = _private_source_sha() if private_spec is not None else None
    from coordinator_core.warm import sandboxed_spawn

    script = Path(script_path)
    if not script.is_absolute() and cwd is not None:
        script = Path(cwd) / script
    script = script.absolute()
    forward_to_host = sandboxed_spawn.caller_is_sandboxed()
    if not forward_to_host:
        try:
            resolved_script = script.resolve(strict=True)
            repo = fire._resolve_target_repo(resolved_script, cwd)
            fire._assert_script_under_repo_root(resolved_script, repo)
            script = resolved_script
        except PermissionError:
            forward_to_host = True
    # The caller must still read the bytes; an unreadable sibling is never delegated.
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    if expected is not None and expected != digest:
        from coordinator_core.ops.dispatch_emit.op import FiredDriftError, guard_against_fired_drift

        guard_against_fired_drift(script, expected)
        raise FiredDriftError(f'{script} changed between emit and fire (sha256 {digest}; expected {expected})')
    if forward_to_host:
        # The host rechecks authority and real containment.
        host_cwd = str(Path(cwd).absolute()) if cwd else cwd
        repo = host_cwd or str(script.parent)
        forward = {'script_path': str(script), 'cwd': host_cwd, 'backend': backend, 'executor': 'headless',
                   'expected': digest, 'run_id': run_id, 'args': args, 'caller_session_id': caller_session_id,
                   'notify_to': notify_to, 'concurrency_cap': concurrency_cap, 'timeout_s': timeout_s,
                   'refire': refire or None}
        if private_engine is not None:
            forward['private_engine'] = private_engine
        return sandboxed_spawn.forward_spawn_to_warm('workflow.fire', {k: v for k, v in forward.items() if v is not None or k == 'cwd'}, str(repo))
    worker_root = Path(__file__).resolve().parents[3] if private_spec is not None else _worker_engine_root()
    registry, identifier, record_path, record = fire.reserve_fire(
        script, repo, digest, expected=expected, concurrency_cap=concurrency_cap, notify_to=notify_to,
        refire=refire,
    )
    if record.get('already_fired'):
        return record
    artifacts = registry / 'headless' / identifier
    record.update(engine_root=str(worker_root))
    record.update(backend=backend, executor='headless', timeout_s=timeout_s,
                  args=args or {}, run_id=run_id, caller_session_id=caller_session_id,
                  script_sha256=digest, artifact_dir=str(artifacts), journal_path=str(artifacts / 'journal.jsonl'))
    child = None
    try:
        if backend == 'codex' and caller_session_id:
            from coordinator_core.ops.workflow_fire.headless_em_grant import registered_launch_thread
            from coordinator_core.session.caller_env import effective_caller_env

            native = registered_launch_thread(effective_caller_env(), caller_session_id, str(repo))
            if native:
                record['usage_owner_session_id'] = native
        if private_spec is not None:
            from coordinator_core.ops.workflow_fire.finalizer import snapshot_contract
            record['terminal_contract'] = snapshot_contract(repo, script)
            record.update(private_engine=private_spec, source_sha=private_sha,
                          private_engine_root=str(Path(__file__).resolve().parents[3]),
                          shared_engine_root=private_spec['shared_engine_root'])
        if not shutil.which('node'):
            raise RuntimeError('headless Workflow requires node on PATH')
        fire._write_record(record_path, record)
        artifacts.mkdir(parents=True)
        log = artifacts / 'worker.log'
        token = uuid.uuid4().hex
        stamp_fire_ownership(script, identifier, token)
        env = fire.build_fire_env()
        env[FIRE_TOKEN_ENV] = token
        env['PYTHONPATH'] = str(worker_root) + os.pathsep + env.get('PYTHONPATH', '')
        command = [sys.executable, '-P', '-m', __name__, '--record', str(record_path)]
        from coordinator_core.bash_guards._heavy_command_class import admit_before_spawn_in_process

        admission = admit_before_spawn_in_process('headless-workflow-worker', 0.5)
        if not admission.admitted:
            raise RuntimeError(f'headless Workflow worker not admitted: {admission.reason}')
        with log.open('wb') as output:
            child = _spawn_worker(command, cwd=repo, env=env, stdout=output)
        # Before the gate, a crash is execution-unknown. The worker itself marks
        # execution_started=True after observing the released gate, so the parent
        # never overwrites a quickly completed child's terminal record.
        record.update(pid=child.pid, pid_birth=_pid_birth(child.pid), state='running', started_at=time.time(), log_path=str(log), command=command,
                      execution_started=None)
        fire._write_record(record_path, record)
        (artifacts / 'start').touch()
        for _ in range(fire._LIVENESS_POLL_ATTEMPTS):
            exit_code = child.poll()
            if exit_code is not None:
                record = fire._read_record(record_path) or record
                if record.get('state') == 'running':
                    record.update(state='exited', exit_code=exit_code,
                                  startup_log_tail=fire._startup_log_tail(log))
                    fire._write_record(record_path, record)
                break
            time.sleep(fire._LIVENESS_POLL_INTERVAL_S)
        return record
    except Exception as exc:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait()
        record['execution_started'] = None if (artifacts / 'start').exists() else False
        _preflight_failed(record_path, record, exc)
        raise


def _land(result, script: str, repo: Path, *, artifacts: Path | None = None, timeout: float = 180):
    """Relay only the engine-authored terminal action, preserving its parameters."""
    if not isinstance(result, dict):
        return None
    action = result.get('next_action')
    if not isinstance(action, dict):
        return None
    operation = action.get('op')
    if operation not in ('dispatch.terminal_commit', 'dispatch.land_run'):
        return None
    params = action.get('params')
    if not isinstance(params, dict):
        raise ValueError('terminal next_action.params must be an object')
    if artifacts is None:
        raise ValueError('terminal action requires a durable artifact directory')
    return _invoke_source(operation, {**params, 'script_path': script}, str(repo), artifacts, timeout)


def _notify(record_path: Path, record: dict) -> dict:
    """Use workflow_fire's existing locked, stable-ID notification path."""
    from coordinator_core.ops.workflow_fire.notify import refresh_record

    return refresh_record(record_path, record)


def _journal_event(event):
    """Retain a fingerprint of a terminal assignment without journaling its run token."""
    from coordinator_core.ops.dispatch_emit.terminal_commit_params_channel import BLOCK_OPEN

    prompt = event.get('prompt')
    if isinstance(prompt, str) and BLOCK_OPEN in prompt:
        return {**event, 'prompt': '[terminal assignment withheld]',
                'prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest()}
    return event


def _observed_stage_actor(facts, record, request_id):
    from coordinator_core.session.native_actor import VerifiedCodexExecActor, validate_actor
    actor = VerifiedCodexExecActor(**{name: facts[name] for name in VerifiedCodexExecActor.__dataclass_fields__})
    if (actor.request_id != request_id or actor.fire_id != record['fire_id']
            or actor.parent_session_id != record['caller_session_id'] or actor.session_id == actor.parent_session_id):
        raise ValueError('native worker differs from issued fire and request')
    return validate_actor(actor, actor.session_id, record['repo_root'])


def _prepare_private_commit(private, event, script, record):
    """Bind the emitter's existing commit-agent stage, never add another commit leg."""
    from coordinator_core.ops.workflow_fire import finalizer
    from coordinator_core.ops.dispatch_emit.terminal_commit_params_channel import prompt_params
    options = event.get('options', {})
    if options.get('phase') != 'commit':
        return None
    if (options.get('label') != 'commit'
            or options.get('agentType') != 'coordinator:git-commit-agent'):
        raise finalizer.FinalizerRefused('private halted/alternate commit stage is not supported')
    params = prompt_params(event['prompt'])
    if params is None:
        raise finalizer.FinalizerRefused('commit agent lacks the emitted terminal params block')
    result = {'next_action': {'op': 'dispatch.land_run', 'params': params}}
    request_id = str(event['id'])
    params_path = finalizer.prepare(private, result, script, request_id)
    if params_path is None:
        return None
    if 'coordinator-invoke' not in private.allowed_cli:
        raise finalizer.FinalizerRefused('private finalizer requires the committed core CLI alias')
    assignment = private.roster['terminal_finalizer']
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    command = '& ' + quote(private.private_bin / 'coordinator-invoke.exe')
    command += ' --repo ' + quote(record['repo_root']) + ' ' + assignment['operation']
    command += ' --params-file ' + quote(params_path)
    prompt = ('You are the terminal finalizer for this completed Workflow. Run exactly the '
              'following existing private coordinator CLI command once. It performs the '
              'engine-authored scoped commit in the host engine. Do not run Git, edit any '
              'file or parameters, invoke installed scripts, or retry the command. Report '
              'its actual result; your response alone is never commit evidence.\n\n' + command)
    return prompt


def _private_commit_receipt(private, request_id, record):
    from coordinator_core.ops.workflow_fire import finalizer
    # The host writes this protected receipt while the verified worker is still live.
    receipt_path = private.artifact_dir / 'terminal-finalizer-receipt.json'
    receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    worker = private._worker(request_id)
    if (receipt.get('state') != 'completed' or receipt.get('fire_id') != record['fire_id']
            or receipt.get('request_id') != request_id or receipt.get('native_uuid') != worker.get('native_uuid')
            or receipt.get('vendor') != 'codex' or not isinstance(receipt.get('commits'), list)
            or not receipt['commits']):
        raise finalizer.FinalizerRefused('missing or mismatched protected host finalizer receipt')
    record['terminal_finalizer'] = {'receipt_path': str(receipt_path),
        'receipt_sha256': hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        'native_uuid': receipt['native_uuid'], 'vendor': 'codex', 'request_id': request_id}
    return receipt['result']


def _executor_root() -> Path:
    """The engine tree this worker runs from: the published stamped root `start` put first on PYTHONPATH."""
    return Path(__file__).resolve().parents[3]


_WARM_ATTEMPT = threading.local()
_NO_SPAWN_LOCK = threading.Lock()


def _install_no_spawn_seam(client) -> None:
    """Make the warm client's server spawn a recorded no-op for a headless attempt.

    `try_warm_dispatch` calls `_spawn_once` on a missing endpoint; a source-pinned fire
    uses a running server or goes cold, never starts one. Other threads pass through.
    """
    with _NO_SPAWN_LOCK:
        if getattr(client._spawn_once, '_headless_no_spawn', False):
            return
        original = client._spawn_once

        def guarded() -> None:
            if getattr(_WARM_ATTEMPT, 'missed', None) is None:
                original()
            else:
                _WARM_ATTEMPT.missed = True

        guarded._headless_no_spawn = True
        client._spawn_once = guarded


def _warm_reply(op: str, params: dict, repo: str, artifacts: Path, timeout: float | None = None) -> dict | None:
    """Ask the warm engine at this worker's published engine root; None means cold.

    The endpoint is keyed on this root's stamp token, so a server at another revision is
    never reached. Every outcome is written to `warm_route.json`: a fall-through is loud.
    `timeout` bounds a compute-only op's warm read; a delivered mutation waits its own
    mutation deadline. A None after delivery re-runs the op cold: a mutating op may repeat.
    `unstamped_root` means the worker ran from an unstamped tree: the cold re-run then answers -32005
    (ipc.UNSTAMPED_ENGINE_ROOT_ERROR) itself, with this reason on disk beside it.
    """
    root = _executor_root()
    note = {'op': op, 'engine_root': str(root)}

    def settle(route: str, reason: str | None = None, reply: dict | None = None):
        note.update(route=route, reason=reason)
        (artifacts / 'warm_route.json').write_text(json.dumps(note), encoding='utf-8')
        if reason is not None:
            print(f'[headless] {op} routed {route}: {reason}', file=sys.stderr, flush=True)
        return reply

    try:
        from coordinator_core.warm import client, skew

        if not client.is_warm_enabled():
            return settle('cold', 'warm_disabled')
        try:
            note['token'] = skew.compute_client_token(root)
        except skew.UnstampedEngineRootError:
            return settle('cold', 'unstamped_root')
        _install_no_spawn_seam(client)
        _WARM_ATTEMPT.missed = False
        try:
            reply = client.try_warm_dispatch({'jsonrpc': '2.0', 'id': 1, 'method': op, 'params': params,
                                              '_origin_worktree': repo}, read_deadline_secs=timeout)
            missed = _WARM_ATTEMPT.missed
        finally:
            _WARM_ATTEMPT.missed = None
    except Exception as exc:
        return settle('cold', f'warm_preamble_error: {type(exc).__name__}: {exc}')
    if reply is not None:
        return settle('warm', None, reply)
    return settle('cold', 'no_server' if missed else 'no_warm_reply: skew, contention or closed pipe')


def _invoke_source(op: str, params: dict, repo: str, artifacts: Path, timeout: float, *, observation: dict | None = None):
    """Invoke this executor's source; emit stays in-process to keep live tokens off disk.

    Other operations use the warm engine at its own stamp or a bounded cold process.
    """
    if not isinstance(op, str) or not isinstance(params, dict):
        raise ValueError('engineOp requires an operation name and object params')
    if set(params) == {'paramsFile'}:
        path = Path(params['paramsFile']).resolve(strict=True)
        path.relative_to(Path(repo).resolve())
        params = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(params, dict):
            raise ValueError('engineOp paramsFile must contain an object')
    artifacts.mkdir(parents=True, exist_ok=True)
    if op == 'dispatch.emit':
        import asyncio
        from coordinator_core.ipc import dispatch_message
        from coordinator_core.warm.skew import compute_client_token

        # Emission can return a live token: never serialize that reply to the workspace.
        compute_client_token(_executor_root())
        reply = asyncio.run(dispatch_message({'jsonrpc': '2.0', 'id': 1, 'method': op,
                                             'params': params, '_origin_worktree': repo}))
        if 'error' in reply:
            raise RuntimeError(json.dumps(reply['error']))
        result = reply['result']
        if isinstance(result, dict) and isinstance(result.get('fire_args'), dict):
            result = {**result, 'fire_args': {k: v for k, v in result['fire_args'].items() if k != 'runToken'}}
        return result
    request, response = artifacts / 'request.json', artifacts / 'response.json'
    request_bytes = json.dumps({'op': op, 'params': params, 'repo_root': repo}).encode("utf-8")
    request.write_bytes(request_bytes)
    if observation is None:
        reply = _warm_reply(op, params, repo, artifacts, timeout)
    else:
        # A landing-path attestation needs a child exit and must not replay a warm mutation.
        reply = None
        route_bytes = json.dumps({
            "op": op, "engine_root": str(_executor_root()), "route": "cold", "reason": "gate_evidence",
        }).encode("utf-8")
        (artifacts / "warm_route.json").write_bytes(route_bytes)
    if reply is not None:
        response.write_text(json.dumps(reply), encoding='utf-8')
    else:
        env = dict(os.environ, COORDINATOR_WARM='0')
        env['PYTHONPATH'] = str(_executor_root())
        with (artifacts / 'stdout.log').open('wb') as out, (artifacts / 'stderr.log').open('wb') as err:
            child = subprocess.run([sys.executable, '-P', '-m', 'coordinator_core.ops.workflow_fire.cold_operation', str(request), str(response)],
                cwd=repo, env=env, stdout=out, stderr=err, check=True, timeout=timeout,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        response_bytes = response.read_bytes()
        if observation is not None:
            observation.update(
                process_exit_code=child.returncode,
                artifact_sha256={
                    name: hashlib.sha256(data).hexdigest()
                    for name, data in zip(_GATE_ARTIFACTS, (request_bytes, response_bytes, route_bytes))
                },
            )
        reply = json.loads(response_bytes)
    if 'error' in reply:
        raise RuntimeError(json.dumps(reply['error']))
    return reply['result']


def _host_gate_binding(record: dict) -> dict:
    return {"run_id": record["run_id"], "fire_id": record["fire_id"],
            **{key: record[key] for key in ("script_path", "script_sha256", "caller_session_id") if key in record},
            "stage": "gate", "operation": "dispatch.ask_gate"}


def _emitted_host_root(result: dict, record: dict) -> dict:
    """Bind a host-observed emit to this fire's caller, script receipt and live run."""
    from coordinator_core.ops.dispatch_emit import run_token

    repo = Path(record['repo_root']).resolve()
    script = (repo / result['path']).resolve(strict=True)
    script.relative_to(repo)
    run_id = result['run_id']
    if script.name != run_id + '.workflow.mjs' or (record.get('run_id') and record['run_id'] != run_id):
        raise ValueError('emitted root run binding mismatch')
    source = script.read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    receipt = json.loads(script.with_name(script.name + '.emitted.json').read_text(encoding='utf-8'))
    caller = record.get('caller_session_id')
    if not caller or receipt.get('session_id') != caller:
        raise ValueError('emitted root caller binding mismatch')
    if digest != result.get('sha256') or receipt.get('sha256') != digest:
        raise ValueError('emitted root script digest mismatch')
    token = run_token.escrowed(repo, run_id)
    if not token or not run_token.admits(repo, run_id, token, str(script)):
        raise ValueError('emitted root live run binding mismatch')
    return {'run_id': run_id, 'script_path': str(script), 'script_sha256': digest,
            'caller_session_id': caller}


_GATE_ARTIFACTS = ("request", "response", "warm_route")


def _gate_login_status(repo: str, timeout: float) -> bool:
    executable = shutil.which("claude")
    if not executable:
        return False
    auth = subprocess.run(
        [executable, "auth", "status", "--json"], cwd=repo,
        capture_output=True, timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if auth.returncode:
        return False
    status = json.loads(auth.stdout)
    return isinstance(status, dict) and status.get("loggedIn") is True


def _host_gate(params: dict, record: dict, artifacts: Path, timeout: float) -> tuple[dict, str]:
    if (not record.get("run_id") or params.get("run_id") != record["run_id"]
            or params.get("require_live_token") is not True):
        raise ValueError("host gate run binding requires the current live run")
    started = time.monotonic()
    # This is provider-status evidence; dispatch.ask_gate itself uses the live run token.
    logged_in = _gate_login_status(record["repo_root"], min(timeout, 10))
    observation: dict = {}
    result = _invoke_source(
        "dispatch.ask_gate", params, record["repo_root"], artifacts,
        max(0.001, timeout - (time.monotonic() - started)), observation=observation,
    )
    if not isinstance(result, dict) or "arm" not in result or "halt" not in result:
        raise ValueError("host gate returned no typed result")
    if observation.get("process_exit_code") != 0:
        raise ValueError("host gate completed without a successful observed exit")
    receipt = {**_host_gate_binding(record), "arm": result["arm"],
               "loggedIn": logged_in, **observation,
               "result_sha256": hashlib.sha256(
                   json.dumps(result, sort_keys=True).encode("utf-8")
               ).hexdigest()}
    artifacts.mkdir(parents=True, exist_ok=True)
    path = artifacts / "consumed.json"
    _save(path, receipt)
    # Digest the bytes on disk: text-mode writes translate newlines on Windows, so a re-encoded dump differs.
    return result, hashlib.sha256(path.read_bytes()).hexdigest()


def _staged_worker_binding(result, owner):
    """Bind the staged workflow to the bytes hashed by the gate's emitter."""
    from coordinator_core.ops.dispatch_emit.nested_run_contract import staged_twin_rel

    stage = result.get("stage_reply")
    if not isinstance(stage, dict) or "twin_sha256" not in stage:
        return None
    digest = stage["twin_sha256"]
    manifest = stage.get("manifest_handle") or {}
    manifest_digest = manifest.get("sha256") if isinstance(manifest, dict) else None
    if not all(isinstance(h, str) and len(h) == 64 and all(c in "0123456789abcdef" for c in h)
               for h in (digest, manifest_digest)):
        raise ValueError("invalid staged workflow digest")
    path = staged_twin_rel(owner["run_id"], manifest_digest)
    if stage.get("twin_path") != path:
        raise ValueError("staged workflow path differs from issued run and manifest")
    return {"depth": 1, "script_path": str(Path(owner["repo_root"]) / path), "script_sha256": digest}


def _pending_staged_twin(result, staged_binding, repo_root: Path, run_id: str) -> str | None:
    """The host-issued twin digest a `stage_pending` result hands to, else None (unbound: the fire halts)."""
    from coordinator_core.ops.dispatch_emit.nested_run_contract import staged_twin_rel

    pending = result.get("stage_pending") if isinstance(result, dict) else None
    handle = pending.get("handle") if isinstance(pending, dict) else None
    if staged_binding is None or not isinstance(handle, dict) or not isinstance(handle.get("sha256"), str):
        return None
    rel = staged_twin_rel(run_id, handle["sha256"])
    if pending.get("twin_path", rel) != rel or staged_binding["script_path"] != str(repo_root / rel):
        return None
    return staged_binding["script_sha256"]


def _is_staged_commit_binding(binding, staged_binding, root_binding=None) -> bool:
    """A commit worker names the gate-issued staged script, or the depth-0 root that issued it."""
    if binding == staged_binding:
        return True
    if not isinstance(binding, dict) or binding.get("depth") != 0:
        return False
    named = {k: v for k, v in binding.items() if k != "depth"}
    return (named == {k: v for k, v in staged_binding.items() if k != "depth"}
            or (root_binding is not None and named == root_binding))


def _read_host_gate_receipt(path: Path, record: dict, digest: str | None) -> dict:
    if digest is None or not path.is_file():
        raise ValueError("host gate receipt missing")
    raw = path.read_bytes()
    receipt = json.loads(raw.decode("utf-8"))
    if (not isinstance(receipt, dict)
            or any(receipt.get(key) != value for key, value in _host_gate_binding(record).items())):
        raise ValueError("host gate receipt binding mismatch")
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("host gate receipt content mismatch")
    artifacts = {"consumed": receipt}
    for name in _GATE_ARTIFACTS:
        data = path.with_name(f"{name}.json").read_bytes()
        if hashlib.sha256(data).hexdigest() != receipt["artifact_sha256"][name]:
            raise ValueError(f"host gate {name} artifact content mismatch")
        artifacts[name] = json.loads(data)
    result = artifacts["response"].get("result")
    if hashlib.sha256(json.dumps(result, sort_keys=True).encode("utf-8")).hexdigest() != receipt["result_sha256"]:
        raise ValueError("host gate response does not match the consumed result")
    return {**receipt, "artifacts": artifacts}


def _run_pinned_test_leg(
    repo: Path, output: Path, artifacts: Path, targets: list[str],
    base: str, timeout: float, baseline_output: Path | None = None,
    *, measured_sha: str | None = None, run_id: str | None = None,
) -> tuple[int, dict]:
    artifacts.mkdir(parents=True, exist_ok=True)
    temporary_output = artifacts / "test-leg-result.json"
    temporary_export = artifacts / "test-leg-baseline.json"
    engine = str(_executor_root())
    bootstrap = ("import runpy,sys;sys.path.insert(0,sys.argv.pop(1));"
                 "runpy.run_module('coordinator_core.ops.test_leg_run',run_name='__main__',alter_sys=True)")
    argv = [sys.executable, "-c", bootstrap, engine]
    if base:
        argv += ["--base-sha", base]
    if measured_sha:
        argv += ["--run-id", run_id, "--measured"]
    if baseline_output is not None:
        if not measured_sha:
            argv += ["--head-sha", "HEAD"]
        argv += ["--base-export-out", str(temporary_export)]
    argv += ["--out", str(temporary_output), "--", *targets]
    _save(artifacts / "test-leg-start.json", {"argv": argv, "cwd": str(repo), "engine_root": engine})
    with (artifacts / "test-leg.stdout").open("wb") as out, (artifacts / "test-leg.stderr").open("wb") as err:
        proc = subprocess.run(argv, cwd=repo, env=dict(os.environ, PYTHONPATH=engine),
                              stdout=out, stderr=err, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if not temporary_output.is_file():
        raise ValueError(f"test leg exited {proc.returncode} without a receipt")
    measured = json.loads(temporary_output.read_text(encoding="utf-8"))
    output.parent.mkdir(parents=True, exist_ok=True)
    _save(output, measured)
    if baseline_output is not None and temporary_export.is_file():
        pending = baseline_output.with_name(f"{baseline_output.name}.{uuid.uuid4().hex}.tmp")
        shutil.copyfile(temporary_export, pending)
        os.replace(pending, baseline_output)
    return proc.returncode, measured


class RunBaseMismatch(ValueError):
    """Typed refusal: the reverify base is none of the run's pinned bases."""

    kind = "run_base_mismatch"

    def __init__(self, base: str, pinned: list[str]):
        self.base, self.pinned = base, pinned
        super().__init__(f"{self.kind}: host Verify tests base {base!r} is not the run's pinned base {pinned!r}")


def _check_run_base(root: Path, run_dir: Path, run_id: str, manifest: dict, base: str) -> None:
    """Refuse only a base that is none of the run's own pinned bases.

    Invariant: manifest `head_sha` is HEAD at the (re)stage write, so a resumed run, or a sibling's halt
    commit landing before the restage, moves it off the run's base. The pinned bases are the write-once
    `run-base` file and the halted record's `run_base_sha`; a sibling commit must never refuse the leg."""
    from coordinator_core.ops.dispatch_emit import ask_fanout_contract

    pinned = ask_fanout_contract.pinned_run_base(root, run_id, manifest)
    if pinned and base != pinned:
        raise RunBaseMismatch(base, [pinned])


def _record_measured_rev(manifest: dict, root: Path, run_id: str, artifacts: Path, timeout: float) -> tuple[str | None, str | None]:
    """Return the run's measured candidate SHA and any diagnostic from measuring it."""
    declared = manifest.get("review_declared_paths")
    if not isinstance(declared, list) or not declared:
        return None, None
    try:
        reply = _invoke_source("dispatch.measured_rev", {"run_id": run_id, "paths": [str(p) for p in declared]},
                               str(root), artifacts / "measured-rev", max(.1, timeout))
    except Exception as exc:  # noqa: BLE001 -- a failed measurement is recorded on the result, never raised past the leg
        return None, f"{type(exc).__name__}: {exc}"
    if isinstance(reply, dict) and reply.get("error"):
        return None, str(reply["error"])
    sha = reply.get("sha") if isinstance(reply, dict) else None
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        return None, "measurement returned no candidate SHA"
    return sha, None


_EVICTED_SKIP = "evicted by this run's halt (parked out of the tree); not run"


def _verify_test_leg(params: dict, repo: str, artifacts: Path, timeout: float, *, run_id: str) -> dict:
    root = Path(repo).resolve()
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id):
        raise ValueError("host Verify tests requires a valid active run ID")
    from coordinator_core.session.record_homes import home_dir
    run_dir = Path(home_dir(str(root), "warp")) / run_id
    output = run_dir / "test-leg.json"
    if (root / params["out_path"]).resolve() != output or output.resolve() != output:
        raise ValueError("host Verify tests output does not belong to the active run")
    targets = params["targets"]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(targets, list) or targets != manifest.get("test_targets"):
        raise ValueError("host Verify tests targets differ from the active run manifest")
    for target in targets:
        if not isinstance(target, str) or not target or target.startswith(("-", "@")) or any(c in target for c in "\r\n"):
            raise ValueError("host Verify tests requires repository-relative pytest selectors")
        path = target.split("::", 1)[0]
        if Path(path).is_absolute() or not (root / path).resolve().is_relative_to(root):
            raise ValueError(f"host Verify tests target is outside the repository: {path!r}")
    base = params.get("base", "")
    _check_run_base(root, run_dir, run_id, manifest, base)
    measured_sha, measured_rev_error = _record_measured_rev(manifest, root, run_id, artifacts, timeout)
    # Invariant: an evicted path is parked out of the tree, so naming it makes pytest exit 4 with no tests.
    from coordinator_core.ops.dispatch_emit import halt_tree_evict

    evicted = set(halt_tree_evict._load(run_dir))
    dropped = [t for t in targets if t.split("::", 1)[0] in evicted]
    targets = [t for t in targets if t not in dropped]
    if dropped and not targets:
        measured = {"status": "pass-with-skips", "tests_run": 0, "tests_failed": 0,
                    "summary": "every target was evicted by this run's halt; nothing to run",
                    "skipped": [{"test": t, "reason": _EVICTED_SKIP} for t in dropped]}
        from coordinator_core.ops.dispatch_emit import test_leg_freshness

        at, head = test_leg_freshness.started_stamp(root, None)
        measured = test_leg_freshness.stamp(measured, measured_at=at, head_sha=head)
        _save(output, measured)
        return {**measured, "evicted_targets": dropped, "sidecar_path": output.relative_to(root).as_posix(),
                **({"measured_rev_error": measured_rev_error} if measured_rev_error else {})}
    if measured_rev_error:
        measured = {"status": "error", "tests_run": 0, "tests_failed": 0,
                    "measured_rev_error": measured_rev_error, "summary": measured_rev_error}
        _save(output, measured)
        return {**measured, "sidecar_path": output.relative_to(root).as_posix()}
    pin = {"measured_sha": measured_sha, "run_id": run_id} if measured_sha else {}
    exit_code, measured = _run_pinned_test_leg(root, output, artifacts, targets, base, timeout, **pin)
    if measured_sha and (measured.get("own_tree") or {}).get("at") != measured_sha:
        measured.update(status="error", summary="test runner did not verify the measured revision")
        _save(output, measured)
    fields = ("status", "tests_run", "tests_failed", "baseline", "baseline_method", "build_clean",
              "skipped", "summary", "importer_overrun", "tripwires_not_run", "zero_run_cause",
              "measured_at", "head_sha")
    result = {key: measured[key] for key in fields if key in measured}
    result["sidecar_path"] = output.relative_to(root).as_posix()
    # Invariant: a declared target that did not run is a skip, never a silent pass: 19 green tests must not
    # read as covering a regression test that was parked or absent.
    unrun = [*dropped, *(t for t in measured.get("missing_targets", []) if t not in dropped)]
    if unrun:
        result["skipped"] = [
            *result.get("skipped", []),
            *({"test": t, "reason": _EVICTED_SKIP if t in dropped else "target missing from the tree; not run"}
              for t in unrun),
        ]
        if dropped:
            result["evicted_targets"] = dropped
        if result["status"] == "pass":
            result["status"] = "pass-with-skips"
    infra = _test_infra_failure(root, artifacts, targets, measured)
    if infra:
        result["test_infra_failure"] = infra
        result["status"] = "error"
        result["summary"] = infra["summary"]
    if exit_code:
        result["status"] = "error"
    return result


_PYTEST_USAGE, _PYTEST_NO_TESTS = 4, 5


def _test_infra_failure(root: Path, artifacts: Path, targets: list[str], measured: dict) -> dict | None:
    """A typed `test_infra_failure` when pytest exited 4 (usage error, e.g. a named target is missing) or
    5 (nothing collected) on the explicitly named `targets`; None otherwise. Invariant: an explicit target
    that ran nothing is never a pass or a vacuous verdict; the failure names the argv and the bad targets."""
    phases = measured.get("pytest_phases") or []
    codes = [p.get("exit_code") for p in phases if isinstance(p, dict)] or [measured.get("pytest_exit_code")]
    bad_code = next((c for c in codes if c in (_PYTEST_USAGE, _PYTEST_NO_TESTS)), None)
    if bad_code is None and measured.get("missing_targets") and not measured.get("tests_run"):
        bad_code = _PYTEST_NO_TESTS  # every target absent: the leg stamps it without spawning pytest
    if bad_code is None or measured.get("tests_run"):
        return None
    absent = [t for t in targets if not (root / t.split("::", 1)[0]).exists()]
    bad = list(dict.fromkeys([*measured.get("missing_targets", []), *absent])) or list(targets)
    try:
        argv = json.loads((artifacts / "test-leg-start.json").read_text(encoding="utf-8")).get("argv")
    except (OSError, ValueError):
        argv = None
    meaning = "usage error" if bad_code == _PYTEST_USAGE else "no tests collected"
    return {"kind": "test_infra_failure", "pytest_exit_code": bad_code, "argv": argv, "bad_targets": bad,
            "summary": f"test_infra_failure: pytest exit {bad_code} ({meaning}) for target(s): {', '.join(bad)}"}


def _declared_code_paths(repo: str, run_key: str) -> list[str]:
    """The run manifest's `review_code_paths`, [] when the manifest is absent, unreadable or declares none."""
    from coordinator_core.session.record_homes import home_dir
    try:
        data = json.loads((Path(home_dir(str(repo), "warp")) / run_key / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    paths = data.get("review_code_paths") if isinstance(data, dict) else None
    return [p for p in paths if isinstance(p, str)] if isinstance(paths, list) else []


def _reverify_test_leg(params: dict, repo: str, artifacts: Path, timeout: float, *, run_id: str | None = None) -> dict | None:
    """Measure the inventory before the review worker can interpret it."""
    deadline = time.monotonic() + timeout
    prefixes = params["slice_prefixes"]
    run_key = params["run_key"]
    reply = _invoke_source("review.reverify", {
        "base": params["base"], "slice_prefixes": prefixes,
        "session_id": params.get("session_id", ""), "run_key": run_key,
    }, repo, artifacts / "inventories", max(.1, deadline - time.monotonic()))
    inventories = reply.get("inventories")
    if not isinstance(inventories, list):
        inventories = [reply if reply.get("error") else {"error": "review.reverify returned no inventories"}]
    errors = [inventory["error"] for inventory in inventories if inventory.get("error")]
    edited = list(dict.fromkeys(
        path for inventory in inventories for key in ("edited", "created") for path in inventory.get(key, [])
    ))
    owned = list(dict.fromkeys(path for inventory in inventories for path in inventory.get("owned", [])))
    tripwires = list(dict.fromkeys(path for inventory in inventories for path in inventory.get("tripwires", [])))
    declared_code = _declared_code_paths(repo, run_key)
    if declared_code and not errors and not edited:
        return {"edited": [], "failing": [], "failing_detail": [], "reverted": [], "status_before": "indeterminate",
                "reverify_error": f"review_scope_empty: no reviewed edit in the slice; declared code paths: {', '.join(declared_code)}",
                "review_scope_empty": True, "declared_code_paths": declared_code, "tests_run": 0, "not_run": []}
    scope = params.get("verify_scope_targets", {})
    scoped = [test for path in edited for test in scope.get(path, [])]
    from coordinator_core.ops.reverify_scope import TRIPWIRE_BOUND, _is_js_test_file

    # Every edited path with a declared scope: the declared targets replace the whole owned inventory.
    # A partly scoped edit set keeps the inventory, which is the only cover for the unscoped paths.
    owned = scoped if scoped and all(scope.get(path) for path in edited) else owned + scoped
    owned = list(dict.fromkeys(owned))
    tripwires = [target for target in tripwires if target not in owned]
    targets = owned + tripwires[:TRIPWIRE_BOUND]

    if not errors and (
        any(_is_js_test_file(target) for target in targets)
        or any(Path(path).suffix == ".js" for path in edited)
    ):
        return None  # Mixed-language reverify retains its existing Node test route.
    root = Path(repo).resolve()
    selected = []
    for target in targets:
        path = (root / target).resolve()
        if (not Path(target).is_absolute() and not target.startswith("-") and path.is_relative_to(root)
                and path.is_file() and path.name.startswith("test_") and path.suffix == ".py"):
            selected.append(target)
    receipt = {"edited": edited, "failing": [], "failing_detail": [], "reverted": [],
               "status_before": "indeterminate" if errors or edited else "not_run",
               "reverify_error": "; ".join(errors) if errors else None, "tests_run": 0,
               "not_run": list(dict.fromkeys(
                   [target for target in targets if target not in selected] + tripwires[TRIPWIRE_BOUND:]
                   + [target for inventory in inventories for target in inventory.get("tripwires_not_selected", []) if target not in selected]
               ))}
    if errors or not edited or not selected:
        if edited and not errors:
            receipt["reverify_error"] = "reverify inventory has edits but no runnable Python test target"
        return receipt

    from coordinator_core.ops.review_mint.execute_review import _base_export_file, _test_leg_file

    output_path = _test_leg_file(run_key, "", prefixes[-1])
    export_path = root / _base_export_file(run_key, "", prefixes[-1])
    pin = {}
    try:
        if run_id:
            from coordinator_core.session.record_homes import home_dir
            if run_id != run_key or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id):
                raise ValueError("review inventory belongs to a different run")
            run_dir = Path(home_dir(str(root), "warp")) / run_id
            manifest = json.loads((run_dir / "manifest.json").read_bytes())
            _check_run_base(root, run_dir, run_id, manifest, params["base"])
            sha, error = _record_measured_rev(manifest, root, run_id, artifacts, deadline - time.monotonic())
            if not sha:
                raise ValueError(error or "review manifest has no measured candidate")
            pin = {"measured_sha": sha, "run_id": run_id}
        exit_code, measured = _run_pinned_test_leg(
            root, root / output_path, artifacts, selected, params["base"],
            max(.1, deadline - time.monotonic()), export_path, **pin,
        )
        if pin and (measured.get("own_tree") or {}).get("at") != pin["measured_sha"]:
            raise ValueError("review runner did not verify the measured revision")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {**receipt, "reverify_error": f"host reverify test leg failed: {exc}"}
    for key in ("failing", "failing_detail", "passed_alone", "passed_at_base", "pre_existing", "tests_run"):
        if key in measured:
            receipt[key] = measured[key]
    receipt["not_run"] = list(dict.fromkeys(
        receipt["not_run"] + measured.get("missing_targets", []) + measured.get("tripwires_not_run", [])
        + measured.get("not_a_test", [])
    ))
    if not exit_code and receipt["tests_run"] and measured.get("status") != "error":
        receipt["status_before"] = "pass" if measured.get("status_after") == "pass" else "fail"
    elif receipt["status_before"] == "indeterminate":
        receipt["reverify_error"] = f"host test leg returned no verified verdict (exit {exit_code})"
    receipt["sidecar_path"] = output_path
    return receipt


def _result_failure(value, *, landing: bool = False) -> str | None:
    """Recognize engine failure envelopes, rather than grep arbitrary task data.

    A Node exit of zero proves that the script returned, not that an engine ask
    completed. Only known envelope fields/wrappers are inspected; a normal
    schema result can contain nested status/error fields without failing a run.
    """
    if not isinstance(value, dict):
        return None
    if value.get('refused') or value.get('halted'):
        return 'engine refused or halted: ' + str(value.get('refused') or value.get('halted'))
    if value.get('run_id') and value.get('halted_by'):
        halted = value['halted_by']
        reason = halted.get('reason') or halted.get('halted') if isinstance(halted, dict) else halted
        return 'engine wake digest halted: ' + str(reason)
    action = value.get('next_action')
    if value.get('run_id') and isinstance(action, dict) and action.get('kind') in ('refire', 'revise_and_refire'):
        return 'engine wake digest requires ' + action['kind']
    status = value.get('status')
    if status in ('emit_refused', 'failed', 'halted', 'blocked', 'refused', 'error',
                  'stage_pending', 'minted', 'sized', 'planned', 'not_committed', 'not_started'):
        return f'engine returned {status}: {value.get("error") or value.get("reason") or "not completed"}'
    if value.get('stage_pending'):
        return 'engine returned an unresolved stage_pending handoff'
    if value.get('criterion_status') in ('not_met', 'indeterminate', 'not_run', 'unstructured'):
        return 'engine criterion is ' + value['criterion_status']
    if value.get('plan_status') in ('not-stamped', 'minted', 'planned', 'open', 'blocked', 'partial', 'in_progress'):
        return 'engine plan is ' + value['plan_status'] + ': ' + str(value.get('plan_status_reason', 'not implemented'))
    if value.get('schema') == 'wake-digest':
        if value.get('outcome') in ('incomplete', 'halted') or value.get('completed') is False:
            return 'engine wake digest is ' + str(value.get('outcome', 'incomplete'))
    # A bare error envelope is failure. Do not mistake an ordinary schema object
    # with its own data/error fields (or a successful ok flag) for that envelope.
    if value.get('error') and (landing or (value.get('ok') is not True and (
            'run_id' in value or set(value) <= {
                'error', 'reason', 'detail', 'code', 'status', 'path', 'script_path'}))):
        return 'engine error: ' + str(value['error'])
    if landing and value.get('committed') is False and not (
            value.get('nothing_to_commit') or value.get('already_landed')):
        return 'terminal action did not commit'
    # Fan launchers wrap child digests; these are declared engine wrappers, not
    # arbitrary recursive traversal through a model's structured output.
    if 'parent_id' in value and isinstance(value.get('runs'), list):
        for child in value['runs']:
            failed = _result_failure(child)
            if failed:
                return failed
    if value.get('run_id') and value.get('status') == 'ok' and 'result' in value:
        child = value['result']
        failed = _result_failure(child)
        if failed:
            return failed
        action = child.get('next_action') if isinstance(child, dict) else None
        if isinstance(action, dict) and action.get('op') in ('dispatch.terminal_commit', 'dispatch.land_run'):
            # The wrapper does not carry the child's script identity. Using the
            # parent's script here would corrupt the authored terminal action.
            return 'fan child terminal action has not been relayed for ' + str(value['run_id'])
    return None


def _halt_failed_fire(record: dict, result, error: str) -> None:
    """Leave a halt record for every run this failed fire drove, so the failure is counted and a resumed record is not stranded."""
    from coordinator_core.ops.dispatch_emit import failed_fire_halt

    try:
        ids = failed_fire_halt.failed_run_ids(result, record.get('script_path'))
        commits: dict = {}
        cause = failed_fire_halt.stage_pending_cause(result)
        named = {'halted': failed_fire_halt.HALT_STAGE_PENDING_UNRESOLVED} if cause else {}
        record['halted_runs'] = failed_fire_halt.halt_failed_fire(
            Path(record['repo_root']), ids, f'{error}: {cause}' if cause else error, commits, **named)
        record['halt_commits'] = {k: {f: v.get(f) for f in ('committed', 'sha', 'error') if f in v} for k, v in commits.items()}
    except Exception as exc:
        record['halt_error'] = f'{type(exc).__name__}: {exc}'


def _cancelled_run_refusal(record: dict) -> str | None:
    """The `workflow.cancel` refusal text when this fire's run carries a live cancelled marker, else None.
    One marker read before each agent dispatch: the backstop for a fire `workflow.cancel` could not reach."""
    run_id = record.get('run_id')
    if not run_id:
        return None
    try:
        from coordinator_core.lifecycle import main_worktree_root
        from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
        from coordinator_core.ops.dispatch_emit.run_cancel import cancelled_refusal

        return cancelled_refusal(main_worktree_root(Path(record['repo_root'])) / RECORD_DIR_ROOT / run_id)
    except Exception:  # noqa: BLE001 -- an unreadable marker never stops a run on its own
        return None


def _preflight_failed(record_path: Path, record: dict, exc: Exception) -> dict:
    """Release a failed reservation and retain the error before a driver starts."""
    record.update(state='exited', exit_code=1, outcome='failed',
                  error=f'{type(exc).__name__}: {exc}', ended_at=time.time(),
                  status_checked_at=time.time(), _annotation_version=fire._ANNOTATION_VERSION,
                  outcome_basis='headless preflight failed before execution')
    record['notify_owner_released'] = True
    _save(record_path, record)
    if record.get('notify_to'):
        try:
            record.update(_notify(record_path, record))
        except Exception as notify_exc:
            record['notify_error'] = f'{type(notify_exc).__name__}: {notify_exc}'
    return record


_GATE_RELAY_GUIDANCE = (
    "\n\nFor dispatch.ask_gate, use the JSON-RPC result on stdout when the command exits 0. "
    "Separate stderr diagnostics do not turn a successful gate result into a refusal. "
    "The shell tool may display both streams together. Preserve any halt in the result itself. "
    "Report a command failure for a nonzero exit, a JSON-RPC error, or a missing or invalid result."
)
_GATE_COMMAND = re.compile(r"(?<![\w.-])dispatch\.ask_gate(?![\w.-])")
_GATE_OUTPUT_LIMIT = 32_768
_GATE_TRACE_LIMIT = 256 * 1024
_GATE_EVENTS_LIMIT = 4


def _journal_gate_commands(agent_dir: Path, request_id: str, journal) -> None:
    """Copy candidate gate diagnostics from Codex's combined stream, without treating them as receipts."""
    try:
        with (agent_dir / "stdout.jsonl").open("rb") as stream:
            remaining, copied = _GATE_TRACE_LIMIT, 0
            while remaining and copied < _GATE_EVENTS_LIMIT:
                line = stream.readline(remaining)
                remaining -= len(line)
                if not line or not line.endswith(b"\n"):
                    break
                try:
                    event = json.loads(line.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if not isinstance(event, dict) or event.get("type") != "item.completed":
                    continue
                item = event.get("item")
                if not isinstance(item, dict) or item.get("type") != "command_execution":
                    continue
                command = item.get("command")
                output = item.get("aggregated_output")
                if (not isinstance(command, str) or "coordinator-invoke" not in command
                        or not _GATE_COMMAND.search(command) or not isinstance(output, str)):
                    continue
                journal({
                    "type": "gate-command-output", "id": request_id, "op": "dispatch.ask_gate",
                    "tool_item_id": item.get("id"), "exit_code": item.get("exit_code"),
                    "combined_output": output[:_GATE_OUTPUT_LIMIT], "truncated": len(output) > _GATE_OUTPUT_LIMIT,
                    "verified": False,
                })
                copied += 1
    except OSError:
        # Evidence collection must preserve the original worker result or exception.
        pass


def execute(record_path: Path, *, agent_runner=None, terminal_runner=_land,
            private_engine_factory=None) -> dict:
    from coordinator_core.codex_runner.workflow import run_agent
    runner = agent_runner or run_agent
    record = json.loads(record_path.read_text(encoding='utf-8'))
    artifacts = Path(record['artifact_dir'])
    try:
        with (artifacts / 'execution.guard').open('x') as guard:
            guard.write(str(os.getpid()))
    except FileExistsError:
        raise RuntimeError('this workflow executor has already started; inspect its durable record')
    deadline = time.monotonic() + record.get('timeout_s', DEFAULT_TIMEOUT_S)
    try:
        while not (artifacts / 'start').exists():
            if time.monotonic() > deadline:
                raise TimeoutError('parent did not release workflow start gate')
            time.sleep(.05)
        record = json.loads(record_path.read_text(encoding='utf-8'))
        record['execution_started'] = True
        _save(record_path, record)
    except Exception as exc:
        return _preflight_failed(record_path, record, exc)
    journal_lock, input_lock = threading.Lock(), threading.Lock()
    def journal(event) -> None:
        with journal_lock, (artifacts / 'journal.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'at': time.time(), **_journal_event(event)}) + '\n')
    process = None
    timer = None
    cancelled: list = []
    result = None
    observed_landing = None
    terminal = False
    error = None
    private = None
    try:
        script = Path(record['script_path'])
        if hashlib.sha256(script.read_bytes()).hexdigest() != record['script_sha256']:
            raise ValueError('emitted workflow changed after fire')
        if record.get('private_engine') is not None:
            from coordinator_core.ops.workflow_fire.private_engine import OwnedPrivateEngine

            spec = validate_private_engine_config(record['private_engine'], backend=record['backend'])
            from coordinator_core.ops.workflow_fire.finalizer import verify_contract
            contract = record.get('terminal_contract')
            if not isinstance(contract, dict):
                raise ValueError('private Workflow requires its pre-worker terminal contract; re-submit through workflow.fire')
            verify_contract(record['repo_root'], contract)
            factory = private_engine_factory or OwnedPrivateEngine
            private = factory(record_path, record, door_image=spec['door_image'],
                              door_provenance=spec['door_provenance'])
            private.roster['terminal_contract'] = contract
            record['private_engine_resources'] = private.resource_plan()
            _save(record_path, record)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('workflow deadline expired before private engine startup')
            record['private_engine_ready'] = private.start(timeout_s=min(10, remaining))
            _save(record_path, record)
            journal({'type': 'private-engine-ready', 'binding': record['private_engine_ready']})
        kwargs = {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)} if os.name == 'nt' else {}
        args_path = artifacts / 'args.json'
        args_path.write_text(json.dumps(record.get('args', {})), encoding='utf-8')
        with (artifacts / 'runtime.stderr').open('w', encoding='utf-8') as stderr:
            process = subprocess.Popen([shutil.which('node'), str(Path(__file__).with_name('runtime.mjs')), str(script), str(args_path)],
                                       cwd=record['repo_root'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=stderr, text=True, encoding='utf-8', **kwargs)
            timer = threading.Timer(max(.1, deadline-time.monotonic()), lambda: process.kill())
            timer.daemon = True
            timer.start()
            gate_lock = threading.Lock()
            gate_started = False
            gate_receipt: tuple[Path, str] | None = None
            emitted_root = None
            sizing_actor = None
            staged_binding = None
            staged_contract = None

            def check_binding(event, owner):
                expected = {key: owner[key] for key in ('script_path', 'script_sha256')}
                if event.get('binding') != {'depth': 0, **expected}:
                    raise ValueError('host operation requires its bound root workflow')

            def respond(event) -> None:
                nonlocal gate_started, gate_receipt, emitted_root, staged_binding, staged_contract, observed_landing, sizing_actor
                reply = {'id': event['id']}
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('workflow deadline expired before queued agent dispatch')
                    if process.poll() is not None:
                        raise RuntimeError('workflow runtime exited before queued agent dispatch')
                    if event['type'] == 'invoke':
                        operation_dir = artifacts / 'operations' / str(event['id'])
                        owner = {**record, **record.get('emitted_root', {})}
                        if event['op'] == 'workflow.host_start':
                            check_binding(event, record)
                            if emitted_root is None or record.get('emitted_root'):
                                raise ValueError('no unstarted host-emitted root')
                            path = (Path(record['repo_root']) / event['params']['script_path']).resolve(strict=True)
                            if str(path) != emitted_root['script_path']:
                                raise ValueError('emitted root path binding mismatch')
                            source = path.read_bytes()
                            if hashlib.sha256(source).hexdigest() != emitted_root['script_sha256']:
                                raise ValueError('emitted root changed before host start')
                            _emitted_host_root({'path': str(path), 'run_id': emitted_root['run_id'],
                                                'sha256': emitted_root['script_sha256']}, record)
                            record['emitted_root'] = emitted_root
                            _save(record_path, record)
                            reply['result'] = {**emitted_root, 'source': source.decode('utf-8'),
                                               'args': {'repoRoot': record['repo_root']}}
                        elif event['op'] == 'gate.host_run':
                            check_binding(event, owner)
                            with gate_lock:
                                if gate_started:
                                    raise ValueError('host gate already started for this fire')
                                gate_started = True
                            result, digest = _host_gate(event['params'], owner, operation_dir, remaining)
                            with gate_lock:
                                staged_binding = _staged_worker_binding(result, owner)
                                if staged_binding is not None:
                                    from coordinator_core.ops.workflow_fire import finalizer
                                    stage = result['stage_reply']
                                    contract = finalizer.snapshot_contract(
                                        owner['repo_root'], Path(staged_binding['script_path']))
                                    files = contract['files']
                                    manifest = stage['manifest_handle']
                                    if (files.get(stage['twin_path']) != staged_binding['script_sha256']
                                            or files.get(manifest['path']) != manifest['sha256']):
                                        raise ValueError('terminal contract differs from host-issued workflow or manifest')
                                    staged_contract = contract
                                gate_receipt = operation_dir / "consumed.json", digest
                            reply['result'] = result
                        elif event['op'] == 'gate.host_receipt':
                            check_binding(event, owner)
                            with gate_lock:
                                if gate_receipt is None:
                                    raise ValueError('host gate receipt missing')
                                path, digest = gate_receipt
                            reply['result'] = _read_host_gate_receipt(path, owner, digest)
                        elif event['op'] == 'workflow.staged_late_rows':
                            params = event['params']
                            if not staged_binding or event.get('binding') != staged_binding or params.get('run_id') != owner.get('run_id'):
                                raise ValueError('late requirement read differs from the host-issued staged run')
                            reply['result'] = _invoke_source('workflow.late_rows', params, record['repo_root'], operation_dir, remaining)
                        elif event['op'] == 'review.reverify.host_test_leg':
                            reply['result'] = _reverify_test_leg(event['params'], record['repo_root'], operation_dir, remaining, run_id=owner.get('run_id'))
                        elif event['op'] == 'verify.host_test_leg':
                            reply['result'] = _verify_test_leg(event['params'], record['repo_root'],
                                                               operation_dir, remaining, run_id=record.get('run_id'))
                        else:
                            reply['result'] = _invoke_source(event['op'], event['params'], record['repo_root'],
                                                            operation_dir, remaining)
                            if event['op'] == 'dispatch.emit' and event.get('binding', {}).get('depth') == 0:
                                check_binding(event, record)
                                result = reply['result']
                                if isinstance(result, dict) and result.get('path') and result.get('sha256'):
                                    emitted_root = _emitted_host_root(result, record)
                                    reply['result'] = {**result, 'fire_args': {
                                        k: v for k, v in result.get('fire_args', {}).items() if k != 'runToken'}}
                    else:
                        refusal = _cancelled_run_refusal(record)
                        if refusal:
                            cancelled.append(refusal)
                            process.kill()
                            raise RuntimeError(refusal)
                        request_id = str(event['id'])
                        options = event.get('options', {})
                        if (staged_binding is not None and options.get('phase') == 'commit'
                                and not _is_staged_commit_binding(
                                    event.get('binding'), staged_binding,
                                    {key: record[key] for key in ('script_path', 'script_sha256')})):
                            raise ValueError('commit worker differs from host-issued staged workflow')
                        if staged_binding is not None and options.get('phase') == 'commit':
                            from coordinator_core.ops.workflow_fire import finalizer
                            if staged_contract is None:
                                raise ValueError('missing host-issued terminal contract')
                            finalizer.verify_contract(record['repo_root'], staged_contract)
                        agent_dir = artifacts / 'agents' / request_id
                        gate_relay = (record['backend'] == 'codex' and options.get('phase') == 'gate'
                                      and options.get('label') == 'gate')
                        extra = {}
                        spawned = False
                        commit_prompt = None
                        native_landing = None
                        reply_stream = None
                        if private is not None:
                            commit_prompt = _prepare_private_commit(private, event, script, record)
                            def observed_spawn(pid) -> None:
                                nonlocal spawned
                                private.on_spawn(pid, request_id)
                                spawned = True
                            extra = {
                                'on_spawn': observed_spawn,
                                'on_native_thread': lambda facts: private.on_native_thread(request_id, facts),
                                'private_cli': {'env': private.worker_env(),
                                    'private_bin': str(private.private_bin),
                                    'allowed_cli': list(private.allowed_cli),
                                    'fire_id': record['fire_id'], 'request_id': request_id,
                                    'bindings_path': str(private.bindings_path)},
                            }
                        elif (sys.platform == 'darwin' and record['backend'] == 'codex'
                              and 'COORDINATOR_CODEX_WORKER_POLICY_FILE' in os.environ
                              and (options.get('label'), options.get('phase'), options.get('agentType'))
                              == ('size', 'size', 'coordinator:sizing-scout')):
                            owner = {**record, **record.get('emitted_root', {})}
                            check_binding(event, owner)
                            def observe_size(facts):
                                nonlocal sizing_actor
                                actor = _observed_stage_actor(facts, record, request_id)
                                sizing_actor = (owner['run_id'], actor)
                            extra['on_native_thread'] = observe_size
                        elif (sys.platform == 'darwin' and record['backend'] == 'codex'
                              and staged_contract is not None and staged_contract['has_request']
                              and options.get('phase') == 'commit' and options.get('label') == 'commit'
                              and options.get('agentType') == 'coordinator:git-commit-agent'
                              and 'COORDINATOR_CODEX_WORKER_POLICY_FILE' in os.environ):
                            from coordinator_core.ops.workflow_fire.finalizer import ObservedLanding
                            from coordinator_core.ops.dispatch_emit.terminal_commit_params_channel import prompt_params
                            from coordinator_core.ops.dispatch_emit.land_run_contract import mode_of
                            issued = prompt_params(event['prompt'])
                            if issued is not None and mode_of(issued) == 'product':
                                reply_stream = tempfile.NamedTemporaryFile(prefix='coordinator-landing-', suffix='.json')
                                try:
                                    native_landing = ObservedLanding(event, record, staged_contract, reply_stream, sizing_actor=sizing_actor)
                                except BaseException:
                                    reply_stream.close()
                                    raise
                                commit_prompt = ('Run this exact command once to request the host-issued product landing. '
                                                 'It waits for the reviewed commit and prints the host reply. Report that reply.\n'
                                                 + native_landing.command)
                            verified_actor = None
                            def observe_commit_worker(facts):
                                nonlocal verified_actor
                                actor = verified_actor = _observed_stage_actor(facts, record, request_id)
                                journal({'type': 'commit-worker-observed', 'id': request_id, 'fire_id': actor.fire_id,
                                         'session_id': actor.session_id, 'pid': actor.pid,
                                         'start_epoch': actor.start_epoch, 'exe_sha256': actor.exe_sha256})
                            extra['on_native_thread'] = observe_commit_worker
                            if native_landing is not None:
                                def acknowledge_commit(sid, command):
                                    if verified_actor is None or verified_actor.session_id != sid:
                                        raise ValueError('commit request lacks observed native session')
                                    native_landing.acknowledge(verified_actor, command)
                                extra['on_native_command'] = acknowledge_commit
                        try:
                            prompt = commit_prompt or event['prompt']
                            if gate_relay:
                                prompt += _GATE_RELAY_GUIDANCE
                            reply['result'] = runner(prompt, options,
                            repo_root=Path(record['repo_root']), artifact_dir=agent_dir,
                            backend=record['backend'], timeout_s=remaining,
                            parent_session_id=record.get('caller_session_id'),
                            request_id=request_id, fire_id=record['fire_id'],
                            **({'run_id': record['run_id']} if record.get('run_id') else {}),
                            **({'usage_owner_session_id': record['usage_owner_session_id']}
                               if record.get('usage_owner_session_id') else {}), **extra)
                            if record.get('backend') == 'codex' and record.get('run_id'):
                                from coordinator_core.ops.dispatch_emit import run_write_prints
                                run_write_prints.record_codex_writes(
                                    Path(record['repo_root']), record['run_id'],
                                    artifacts / 'agents' / str(event['id']) / 'stdout.jsonl')
                            if native_landing is not None:
                                if native_landing.result is None:
                                    raise ValueError('commit worker did not acknowledge its host landing request')
                                reply['result'] = native_landing.result
                                if native_landing.result.get('committed'):
                                    observed_landing = native_landing.result
                            elif commit_prompt is not None:
                                # The model reply is not proof: replace it with the host's sealed result.
                                reply['result'] = _private_commit_receipt(private, request_id, record)
                        finally:
                            if reply_stream is not None:
                                reply_stream.close()
                            if gate_relay:
                                _journal_gate_commands(agent_dir, request_id, journal)
                            if private is not None and spawned:
                                private.end_worker(request_id)
                except Exception as exc:
                    reply['error'] = f'{type(exc).__name__}: {exc}'
                    if isinstance(getattr(exc, 'denial', None), dict):
                        reply['denial'] = exc.denial
                journal({'type': 'agent-response', **reply})
                with input_lock:
                    if process.poll() is None:
                        process.stdin.write(json.dumps(reply) + '\n')
                        process.stdin.flush()
            child_workflows = record.setdefault('child_workflows', [])

            def pump() -> None:
                nonlocal result, terminal, error
                journal({'type': 'started', 'pid': process.pid, 'script_path': str(script)})
                with ThreadPoolExecutor(max_workers=8) as workers:
                    pending_agents = []
                    try:
                        for line in process.stdout:
                            event = json.loads(line)
                            journal(event)
                            if event.get('event') == 'workflow-start':
                                child_path = event.get('script_path')
                                if isinstance(child_path, str) and child_path not in child_workflows:
                                    child_workflows.append(child_path)
                            if event['type'] in ('agent', 'invoke'):
                                pending_agents.append(workers.submit(respond, event))
                            elif event['type'] == 'result':
                                result, terminal = event.get('result'), True
                            elif event['type'] == 'error':
                                error = event.get('error', 'runtime failed')
                        code = process.wait()
                    finally:
                        # A timeout or malformed runtime event must not drain the
                        # entire queued fan-out after the workflow has already died.
                        for future in pending_agents:
                            future.cancel()
                if code or error or not terminal:
                    raise RuntimeError(error or f'runtime exited {code} without a completed result')

            pump()
            from coordinator_core.ops.dispatch_emit.fan_launcher import MAX_STAGE_ROUNDS
            for _ in range(MAX_STAGE_ROUNDS):
                twin_sha = _pending_staged_twin(result, staged_binding, Path(record['repo_root']),
                                                {**record, **record.get('emitted_root', {})}.get('run_id'))
                if twin_sha is None:
                    break
                terminal, error = False, None
                process = subprocess.Popen(
                    [shutil.which('node'), str(Path(__file__).with_name('runtime.mjs')),
                     staged_binding['script_path'], str(args_path), '--staged-sha', twin_sha],
                    cwd=record['repo_root'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=stderr, text=True, encoding='utf-8', **kwargs)
                pump()
        record.update(result=result)
        # A wake digest may still ask the terminal handler to record halt
        # artifacts. Preserve that authored action before classifying its outcome.
        if terminal_runner is _land and private is not None:
            if isinstance(result, dict) and result.get('next_action'):
                raise RuntimeError('private Workflow returned an unconsumed terminal action')
            landing = (json.loads(Path(record['terminal_finalizer']['receipt_path']).read_text(encoding='utf-8'))['result']
                       if record.get('terminal_finalizer') else None)
        elif terminal_runner is _land and observed_landing is not None:
            landing = observed_landing
        elif terminal_runner is _land:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('workflow deadline expired before terminal action')
            landing = _land(result, str(script), Path(record['repo_root']),
                artifacts=artifacts / 'terminal', timeout=remaining)
        else:
            landing = terminal_runner(result, str(script), Path(record['repo_root']))
        record.update(landing=landing)
        failure = _result_failure(result) or _result_failure(landing, landing=True)
        if failure:
            raise RuntimeError(failure)
        record.update(exit_code=0, outcome='clean', outcome_basis='headless workflow returned and terminal action accepted')
    except Exception as exc:
        record.update(exit_code=1, outcome='failed', error=f'{type(exc).__name__}: {exc}', outcome_basis='headless executor error')
        journal({'type': 'failed', 'error': record['error']})
        if cancelled:
            # The cancelled marker is the run's halt; a failed-fire halt would overwrite it.
            record['error'] = cancelled[0]
        else:
            _halt_failed_fire(record, result, record['error'])
    finally:
        if timer:
            timer.cancel()
        if process and process.poll() is None:
            process.kill()
            process.wait()
        if private is not None:
            try:
                private.close()
                record['private_engine_cleanup'] = {'closed': True}
            except Exception as exc:
                record.update(exit_code=1, outcome='failed',
                              error=f'private engine cleanup failed: {type(exc).__name__}: {exc}',
                              outcome_basis='private engine cleanup failed')
                record['private_engine_cleanup'] = {'closed': False, 'error': str(exc)}
                journal({'type': 'private-engine-cleanup-failed', 'error': str(exc)})
    record.update(state='exited', ended_at=time.time(), status_checked_at=time.time(),
                  _annotation_version=fire._ANNOTATION_VERSION)
    record['notify_owner_released'] = True
    _save(record_path, record)
    journal({'type': 'completed', 'exit_code': record['exit_code']})
    if record.get('outcome') == 'failed' and record.get('run_id'):
        # A killed or crashed runtime never reaches the script's halt finaliser, so a `resumed` marker would
        # outlive its fire and hide the run from every halted-only lookup (a refire then mints a fresh run).
        try:
            from coordinator_core.ops.dispatch_emit.halted_run import rehalt_if_resumer_dead

            rehalt_if_resumer_dead(Path(record['repo_root']), record['run_id'], record.get('error') or 'fire failed')
        except Exception as exc:  # noqa: BLE001 -- the fire record is the truth; the hook-side re-halt covers a miss
            journal({'type': 'rehalt-failed', 'error': f'{type(exc).__name__}: {exc}'})
    if record.get('notify_to'):
        try:
            record.update(_notify(record_path, record))
        except Exception as exc:
            record['notify_error'] = f'{type(exc).__name__}: {exc}'
    return record


if __name__ == '__main__':
    from coordinator_core.spawn_policy import windowless  # console-less root: every child spawns hidden

    windowless.install()
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--record', type=Path, required=True)
    options = parser.parse_args()
    sys.exit(execute(options.record)['exit_code'])
