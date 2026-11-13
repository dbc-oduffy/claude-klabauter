"""Engine-side heavy-spawn admission: the memory reading and reservation write for a caller whose own token cannot read them."""
from coordinator_core.ipc import register_op, CallerFacingValidationError

_ADMIT_SCHEMA = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "minLength": 1},
        "default_peak_gib": {"type": "number", "exclusiveMinimum": 0},
        "register_pid": {"type": "integer", "minimum": 1},
        "session_id": {"type": "string"},
    },
    "required": ["key", "default_peak_gib"],
    "additionalProperties": False,
}
_REGISTER_SCHEMA = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "minLength": 1},
        "peak_gib": {"type": "number", "exclusiveMinimum": 0},
        "pid": {"type": "integer", "minimum": 1},
    },
    "required": ["key", "peak_gib", "pid"],
    "additionalProperties": False,
}
_RELEASE_SCHEMA = {
    "type": "object",
    "properties": {"key": {"type": "string", "minLength": 1}, "pid": {"type": "integer", "minimum": 1}},
    "required": ["key", "pid"],
    "additionalProperties": False,
}


def _checked(op, params, schema):
    """Validated params: exact field set, required present, numbers real (bool is not a number or pid)."""
    p = params if isinstance(params, dict) else {}
    props = schema["properties"]
    unknown = set(p) - set(props)
    if unknown:
        raise CallerFacingValidationError(f"{op}: unknown fields {sorted(unknown)}")
    for name in schema["required"]:
        if name not in p:
            raise CallerFacingValidationError(f"{op}: {name} is required")
    for name, value in p.items():
        kind = props[name]["type"]
        ok = (
            isinstance(value, str) if kind == "string"
            else isinstance(value, int) and not isinstance(value, bool) if kind == "integer"
            else isinstance(value, (int, float)) and not isinstance(value, bool)
        )
        if not ok:
            raise CallerFacingValidationError(f"{op}: {name} must be {kind}")
        if kind == "string" and props[name].get("minLength") and not value:
            raise CallerFacingValidationError(f"{op}: {name} must be non-empty")
        if kind != "string" and value <= 0:
            raise CallerFacingValidationError(f"{op}: {name} must be positive")
    return p


@register_op("box_memory.admit", params_schema=_ADMIT_SCHEMA)
def admit(params, repo_root=None):
    """Measure box memory and decide whether `key` may spawn at `default_peak_gib`, reserving `register_pid` on admit.

    Runs `_admit_local` in the unsandboxed engine: same arithmetic, lock and reservation as an in-process
    admit. Never waits for a slot (a held engine thread is the load the brightline forbids); a HOLD
    is returned as `admitted: false`. A host error propagates as a JSON-RPC error envelope.
    """
    p = _checked("box_memory.admit", params, _ADMIT_SCHEMA)
    from coordinator_core.bash_guards import _heavy_command_class as hcc

    with hcc.in_engine():
        verdict = hcc._admit_local(
            p["key"], float(p["default_peak_gib"]), hcc._load_box_memory_host(), p.get("register_pid"),
            wait_kw=None, session_id=p.get("session_id", ""),
        )
    return {
        "admitted": bool(verdict.admitted), "reason": verdict.reason, "key": verdict.key,
        "peak_gib": verdict.peak_gib, "measured": True,
    }


@register_op("box_memory.register", params_schema=_REGISTER_SCHEMA)
def register(params, repo_root=None):
    """Write the headroom reservation for (key, pid) into the admission file; `ok` is false on a write failure."""
    p = _checked("box_memory.register", params, _REGISTER_SCHEMA)
    from coordinator_core.bash_guards import _heavy_command_class as hcc

    host = hcc._load_box_memory_host()
    try:
        with hcc.in_engine():
            host.register_command(host.admission_file(), p["key"], p["pid"], float(p["peak_gib"]))
    except OSError:
        return {"ok": False}
    return {"ok": True}


@register_op("box_memory.release", params_schema=_RELEASE_SCHEMA)
def release(params, repo_root=None):
    """Free the reservation for (key, pid); a missing row or unwritable file still reports ok."""
    p = _checked("box_memory.release", params, _RELEASE_SCHEMA)
    from coordinator_core.bash_guards import _heavy_command_class as hcc

    host = hcc._load_box_memory_host()
    try:
        with hcc.in_engine():
            host.release_command(host.admission_file(), p["key"], p["pid"])
    except OSError:
        pass
    return {"ok": True}
