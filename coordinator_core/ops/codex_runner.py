"""
coordinator_core.ops.codex_runner -- JSON-RPC "codex_runner.start" and "codex_runner.result".

`start` validates a run request, writes `<settings_home>/codex-runs/<run_id>/request.json`,
spawns `python -m coordinator_core.codex_runner <run_dir>` detached and returns at spawn.
`result` reads `result.json`, or reports `running` while the worker is alive.

Self-registration: importing this module registers both ops. Wired in `ops/__init__.py`
(`_EAGER_OP_MODULES`), `ops/_registry_map.py`, `op_scopes.py` and `authz/classification.py`.

Negative-spec:
    - `start` never waits on the worker; codex's duration is the worker's, not this op's.
    - `repo_root` is a wire param used verbatim, never the engine-injected kwarg.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core._settings_home import settings_home_child_env
from coordinator_core.codex_runner import contract
from coordinator_core.codex_runner.contract import RunRequest
from coordinator_core.ipc import CallerFacingValidationError, register_op

STATUS_RUNNING = "running"
CHILD_FILENAME = "child.json"
LOG_FILENAME = "worker.log"
_REQUEST = "request.json"
_RESULT = "result.json"
_PARAMS = ("mode", "repo_root", "artifact", "plan", "chunk", "parent_session_id")


def _detach_kwargs() -> Dict[str, Any]:
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


def _pid_alive(pid: int) -> bool:
    from coordinator_core.session import core

    try:
        return bool(core.pid_alive(pid))
    except Exception:
        return False


@register_op("codex_runner.start")
def _codex_runner_start(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Start one detached codex run.

    Params: mode ("review" | "agy-review" | "execute"), repo_root, and artifact (review,
    agy-review) or plan+chunk (execute); parent_session_id optional. `allow_api_key` is not
    a wire param: the op never forwards it.
    Returns: {"run_id", "run_dir", "pid"} -- the pid is the worker's, not codex's.
    """
    p = params if isinstance(params, dict) else {}
    for key in _PARAMS:
        if key in p and p[key] is not None and not isinstance(p[key], str):
            raise CallerFacingValidationError(f"codex_runner.start: {key} must be a string")
    try:
        req = RunRequest.from_dict({k: p.get(k) for k in _PARAMS})
    except ValueError as exc:
        raise CallerFacingValidationError(f"codex_runner.start: {exc}") from exc
    if not os.path.isdir(req.repo_root):
        raise CallerFacingValidationError(f"codex_runner.start: repo_root is not a directory: {req.repo_root}")

    run_id = f"{req.mode}-{uuid.uuid4().hex[:12]}"
    run_dir = contract.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / _REQUEST).write_text(json.dumps(req.to_dict()), encoding="utf-8")

    try:
        with open(run_dir / LOG_FILENAME, "wb") as log:
            proc = subprocess.Popen(
                [sys.executable, "-m", "coordinator_core.codex_runner", str(run_dir)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                cwd=req.repo_root,
                env=settings_home_child_env(os.environ),
                **_detach_kwargs(),
            )
    except OSError as exc:
        # Without a result the run would read as `running` forever (no child.json to probe).
        (run_dir / _RESULT).write_text(
            json.dumps(
                contract.RunResult(
                    run_id=run_id, status=contract.STATUS_FAILED, error=f"worker spawn failed: {exc}"
                ).to_dict()
            ),
            encoding="utf-8",
        )
        raise
    (run_dir / CHILD_FILENAME).write_text(json.dumps({"pid": proc.pid}), encoding="utf-8")
    return {"run_id": run_id, "run_dir": str(run_dir), "pid": proc.pid}


@register_op("codex_runner.result")
def _codex_runner_result(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Read one run's outcome.

    Params: run_id.
    Returns: the worker's RunResult dict, or {"run_id", "status": "running"} while the worker
    lives; a worker that died without a result reports status "failed".
    """
    run_id = params.get("run_id") if isinstance(params, dict) else None
    if not isinstance(run_id, str):
        raise CallerFacingValidationError("codex_runner.result: run_id (string) is required")
    try:
        run_dir = contract.run_dir(run_id)
    except ValueError as exc:
        raise CallerFacingValidationError(f"codex_runner.result: {exc}") from exc
    if not (run_dir / _REQUEST).is_file():
        raise CallerFacingValidationError(f"codex_runner.result: unknown run_id: {run_id}")

    try:
        return json.loads((run_dir / _RESULT).read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass

    try:
        pid = int(json.loads((run_dir / CHILD_FILENAME).read_text(encoding="utf-8"))["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        # start has not yet recorded the worker pid: the spawn window.
        return {"run_id": run_id, "status": STATUS_RUNNING}
    if _pid_alive(pid):
        return {"run_id": run_id, "status": STATUS_RUNNING}
    try:  # the worker may have finished between the first read and the liveness check
        return json.loads((run_dir / _RESULT).read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    return contract.RunResult(
        run_id=run_id,
        status=contract.STATUS_FAILED,
        error="worker exited without writing result.json",
    ).to_dict()
