"""
coordinator_core.group_em.slot_grant -- the live Group EM's memory-slot grant: record, issue,
revoke, and the fail-closed reader a run's wait consults before a step over the slot threshold.

Record: ``<settings-home>/state/group-em/slot-grants/<issuer_sid>/<grantee_sid>.<run>.json`` -- under
the ISSUER's key, beside the suite grants. Claude-Klabauter-owned.

NEGATIVE SPEC:

  - The issuer is the CALLING session (``core.resolve_session_id``), never an argument. Issue
    refuses unless that sid is the verified, live box holder (``nomination.box_holder``).
  - The reader names ONE path under the CURRENT verified live holder's key. No glob, no scan, no
    spawn. A displaced or departed holder's grants stop authorizing with no revocation sweep.
  - Fail closed: any error, malformed record, or unresolvable sid reads UNGRANTED. ``check`` never
    raises.
  - ``expires_at - issued_at`` above ``TTL_CEILING_MINUTES`` reads UNGRANTED.
  - The record authorizes one run's declared-peak step only; it never raises a budget or cap.
  - A grant is keyed (grantee, run) and is not consumed by use: steps sharing a run label share it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.group_em import nomination
from coordinator_core.group_em.admission_post import valid_peak_mb
from coordinator_core.group_em.suite_grant import (
    GRANTEE_NOT_LIVE,
    GRANTEE_UNRESOLVED,
    GROUP_EM_NOT_LIVE,
    ISSUER_UNRESOLVED,
    NOT_GROUP_EM,
    NOTE_EMPTY,
    TTL_OUT_OF_RANGE,
    IssueResult,
    _iso,
    _parse_iso,
    _resolve_grantee,
    _safe_sid,
)
from coordinator_core.session import core
from coordinator_core.session.liveness import session_live

SCHEMA_VERSION = 1
KIND = "group-em-slot-grant"
TTL_DEFAULT_MINUTES = 60
TTL_CEILING_MINUTES = 240

RUN_INVALID = "RUN_INVALID"
PEAK_INVALID = "PEAK_INVALID"

#: Generator-provenance declaration: records live under the operator home, never in this repo.
GENERATES = []


def _grants_dir(directory: Optional[Path]) -> Path:
    base = directory if directory is not None else settings_home() / "state" / "group-em"
    return base / "slot-grants"


def _grant_path(issuer_sid: str, grantee_sid: str, run: str, directory: Optional[Path]) -> Path:
    return _grants_dir(directory) / issuer_sid / f"{grantee_sid}.{run}.json"


def issue_slot_grant(
    grantee: str,
    run: str,
    declared_peak_mb: float,
    note: str,
    *,
    ttl_minutes: int = TTL_DEFAULT_MINUTES,
    directory: Optional[Path] = None,
    now: Optional[float] = None,
) -> IssueResult:
    """Write the calling live verified box holder's slot grant for ``run`` to ``grantee`` (a
    session id, or a registry name resolving to exactly one session). Reissuing overwrites."""
    sid = core.resolve_session_id()
    if not _safe_sid(sid):
        return IssueResult(False, ISSUER_UNRESOLVED, None)
    holder = nomination.box_holder(directory, now)
    if holder is None or str(holder.get("session_id") or "") != sid:
        return IssueResult(False, NOT_GROUP_EM, None)
    if not holder.get("live"):
        return IssueResult(False, GROUP_EM_NOT_LIVE, None)
    if not isinstance(note, str) or not note.strip():
        return IssueResult(False, NOTE_EMPTY, None)
    if not _safe_sid(run):
        return IssueResult(False, RUN_INVALID, None)
    peak = valid_peak_mb(declared_peak_mb)
    if peak is None:
        return IssueResult(False, PEAK_INVALID, None)
    if (
        isinstance(ttl_minutes, bool)
        or not isinstance(ttl_minutes, int)
        or not 1 <= ttl_minutes <= TTL_CEILING_MINUTES
    ):
        return IssueResult(False, TTL_OUT_OF_RANGE, None)
    grantee_sid, row = _resolve_grantee(grantee)
    if not _safe_sid(grantee_sid):
        return IssueResult(False, GRANTEE_UNRESOLVED, None)
    if not session_live(grantee_sid):
        return IssueResult(False, GRANTEE_NOT_LIVE, None)
    issued = time.time() if now is None else now
    record = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "issuer_session_id": sid,
        "issuer_name": nomination._holder_name(holder),
        "grantee_session_id": grantee_sid,
        "grantee_name": row.name if row is not None else "",
        "run": run,
        "declared_peak_mb": peak,
        "issued_at": _iso(issued),
        "expires_at": _iso(issued + ttl_minutes * 60),
        "note": note.strip(),
    }
    target = _grant_path(sid, grantee_sid, run, directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(target, (json.dumps(record, indent=2) + "\n").encode("utf-8"))
    return IssueResult(True, "issued", record)


def revoke_slot_grant(grantee: str, run: str, *, directory: Optional[Path] = None) -> IssueResult:
    """Remove the calling session's own grant of ``run`` to ``grantee``; idempotent."""
    sid = core.resolve_session_id()
    if not _safe_sid(sid):
        return IssueResult(False, ISSUER_UNRESOLVED, None)
    grantee_sid, _row = _resolve_grantee(grantee)
    if grantee_sid is None and _safe_sid(grantee):
        grantee_sid = grantee
    if not _safe_sid(grantee_sid):
        return IssueResult(False, GRANTEE_UNRESOLVED, None)
    if not _safe_sid(run):
        return IssueResult(False, RUN_INVALID, None)
    try:
        _grant_path(sid, grantee_sid, run, directory).unlink()
    except FileNotFoundError:
        return IssueResult(True, "absent", None)
    return IssueResult(True, "revoked", None)


def check_slot_grant(
    session_id: str,
    run: str,
    directory: Optional[Path] = None,
    now: Optional[float] = None,
    peak_mb: Optional[float] = None,
) -> tuple[bool, Optional[dict]]:
    """``(granted, record)``: does the current live verified box holder's grant name ``session_id``
    for ``run``? ``record`` is the parsed grant whenever one was read, granted or not. With
    ``peak_mb`` given the grant's ``declared_peak_mb`` must cover it. Reads one path; never raises."""
    try:
        if not _safe_sid(session_id) or not _safe_sid(run):
            return False, None
        holder = nomination.box_holder(directory, now)
        if holder is None or not holder.get("live"):
            return False, None
        holder_sid = str(holder.get("session_id") or "")
        if not _safe_sid(holder_sid):
            return False, None
        try:
            record = json.loads(_grant_path(holder_sid, session_id, run, directory).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False, None
        if not isinstance(record, dict):
            return False, None
        current = time.time() if now is None else now
        issued, expires = _parse_iso(record.get("issued_at")), _parse_iso(record.get("expires_at"))
        ok = (
            record.get("schema_version") == SCHEMA_VERSION
            and record.get("kind") == KIND
            and record.get("issuer_session_id") == holder_sid
            and record.get("grantee_session_id") == session_id
            and record.get("run") == run
            and valid_peak_mb(record.get("declared_peak_mb")) is not None
            and (peak_mb is None or record["declared_peak_mb"] >= peak_mb)
            and issued is not None
            and expires is not None
            and issued <= current < expires
            and expires - issued <= TTL_CEILING_MINUTES * 60
            and session_live(session_id)
        )
        return bool(ok), record
    except Exception:
        return False, None
