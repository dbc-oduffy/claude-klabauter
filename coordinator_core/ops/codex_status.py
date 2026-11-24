"""Bounded observed Codex status and derived lifetime spend cache."""
import sys
import threading

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


REFRESH_EVENTS = ("Stop", "SubagentStop", "PostCompact")
REFRESH_PAYLOAD_KEYS = ("session_id", "cwd", "transcript_path", "agent_transcript_path")
REFRESH_PARAMS_SCHEMA = {
    "type": "object", "properties": {
        "event": {"type": "string", "enum": list(REFRESH_EVENTS)},
        **{name: {"type": "string"} for name in REFRESH_PAYLOAD_KEYS},
    }, "required": ["event", "session_id", "cwd"], "additionalProperties": False,
}


MAX_CONCURRENT_REFRESHES = 4
_refresh_slots = threading.BoundedSemaphore(MAX_CONCURRENT_REFRESHES)


def _refresh_worker(payload: dict, event) -> None:
    try:
        from coordinator_core.codex_status.hook import handle, record_result
        try:
            result = handle(payload, event)
        except Exception as exc:  # noqa: BLE001 -- the ack already left; last-refresh.json is the failure surface
            reason = f"{type(exc).__name__}: {exc}"
            record_result(payload.get("session_id"), event, {"updated": False, "reason": reason})
            print(f"codex status: {reason}", file=sys.stderr)
            return
        if not result.get("updated") and result.get("reason") not in {"unchanged", "no exact registered route alias"}:
            print(f"codex status: {result.get('reason')}", file=sys.stderr)
    finally:
        _refresh_slots.release()


@register_op("codex_status.refresh", params_schema=REFRESH_PARAMS_SCHEMA)
def refresh(params, repo_root=None) -> dict:
    """Run the Codex lifecycle status refresh (rollout snapshot, app-server title set) off the caller's wait.

    Acknowledges at once and applies on a daemon thread, so a hook that dials this op never
    waits on the app-server proxy. Failures other than a benign skip go to the engine's stderr and the session's last-refresh.json.
    """
    p = params if isinstance(params, dict) else {}
    unknown = set(p) - {"event", *REFRESH_PAYLOAD_KEYS}
    if unknown:
        raise CallerFacingValidationError(f"codex_status.refresh: unknown fields {sorted(unknown)}")
    event = p.get("event")
    if event not in REFRESH_EVENTS:
        raise CallerFacingValidationError("codex_status.refresh: event must be Stop, SubagentStop or PostCompact")
    payload = {key: p[key] for key in REFRESH_PAYLOAD_KEYS if isinstance(p.get(key), str)}
    if not _refresh_slots.acquire(blocking=False):
        return {"accepted": True, "skipped": "busy"}
    try:
        threading.Thread(target=_refresh_worker, args=(payload, event), name="codex-status-refresh", daemon=True).start()
    except BaseException:
        _refresh_slots.release()
        raise
    return {"accepted": True}


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
