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
from pathlib import Path
from typing import Any, Mapping

#: Corpus-mutator declaration (generator-provenance sweep): `_write_atomic`
#: rewrites `state/subagent-share/<caller_session_id>/group-em-baseline-
#: <repo_key>.json`, one file per (repo-key, session) pair -- a
#: data-dependent set no fixed GENERATES artifact could name. `repo_root`
#: itself is caller-supplied (see `diff_and_persist`), same house pattern as
#: the sibling counters beside this file (`guard_advisory_counter.py`,
#: `engine_provenance_counter.py`); the extension-scoped glob matches this
#: repo's own tracked `state/subagent-share/` tree the same way theirs does.
MUTATES = ["state/subagent-share/**/*.json"]

PeerRecord = Mapping[str, Any]
PeerSet = Mapping[str, PeerRecord]


def _repo_root() -> Path:
    # coordinator_core/group_em/baseline.py -> parents[2] is the repo root.
    return Path(__file__).resolve().parents[2]


def _store_path(repo_key: str, session_id: str, *, repo_root: Path | None = None) -> Path:
    root = repo_root if repo_root is not None else _repo_root()
    return root / "state" / "subagent-share" / session_id / f"group-em-baseline-{repo_key}.json"


def _load_previous(path: Path) -> dict[str, PeerRecord] | None:
    """Return the previous tick's peer set, or None if there is none to use.

    None covers both "no file yet" and "file present but unreadable/corrupt" --
    both degrade to the same first_tick treatment; the caller never learns
    which, by design (a torn file is not a diagnosable event here).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    peers = data.get("peers")
    if not isinstance(peers, dict):
        return None
    for value in peers.values():
        if not isinstance(value, dict):
            return None
    return peers


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
        }
    """
    path = _store_path(repo_key, session_id, repo_root=repo_root)
    previous = _load_previous(path)

    current_ids = set(current_peers.keys())

    if previous is None:
        result = {
            "spawned": [],
            "exited": [],
            "changed": [],
            "first_tick": True,
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
        }

    _write_atomic(path, {"peers": dict(current_peers)})
    return result
