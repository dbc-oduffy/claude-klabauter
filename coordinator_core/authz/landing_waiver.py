"""
coordinator_core.authz.landing_waiver -- a live Group EM's one-shot waiver for the local landing
guards (block-em-halted-withheld-commit and the terminal-commit landing refusals).

Record: ``<settings-home>/state/group-em/landing-waivers/<issuer_sid>/<waiver_id>.json``, under the
ISSUER's key beside the nomination records, never in repo ``state/`` (any agent can write there, and
the consuming ``os.replace`` would dirty the tree). Written by ``python -m
coordinator_core.authz.landing_waiver record ...``; no PM turn is involved.

NEGATIVE SPEC:

  - The issuer is the CALLING session (``core.resolve_session_id``), never an argument, and must
    be the verified, live box holder (``nomination.box_holder``).
  - The reader lists ONE directory: the CURRENT verified live holder's. A file planted under any
    other key, or written by a displaced holder, never matches. A bare ``approver`` string in a
    file authorizes nothing: ``issuer_session_id`` must equal the live holder's session id.
  - A waiver matches only when its ``repo_root`` is the caller's root, the guard is listed, every
    hit's run id is in ``run_ids`` and every hit path is in ``paths``; a consumed one never matches.
  - Fail closed: any error or malformed record reads NO waiver. ``matching_waiver`` never raises.
  - A hit whose run id is "" (landing findings carry none) is matched on path and guard alone.
  - Waives local landing guards only; push, merge to main and other irreversible acts keep their
    own assent.
  - ``consume`` runs at the guard's decision point: a commit that then fails spends the waiver and
    the G-EM re-records.

Zero process spawns: file reads and one directory listing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, NamedTuple, Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.group_em import nomination
from coordinator_core.session import core

SCHEMA_VERSION = 1
KIND = "group-em-landing-waiver"
APPROVERS = ("group-em",)

#: Closed set of waivable guards; test-tier and floor guards stay PM-owned.
WAIVABLE_GUARDS = frozenset(
    {"block-em-halted-withheld-commit", "import-closure-untracked", "peer-hunks-co-declared"}
)

GUARD_NOT_WAIVABLE = "GUARD_NOT_WAIVABLE"
NOT_GROUP_EM = "NOT_GROUP_EM"
GROUP_EM_NOT_LIVE = "GROUP_EM_NOT_LIVE"
ISSUER_UNRESOLVED = "ISSUER_UNRESOLVED"
APPROVER_NOT_ALLOWED = "APPROVER_NOT_ALLOWED"
FIELD_EMPTY = "FIELD_EMPTY"

#: Generator-provenance declaration (coordinator_core/ops/generator_census). Records live under
#: `settings_home() / "state" / "group-em" / "landing-waivers"`, the operator home, never a path
#: inside this repo's tracked tree -- same disposition as suite_grant.py.
GENERATES = []


class Waiver(NamedTuple):
    """A matched, unconsumed waiver: its file ``path`` and parsed ``record``."""

    path: Path
    record: dict


class RecordResult(NamedTuple):
    """``ok`` with the written ``path``, or a refusal ``reason`` (one of the constants above)."""

    ok: bool
    reason: str
    path: Optional[Path]


def _dir(directory: Optional[Path]) -> Path:
    base = directory if directory is not None else settings_home() / "state" / "group-em"
    return base / "landing-waivers"


def _safe_sid(sid: object) -> bool:
    return isinstance(sid, str) and bool(sid) and all(c.isalnum() or c in "-_" for c in sid)


def _norm_root(root: object) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(str(root))))


def _norm_path(path: str) -> str:
    path = path.replace("\\", "/")
    return path[2:] if path.startswith("./") else path


def _str_list(value: object) -> Optional[list[str]]:
    if isinstance(value, list) and value and all(isinstance(v, str) and v for v in value):
        return list(value)
    return None


def record_waiver(
    root: object,
    approver: str,
    ruling_ref: str,
    guards: Iterable[str],
    paths: Iterable[str],
    run_ids: Iterable[str],
    *,
    directory: Optional[Path] = None,
    now: Optional[float] = None,
) -> RecordResult:
    """Write the calling live verified box holder's waiver for ``guards``, covering ``paths`` of
    the halted ``run_ids`` under repo ``root``; ``ruling_ref`` cites the PM turn or ruling."""
    sid = core.resolve_session_id()
    if not _safe_sid(sid):
        return RecordResult(False, ISSUER_UNRESOLVED, None)
    holder = nomination.box_holder(directory, now)
    if holder is None or str(holder.get("session_id") or "") != sid:
        return RecordResult(False, NOT_GROUP_EM, None)
    if not holder.get("live"):
        return RecordResult(False, GROUP_EM_NOT_LIVE, None)
    if approver not in APPROVERS:
        return RecordResult(False, APPROVER_NOT_ALLOWED, None)
    guard_list = sorted({g for g in guards if isinstance(g, str) and g.strip()})
    path_list = sorted({_norm_path(p) for p in paths if isinstance(p, str) and p.strip()})
    run_list = sorted({r for r in run_ids if isinstance(r, str) and r.strip()})
    if not (isinstance(ruling_ref, str) and ruling_ref.strip() and guard_list and path_list and run_list):
        return RecordResult(False, FIELD_EMPTY, None)
    if not set(guard_list) <= WAIVABLE_GUARDS:
        return RecordResult(False, GUARD_NOT_WAIVABLE, None)
    issued = time.time() if now is None else now
    waiver_id = uuid.uuid4().hex[:12]
    record = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "waiver_id": waiver_id,
        "approver": approver,
        "issuer_session_id": sid,
        "repo_root": _norm_root(root),
        "ruling_ref": ruling_ref.strip(),
        "run_ids": run_list,
        "guards": guard_list,
        "paths": path_list,
        "issued_at": datetime.fromtimestamp(issued, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "consumed_at": None,
    }
    target = _dir(directory) / sid / f"{waiver_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(target, (json.dumps(record, indent=2) + "\n").encode("utf-8"))
    return RecordResult(True, "recorded", target)


def _covers(record: dict, root: object, guard: str, hits: Mapping[str, str], holder_sid: str) -> bool:
    guards, paths, run_ids = (_str_list(record.get(k)) for k in ("guards", "paths", "run_ids"))
    if guards is None or paths is None or run_ids is None:
        return False
    ref = record.get("ruling_ref")
    return bool(
        record.get("schema_version") == SCHEMA_VERSION
        and record.get("kind") == KIND
        and record.get("consumed_at") is None
        and record.get("approver") in APPROVERS
        and record.get("issuer_session_id") == holder_sid
        and record.get("repo_root") == _norm_root(root)
        and isinstance(ref, str)
        and ref.strip()
        and guard in guards
        and hits
        and all(_norm_path(p) in paths and (run == "" or run in run_ids) for p, run in hits.items())
    )


def matching_waiver(
    root: object,
    guard: str,
    hits: Mapping[str, str],
    *,
    directory: Optional[Path] = None,
    now: Optional[float] = None,
    hands_on: bool = False,
) -> Optional[Waiver]:
    """The live G-EM's unconsumed waiver covering every ``hits`` entry (repo-relative path -> halted
    run id) for ``guard``, else None. ``hands_on`` (the caller's interaction mode) reads no waiver;
    terminal_commit_refusals passes it, block_em_halted_withheld_commit does not. Never raises."""
    try:
        if hands_on or guard not in WAIVABLE_GUARDS:
            return None
        holder = nomination.box_holder(directory, now)
        if holder is None or not holder.get("live"):
            return None
        holder_sid = str(holder.get("session_id") or "")
        if not _safe_sid(holder_sid):
            return None
        base = _dir(directory) / holder_sid
        for name in sorted(os.listdir(base)):
            if not name.endswith(".json") or name.endswith(".consumed.json"):
                continue
            path = base / name
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(record, dict) and _covers(record, root, guard, hits, holder_sid):
                return Waiver(path, record)
        return None
    except Exception:
        return None


def consume(waiver: Waiver, *, now: Optional[float] = None) -> bool:
    """Claim ``waiver`` by atomic rename to ``<id>.consumed.json``. True for the one caller whose
    rename won; False for a concurrent loser (source gone) or any error. Exactly one caller is waived."""
    target = waiver.path.with_name(waiver.path.name[: -len(".json")] + ".consumed.json")
    try:
        os.replace(waiver.path, target)
    except OSError:
        return False
    try:
        stamp = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
        record = {**waiver.record, "consumed_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ")}
        atomic_write_bytes(target, (json.dumps(record, indent=2) + "\n").encode("utf-8"))
    except Exception:
        pass
    return True


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m coordinator_core.authz.landing_waiver")
    sub = parser.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="record a one-shot landing waiver (live G-EM only)")
    rec.add_argument("--approver", default="group-em", choices=APPROVERS)
    rec.add_argument("--ruling-ref", required=True, help="the PM turn or ruling cited")
    rec.add_argument("--guard", action="append", required=True, dest="guards")
    rec.add_argument("--path", action="append", required=True, dest="paths", help="repo-relative")
    rec.add_argument("--run-id", action="append", required=True, dest="run_ids")
    rec.add_argument("--root", default=os.getcwd())
    args = parser.parse_args(argv)
    result = record_waiver(args.root, args.approver, args.ruling_ref, args.guards, args.paths, args.run_ids)
    if not result.ok:
        print(f"landing waiver refused: {result.reason}", file=sys.stderr)
        return 1
    print(result.path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
