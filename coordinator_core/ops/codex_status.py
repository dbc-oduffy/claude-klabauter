"""Bounded observed Codex status and derived lifetime spend cache."""
from coordinator_core.ipc import register_op, CallerFacingValidationError
from coordinator_core.group_em.atomic_record import LockTimeout

PARAMS_SCHEMA = {
    "type": "object", "properties": {
        "session_id": {"type": "string", "format": "uuid", "description": "Genuine root thread UUID; defaults to carried CODEX_THREAD_ID."},
        **{name: {"type": "array", "maxItems": 64, "items": {"type": "string"}} for name in ("rollouts", "discover_dirs", "event_journals")},
        "coverage_manifest": {"type": "string", "description": "Producer-owned coverage receipt with matching session_id and subagents/workflows booleans."},
        "byte_budget": {"type": "integer", "minimum": 1, "maximum": 4194304, "default": 4194304},
    }, "additionalProperties": False,
}


@register_op("codex_status.snapshot", params_schema=PARAMS_SCHEMA)
def snapshot(params, repo_root=None):
    """Render exact registered routing alias, observed model/effort/context, and tree API-cost estimate.

    Reads bounded appended JSONL usage from explicit or registered transcript paths.
    Fork history is deduplicated, subagents join only by explicit parent identity,
    and unavailable prices or child/workflow coverage remain visible as incomplete.
    Writes only a derived per-session cache; never changes Codex UI, routing, or services.
    """
    from coordinator_core.codex_status.__main__ import snapshot as collect
    p = params if isinstance(params, dict) else {}
    unknown = set(p) - set(PARAMS_SCHEMA["properties"])
    if unknown:
        raise CallerFacingValidationError(f"codex_status.snapshot: unknown fields {sorted(unknown)}")
    for key in ("rollouts", "discover_dirs", "event_journals"):
        if key in p and (not isinstance(p[key], list) or any(not isinstance(v, str) for v in p[key])):
            raise CallerFacingValidationError(f"{key} must be a string list")
        if len(p.get(key, [])) > 64:
            raise CallerFacingValidationError(f"{key} accepts at most 64 paths")
    if len(p.get("discover_dirs", [])) > 4:
        raise CallerFacingValidationError("discover_dirs accepts at most four explicitly selected directories")
    budget = p.get("byte_budget", 4194304)
    if isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= 4194304:
        raise CallerFacingValidationError("byte_budget must be 1..4194304")
    try:
        return collect(p)
    except (OSError, ValueError, TypeError, KeyError, LockTimeout) as exc:
        raise CallerFacingValidationError(f"codex_status.snapshot: {exc}") from exc
