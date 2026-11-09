"""Bounded, provider-neutral doors to the six existing session ceremonies.

The op writes one request and detaches one composition worker. The worker uses
the native ceremony implementation; its duration is never charged to a warm op.
An accepted phase is not a completed ceremony. Its persisted result and pushed
notice name pending decisions, native directives, and emitted workflow fires.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from coordinator_core._settings_home import settings_home, settings_home_child_env
from coordinator_core.ipc import CallerFacingValidationError, register_op
from coordinator_core.ops.agent_pipe import _no_console_detach_kwargs
from coordinator_core.session.core import attributable_session_id, subprocess_identity_env

CEREMONIES = {
    "pickup": ("brief", "apply", "drop", "stamp-check"),
    "handoff": ("brief", "apply"),
    "workday_start": ("launch", "finish"),
    "workday_complete": ("launch", "finish"),
    "workstream_start": ("brief", "advance"),
    "quick_wrap": ("brief", "advance"),
}


def _runs() -> Path:
    return Path(settings_home()) / "ceremony-runs"


def _run_dir(run_id: str) -> Path:
    if not isinstance(run_id, str) or len(run_id) != 32 or any(c not in "0123456789abcdef" for c in run_id):
        raise CallerFacingValidationError("ceremony.result: invalid run_id")
    return _runs() / run_id


def build_request(ceremony: str, p: dict, repo_root: Path | None) -> dict:
    if not isinstance(p, dict):
        raise CallerFacingValidationError("ceremony: params must be an object")
    if repo_root is None or not Path(repo_root).is_dir():
        raise CallerFacingValidationError("ceremony: repository root is required")
    phase = p.get("phase", CEREMONIES[ceremony][0])
    if phase not in CEREMONIES[ceremony]:
        raise CallerFacingValidationError(f"ceremony.{ceremony}: unsupported phase {phase!r}")
    allowed = {"phase", "artifact_path", "decisions", "title", "session_id", "notify_to", "run_id", "agent_results", "flags", "fire", "prior_run_id", "directive_ids", "work_finished"}
    unknown = set(p) - allowed
    if unknown:
        raise CallerFacingValidationError(f"ceremony: unknown fields {sorted(unknown)}")
    for key in ("artifact_path", "title", "session_id", "notify_to", "run_id", "agent_results", "prior_run_id"):
        if key in p and (not isinstance(p[key], str) or not p[key].strip()):
            raise CallerFacingValidationError(f"ceremony: {key} must be a nonempty string")
    for key in ("decisions", "flags"):
        if key in p and not isinstance(p[key], dict):
            raise CallerFacingValidationError(f"ceremony: {key} must be an object")
    if phase == "advance":
        _run_dir(p.get("prior_run_id", ""))
        ids = p.get("directive_ids", [])
        if not isinstance(ids, list) or any(not isinstance(v, str) for v in ids):
            raise CallerFacingValidationError("ceremony: directive_ids must be string list")
    if "work_finished" in p and not isinstance(p["work_finished"], bool):
        raise CallerFacingValidationError("ceremony: work_finished must be boolean")
    if ceremony == "pickup" and not p.get("artifact_path"):
        raise CallerFacingValidationError("ceremony.pickup: artifact_path is required")
    if phase == "finish" and not p.get("run_id"):
        raise CallerFacingValidationError("ceremony: native run_id is required for finish")
    if "fire" in p and not isinstance(p["fire"], bool):
        raise CallerFacingValidationError("ceremony: fire must be boolean")
    extras = set(p) - {"phase", "session_id", "notify_to", "fire"}
    valid = {
        "pickup": {"artifact_path", "decisions"},
        "handoff": {"artifact_path", "decisions", "title"},
        "workday_start": {"flags"} if phase == "launch" else {"run_id", "agent_results"},
        "workday_complete": {"flags"} if phase == "launch" else {"run_id", "agent_results"},
        "workstream_start": {"prior_run_id", "directive_ids", "decisions"} if phase == "advance" else set(),
        "quick_wrap": {"prior_run_id", "directive_ids", "decisions", "work_finished"} if phase == "advance" else {"work_finished"},
    }[ceremony]
    if extras - valid:
        raise CallerFacingValidationError(f"ceremony: fields do not apply to {ceremony}/{phase}: {sorted(extras - valid)}")
    sid = p.get("session_id") or attributable_session_id()
    from coordinator_core.session.core import _UUID_RE
    if not isinstance(sid, str) or not _UUID_RE.fullmatch(sid):
        raise CallerFacingValidationError("ceremony: caller UUID session_id is required")
    return {**p, "ceremony": ceremony, "phase": phase, "session_id": sid,
            "repo_root": str(Path(repo_root).resolve()), "fire": p.get("fire", True),
            "notify_to": p.get("notify_to") or sid}


def _launch(ceremony: str, p: dict, repo_root: Path | None) -> dict:
    request = build_request(ceremony, p, repo_root)
    run_id = uuid.uuid4().hex
    from coordinator_core.bash_guards import _heavy_command_class as capacity
    admission_key = "ceremony-composition-" + ceremony
    try:
        host = capacity._load_box_memory_host()
    except Exception as exc:
        return {"state": "held", "reason": f"memory unreadable ({type(exc).__name__})", "ceremony_completed": False}
    admission = capacity.admit_before_spawn(admission_key, 0.25, host=host, register_pid=os.getpid(), session_id=request["session_id"])
    if not admission.admitted:
        return {"state": "held", "reason": admission.reason, "ceremony_completed": False}
    request["admission_key"] = admission_key
    try:
        return _launch_admitted(request, run_id, admission, admission_key, host)
    finally:
        capacity.release_spawn(admission_key, os.getpid(), host)


def _launch_admitted(request, run_id, admission, admission_key, host):
    from coordinator_core.bash_guards import _heavy_command_class as capacity
    ceremony = request["ceremony"]
    run_dir = _run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "request.json").write_text(json.dumps(request), encoding="utf-8")
    env = settings_home_child_env(subprocess_identity_env())
    env["COORDINATOR_SESSION_ID"] = request["session_id"]
    package_root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [package_root, env.get("PYTHONPATH")]))
    proc = None
    try:
        with (run_dir / "worker.log").open("wb") as log:
            proc = subprocess.Popen(
                [sys.executable, "-m", "coordinator_core.session_ceremonies", str(run_dir)],
                cwd=request["repo_root"], env=env, stdin=subprocess.PIPE, bufsize=0,
                stdout=log, stderr=log, **_no_console_detach_kwargs(),
            )
        import psutil
        try:
            birth = psutil.Process(proc.pid).create_time()
        except psutil.Error:
            birth = None
        from coordinator_core.session_ceremonies import _write_json
        _write_json(run_dir / "child.json", {"pid": proc.pid, "birth": birth})
        import contextlib
        lock = getattr(host, "admission_lock", None)
        with (lock(host.admission_file()) if lock is not None else contextlib.nullcontext()):
            registered = capacity.register_spawn(admission_key, admission.peak_gib, proc.pid, host)
        if registered is False:
            raise RuntimeError("ceremony child memory reservation failed")
        pending = _runs() / "pending-notifications"
        pending.mkdir(exist_ok=True)
        _write_json(pending / (run_id + ".json"), {"run_dir": str(run_dir), "target": request["notify_to"]})
        # All identity/reservation/notification artifacts precede the token.
        (run_dir / "admitted").write_text("registered", encoding="ascii")
        if proc.stdin.write(b"admitted\n") != len(b"admitted\n"):
            raise RuntimeError("ceremony child startup token was incomplete")
        proc.stdin.flush()
        proc.stdin.close()
        return {"run_id": run_id, "state": "accepted", "ceremony": ceremony,
                "phase": request["phase"], "pid": proc.pid, "run_dir": str(run_dir),
                "ceremony_completed": False}
    except Exception as exc:
        return _failed_launch(run_dir, request, run_id, admission_key, host, proc, exc)
    finally:
        stream = getattr(proc, "stdin", None)
        if stream is not None and not stream.closed:
            stream.close()


def _failed_launch(run_dir, request, run_id, admission_key, host, proc, error):
    from coordinator_core.bash_guards import _heavy_command_class as capacity
    from coordinator_core.session_ceremonies import _write_json
    failed = {"run_id": run_id, "state": "phase-failed", "ceremony_completed": False, "error": str(error)}
    if proc is not None:
        failed["child_pid"] = proc.pid
        try:
            proc.terminate()
            proc.wait(timeout=0.2)
        except (OSError, subprocess.TimeoutExpired) as exc:
            failed["child_termination_error"] = str(exc)
        capacity.release_spawn(admission_key, proc.pid, host)
    (run_dir / "admitted").unlink(missing_ok=True)
    _write_json(run_dir / "result.json", failed)
    pending = _runs() / "pending-notifications"
    pending.mkdir(exist_ok=True)
    _write_json(pending / (run_id + ".json"), {"run_dir": str(run_dir), "target": request["notify_to"]})
    return failed


def _handler(ceremony):
    def invoke(params, repo_root=None):
        return _launch(ceremony, params, repo_root)
    invoke.__doc__ = f"Launch a native {ceremony} phase asynchronously; read ceremony.result after its completion notice. Accepted is not ceremony-completed."
    return invoke


def _params_schema(ceremony):
    properties = {
        "phase": {"type": "string", "enum": list(CEREMONIES[ceremony]), "default": CEREMONIES[ceremony][0]},
        "session_id": {"type": "string", "format": "uuid", "description": "Caller UUID; defaults to carried session identity."},
        "notify_to": {"type": "string", "minLength": 1, "description": "Bridge recipient identity; defaults to caller UUID."},
        "fire": {"type": "boolean", "default": True, "description": "Fire native emitted workflows through workflow.fire."},
    }
    fields = {
        "pickup": ("artifact_path", "decisions"),
        "handoff": ("artifact_path", "decisions", "title"),
        "workday_start": ("flags", "run_id", "agent_results"),
        "workday_complete": ("flags", "run_id", "agent_results"),
        "workstream_start": ("prior_run_id", "directive_ids", "decisions"),
        "quick_wrap": ("prior_run_id", "directive_ids", "decisions", "work_finished"),
    }[ceremony]
    for field in fields:
        properties[field] = {"type": "string", "minLength": 1}
    if "decisions" in fields:
        properties["decisions"] = {"type": "object", "description": "Native judgment ID to disposition object or shorthand value."}
    if "flags" in fields:
        properties["flags"] = {"type": "object", "description": "Native launch flag names, including -- prefix, to string or boolean values."}
    if "prior_run_id" in fields:
        properties["prior_run_id"] = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
        properties["directive_ids"] = {"type": "array", "items": {"type": "string"}}
    if "work_finished" in fields:
        properties["work_finished"] = {"type": "boolean", "description": "EM judgment discharging quick-wrap condition 4."}
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if ceremony == "pickup":
        schema["required"] = ["artifact_path"]
    if ceremony in ("workday_start", "workday_complete"):
        schema["allOf"] = [{"if": {"properties": {"phase": {"const": "finish"}}, "required": ["phase"]}, "then": {"required": ["run_id"]}}]
    if "prior_run_id" in fields:
        schema["allOf"] = [{"if": {"properties": {"phase": {"const": "advance"}}, "required": ["phase"]}, "then": {"required": ["prior_run_id"]}}]
    return schema


for _name in CEREMONIES:
    register_op("ceremony." + _name, params_schema=_params_schema(_name))(_handler(_name))


@register_op("ceremony.result", params_schema={"type": "object", "properties": {
    "run_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"}}, "required": ["run_id"], "additionalProperties": False})
def result(params: dict, repo_root=None) -> dict:
    """Read a persisted ceremony phase result after its notice; distinguish running, failed, and completed native obligations."""
    run_dir = _run_dir(params.get("run_id", ""))
    path = run_dir / "result.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    if not (run_dir / "request.json").is_file():
        raise CallerFacingValidationError("ceremony.result: unknown run_id")
    child = run_dir / "child.json"
    if child.is_file():
        import psutil
        record = json.loads(child.read_text(encoding="utf-8"))
        try:
            proc = psutil.Process(record["pid"])
            live = record.get("birth") is None or proc.create_time() == record["birth"]
        except psutil.AccessDenied:
            return {"run_id": run_dir.name, "state": "indeterminate", "ceremony_completed": False, "error": "worker liveness unreadable"}
        except psutil.Error:
            live = False
        if not live:
            return {"run_id": run_dir.name, "state": "phase-failed", "ceremony_completed": False,
                    "error": "worker exited before persisting a terminal result"}
    return {"run_id": run_dir.name, "state": "running", "ceremony_completed": False}
