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
import time
import uuid

from coordinator_core.ops.workflow_fire import fire
from coordinator_core.ops.workflow_fire.backends import check as check_backend
from coordinator_core.ops.workflow_fire.receipt import FIRE_TOKEN_ENV, stamp_fire_ownership


def _save(path: Path, value: dict) -> None:
    temporary = path.with_name(f'{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp')
    temporary.write_text(json.dumps(value, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def _pid_birth(pid: int) -> float | None:
    import psutil

    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def validate_private_engine_config(value, *, backend='codex') -> dict | None:
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


def start(script_path: str, *, cwd: str | None = None, notify_to: str | None = None,
          backend: str = 'codex', concurrency_cap: int = fire.DEFAULT_CONCURRENCY_CAP,
          timeout_s: int = 7200, args: dict | None = None, run_id: str | None = None,
          caller_session_id: str | None = None, expected: str | None = None,
          private_engine: dict | None = None) -> dict:
    check_backend(backend)
    if type(concurrency_cap) is not int or concurrency_cap < 1:
        raise ValueError('concurrency_cap must be a positive integer')
    if type(timeout_s) is not int or not 1 <= timeout_s <= 86400:
        raise ValueError('timeout_s must be an integer within 1..86400')
    if args is not None and not isinstance(args, dict):
        raise ValueError('workflow args must be an object')
    private_spec = validate_private_engine_config(private_engine, backend=backend)
    private_sha = _private_source_sha() if private_spec is not None else None
    script = Path(script_path).resolve(strict=True)
    repo = fire._resolve_target_repo(script, cwd)
    fire._assert_script_under_repo_root(script, repo)
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    if expected is not None and expected != digest:
        from coordinator_core.ops.dispatch_emit.op import guard_against_fired_drift

        guard_against_fired_drift(script, expected)
    registry, identifier, record_path, record = fire.reserve_fire(
        script, repo, digest, expected=expected, concurrency_cap=concurrency_cap, notify_to=notify_to,
    )
    if record.get('already_fired'):
        return record
    artifacts = registry / 'headless' / identifier
    record.update(backend=backend, executor='headless', timeout_s=timeout_s,
                  args=args or {}, run_id=run_id, caller_session_id=caller_session_id,
                  script_sha256=digest, artifact_dir=str(artifacts), journal_path=str(artifacts / 'journal.jsonl'))
    child = None
    try:
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
        source_root = str(Path(__file__).resolve().parents[3])
        env['PYTHONPATH'] = source_root + os.pathsep + env.get('PYTHONPATH', '')
        command = [sys.executable, '-m', __name__, '--record', str(record_path)]
        from coordinator_core.bash_guards._heavy_command_class import admit_before_spawn

        admission = admit_before_spawn('headless-workflow-worker', 0.5)
        if not admission.admitted:
            raise RuntimeError(f'headless Workflow worker not admitted: {admission.reason}')
        with log.open('wb') as output:
            kwargs = {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)} if os.name == 'nt' else {'start_new_session': True}
            child = subprocess.Popen(command, cwd=repo, env=env, stdin=subprocess.DEVNULL,
                                     stdout=output, stderr=subprocess.STDOUT, **kwargs)
        # Before the gate, a crash is execution-unknown. The worker itself marks
        # execution_started=True after observing the released gate, so the parent
        # never overwrites a quickly completed child's terminal record.
        record.update(pid=child.pid, pid_birth=_pid_birth(child.pid), state='running', started_at=time.time(), log_path=str(log), command=command,
                      execution_started=None)
        fire._write_record(record_path, record)
        (artifacts / 'start').touch()
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


def _invoke_source(op: str, params: dict, repo: str, artifacts: Path, timeout: float):
    """Run the same source revision as this executor, with a bounded operation process."""
    if not isinstance(op, str) or not isinstance(params, dict):
        raise ValueError('engineOp requires an operation name and object params')
    if set(params) == {'paramsFile'}:
        path = Path(params['paramsFile']).resolve(strict=True)
        path.relative_to(Path(repo).resolve())
        params = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(params, dict):
            raise ValueError('engineOp paramsFile must contain an object')
    artifacts.mkdir(parents=True, exist_ok=True)
    request, response = artifacts / 'request.json', artifacts / 'response.json'
    request.write_text(json.dumps({'op': op, 'params': params, 'repo_root': repo}), encoding='utf-8')
    env = dict(os.environ, COORDINATOR_WARM='0')
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[3])
    with (artifacts / 'stdout.log').open('wb') as out, (artifacts / 'stderr.log').open('wb') as err:
        subprocess.run([sys.executable, '-m', 'coordinator_core.ops.workflow_fire.cold_operation', str(request), str(response)],
            cwd=repo, env=env, stdout=out, stderr=err, check=True, timeout=timeout,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    reply = json.loads(response.read_text(encoding='utf-8'))
    if 'error' in reply:
        raise RuntimeError(json.dumps(reply['error']))
    return reply['result']


def _result_failure(value, *, landing=False) -> str | None:
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
                  'stage_pending', 'minted', 'sized', 'planned', 'not_committed'):
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
    deadline = time.monotonic() + record.get('timeout_s', 7200)
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
    def journal(event):
        with journal_lock, (artifacts / 'journal.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'at': time.time(), **event}) + '\n')
    process = None
    timer = None
    result = None
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
            timer = threading.Timer(max(.1, deadline-time.monotonic()), process.kill)
            timer.daemon = True
            timer.start()
            def respond(event):
                reply = {'id': event['id']}
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('workflow deadline expired before queued agent dispatch')
                    if process.poll() is not None:
                        raise RuntimeError('workflow runtime exited before queued agent dispatch')
                    if event['type'] == 'invoke':
                        reply['result'] = _invoke_source(event['op'], event['params'], record['repo_root'],
                            artifacts / 'operations' / str(event['id']), remaining)
                    else:
                        request_id = str(event['id'])
                        extra = {}
                        spawned = False
                        commit_prompt = None
                        if private is not None:
                            commit_prompt = _prepare_private_commit(private, event, script, record)
                            def observed_spawn(pid):
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
                        try:
                            reply['result'] = runner(commit_prompt or event['prompt'], event.get('options', {}),
                            repo_root=Path(record['repo_root']), artifact_dir=artifacts / 'agents' / str(event['id']),
                            backend=record['backend'], timeout_s=remaining,
                            parent_session_id=record.get('caller_session_id'),
                            request_id=request_id, fire_id=record['fire_id'], **extra)
                            if commit_prompt is not None:
                                # The model reply is not proof: replace it with the host's sealed result.
                                reply['result'] = _private_commit_receipt(private, request_id, record)
                        finally:
                            if private is not None and spawned:
                                private.end_worker(request_id)
                except Exception as exc:
                    reply['error'] = f'{type(exc).__name__}: {exc}'
                journal({'type': 'agent-response', **reply})
                with input_lock:
                    if process.poll() is None:
                        process.stdin.write(json.dumps(reply) + '\n')
                        process.stdin.flush()
            journal({'type': 'started', 'pid': process.pid, 'script_path': str(script)})
            with ThreadPoolExecutor(max_workers=8) as workers:
                pending_agents = []
                try:
                    for line in process.stdout:
                        event = json.loads(line)
                        journal(event)
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
        record.update(result=result)
        # A wake digest may still ask the terminal handler to record halt
        # artifacts. Preserve that authored action before classifying its outcome.
        if terminal_runner is _land and private is not None:
            if isinstance(result, dict) and result.get('next_action'):
                raise RuntimeError('private Workflow returned an unconsumed terminal action')
            landing = (json.loads(Path(record['terminal_finalizer']['receipt_path']).read_text(encoding='utf-8'))['result']
                       if record.get('terminal_finalizer') else None)
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
