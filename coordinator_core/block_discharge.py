"""coordinator_core.block_discharge — append-only, per-session ledger of
Stop-guard fires and their discharges.

Port of DoE-claude's `coordinator/hooks/scripts/_block_discharge.py`
(`docs/plans/2026-09-06-block-discharge-durable-artifact.md`, chunk C1),
loaded by DoE-claude's `coordinator/bin/block-discharge.py` CLI by path and
imported by three of its hook guards. Ported near-verbatim per
`docs/plans/2026-09-18-doe-holds-no-scripts.md` chunk W2-C3.

Problem this closes: a blocking Stop guard fires into a session whose only
reader is the agent it just objected to, and the turn continues. Unattended,
that agent is sole judge of its own compliance and the verdict never routes
anywhere else. This module gives a fire a durable, out-of-session record and
a way for the agent (or a later checker) to see whether it was ever
discharged.

Record shapes, one JSON object per line:

    {"kind": "fire", "nonce": ..., "guard": ..., "session_id": ...,
     "reason": ..., "at": ...}
    {"kind": "discharge", "nonce": ..., "action": ..., "at": ...}

`nonce` is the join key between the two record kinds -- minted fresh by
`record_fire` and never invented by a caller, so a `record_discharge` call
naming a nonce with no matching fire record is rejected (returns False)
rather than silently recording a self-issued discharge.

Storage: `state/block-discharge/<session-id>.jsonl` -- ONE file per session,
appended to, never one file per fire. Many peer sessions share this
worktree; a per-fire file would multiply `git status` churn for all of them
with no read-side gain. `repo_root` is always a caller-supplied parameter --
this module does not walk for it and does not spawn `git rev-parse`.

Writer discipline -- deliberately NOT a whole-file read-modify-write
replace. An audit record must not be lossy under concurrent writers, since
more than one guard can record a fire in the same turn. So every write here
goes through `coordinator_core.atomic_append.append_line`, the shared
atomic-append primitive: genuine kernel `O_APPEND` on POSIX, `CreateFileW`
with `FILE_APPEND_DATA` on Windows (the CRT's `O_APPEND` emulation there is
neither atomic under concurrent writers nor byte-preserving -- it also
silently rewrites `\n` to `\r\n`), so concurrent writers cannot clobber each
other's lines and a written line's bytes are never mangled.

Import-light and stdlib-only, by design: this runs on the Stop hot path,
which the dispatcher exists to keep cheap. No `subprocess`, no third-party
import, no module-level file I/O.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Optional

from coordinator_core import atomic_append

_LEDGER_DIRNAME = os.path.join("state", "block-discharge")


class InvalidSessionId(ValueError):
    pass


def _ledger_path(repo_root: str, session_id: str) -> str:
    if not session_id or "/" in session_id or "\\" in session_id or session_id.startswith("."):
        raise InvalidSessionId(f"unsafe session_id: {session_id!r}")
    return os.path.join(repo_root, _LEDGER_DIRNAME, session_id + ".jsonl")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _append_record(path: str, record: dict) -> bool:
    """Single-write `O_APPEND` append of one JSON line. Best-effort: any
    failure (missing directory, permissions, disk full) is swallowed and
    reported as False -- callers translate that into their own contract
    (`record_fire` returns None rather than a nonce it could not durably
    record; `record_discharge` returns False)."""
    try:
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
        # primitive: plain os.open(..., O_APPEND) is both non-atomic under
        atomic_append.append_line(path, line)
        return True
    except OSError:
        return False


def _read_one(path: str) -> tuple:
    records: list = []
    skipped = 0
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except Exception:  # noqa: BLE001
                    skipped += 1
                    continue
                if isinstance(record, dict):
                    records.append(record)
                else:
                    skipped += 1
    except OSError:
        return [], 0
    return records, skipped


def record_fire(repo_root: str, session_id: str, guard: str, reason: str) -> Optional[str]:
    nonce = uuid.uuid4().hex
    record = {
        "kind": "fire",
        "nonce": nonce,
        "guard": guard,
        "session_id": session_id,
        "reason": reason,
        "at": _now_iso(),
    }
    try:
        path = _ledger_path(repo_root, session_id)
        ok = _append_record(path, record)
    except Exception:  # noqa: BLE001
        ok = False
    return nonce if ok else None


def record_discharge(repo_root: str, session_id: str, nonce: str, action: str) -> bool:
    try:
        path = _ledger_path(repo_root, session_id)
        records, _skipped = _read_one(path)
    except Exception:  # noqa: BLE001
        return False
    has_matching_fire = any(
        record.get("kind") == "fire" and record.get("nonce") == nonce for record in records
    )
    if not has_matching_fire:
        return False
    record = {
        "kind": "discharge",
        "nonce": nonce,
        "action": action,
        "at": _now_iso(),
    }
    try:
        return _append_record(path, record)
    except Exception:  # noqa: BLE001
        return False


def read_ledger(repo_root: str, session_id: Optional[str] = None) -> tuple:
    if session_id is not None:
        return _read_one(_ledger_path(repo_root, session_id))

    directory = os.path.join(repo_root, "state", "block-discharge")
    all_records: list = []
    total_skipped = 0
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return [], 0
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        records, skipped = _read_one(os.path.join(directory, name))
        all_records.extend(records)
        total_skipped += skipped
    return all_records, total_skipped
