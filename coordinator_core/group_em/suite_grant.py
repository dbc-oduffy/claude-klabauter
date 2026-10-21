"""
coordinator_core.group_em.suite_grant -- the live Group EM's test-run grant: record, issue,
revoke, and the fail-closed reader the suite gate consults.

Record: ``<settings-home>/state/group-em/suite-grants/<issuer_sid>/<grantee_sid>.json`` -- under the
ISSUER's key, beside the nomination records, never in a grantee's ``.git/coordinator-sessions/``
dir (grant.py's "a writer writes only its own session" invariant holds). Claude-Klabauter-owned; DoE's
``tier-u-grant.schema.json`` and ``grant.py``'s ``_VALID_GRANTED_BY`` are untouched.

NEGATIVE SPEC:

  - The issuer is the CALLING session (``core.resolve_session_id``), never an argument. Issue
    refuses unless that sid is the verified, live box holder (``nomination.box_holder``); a
    pending or rejected entry verdict is no standing.
  - The reader names ONE path under the CURRENT verified live holder's key. No glob, no scan, no
    spawn. A displaced or departed holder's grants stop authorizing with no revocation sweep, and a
    file planted under any other key is never read.
  - Fail closed: any error, malformed record, or unresolvable sid reads UNGRANTED. ``check`` never
    raises.
  - ``expires_at - issued_at`` above ``TTL_CEILING_MINUTES`` reads UNGRANTED, so a hand-edited
    record cannot outlive the ceiling.
  - The record authorizes the Tier-U/F suite gate only.

Spoofing stance: the caller sid comes from env, as it does for ``tier-u-grant-cli grant pm``;
this raises the cost of error, not of intent.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.group_em import nomination, session_registry
from coordinator_core.session import core
from coordinator_core.session.liveness import session_live

SCHEMA_VERSION = 1
KIND = "group-em-suite-grant"
TTL_DEFAULT_MINUTES = 120
TTL_CEILING_MINUTES = 720

NOT_GROUP_EM = "NOT_GROUP_EM"
GROUP_EM_NOT_LIVE = "GROUP_EM_NOT_LIVE"
ISSUER_UNRESOLVED = "ISSUER_UNRESOLVED"
GRANTEE_UNRESOLVED = "GRANTEE_UNRESOLVED"
GRANTEE_NOT_LIVE = "GRANTEE_NOT_LIVE"
TTL_OUT_OF_RANGE = "TTL_OUT_OF_RANGE"
NOTE_EMPTY = "NOTE_EMPTY"

#: Generator-provenance declaration (coordinator_core/ops/generator_census). Records live under
#: `settings_home() / "state" / "group-em" / "suite-grants"`, the operator home, never a path
#: inside this repo's tracked tree -- same disposition as nomination.py.
GENERATES = []


class IssueResult(NamedTuple):
    """``ok`` with the written ``record``, or a refusal ``reason`` (one of the constants above)."""

    ok: bool
    reason: str
    record: Optional[dict]


def _grants_dir(directory: Optional[Path]) -> Path:
    base = directory if directory is not None else settings_home() / "state" / "group-em"
    return base / "suite-grants"


def _safe_sid(sid: object) -> bool:
    return isinstance(sid, str) and bool(sid) and all(c.isalnum() or c in "-_" for c in sid)


def _grant_path(issuer_sid: str, grantee_sid: str, directory: Optional[Path]) -> Path:
    return _grants_dir(directory) / issuer_sid / f"{grantee_sid}.json"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: object) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _parts(path: str) -> tuple[str, ...]:
    return Path(os.path.normcase(os.path.normpath(os.path.abspath(path)))).parts


def _at_or_under(root: str, scope_root: str) -> bool:
    """Component-wise, so ``/r/a`` is not under ``/r/ab``; case-folds only where the OS does."""
    r, s = _parts(root), _parts(scope_root)
    return len(r) >= len(s) and r[: len(s)] == s


def _resolve_grantee(grantee: str) -> tuple[Optional[str], Optional[session_registry.RegistryRow]]:
    """A session id, or a registry name matching exactly one session; ``(None, None)`` otherwise."""
    rows = session_registry.read_rows()
    for row in rows:
        if row.session_id == grantee:
            return row.session_id, row
    named = [row for row in rows if row.name and row.name == grantee]
    if len(named) == 1:
        return named[0].session_id, named[0]
    return None, None


def issue_suite_grant(
    grantee: str,
    note: str,
    *,
    ttl_minutes: int = TTL_DEFAULT_MINUTES,
    repo: Optional[str] = None,
    directory: Optional[Path] = None,
    now: Optional[float] = None,
) -> IssueResult:
    """Write the calling live verified box holder's test-run grant to ``grantee`` (a session id,
    or a registry name resolving to exactly one session). Reissuing overwrites. The grantee may be
    the holder itself. ``scope_root`` is ``repo`` or the grantee's registry ``cwd``."""
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
    if (
        isinstance(ttl_minutes, bool)
        or not isinstance(ttl_minutes, int)
        or not 1 <= ttl_minutes <= TTL_CEILING_MINUTES
    ):
        return IssueResult(False, TTL_OUT_OF_RANGE, None)
    grantee_sid, row = _resolve_grantee(grantee)
    if not _safe_sid(grantee_sid):
        return IssueResult(False, GRANTEE_UNRESOLVED, None)
    scope_root = repo or (row.cwd if row is not None else "")
    if not scope_root:
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
        "scope_root": scope_root,
        "issued_at": _iso(issued),
        "expires_at": _iso(issued + ttl_minutes * 60),
        "note": note.strip(),
    }
    target = _grant_path(sid, grantee_sid, directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(target, (json.dumps(record, indent=2) + "\n").encode("utf-8"))
    return IssueResult(True, "issued", record)


def revoke_suite_grant(
    grantee: str, *, directory: Optional[Path] = None, now: Optional[float] = None
) -> IssueResult:
    """Remove the calling session's own grant to ``grantee``; idempotent. Touches only the
    caller's key, so no standing is needed to withdraw one's own grant."""
    sid = core.resolve_session_id()
    if not _safe_sid(sid):
        return IssueResult(False, ISSUER_UNRESOLVED, None)
    grantee_sid, _row = _resolve_grantee(grantee)
    if grantee_sid is None and _safe_sid(grantee):
        grantee_sid = grantee
    if not _safe_sid(grantee_sid):
        return IssueResult(False, GRANTEE_UNRESOLVED, None)
    try:
        _grant_path(sid, grantee_sid, directory).unlink()
    except FileNotFoundError:
        return IssueResult(True, "absent", None)
    return IssueResult(True, "revoked", None)


def check_group_em_suite_grant(
    cwd: Optional[str] = None,
    *,
    session_id: Optional[str] = None,
    repo_root: Optional[str] = None,
    directory: Optional[Path] = None,
    now: Optional[float] = None,
) -> tuple[bool, Optional[dict]]:
    """``(granted, record)``: does the current live verified box holder's grant name the calling
    session for the consumer root? ``record`` is the parsed grant whenever one was read, granted
    or not, so a denial can quote it. Reads one path; never raises."""
    try:
        sid = session_id or core.resolve_session_id(cwd)
        if not _safe_sid(sid):
            return False, None
        holder = nomination.box_holder(directory, now)
        if holder is None or not holder.get("live"):
            return False, None
        holder_sid = str(holder.get("session_id") or "")
        if not _safe_sid(holder_sid):
            return False, None
        try:
            record = json.loads(_grant_path(holder_sid, sid, directory).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False, None
        if not isinstance(record, dict):
            return False, None
        current = time.time() if now is None else now
        issued, expires = _parse_iso(record.get("issued_at")), _parse_iso(record.get("expires_at"))
        scope_root = record.get("scope_root")
        ok = (
            record.get("schema_version") == SCHEMA_VERSION
            and record.get("kind") == KIND
            and record.get("issuer_session_id") == holder_sid
            and record.get("grantee_session_id") == sid
            and issued is not None
            and expires is not None
            and issued <= current < expires
            and expires - issued <= TTL_CEILING_MINUTES * 60
            and isinstance(scope_root, str)
            and bool(scope_root)
            and _at_or_under(repo_root or cwd or os.getcwd(), scope_root)
            and session_live(sid, cwd)
        )
        return bool(ok), record
    except Exception:
        return False, None
