"""coordinator_core.group_em.baseline -- the previous tick's peer set, persisted
so spawn/exit/state-transition diffing survives context compaction.

SUPERSESSION NOTICE. The `group-em` skill body (the doe-claude-em repo,
READ ONLY from this plane) still says "Nothing is written to disk on invoke" and "No
registration ceremony, no persistence". The PM's 2026-08-30 ruling retired
that no-persistence rule for the READ path only -- this module is the
licensed exception, cited here in-band because a future reader will find the
skill's old text first and needs the supersession stated where the code that
contradicts it actually lives.

WHAT THIS MODULE DOES, exactly and only: it stores one JSON snapshot of "the
peer set as of the last tick" and diffs the current tick's peer set against
it. That's the whole job.

WHAT THIS MODULE DOES NOT DO (negative spec, binding):
  - No elapsed-time, idle-duration, or "probably stuck" predicate. Only two
    observed sets go in; only a set-diff comes out.
  - No shared/append-across-sessions store. One file per (repo-key, caller
    session id) -- two concurrent sessions on this box never contend for one
    baseline file.
  - No fallback beyond the one named degrade: a torn/corrupt/truncated store
    is treated as absent and reported as `first_tick: true`. It is never
    partially parsed, never guessed at, never raised past the caller.

FIRST TICK IS A DISTINCT CASE. With no stored baseline (missing file, or a
file that fails to parse), every peer in the current tick is reported under
`spawned: []` -- an empty list -- plus `first_tick: True`. Peers that were
already running before this store existed are not spawns; a caller that
diffed against an absent baseline as if it were an empty one would report
six already-running sessions as six simultaneous spawns, which is a fiction.

`first_tick: True` STAYS ONE VERDICT, but "no file yet" and "file present
and unreadable" are not the same event to whoever is reading it: the former
is a fresh watch, the latter is a bug. `first_tick_reason`
(`FIRST_TICK_NEVER_ARMED` | `FIRST_TICK_UNREADABLE`) carries that distinction
beside the unchanged verdict, mirroring `watch_heartbeat`'s
`absent_reason` split -- see `_load_previous`.

EVERY DIFF CARRIES ITS OWN INSTANT. `as_of` is the ISO timestamp this diff
was taken at; `previous_taken_at` is the ISO timestamp the prior baseline was
persisted at (None on `first_tick`, or when an old pre-timestamp record is
read back). The persisted `taken_at` is the load-bearing half: it is what
lets the NEXT tick's diff report how stale the baseline it diffed against
was, so `exited: [sid]` can be read as "gone since the last few seconds" or
"gone since a three-day-old baseline" rather than reading the same either way.

STORAGE SHAPE. One file per (repo-key, caller session id), under this
machine's own working tree:

    <repo_root>/state/subagent-share/<caller_session_id>/group-em-baseline-<repo_key>.json

This mirrors the existing `state/subagent-share/<session_id>/` sandbox
convention already used for `advisory-fire-counts.jsonl` and
`deny-fire-counts.jsonl` beside it (see
`coordinator_core/bash_guards/bump_foreign_repo_write.py`'s own
`state/subagent-share/<session_id>/` note). Per-machine because it is plain
local disk under the working tree, never synced or merged across machines;
per-caller-session because the file lives inside that session's own
sandbox directory, so two concurrent sessions never share a path.

Writes are atomic: build the new content, write it to a sibling temp file,
then `os.replace` it over the real path. A process killed mid-write leaves
either the old file intact or the new file complete -- never a half-written
one.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from coordinator_core.group_em.watch_heartbeat import ABSENT_NEVER_ARMED, ABSENT_UNREADABLE
from coordinator_core.group_em.watch_heartbeat import iso_instant as _iso

PeerRecord = Mapping[str, Any]
PeerSet = Mapping[str, PeerRecord]

#: Why `_load_previous` returned no usable baseline. Aliases of
#: `watch_heartbeat.ABSENT_NEVER_ARMED` / `ABSENT_UNREADABLE` -- imported,
#: not re-declared (overengineering-reviewer finding 4): "no file yet" and
#: "a file exists but is torn/corrupt" are different events to whoever reads
#: `first_tick_reason` even though both degrade to the same `first_tick:
#: True` treatment for the diff itself. Local names read better at this
#: module's call sites, so they stay as aliases rather than a rename.
FIRST_TICK_NEVER_ARMED = ABSENT_NEVER_ARMED
FIRST_TICK_UNREADABLE = ABSENT_UNREADABLE


def _repo_root() -> Path:
    # coordinator_core/group_em/baseline.py -> parents[2] is the repo root.
    return Path(__file__).resolve().parents[2]


def _store_path(repo_key: str, session_id: str, *, repo_root: Path | None = None) -> Path:
    root = repo_root if repo_root is not None else _repo_root()
    return root / "state" / "subagent-share" / session_id / f"group-em-baseline-{repo_key}.json"


def _load_previous(
    path: Path,
) -> tuple[dict[str, PeerRecord] | None, str | None, str | None]:
    """Return `(peers, taken_at, reason)` for the previous tick.

    `peers` is None when there is nothing usable -- either "no file yet" or
    "file present but unreadable/corrupt". Unlike an earlier revision of this
    module, that verdict-level collapse does NOT also collapse the reason:
    `reason` is `FIRST_TICK_NEVER_ARMED` or `FIRST_TICK_UNREADABLE`
    respectively, mirroring `watch_heartbeat._read_record` /
    `read_liveness`'s `absent_reason` split for the identical two-cases-one-
    verdict shape. `taken_at` is the stored `taken_at` string (None when
    `peers` is None, or when an old pre-timestamp record is read back).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None, FIRST_TICK_NEVER_ARMED
    except OSError:
        return None, None, FIRST_TICK_UNREADABLE
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None, None, FIRST_TICK_UNREADABLE
    if not isinstance(data, dict):
        return None, None, FIRST_TICK_UNREADABLE
    peers = data.get("peers")
    if not isinstance(peers, dict):
        return None, None, FIRST_TICK_UNREADABLE
    for value in peers.values():
        if not isinstance(value, dict):
            return None, None, FIRST_TICK_UNREADABLE
    taken_at = data.get("taken_at")
    if not isinstance(taken_at, str):
        taken_at = None
    return peers, taken_at, None


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp_name, path)
    finally:
        try:
            os.remove(tmp_name)
        except OSError:
            pass


def diff_and_persist(
    current_peers: PeerSet,
    *,
    repo_key: str,
    session_id: str,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Diff `current_peers` against the stored previous tick, then persist
    `current_peers` as the new baseline for the next call.

    `current_peers` maps peer session id -> a record dict. Only two keys of
    that record are read for the `changed` comparison: `state` and `reason`;
    any other keys are stored verbatim but not inspected.

    Returns:
        {
          "spawned": [session_id, ...],   # present now, absent before
          "exited": [session_id, ...],    # present before, absent now
          "changed": [session_id, ...],   # present both ticks, state/reason differs
          "first_tick": bool,             # True iff no usable prior baseline existed
          "first_tick_reason": str|None,  # FIRST_TICK_* when first_tick, else None
          "as_of": str,                   # ISO instant this diff was taken
          "previous_taken_at": str|None,  # ISO instant of the prior baseline, or None
        }
    """
    now_iso = _iso(time.time())
    path = _store_path(repo_key, session_id, repo_root=repo_root)
    previous, previous_taken_at, first_tick_reason = _load_previous(path)

    current_ids = set(current_peers.keys())

    if previous is None:
        result = {
            "spawned": [],
            "exited": [],
            "changed": [],
            "first_tick": True,
            "first_tick_reason": first_tick_reason,
            "as_of": now_iso,
            "previous_taken_at": None,
        }
    else:
        previous_ids = set(previous.keys())
        spawned = sorted(current_ids - previous_ids)
        exited = sorted(previous_ids - current_ids)
        changed = []
        for sid in sorted(current_ids & previous_ids):
            before = previous[sid]
            after = current_peers[sid]
            if before.get("state") != after.get("state") or before.get("reason") != after.get(
                "reason"
            ):
                changed.append(sid)
        result = {
            "spawned": spawned,
            "exited": exited,
            "changed": changed,
            "first_tick": False,
            "first_tick_reason": None,
            "as_of": now_iso,
            "previous_taken_at": previous_taken_at,
        }

    _write_atomic(path, {"peers": dict(current_peers), "taken_at": now_iso})
    return result
