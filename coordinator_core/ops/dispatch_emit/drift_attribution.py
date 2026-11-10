"""Attribute each drifted path of a landing to run-owned, outside-declared or foreign bytes.

Invariant: a path is run-owned only when its current bytes equal a write the run's own ledger
recorded last for that path (session sink, named fix-pass agent sinks, or caller-supplied hashes /
blobs). A touch is never enough: a foreign write after the run's last own write changes the bytes
and so stays unowned. Run-owned wins over the declared-writes test; an unowned path outside
`declared_writes` is withheld, never refused; an unowned declared path is foreign.
Hash-only: spawns nothing, writes nothing.

Where the review/fix leg's writes are recorded today (diagnosis, for the dependants):
the write hook appends a TOUCH event with an eol-folded sha256 `content_hash` to the writer's
touch-record sink: the run session's `<sessions hub>/<sid>` sink and each fix-pass
agent's `<sessions hub>/.agents/<agent_id>` sink. `terminal_commit_review._recorded_fingerprints`
and `_review_write_events` read those sinks to build the commit's `expected_content_sha256`, anchored on the
review sidecar's mtime, so a refire or a write after that anchor falls outside it. The measured-rev
record (`measured_rev`) holds git blob shas taken at measurement time and never sees a later
review/fix write. `classify` re-reads the ledgers at call time, the re-measure point.
"""

from __future__ import annotations

import fnmatch
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from coordinator_core.git import commit

RUN_OWNED = "run_owned"
OUTSIDE_DECLARED = "outside_declared"
FOREIGN = "foreign"

_RUN_STAGE_FILE = "run-stage.json"
_GLOB_CHARS = frozenset("*?[")


def _norm(path: str) -> str:
    rel = str(path).strip().replace("\\", "/")
    return rel[2:] if rel.startswith("./") else rel


def _is_declared(rel: str, declared: Sequence[str]) -> bool:
    """Exact path, `dir/` prefix, or glob match against a declared-writes entry."""
    for raw in declared:
        entry = _norm(raw)
        if not entry:
            continue
        if entry == rel:
            return True
        if entry.endswith("/") and rel.startswith(entry):
            return True
        if _GLOB_CHARS & set(entry) and fnmatch.fnmatchcase(rel, entry):
            return True
    return False


def _run_stage(run_dir: Path) -> dict:
    try:
        data = json.loads((Path(run_dir) / _RUN_STAGE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _epoch(stamp: object) -> Optional[float]:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _ledger_hashes(
    root: Path, paths: Sequence[str], session_id: Optional[str], agent_ids: Iterable[str],
    run_started_at: Optional[float],
) -> Dict[str, set]:
    """`{path: {sha256}}` of the newest hashed write each ledger recorded for the path. An event
    older than `run_started_at` is another run's touch and counts for nothing."""
    from coordinator_core.session import touch_record

    out: Dict[str, set] = {}
    if session_id:
        for p, event in touch_record.last_seen_events(session_id, paths, root).items():
            if event is None or not event[0]:
                continue
            if run_started_at is not None and event[1] < run_started_at:
                continue
            out.setdefault(p, set()).add(event[0])
    for agent_id in dict.fromkeys(a for a in agent_ids if a):
        from coordinator_core.ops.dispatch_emit.terminal_commit_review import _review_write_events

        events = _review_write_events(root, {p: agent_id for p in paths})
        for p, (digest, stamp) in events.items():
            if digest and (run_started_at is None or stamp >= run_started_at):
                out.setdefault(p, set()).add(digest)
    return out


def session_agent_ids(root: Path, session_id: Optional[str]) -> List[str]:
    """Every agent the session dispatched, whatever its subagent type, that the session owns (the
    run's reverify, verify and evidence legs write under executor and falsifier types the
    review-writer denylist skips). Empty on an unreadable ledger; reads one file and each owner pointer."""
    if not session_id:
        return []
    from coordinator_core.session import core, touch_record

    hub = core.sessions_dir(str(root))
    if not hub:
        return []
    try:
        text = (Path(hub) / session_id / "dispatched-agents.txt").read_text(encoding="utf-8")
    except OSError:
        return []
    out: List[str] = []
    for row in text.splitlines():
        agent_id = row.split("\t", 1)[0].strip()
        if not agent_id or agent_id in out:
            continue
        owner, conclusive = touch_record.agent_owner_sid(str(Path(hub) / ".agents" / agent_id))
        if conclusive and owner == session_id:
            out.append(agent_id)
    return out


def run_recorded_blobs(record_dir: Optional[Path], paths: Iterable[str]) -> Dict[str, List[str]]:
    """`{path: [git blob sha]}` the run's own record dir holds for `paths`: the chunk blobs and
    post-checkpoint blobs of `kept.json`, and the `measured-rev.json` blobs. The executor's
    Write/Edit reaches no touch ledger (the write hook files it under the subagent's own session),
    so these engine-written records are the only proof of the bytes a chunk left. Feeds `classify`'s
    `own_blobs`: a foreign rewrite changes the bytes and matches none. Reads two files, spawns nothing."""
    if record_dir is None:
        return {}
    from coordinator_core.ops.dispatch_emit import kept_work

    wanted = {_norm(p) for p in paths if _norm(p)}
    found: Dict[str, List[str]] = {}

    def _add(rel: object, blob: object) -> None:
        key = _norm(rel) if isinstance(rel, str) else ""
        if key in wanted and isinstance(blob, str) and blob and blob not in found.setdefault(key, []):
            found[key].append(blob)

    kept = kept_work.read(Path(record_dir))
    for entry in kept["chunks"].values():
        recorded = entry.get("paths") if isinstance(entry, dict) else None
        for rel, blob in (recorded.items() if isinstance(recorded, dict) else ()):
            _add(rel, blob)
    for rel, blob in kept[kept_work.POST_CHECKPOINT_KEY].items():
        _add(rel, blob)
    try:
        measured = json.loads((Path(record_dir) / "measured-rev.json").read_text(encoding="utf-8"))
        blobs = measured.get("blobs")
    except (OSError, ValueError, AttributeError):
        blobs = None
    for rel, blob in (blobs.items() if isinstance(blobs, dict) else ()):
        _add(rel, blob)
    return found


def run_reviewed_hashes(root: Path, session_id: Optional[str], run_id: Optional[str]) -> Dict[str, str]:
    """`{path: sha256}` of the bytes the run's stored review prep read, found through the run-keyed
    pointer alone. Unlike the commit's own prep lookup it does not require the prep's `paths` to
    cover every path the commit declares (a delivery note or sizing record the review never read),
    so late-leg bytes the review read stay credited. Empty with no run, no unique stored reply or no
    `reviewed_sha256`. Reads a pointer and a sidecar; spawns nothing."""
    if not session_id or not run_id:
        return {}
    from coordinator_core.ops import review_prep

    reply = review_prep.stored_reply_for_run(Path(root), session_id, None, run_key=run_id)
    reviewed = reply.get("reviewed_sha256") if isinstance(reply, dict) else None
    if not isinstance(reviewed, dict):
        return {}
    return {_norm(p): h for p, h in reviewed.items() if isinstance(p, str) and isinstance(h, str) and h}


def classify(
    root: Path,
    run_dir: Path,
    paths: Iterable[str],
    declared_writes: Iterable[str],
    *,
    session_id: Optional[str] = None,
    agent_ids: Iterable[str] = (),
    own_hashes: Optional[Mapping[str, Iterable[str]]] = None,
    own_blobs: Optional[Mapping[str, Iterable[str]]] = None,
    run_started_at: Optional[float] = None,
) -> Dict[str, List[str]]:
    """`{"run_owned": [...], "outside_declared": [...], "foreign": [...]}` over `paths`, each list
    in `paths` order, deduped, every path in exactly one.

    run_owned: the path exists and its bytes equal the run's last recorded write: the newest hashed
    TOUCH of the run session (`session_id`, default `run_dir/run-stage.json`'s) or of a named
    fix-pass agent (`agent_ids`), an `own_hashes` entry ({path: sha256, eol-folded, the touch-record
    hash}), or an `own_blobs` entry ({path: git blob sha}). Ledger events older than
    `run_started_at` (default the run-stage `started_at`) are ignored.
    outside_declared: not run-owned and not covered by `declared_writes` (exact path, `dir/` prefix
    or glob): the caller withholds it from the landing and does not refuse.
    foreign: not run-owned and covered by `declared_writes`: a real foreign write; the caller refuses.
    A deleted path is never run-owned. Spawns nothing; reads only the ledgers and the listed files."""
    from coordinator_core.session import touch_record

    root = Path(root)
    wanted = list(dict.fromkeys(_norm(p) for p in paths if _norm(p)))
    declared = [d for d in declared_writes if isinstance(d, str)]
    stage = _run_stage(run_dir)
    if session_id is None:
        sid = stage.get("session_id")
        session_id = sid if isinstance(sid, str) and sid else None
    if run_started_at is None:
        run_started_at = _epoch(stage.get("started_at"))

    owned_hashes = _ledger_hashes(root, wanted, session_id, agent_ids, run_started_at)
    for p, hashes in (own_hashes or {}).items():
        owned_hashes.setdefault(_norm(p), set()).update(h for h in hashes if h)
    owned_blobs = {_norm(p): {b for b in blobs if b} for p, blobs in (own_blobs or {}).items()}

    out: Dict[str, List[str]] = {RUN_OWNED: [], OUTSIDE_DECLARED: [], FOREIGN: []}
    for rel in wanted:
        target = root / rel
        owned = False
        if target.is_file():
            hashes = owned_hashes.get(rel)
            if hashes and touch_record.compute_content_hash(target) in hashes:
                owned = True
            elif rel in owned_blobs and commit.checkin_blob_sha(root, rel) in owned_blobs[rel]:
                owned = True
        if owned:
            out[RUN_OWNED].append(rel)
        elif _is_declared(rel, declared):
            out[FOREIGN].append(rel)
        else:
            out[OUTSIDE_DECLARED].append(rel)
    return out
