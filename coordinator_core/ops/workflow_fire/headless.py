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
          timeout_s: int = 7200, args: dict | None = None, run_id: str | None = None,
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
    script = Path(script_path).resolve(strict=True)
    repo = fire._resolve_target_repo(script, cwd)
    fire._assert_script_under_repo_root(script, repo)
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    if expected is not None and expected != digest:
        from coordinator_core.ops.dispatch_emit.op import guard_against_fired_drift

        guard_against_fired_drift(script, expected)
    from coordinator_core.warm import sandboxed_spawn

    if sandboxed_spawn.caller_is_sandboxed():
        forward = {'script_path': str(script), 'cwd': str(repo), 'backend': backend, 'executor': 'headless',
                   'expected': expected, 'run_id': run_id, 'args': args, 'caller_session_id': caller_session_id,
                   'notify_to': notify_to, 'concurrency_cap': concurrency_cap, 'timeout_s': timeout_s,
                   'refire': refire or None}
        if private_engine is not None:
            forward['private_engine'] = private_engine
        return sandboxed_spawn.forward_spawn_to_warm('workflow.fire', {k: v for k, v in forward.items() if v is not None}, str(repo))
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
    """Run the same source revision as this executor: the warm engine at its own stamp, else a bounded cold process."""
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
            "stage": "gate", "operation": "dispatch.ask_gate"}


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
    digest = hashlib.sha256(json.dumps(receipt, indent=2).encode("utf-8")).hexdigest()
    _save(path, receipt)
    return result, digest


def _read_host_gate_receipt(path: Path, record: dict, digest: str | None) -> dict:
    if digest is None or not path.is_file():
        raise ValueError("host gate receipt missing")
    raw = path.read_text(encoding="utf-8")
    receipt = json.loads(raw)
    if (not isinstance(receipt, dict)
            or any(receipt.get(key) != value for key, value in _host_gate_binding(record).items())):
        raise ValueError("host gate receipt binding mismatch")
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != digest:
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
    if baseline_output is not None:
        argv += ["--head-sha", "HEAD", "--base-export-out", str(temporary_export)]
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


_EVICTED_SKIP = "evicted by this run's halt (parked out of the tree); not run"


def _verify_test_leg(params: dict, repo: str, artifacts: Path, timeout: float, *, run_id: str) -> dict:
    root = Path(repo).resolve()
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id):
        raise ValueError("host Verify tests requires a valid active run ID")
    run_dir = root / "state" / "warp" / run_id
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
    manifest_base = manifest.get("head_sha")
    if manifest_base and base != manifest_base:
        raise ValueError("host Verify tests baseline differs from the active run manifest")
    # Invariant: an evicted path is parked out of the tree, so naming it makes pytest exit 4 with no tests.
    from coordinator_core.ops.dispatch_emit import halt_tree_evict

    evicted = set(halt_tree_evict._load(run_dir))
    dropped = [t for t in targets if t.split("::", 1)[0] in evicted]
    targets = [t for t in targets if t not in dropped]
    if dropped and not targets:
        measured = {"status": "pass-with-skips", "tests_run": 0, "tests_failed": 0,
                    "summary": "every target was evicted by this run's halt; nothing to run",
                    "skipped": [{"test": t, "reason": _EVICTED_SKIP} for t in dropped]}
        _save(output, measured)
        return {**measured, "evicted_targets": dropped, "sidecar_path": output.relative_to(root).as_posix()}
    exit_code, measured = _run_pinned_test_leg(root, output, artifacts, targets, base, timeout)
    fields = ("status", "tests_run", "tests_failed", "baseline", "baseline_method", "build_clean",
              "skipped", "summary", "importer_overrun", "tripwires_not_run", "zero_run_cause")
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
    if exit_code:
        result["status"] = "error"
    return result


def _reverify_test_leg(params: dict, repo: str, artifacts: Path, timeout: float) -> dict | None:
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
    scope = params.get("verify_scope_targets", {})
    scoped = [test for path in edited for test in scope.get(path, [])]
    from coordinator_core.ops.reverify_scope import TRIPWIRE_BOUND, _is_js_test_file

    owned = list(dict.fromkeys(owned + scoped))
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
    try:
        exit_code, measured = _run_pinned_test_leg(
            root, root / output_path, artifacts, selected, params["base"],
            max(.1, deadline - time.monotonic()), export_path,
        )
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
        record['halted_runs'] = failed_fire_halt.halt_failed_fire(Path(record['repo_root']), ids, error, commits)
        record['halt_commits'] = {k: {f: v.get(f) for f in ('committed', 'sha', 'error') if f in v} for k, v in commits.items()}
    except Exception as exc:
        record['halt_error'] = f'{type(exc).__name__}: {exc}'


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
    def journal(event) -> None:
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
            gate_lock = threading.Lock()
            gate_started = False
            gate_receipt: tuple[Path, str] | None = None

            def respond(event) -> None:
                nonlocal gate_started, gate_receipt
                reply = {'id': event['id']}
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('workflow deadline expired before queued agent dispatch')
                    if process.poll() is not None:
                        raise RuntimeError('workflow runtime exited before queued agent dispatch')
                    if event['type'] == 'invoke':
                        operation_dir = artifacts / 'operations' / str(event['id'])
                        if event['op'] == 'gate.host_run':
                            with gate_lock:
                                if gate_started:
                                    raise ValueError('host gate already started for this fire')
                                gate_started = True
                            result, digest = _host_gate(event['params'], record, operation_dir, remaining)
                            with gate_lock:
                                gate_receipt = operation_dir / "consumed.json", digest
                            reply['result'] = result
                        elif event['op'] == 'gate.host_receipt':
                            with gate_lock:
                                if gate_receipt is None:
                                    raise ValueError('host gate receipt missing')
                                path, digest = gate_receipt
                            reply['result'] = _read_host_gate_receipt(path, record, digest)
                        elif event['op'] == 'review.reverify.host_test_leg':
                            reply['result'] = _reverify_test_leg(event['params'], record['repo_root'], operation_dir, remaining)
                        elif event['op'] == 'verify.host_test_leg':
                            reply['result'] = _verify_test_leg(event['params'], record['repo_root'],
                                                               operation_dir, remaining, run_id=record.get('run_id'))
                        else:
                            reply['result'] = _invoke_source(event['op'], event['params'], record['repo_root'],
                                                            operation_dir, remaining)
                    else:
                        request_id = str(event['id'])
                        extra = {}
                        spawned = False
                        commit_prompt = None
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
            child_workflows = record.setdefault('child_workflows', [])
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
