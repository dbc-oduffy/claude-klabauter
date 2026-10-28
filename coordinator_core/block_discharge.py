"""coordinator_core.block_discharge — append-only, per-session ledger of
Stop-guard fires and their discharges.

Port of coordinator-content-repo's `coordinator/hooks/scripts/_block_discharge.py`
(`docs/plans/2026-09-06-block-discharge-durable-artifact.md`, chunk C1),
loaded by coordinator-content-repo's `coordinator/bin/block-discharge.py` CLI by path and
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
     "repo": ..., "reason": ..., "at": ...}
    {"kind": "discharge", "nonce": ..., "action": ..., "at": ...}

`nonce` is the join key between the two record kinds -- minted fresh by
`record_fire` and never invented by a caller, so a `record_discharge` call
naming a nonce with no matching fire record is rejected (returns False)
rather than silently recording a self-issued discharge.

Storage: `<settings-home>/machine-local/block-discharge/<session-id>.jsonl`
(`ledger_dir()`) -- a per-box store outside every repo, never committed: the
spool is per-box session telemetry and committing it polluted shared history.
ONE flat file per session, appended to, never one file per fire; session ids
are box-unique, so a nonce is found wherever the guard fired from.
`repo_root` on `record_fire` is stored as the fire's `repo` field (which
checkout fired); on `record_discharge`/`read_ledger` it is accepted for
signature compatibility with cross-repo callers and not read.

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

from coordinator_core import _settings_home, atomic_append


class InvalidSessionId(ValueError):
    pass


def ledger_dir() -> str:
    """Per-box spool directory; re-derived per call so a redirected settings
    home is honoured. Pure env/home read, no spawn."""
    return os.path.join(str(_settings_home.machine_local_dir()), "block-discharge")


def _ledger_path(repo_root: str, session_id: str) -> str:
    if not session_id or "/" in session_id or "\\" in session_id or session_id.startswith("."):
        raise InvalidSessionId(f"unsafe session_id: {session_id!r}")
    return os.path.join(ledger_dir(), session_id + ".jsonl")


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
        # coordinator_core.atomic_append is the one atomic-multi-process-append
        # primitive: plain os.open(..., O_APPEND) is both non-atomic under
        # concurrent writers on Windows (CRT emulates it via seek+write) AND
        # silently rewrites '\n' to '\r\n' there (CRT text-mode translation),
        # even for already-encoded bytes. Reuse rather than re-open-code either
        # fix here.
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


def _resolved_level(guard: str) -> str:
    try:
        from coordinator_core.machine_profile import resolved_level

        return resolved_level(guard)
    except Exception:  # noqa: BLE001
        return "unknown"


def record_fire(repo_root: str, session_id: str, guard: str, reason: str) -> Optional[str]:
    nonce = uuid.uuid4().hex
    record = {
        "kind": "fire",
        "nonce": nonce,
        "guard": guard,
        "session_id": session_id,
        "repo": repo_root,
        "reason": reason,
        "level": _resolved_level(guard),
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

    directory = ledger_dir()
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
