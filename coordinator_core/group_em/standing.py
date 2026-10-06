"""coordinator_core.group_em.standing -- read-only Group EM standing reads.

`who` and `standing` over the nomination record and the session registry; they
never claim, unlink, or write. The `groupem.standing` op and the
`group-em-nomination` CLI shim both bind here, so the record shape and the
liveness join have one copy.
"""
from __future__ import annotations

from typing import Optional

from coordinator_core.group_em import nomination, session_registry


def who(repo_root: str) -> Optional[dict]:
    """The nomination record for `repo_root` annotated with `live` and
    `live_reason` and `live_state`; None when no record is on file."""
    record = nomination.read_record(repo_root)
    if record is None:
        return None
    live, live_reason, live_state = session_registry.liveness_annotation(record)
    annotated = dict(record)
    annotated["live"] = live
    annotated["live_reason"] = live_reason
    annotated["live_state"] = live_state
    annotated["entry_status"] = nomination.entry_status(record)["status"]
    return annotated


def _session_id_for_name(name: str) -> Optional[str]:
    if not name:
        return None
    matches = {row.session_id for row in session_registry.read_rows() if row.name == name}
    if len(matches) != 1:
        return None
    return matches.pop()


def standing(repo_root: str, peer: str) -> Optional[dict]:
    """`who`'s record plus `standing`: "live" (peer is the recorded holder and
    live), "not_live" (recorded holder, process gone) or "no_match". `peer` is
    a session id or a registry name. None when no record is on file."""
    record = who(repo_root)
    if record is None:
        return None
    holder = str(record.get("session_id") or "")
    matches = bool(peer) and (peer == holder or _session_id_for_name(peer) == holder)
    if not matches or record["entry_status"] != "verified":
        record["standing"] = "no_match"
    elif record.get("live"):
        record["standing"] = "live"
    else:
        record["standing"] = "not_live"
    return record
