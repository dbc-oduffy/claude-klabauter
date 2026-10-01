"""
coordinator_core.ops.fanout.census — per-worker state rows for one fanout job.

Purpose: join a manifest, a list-sessions payload, get-session details and parent-channel
check-in comments into one CensusRow per manifest worker (manifest order) plus the ids of
strays: sessions tagged for this job whose worker tag names no manifest worker.

Negative spec: pure; no file, process or network access, no op registration. Session payload
fields are read defensively (absent -> None) because the real shapes are unverified. Identity
is the fanout:<job_id> + fanout-worker:<id> tag pair only — never title or prompt text.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from coordinator_core.ops.fanout import contract, transport

RUNNING_BUCKETS = frozenset({"running", "in_progress", "active", "queued", "pending"})


def _get(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _session_list(sessions: Any) -> list[dict]:
    if isinstance(sessions, dict):
        for key in ("sessions", "items", "data"):
            if isinstance(sessions.get(key), list):
                sessions = sessions[key]
                break
    return [s for s in sessions if isinstance(s, dict)] if isinstance(sessions, list) else []


def _tags(session: dict) -> list[str]:
    out: list[str] = []
    for tag in session.get("tags") or ():
        if isinstance(tag, str):
            out.append(tag)
        elif isinstance(tag, dict) and isinstance(tag.get("name"), str):
            out.append(tag["name"])
    return out


def _sid(session: dict) -> Optional[str]:
    sid = session.get("id") or session.get("session_id")
    return str(sid) if sid is not None else None


def _cost(detail: Any) -> Optional[float]:
    for path in (("usage", "cost_usd"), ("usage", "cost"), ("usage", "total_cost_usd"), ("cost_usd",), ("cost",)):
        value = _get(detail, *path)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _comment_body(comment: Any) -> str:
    if isinstance(comment, str):
        return comment
    body = comment.get("body") if isinstance(comment, dict) else None
    return body if isinstance(body, str) else ""


def _checkins(comments: Iterable[Any], channel: str) -> dict[str, str]:
    parse = transport.for_channel(channel).parse_checkin
    out: dict[str, str] = {}
    for comment in comments or ():
        parsed = parse(_comment_body(comment))
        if parsed:
            out.setdefault(parsed[0], parsed[1])
    return out


def _state(bucket: Optional[str]) -> str:
    if bucket in contract.DONE_BUCKETS:
        return "done"
    if bucket in contract.FAILED_BUCKETS:
        return "failed"
    if bucket in RUNNING_BUCKETS:
        return "running"
    return "unknown"


def census(
    manifest: dict,
    sessions: Any,
    session_details: Optional[dict],
    checkin_comments: Optional[Iterable[Any]],
) -> contract.CensusResult:
    job_id = manifest["job_id"]
    details = session_details if isinstance(session_details, dict) else {}
    checkins = _checkins(checkin_comments or (), manifest["channel"])
    job = contract.job_tag(job_id)
    worker_ids = [w["id"] for w in manifest["workers"]]
    by_worker: dict[str, list[dict]] = {wid: [] for wid in worker_ids}
    strays: list[str] = []

    for session in _session_list(sessions):
        tags = _tags(session)
        if job not in tags:
            continue
        owner = next((wid for wid in worker_ids if contract.worker_tag(wid) in tags), None)
        if owner is not None:
            by_worker[owner].append(session)
        else:
            sid = _sid(session)
            if sid is not None:
                strays.append(sid)

    rows: list[contract.CensusRow] = []
    for wid in worker_ids:
        found = by_worker[wid]
        sids = [s for s in (_sid(x) for x in found) if s is not None]
        first = found[0] if found else None
        detail = details.get(sids[0]) if sids else None
        bucket = None
        if first is not None:
            bucket = first.get("status_bucket") or _get(detail, "status_bucket")
        hit = next((s for s in sids if s in checkins), None)
        if not found:
            state = "missing"
        elif len(found) > 1:
            state = "duplicate"
        else:
            state = _state(bucket)
        rows.append(
            {
                "worker_id": wid,
                "state": state,
                "session_ids": sids,
                "status_bucket": bucket,
                "status_detail": _get(detail, "post_turn_summary", "status_detail"),
                "cost_usd": _cost(detail),
                "checked_in": hit is not None,
                "channel_url": checkins[hit] if hit is not None else None,
            }
        )
    return {"job_id": job_id, "rows": rows, "strays": strays}
