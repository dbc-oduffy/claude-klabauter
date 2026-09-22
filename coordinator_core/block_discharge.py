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
is a single `O_APPEND` append of one encoded JSON line. On POSIX this is
atomic at the OS level (single `write()` under `O_APPEND`), so concurrent
writers cannot clobber each other's lines -- that guarantee is believed true
but not independently verified for Windows (`O_APPEND` maps to
`FILE_APPEND_DATA`), which this project's Windows-first-class rule means
should not be read as pinned by test here.

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

_LEDGER_DIRNAME = os.path.join("state", "block-discharge")


class InvalidSessionId(ValueError):
    """Raised when a caller-supplied `session_id` is not a safe filename component."""


def _ledger_path(repo_root: str, session_id: str) -> str:
    # session_id is joined straight into a
    # filesystem path with no validation at any entry point. Reject anything
    # containing a path separator or a leading '.' rather than silently
    # joining, since this is the one place `state/block-discharge/<id>.jsonl`
    # is constructed and DoE imports record_fire directly.
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
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def _read_one(path: str) -> tuple:
    """Return `(records, skipped_count)` for one ledger file. A malformed or
    truncated line (e.g. a container torn down mid-write) is skipped and
    counted rather than aborting the read -- an unreadable ledger is an
    unresolved audit, not a clean one, and the skip count lets a checker
    report that and exit non-zero instead of reading a partial ledger as if
    it were complete."""
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
    """Append a `fire` record and return its freshly minted nonce.

    Best-effort: on a write failure this returns `None` -- a sentinel, never
    a nonce it could not durably record -- so a write failure cannot convert
    a block into a crash, and cannot mint a nonce that will never be
    honourable (nothing durable exists for it to join against)."""
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
    """Append a `discharge` record for `nonce`, and return whether it was
    recorded.

    Returns False if no `fire` record in this session's ledger carries
    `nonce` -- an invented nonce cannot self-discharge. `action` may be a
    reasoned no-op or a disputed-block explanation; this function (and any
    `check`-side reader) deliberately does not adjudicate `action` content --
    it exists so a human reader can see the agent's account."""
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
    """Return `(records, skipped_count)` -- the read side a `check` CLI
    verb uses.

    `session_id=None` reads every session's ledger under
    `state/block-discharge/` and aggregates both the records and the skip
    count; a given `session_id` reads just that one file. Missing directory
    or missing file is not an error -- it is an empty, clean ledger (nothing
    has fired yet)."""
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
