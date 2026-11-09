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
from coordinator_core.worker_contracts import PROVIDERS

STATUS_RUNNING = "running"
CHILD_FILENAME = "child.json"
LOG_FILENAME = "worker.log"
_REQUEST = "request.json"
_RESULT = "result.json"
_STRING_PARAMS = ("mode", "repo_root", "artifact", "plan", "chunk", "parent_session_id",
                  "persona", "task_file", "tier_map", "provider", "agent_type", "model", "brief")
_PARAMS = (*_STRING_PARAMS, "assignment")

_PROVIDERS = sorted(PROVIDERS)
_ASSIGNMENT_SCHEMA = {
    "type": "object", "required": ["session_id", "run", "declared_peak_mb"],
    "description": "An existing live Group EM grant must cover this session/run and declared or larger recorded peak.",
    "properties": {
        "session_id": {"type": "string", "minLength": 1},
        "run": {"type": "string", "minLength": 1},
        "declared_peak_mb": {"type": "number", "exclusiveMinimum": 0, "maximum": 1048576},
    },
}
_WORKER_ASSIGNMENT_SCHEMA = {
    **_ASSIGNMENT_SCHEMA,
    "required": [*_ASSIGNMENT_SCHEMA["required"], "source", "branch", "quiet_box", "quiet_max_sessions", "evidence"],
    "properties": {
        **_ASSIGNMENT_SCHEMA["properties"], "source": {"const": "pm"},
        "branch": {"type": "string", "minLength": 1, "description": "Assigned independent checkout branch; main/master and detached HEAD are refused."},
        "quiet_box": {"const": True}, "quiet_max_sessions": {"type": "integer", "minimum": 1},
        "evidence": {"type": "string", "minLength": 1, "description": "PM assignment reference; this text does not issue a Group EM slot."},
    },
}
_START_SCHEMA = {
    "type": "object", "required": ["mode", "repo_root"],
    "properties": {
        "mode": {"type": "string", "enum": list(contract.MODES)},
        "repo_root": {"type": "string", "minLength": 1, "description": "Existing working checkout directory, used verbatim as the child's cwd."},
        **{key: {"type": ["string", "null"]} for key in _STRING_PARAMS if key not in {"mode", "repo_root", "provider"}},
        "provider": {"type": ["string", "null"], "enum": [None, "codex", "agy", "grok", *_PROVIDERS]},
        "assignment": {"type": ["object", "null"]},
    },
    "oneOf": [
        {"properties": {"mode": {"enum": list(contract.ARTIFACT_MODES)},
                        "artifact": {"type": "string", "minLength": 1},
                        "plan": {"enum": [None, ""]}, "chunk": {"enum": [None, ""]}},
         "required": ["artifact"]},
        {"properties": {"mode": {"const": "execute"}, "plan": {"type": "string", "minLength": 1},
                        "chunk": {"type": "string", "minLength": 1}, "artifact": {"enum": [None, ""]}},
         "required": ["plan", "chunk"]},
        {"properties": {"mode": {"const": "persona"}, "provider": {"enum": _PROVIDERS},
                        "assignment": _ASSIGNMENT_SCHEMA,
                        **{key: {"type": "string", "minLength": 1} for key in ("persona", "task_file", "tier_map")},
                        **{key: {"enum": [None, ""]} for key in ("artifact", "plan", "chunk")}},
         "required": ["persona", "task_file", "tier_map", "provider", "assignment"]},
        {"properties": {"mode": {"const": "worker"}, "task_file": {"type": "string", "minLength": 1},
                        "assignment": _WORKER_ASSIGNMENT_SCHEMA, "provider": {"enum": [None, "codex"]},
                        **{key: {"enum": [None, ""]} for key in ("artifact", "plan", "chunk", "persona", "tier_map")}},
         "required": ["task_file", "assignment"]},
    ],
}

_START_SCHEMA["properties"]["verdicts"] = {"type": "array", "items": {"type": "string", "minLength": 1}}
_START_SCHEMA["oneOf"].append({
    "properties": {"mode": {"const": "named-review"}, "provider": {"enum": ["codex", "agy", "grok"]},
                   **{key: {"type": "string", "minLength": 1} for key in ("agent_type", "model", "brief")},
                   **{key: {"enum": [None, ""]} for key in ("artifact", "plan", "chunk")}},
    "required": ["agent_type", "provider", "model", "brief"],
})

_RESULT_SCHEMA = {
    "type": "object", "required": ["run_id"],
    "properties": {"run_id": {"type": "string", "minLength": 1,
                               "pattern": r"^[^/\\\u0000]+$", "not": {"enum": [".", ".."]},
                               "description": "Opaque run_id returned by codex_runner.start; no slash, backslash or NUL."}},
}
for _field, _description in {
    "artifact": "Artifact path to stage for review or agy-review.",
    "plan": "Plan path containing the open execution chunk.",
    "chunk": "Declared open chunk identifier within plan.",
    "persona": "The same shared role definition used by Claude, declaring model: opus.",
    "task_file": "UTF-8 file containing the assigned worker task or persona review request.",
    "tier_map": "Trusted reviewed JSON provider/tier map; no model-equivalence defaults are guessed.",
    "provider": "Serving provider namespace for persona mode; worker mode is Codex only.",
    "parent_session_id": "Parent session identifier for roster association; worker/persona default to assignment.session_id.",
}.items():
    _START_SCHEMA["properties"][_field]["description"] = _description


def _no_console_detach_kwargs() -> Dict[str, Any]:
    # Trap: DETACHED_PROCESS must not be ORed in; Win32 then ignores CREATE_NO_WINDOW.
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


def _pid_alive(pid: int) -> bool:
    from coordinator_core.session import core

    try:
        return bool(core.pid_alive(pid))
    except Exception:
        return False


@register_op("codex_runner.start", params_schema=_START_SCHEMA)
def _codex_runner_start(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Start one detached artifact review, plan-chunk execution, persona review, or Codex worker.

    Params: mode ("review" | "agy-review" | "grok-review" | "named-review" | "execute" | "persona" | "worker"), repo_root,
    and artifact (review, agy-review), plan+chunk (execute), persona+task_file+tier_map+
    provider+assignment (persona), or task_file+assignment (worker).
    parent_session_id optional. `allow_api_key` is not
    a wire param: the op never forwards it.
    Returns: {"run_id", "run_dir", "pid"} -- the pid is the worker's, not codex's.

    Worker mode requires the PM-assigned branch/quiet-box evidence and a live Group EM
    session/run memory grant; it inherits installed Codex model/effort defaults.
    Persona mode requires the shared opus-tier role file and explicit reviewed provider
    tier map; it receives no worker gate delegation. Their terminal result contains up to
    1 MiB of UTF-8 stdout in output; usage.output_truncated records whether more
    output remains in the run's stdout artifact. Acceptance is not completion; read the persisted result
    after completion rather than spinning on codex_runner.result while inference runs.
    """
    p = params if isinstance(params, dict) else {}
    for key in _STRING_PARAMS:
        if key in p and p[key] is not None and not isinstance(p[key], str):
            raise CallerFacingValidationError(f"codex_runner.start: {key} must be a string")
    verdicts = p.get("verdicts", [])
    if not isinstance(verdicts, list) or any(not isinstance(v, str) or not v for v in verdicts):
        raise CallerFacingValidationError("codex_runner.start: verdicts must be a list of non-empty strings")
    if "assignment" in p and p["assignment"] is not None and not isinstance(p["assignment"], dict):
        raise CallerFacingValidationError("codex_runner.start: assignment must be an object")
    try:
        req = RunRequest.from_dict({**{k: p.get(k) for k in _PARAMS}, "verdicts": verdicts})
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
                **_no_console_detach_kwargs(),
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


@register_op("codex_runner.result", params_schema=_RESULT_SCHEMA)
def _codex_runner_result(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Read a persisted runner outcome, or report its current process state without waiting.

    Params: run_id.
    Returns: the worker's RunResult dict, or {"run_id", "status": "running"} while the worker
    lives; a worker that died without a result reports status "failed".
    Worker and persona results include output when the mode produced it. This operation
    never waits on inference and never launches or restarts a worker.
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
