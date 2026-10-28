"""coordinator_core.session.venv_grant -- session-scoped, PM-turn-cited, path-scoped, TTL-bound
grant that clears ``block_venv_creation`` for one path prefix.

Record: ``<session_dir>/venv-grant.json`` (one per session; reissue overwrites). The cited turn is
resolved in the session's own ``pm_turns.jsonl`` at issue, so a paraphrase cannot mint a grant.

    python -m coordinator_core.session.venv_grant grant <pm-turn> <prefix> [--ttl N]
    python -m coordinator_core.session.venv_grant revoke
    python -m coordinator_core.session.venv_grant check <path>

Negative spec: the reader opens one path for the calling session, never a glob; ``check_venv_grant``
never raises and reads every malformed, mismatched, expired or over-ceiling record as ungranted.
The caller is not discriminated here (no hook payload reaches this CLI).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple, Optional, Tuple

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.group_em.suite_grant import _at_or_under
from coordinator_core.ops import baton_pm_turns
from coordinator_core.session import core

# Generator-provenance declaration (coordinator_core/ops/generator_census): writes only
# `.git/coordinator-sessions/<sid>/venv-grant.json`, never a tracked repo artifact.
GENERATES = []

TTL_DEFAULT_MINUTES = 60
TTL_CEILING_MINUTES = 720
_GRANT_FILENAME = "venv-grant.json"
_KIND = "venv-grant"
_EXCERPT_CHARS = 200
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

NO_SESSION = "session id or session dir unresolvable"
BAD_TURN = "pm_turn must be a non-negative integer"
TURN_ABSENT = "pm_turn is not recorded in this session's pm_turns log"
BAD_PREFIX = "path_prefix must be an absolute path other than a filesystem root"
BAD_TTL = f"ttl_minutes must be an integer in 1..{TTL_CEILING_MINUTES}"
WRITE_FAILED = "grant record could not be written"
NOT_GRANTED = "no venv grant record for this session"


class IssueResult(NamedTuple):
    """``ok`` with the written ``record``, or a refusal ``reason`` (one of the constants above)."""

    ok: bool
    reason: str
    record: Optional[dict]


def _grant_file(sid: str, cwd: Optional[str], *, create: bool = False) -> Optional[Path]:
    sdir = core.ensure_session(sid, cwd) if create else core.session_dir(sid, cwd)
    if not sdir:
        return None
    if create and not os.path.isdir(sdir):
        return None
    return Path(sdir) / _GRANT_FILENAME


def _utc(now: Optional[datetime]) -> datetime:
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0)


def _parse_ts(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, _TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _prefix_ok(path_prefix: object) -> bool:
    if not isinstance(path_prefix, str) or not path_prefix.strip() or not os.path.isabs(path_prefix):
        return False
    norm = os.path.normpath(path_prefix)
    return os.path.dirname(norm) != norm


def write_venv_grant(
    pm_turn: int,
    path_prefix: str,
    *,
    ttl_minutes: int = TTL_DEFAULT_MINUTES,
    session_id: Optional[str] = None,
    cwd: Optional[str] = None,
    now: Optional[datetime] = None,
) -> IssueResult:
    """Create-or-overwrite the session's grant, after resolving ``pm_turn`` in its pm_turns log."""
    if not _is_int(pm_turn) or pm_turn < 0:
        return IssueResult(False, BAD_TURN, None)
    if not _prefix_ok(path_prefix):
        return IssueResult(False, BAD_PREFIX, None)
    if not _is_int(ttl_minutes) or not 1 <= ttl_minutes <= TTL_CEILING_MINUTES:
        return IssueResult(False, BAD_TTL, None)

    sid = session_id or core.resolve_session_id(cwd)
    if not sid:
        return IssueResult(False, NO_SESSION, None)

    turns = baton_pm_turns.read_turns(sid, cwd, turn=pm_turn)
    found = turns.get("turns") or []
    if not turns.get("ok") or not found or found[0].get("turn") != pm_turn:
        return IssueResult(False, TURN_ABSENT, None)

    grant_file = _grant_file(sid, cwd, create=True)
    if grant_file is None:
        return IssueResult(False, NO_SESSION, None)

    issued = _utc(now)
    record = {
        "schema_version": 1,
        "kind": _KIND,
        "session_id": sid,
        "pm_turn": pm_turn,
        "pm_turn_excerpt": str(found[0].get("prompt", ""))[:_EXCERPT_CHARS],
        "path_prefix": os.path.normpath(os.path.abspath(path_prefix)),
        "issued_at": issued.strftime(_TS_FORMAT),
        "expires_at": (issued + timedelta(minutes=ttl_minutes)).strftime(_TS_FORMAT),
    }
    try:
        atomic_write_bytes(grant_file, (json.dumps(record) + "\n").encode("utf-8"))
    except OSError:
        return IssueResult(False, WRITE_FAILED, None)
    return IssueResult(True, "", record)


def revoke_venv_grant(
    *, session_id: Optional[str] = None, cwd: Optional[str] = None
) -> IssueResult:
    """Delete the session's grant record; ``ok`` also when none existed."""
    sid = session_id or core.resolve_session_id(cwd)
    if not sid:
        return IssueResult(False, NO_SESSION, None)
    grant_file = _grant_file(sid, cwd)
    if grant_file is None:
        return IssueResult(False, NO_SESSION, None)
    try:
        grant_file.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        return IssueResult(False, WRITE_FAILED, None)
    return IssueResult(True, "", None)


def _live_record(sid: str, cwd: Optional[str], now: Optional[datetime]) -> Optional[dict]:
    grant_file = _grant_file(sid, cwd)
    if grant_file is None:
        return None
    try:
        record = json.loads(grant_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    if record.get("schema_version") != 1 or record.get("kind") != _KIND:
        return None
    if record.get("session_id") != sid or not _is_int(record.get("pm_turn")):
        return None
    if not _prefix_ok(record.get("path_prefix")):
        return None
    issued, expires = _parse_ts(record.get("issued_at")), _parse_ts(record.get("expires_at"))
    if issued is None or expires is None:
        return None
    if not timedelta(0) < expires - issued <= timedelta(minutes=TTL_CEILING_MINUTES):
        return None
    if _utc(now) >= expires:
        return None
    return record


def check_venv_grant(
    target: str,
    *,
    session_id: Optional[str] = None,
    cwd: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Tuple[bool, Optional[dict]]:
    """Does the calling session hold a live grant whose prefix covers ``target`` (absolute)?
    Returns ``(granted, record)``; never raises."""
    try:
        if not isinstance(target, str) or not target or not os.path.isabs(target):
            return False, None
        sid = session_id or core.resolve_session_id(cwd)
        if not sid:
            return False, None
        record = _live_record(sid, cwd, now)
        if record is None or not _at_or_under(target, record["path_prefix"]):
            return False, None
        return True, record
    except Exception:
        return False, None


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="venv_grant")
    sub = parser.add_subparsers(dest="cmd", required=True)
    grant = sub.add_parser("grant")
    grant.add_argument("pm_turn", type=int)
    grant.add_argument("prefix")
    grant.add_argument("--ttl", type=int, default=TTL_DEFAULT_MINUTES)
    sub.add_parser("revoke")
    check = sub.add_parser("check")
    check.add_argument("path")
    args = parser.parse_args(argv)

    if args.cmd == "grant":
        res = write_venv_grant(args.pm_turn, args.prefix, ttl_minutes=args.ttl)
        print(json.dumps(res.record) if res.ok else res.reason, file=sys.stdout if res.ok else sys.stderr)
        return 0 if res.ok else 2
    if args.cmd == "revoke":
        res = revoke_venv_grant()
        if not res.ok:
            print(res.reason, file=sys.stderr)
        return 0 if res.ok else 2
    granted, record = check_venv_grant(args.path)
    print(json.dumps(record) if granted else NOT_GRANTED)
    return 0 if granted else 1


if __name__ == "__main__":
    sys.exit(main())
