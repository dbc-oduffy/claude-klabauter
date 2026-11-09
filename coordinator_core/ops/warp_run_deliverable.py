"""Shared reader of a warp run's deliverable binding, for the commit-trailer resolver and the
hand-shipped sizing sweep.

`live_run_deliverable_id` answers which deliverable a session's live, sizing-bound run is for;
`run_records` lists every run dir with its submission, declared writes and reconciled state;
`write_reconciled` records the run-local terminal marker the sweep leaves.

Invariants: in-process file I/O only (no spawn, no transcript read), so it is safe on the commit
hot path. Ambiguity returns "" (omit rather than guess).
"""

from __future__ import annotations

import json
import logging
import os
import re
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT, record_dir
from coordinator_core.ops.dispatch_emit.commit_request import MalformedCommitRequestError, parse_marker
from coordinator_core.session import record_homes

RECONCILED_FILE = "reconciled.json"
COMMIT_REQUEST_FILE = "commit-request.txt"
SUBMISSION_FILE = "submission.json"

_SCALAR_RE = r"^{key}:[ \t]*(?:\"([^\"\n]*)\"|'([^'\n]*)'|([^\s#'\"][^\s#]*))"


@dataclass(frozen=True)
class RunRecord:
    """One run dir under `scratch/warp`. `submitted_at` is 0.0 and `session_id` "" when
    `submission.json` is absent or unreadable; `sizing_rel` is "" when no record names one."""

    run_id: str
    session_id: str
    submitted_at: float
    sizing_rel: str
    writes: tuple
    reconciled: bool


def _norm(path: str) -> str:
    text = (path or "").replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _read_json(path: Path) -> dict:
    from coordinator_core.git import call_io_scope

    try:
        raw = call_io_scope.read_bytes(path)
        data = json.loads(raw) if raw is not None else None
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def sizing_scalar(repo_root: Path, sizing_rel: str, key: str) -> str:
    """A top-level scalar of a sizing yaml (`deliverable_id`, `status`) read by line match, "" when
    the file or key is absent. No YAML parse: the commit hot path pays for a line scan only."""
    if not sizing_rel or not key:
        return ""
    from coordinator_core.git import call_io_scope

    try:
        raw = call_io_scope.read_bytes(Path(repo_root) / sizing_rel)
    except OSError:
        return ""
    if raw is None:
        return ""
    match = re.search(_SCALAR_RE.format(key=re.escape(key)), raw.decode("utf-8"), re.MULTILINE)
    if match is None:
        return ""
    return next((g for g in match.groups() if g is not None), "").strip()


def declared_writes(run_dir: Path) -> tuple:
    """Every chunk path the run's `commit-request.txt` promises its terminal commit, normalized;
    empty when the marker is absent or malformed."""
    try:
        request = parse_marker((run_dir / COMMIT_REQUEST_FILE).read_text(encoding="utf-8"))
    except (OSError, MalformedCommitRequestError):
        return ()
    if request is None:
        return ()
    seen: dict = {}
    for chunk in request.chunks:
        for path in chunk.paths:
            seen.setdefault(_norm(path), None)
    return tuple(seen)


def record_sizing_rel(repo_root: Path, run_id: str) -> str:
    """The normalized `sizing_rel` a run's record-dir `run.json` names, "" when absent or unsafe."""
    try:
        run_json = record_dir(repo_root, run_id) / "run.json"
    except ValueError:
        return ""
    value = _read_json(run_json).get("sizing_rel")
    return _norm(value) if isinstance(value, str) else ""


def run_records(repo_root: Path) -> Iterator[RunRecord]:
    """One `RunRecord` per run dir under `scratch/warp`, in name order."""
    root = Path(repo_root)
    try:
        run_dirs = sorted(p for p in (root / RUN_DIR_ROOT).iterdir() if p.is_dir())
    except OSError:
        run_dirs = []
    seen = {p.name for p in run_dirs}
    for run_dir in run_dirs:
        submission = _read_json(run_dir / SUBMISSION_FILE)
        sizing_rel = submission.get("sizing_rel")
        sizing_rel = _norm(sizing_rel) if isinstance(sizing_rel, str) else ""
        submitted_at = submission.get("submitted_at")
        session_id = submission.get("session_id")
        yield RunRecord(
            run_id=run_dir.name,
            session_id=session_id if isinstance(session_id, str) else "",
            submitted_at=float(submitted_at) if isinstance(submitted_at, (int, float)) else 0.0,
            sizing_rel=sizing_rel or record_sizing_rel(root, run_dir.name),
            writes=declared_writes(run_dir),
            reconciled=(run_dir / RECONCILED_FILE).is_file(),
        )
    yield from _tracked_outcome_records(root, seen)


def _tracked_outcome_records(root: Path, seen: set) -> Iterator[RunRecord]:
    """`RunRecord`s of runs another box ran, read from the tracked `outcome.json` (the run dir is
    local-only); a `landed` outcome counts as reconciled, a halted one as still open."""
    evidence_dir = Path(record_homes.home_dir(str(root), "run-evidence"))
    try:
        names = sorted(p.name for p in evidence_dir.iterdir() if p.is_dir())
    except OSError:
        return
    for name in names:
        if name in seen:
            continue
        outcome = _read_json(evidence_dir / name / "outcome.json")
        if not outcome:
            continue
        sizing_rel = outcome.get("sizing_rel")
        yield RunRecord(
            run_id=name,
            session_id="",
            submitted_at=0.0,
            sizing_rel=_norm(sizing_rel) if isinstance(sizing_rel, str) else "",
            writes=(),
            reconciled=outcome.get("state") == "landed",
        )


def live_run_deliverable_id(repo_root: Path, session_id: str, paths: Iterable[str]) -> str:
    """The `deliverable_id` of the one sizing-bound live run `session_id` holds whose declared
    writes include a staged path, else "". Zero, or two-plus distinct ids, return "".

    The path gate keeps an abandoned run's token (live for `run_token.TTL_SECONDS`) from claiming
    every later commit of the session."""
    if not session_id:
        return ""
    staged = {_norm(p) for p in paths if p}
    if not staged:
        return ""
    root = Path(repo_root)
    ids: set = set()
    for record in run_token.live_records(root):
        sizing_rel = record.get("sizing_rel")
        if record.get("session_id") != session_id or not isinstance(sizing_rel, str) or not sizing_rel:
            continue
        run_dir = root / RUN_DIR_ROOT / str(record["run_id"])
        if (run_dir / RECONCILED_FILE).is_file() or staged.isdisjoint(declared_writes(run_dir)):
            continue
        did = sizing_scalar(root, _norm(sizing_rel), "deliverable_id")
        if did:
            ids.add(did)
    return next(iter(ids)) if len(ids) == 1 else ""


def _fresh_entry(root: Path, run_id: str):
    """`halted_withheld_index.fresh_entry` for `run_id` under `root`: `halted_run.fresh_index_entry`
    without deriving the root from a record-dir `Path` (a hot per-run call of the claim pass)."""
    from coordinator_core.ops.dispatch_emit import halted_withheld_index

    return halted_withheld_index.fresh_entry(root, run_id)


def _no_marker(root: Path, run_id: str) -> bool:
    """Whether `run_id`'s `state/warp` record dir exists and holds no `halted.json`: two stats, no
    record-dir resolve. The answer to "is this run halted, landed or resumed?" for a token-only run, which
    is most of the in-window runs. False when the dir is absent (the marker may sit under `scratch/warp`),
    when the id is not a single safe segment, or when a marker exists: the caller reads it."""
    from coordinator_core.ops.dispatch_emit.halted_run import HALTED_FILE

    if not isinstance(run_id, str) or not safe_id(run_id):
        return False
    rdir = os.path.join(root, RECORD_DIR_ROOT, run_id)
    return not os.path.exists(os.path.join(rdir, HALTED_FILE)) and os.path.isdir(rdir)


def _halted(root: Path, run_id: str) -> bool:
    """A run whose record carries a `halted` marker writes nothing more; its token stays fresh for
    the TTL and its declaration contests no live write. Its declared writes stay held against
    another run's carry, while they carry hunks attributable to it, until it resumes, ages out, or
    `halted_hold_release.release_reason` frees it (`halted_run_write_claims`). A `resumed` marker is
    live again."""
    from coordinator_core.ops.dispatch_emit import halted_run

    if _no_marker(root, run_id):
        return False
    try:
        if _fresh_entry(root, run_id) is not None:
            return True
        marker = halted_run.read_strict_run(record_dir(root, run_id))
    except halted_run.UnreadableHaltRecord:
        return True
    except ValueError:  # not a run id (a claim's reason string): no record, not halted
        return False
    return marker is not None and marker.get("status") == halted_run.STATUS_HALTED


def resumed_run_released(root: Path, run_id: str, exclude_run: Optional[str]) -> bool:
    """Whether `run_id`'s marker reads `resumed` or `landed` and `halted_hold_release.release_reason`
    frees it (recorded landed, superseded or shipped sizing, or a landed successor). A run with no
    marker, or any other status, is False without consulting the predicate."""
    from coordinator_core.ops.dispatch_emit import halted_hold_release, halted_run

    if _no_marker(root, run_id):
        return False
    try:
        marker = halted_run.read_strict_run(record_dir(root, run_id))
        if marker is None or marker.get("status") not in (halted_run.STATUS_RESUMED, halted_run.STATUS_LANDED):
            return False
        return bool(halted_hold_release.release_reason(root, run_id, exclude_run, None))
    except Exception:  # noqa: BLE001 -- unreadable record holds
        return False


def _landed(root: Path, run_id: str) -> bool:
    """A run whose record marker is `landed` (`halted_run.mark_landed`) has put its work in a
    commit: it writes nothing more and claims nothing, whatever its token's remaining TTL."""
    from coordinator_core.ops.dispatch_emit import halted_run

    if _no_marker(root, run_id):
        return False
    try:
        marker = halted_run.read_strict_run(record_dir(root, run_id))
    except ValueError:  # UnreadableHaltRecord, or not a run id: no landed record
        return False
    return marker is not None and marker.get("status") == halted_run.STATUS_LANDED


def _manifest_declared(run_id: str, root: Path) -> tuple:
    """`(paths, prefixes, unreadable)` a run's `manifest.json` (`rows[].writes`, `doc_writes`)
    and `commit-request.txt` (chunk paths and prefixes) declare, normalized. `unreadable` is True
    when a file exists but does not parse; an absent file is not unreadable. A manifest that fails
    mid-parse keeps the paths gathered before the failure. Inside `halted_hold_release.release_scope()` the
    answer, `unreadable` included, is memoised per (root, run_id) in the scope's state; outside a scope
    nothing is cached."""
    from coordinator_core.ops.dispatch_emit import halted_hold_release

    state = halted_hold_release._SCOPE.get()
    if state is None:
        return _manifest_declared_uncached(run_id, root)
    memo = state.setdefault("manifest_declared", {})
    key = (str(root), run_id)
    if key not in memo:
        memo[key] = _manifest_declared_uncached(run_id, root)
    return memo[key]


def _manifest_declared_uncached(run_id: str, root: Path) -> tuple:
    paths: list = []
    prefixes: list = []
    unreadable = False
    from coordinator_core.git import call_io_scope

    try:
        raw_manifest = call_io_scope.read_bytes(record_dir(root, run_id) / "manifest.json")
        text = raw_manifest.decode("utf-8") if raw_manifest is not None else None
    except (OSError, ValueError):
        text = None
    if text is not None:
        try:
            data = json.loads(text)
            rows = data["rows"]
            for row in rows:
                paths.extend(w for w in row["writes"] if isinstance(w, str))
            paths.extend(w for w in (data.get("doc_writes") or ()) if isinstance(w, str))
        except (ValueError, KeyError, TypeError):
            unreadable = True
    try:
        raw_request = call_io_scope.read_bytes(root / RUN_DIR_ROOT / run_id / COMMIT_REQUEST_FILE)
        request = parse_marker(raw_request.decode("utf-8")) if raw_request is not None else None
    except OSError:
        request = None
    except MalformedCommitRequestError:
        request = None
        unreadable = True
    if request is not None:
        for chunk in request.chunks:
            paths.extend(chunk.paths)
            prefixes.extend(chunk.prefixes)
    return (
        tuple(p for p in (_norm(x) for x in paths) if p),
        tuple(p.rstrip("/") for p in (_norm(x) for x in prefixes) if p.rstrip("/")),
        unreadable,
    )


def _withheld_paths(root: Path, run_id: str) -> set:
    """Normalized paths a run recorded in `withhold.jsonl`: it promised not to carry them."""
    from coordinator_core.ops.dispatch_emit import run_withhold

    return {n for n in (_norm(p) for p in run_withhold.read([record_dir(root, run_id)])) if n}


#: A run still at stage `minted` this long after its last recorded activity is abandoned: its agent
#: died or was stopped before the ledger advanced it, and it holds nothing against a peer.
MINTED_STALE_SECONDS = 1800
ABANDONED_AT_MINTED = "abandoned_at_minted"
#: A run at stage `staged` with no live session and no activity this long (hours) holds nothing.
STAGED_STALE_SECONDS = 4 * 3600
ABANDONED_AT_STAGED = "abandoned_at_staged"


def _stamp_epoch(value: object) -> Optional[float]:
    """Epoch seconds of a `run-stage.json` ISO-8601 UTC stamp, None when absent or unparseable."""
    from datetime import datetime, timezone

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _freshest_run_artifact_epoch(root: Path, run_id: str) -> Optional[float]:
    """Newest mtime among the entries of the run's record dir and scratch run dir, plus the mtime of
    each subdirectory there (a file added under it bumps it); `halted.json` and `run-stage.json` (the
    stamp is read separately) excluded. A launcher that drives a child's stages leaves its writes
    here while the child's own `run-stage.json` stays at `minted`. None when neither dir is
    readable. Stat calls only."""
    import os

    newest: Optional[float] = None
    for base in (record_dir(root, run_id), Path(root) / RUN_DIR_ROOT / run_id):
        try:
            with os.scandir(base) as entries:
                for entry in entries:
                    if entry.name in ("halted.json", "run-stage.json"):
                        continue
                    mtime = entry.stat().st_mtime
                    if newest is None or mtime > newest:
                        newest = mtime
        except OSError:
            continue
    return newest


def _own_activity_epoch(root: Path, run_id: str) -> Optional[float]:
    """Later of a run's stage stamp (`last_activity_at`, else `started_at`) and its newest artifact
    (`_freshest_run_artifact_epoch`); None when neither exists."""
    from coordinator_core.ops.dispatch_emit.ask_contract import read_run_stage

    stage = read_run_stage(root, run_id) or {}
    seen = _stamp_epoch(stage.get("last_activity_at"))
    if seen is None:
        seen = _stamp_epoch(stage.get("started_at"))
    artifact = _freshest_run_artifact_epoch(root, run_id)
    if artifact is not None and (seen is None or artifact > seen):
        seen = artifact
    return seen


def _descends_from(root: Path, run_id: str, ancestor: str, ancestor_sizing: str) -> bool:
    """Whether `run_id` is a descendant run of `ancestor`: its `run.json` `resumed_from` chain reaches
    `ancestor` (a warp child names the ask run it continues there), or at any hop its `sizing_rel`
    is `ancestor_sizing` (the child binds the ask run's sizing). Cycle-safe, capped at
    `CARRIED_HOP_CAP`; run ids are compared, never parsed."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

    seen: set = set()
    cur = run_id
    for _ in range(rl.CARRIED_HOP_CAP):
        if cur in seen:
            return False
        seen.add(cur)
        data = _run_json(root, cur)
        sizing = data.get("sizing_rel")
        if ancestor_sizing and isinstance(sizing, str) and _norm(sizing) == ancestor_sizing:
            return True
        prev = data.get(rl.RESUMED_FROM_FIELD)
        if not (isinstance(prev, str) and safe_id(prev)):
            return False
        if prev == ancestor:
            return True
        cur = prev
    return False


def _descendant_activity_epoch(
    root: Path, run_id: str, now: float, bound: float = MINTED_STALE_SECONDS,
) -> Optional[float]:
    """Newest activity epoch among live descendant runs of `run_id` (`_descends_from`), None when no
    descendant was active within `bound` seconds of `now`. A descendant that is halted or
    landed does not count. Candidates are the `RECORD_DIR_ROOT` entries whose directory mtime falls
    in the window (an atomic stage write or a new artifact bumps it), so the scan is one listing plus
    reads for the few recent dirs; stat/read calls only."""
    ask_sizing = _lineage_sizing(root, run_id)
    newest: Optional[float] = None
    try:
        with os.scandir(Path(root) / RECORD_DIR_ROOT) as entries:
            recent = [
                e.name for e in entries
                if e.name != run_id and e.is_dir() and now - e.stat().st_mtime <= bound
            ]
    except OSError:
        return None
    for name in recent:
        if not safe_id(name) or not _descends_from(root, name, run_id, ask_sizing):
            continue
        if _halted(root, name) or _landed(root, name):
            continue
        seen = _own_activity_epoch(root, name)
        if seen is not None and now - seen <= bound and (newest is None or seen > newest):
            newest = seen
    return newest


def _stale_at_minted(root: Path, run_id: str, now: Optional[float] = None) -> bool:
    """`_stale_at` for stage `minted` and `MINTED_STALE_SECONDS`."""
    return _stale_at(root, run_id, "minted", MINTED_STALE_SECONDS, now)


def _stale_at_staged(root: Path, run_id: str, now: Optional[float] = None) -> bool:
    """`_stale_at` for stage `staged` and `STAGED_STALE_SECONDS`."""
    return _stale_at(root, run_id, "staged", STAGED_STALE_SECONDS, now)


def _stale_at(root: Path, run_id: str, stage_name: str, bound: float, now: Optional[float] = None) -> bool:
    """True when the run's `run-stage.json` reads `stage_name` with its freshest activity older than
    `bound` seconds. Freshest activity is the later of the stage stamp (`last_activity_at`,
    else `started_at`), the newest artifact the launcher left in the run's record/scratch dirs
    (`_freshest_run_artifact_epoch`), and, once those read stale, any live descendant run's activity
    (`_descendant_activity_epoch`): an ask run driven through a child warp run stays at `minted`.
    An absent or malformed stage record, any other stage, or no parseable stamp is not stale."""
    import time

    from coordinator_core.ops.dispatch_emit.ask_contract import read_run_stage

    stage = read_run_stage(root, run_id)
    if not stage or stage.get("stage") != stage_name:
        return False
    seen = _stamp_epoch(stage.get("last_activity_at"))
    if seen is None:
        seen = _stamp_epoch(stage.get("started_at"))
    if seen is None:
        return False
    artifact = _freshest_run_artifact_epoch(root, run_id)
    if artifact is not None and artifact > seen:
        seen = artifact
    current = time.time() if now is None else now
    if current - seen <= bound:
        return False
    child = _descendant_activity_epoch(root, run_id, current, bound)
    return child is None or current - child > bound


def _halt_abandoned(root: Path, run_id: str, stage_name: str = "minted") -> None:
    """Record the terminal `halted` outcome of a run stale at `minted` or `staged`, so `_halted` sees
    it and the firing EM finds a halt record. Best effort: a failed write leaves the run merely
    released."""
    from coordinator_core.ops.dispatch_emit import halted_run

    staged = stage_name == "staged"
    bound = STAGED_STALE_SECONDS if staged else MINTED_STALE_SECONDS
    try:
        halted_run.write_halt_marker(
            record_dir(root, run_id),
            halted=ABANDONED_AT_STAGED if staged else ABANDONED_AT_MINTED,
            sizing_rel=record_sizing_rel(root, run_id) or None,
            reason=f"abandoned: stuck at stage {stage_name} for over {bound}s with no activity",
        )
    except Exception:  # noqa: BLE001 -- the release does not depend on the record
        logging.getLogger(__name__).warning("run %s: abandoned-at-%s halt record not written", run_id, stage_name)


def _live_peer_run(
    root: Path, record: dict, exclude_session: str, cwd: Optional[str], minted_before: Optional[tuple] = None,
) -> bool:
    """True when a held `record` is a live peer's run: fresh (claimed records included), a
    non-empty `session_id` other than `exclude_session` that is `touch_record.session_live`, not
    halted, not landed, not stale at stage `minted` or `staged` (`_stale_at_minted`,
    `_stale_at_staged`; such a run is halted here),
    not minted with an unproven runner (`warp_live.runner_unproven`; resumable, not held),
    and (when given) strictly senior to `minted_before=(minted_at, run_id)`. Inside a `call_io_scope`
    the record-reading tail is answered once per run and `cwd`: the landing's several claim scans
    weigh each sibling run once."""
    from coordinator_core.git import call_io_scope

    sid = record.get("session_id")
    run_id = record["run_id"]
    if not isinstance(sid, str) or not sid or sid == exclude_session or not run_token._fresh(record):
        return False
    if minted_before is not None and (record["minted_at"], run_id) >= tuple(minted_before):
        return False
    return call_io_scope.memo(
        "wrd.live_peer_run", repr((str(root), run_id, sid, cwd)), lambda: _live_peer_tail(root, run_id, sid, cwd),
    )


def _live_peer_tail(root: Path, run_id: str, sid: str, cwd: Optional[str]) -> bool:
    from coordinator_core.session import touch_record

    if _halted(root, run_id) or _landed(root, run_id) or not touch_record.session_live(sid, cwd):
        return False
    if _stale_at_minted(root, run_id):
        _halt_abandoned(root, run_id)
        return False
    if _stale_at_staged(root, run_id):
        _halt_abandoned(root, run_id, "staged")
        return False
    from coordinator_core import warp_live

    return not warp_live.runner_unproven(root, run_id)


def live_record_dir_owners(
    repo_root: Path, paths: Iterable[str], *, exclude_session: str, cwd: Optional[str] = None,
) -> dict:
    """`{path: sorted peer session ids}` for each of `paths` under a run's record dir
    (`RECORD_DIR_ROOT/<run_id>/...`) whose run is held by a live peer (`_live_peer_run`).

    Paths outside a record dir, runs held by `exclude_session`, and unheld runs are absent.
    One token-dir listing, no spawn."""
    root = Path(repo_root)
    prefix = RECORD_DIR_ROOT + "/"
    by_run: dict = {}
    for path in (_norm(p) for p in paths if p):
        if path.startswith(prefix):
            run_id = path[len(prefix):].split("/", 1)[0]
            if run_id:
                by_run.setdefault(run_id, []).append(path)
    owners: dict = {}
    if not by_run:
        return owners
    for record in run_token.held_records(root):
        run_id = record["run_id"]
        if run_id in by_run and _live_peer_run(root, record, exclude_session, cwd):
            for path in by_run[run_id]:
                owners.setdefault(path, set()).add(record["session_id"])
    return {p: sorted(s) for p, s in owners.items()}


def _run_json(root: Path, run_id: str) -> dict:
    """A run's `run.json` from its record dir, else from `RUN_DIR_ROOT/<run_id>`: an in-flight run's
    record dir can already exist under `state/warp` (a usage record) while its `run.json` is still in scratch."""
    for base in (RECORD_DIR_ROOT, RUN_DIR_ROOT):
        data = _read_json(Path(root) / base / run_id / "run.json")
        if data:
            return data
    return {}


def _lineage_sizing(root: Path, run_id: str) -> str:
    value = _run_json(root, run_id).get("sizing_rel")
    return _norm(value) if isinstance(value, str) else ""


def _own_lineage(root: Path, run_id: Optional[str]) -> tuple:
    """`(sizing_rel, resumed_from predecessor run ids)` of `run_id`; `("", frozenset())` when the run has no
    sizing, so no predecessor can match. The chain walk is cycle-safe and capped at `CARRIED_HOP_CAP`."""
    if run_id is None or not safe_id(run_id):
        return "", frozenset()
    sizing = _lineage_sizing(root, run_id)
    if not sizing:
        return "", frozenset()
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

    chain: list = []
    cur = run_id
    for _ in range(rl.CARRIED_HOP_CAP):
        prev = _run_json(root, cur).get(rl.RESUMED_FROM_FIELD)
        if not (isinstance(prev, str) and safe_id(prev)) or prev == run_id or prev in chain:
            break
        chain.append(prev)
        cur = prev
    return sizing, frozenset(chain)


_SCOPE_PEERS: ContextVar[Optional[tuple]] = ContextVar("warp_run_deliverable_scope_peers", default=None)


def _scoped_peer_views() -> Optional[dict]:
    """The per-land_run-scope `{view key: peer view}` memo; None outside a `land_deadline.scope`. Keyed by
    the scope's deadline value, so a new scope starts empty and nothing is held across scopes."""
    from coordinator_core.ops.dispatch_emit import land_deadline

    deadline = land_deadline._DEADLINE.get()
    if deadline is None:
        return None
    held = _SCOPE_PEERS.get()
    if held is None or held[0] != deadline:
        held = (deadline, {})
        _SCOPE_PEERS.set(held)
    return held[1]


def _peer_view(
    root: Path, exclude_session: str, cwd: Optional[str], minted_before: Optional[tuple], exclude_run: Optional[str],
) -> Optional[list]:
    """`[(minted_at, run_id, session_id, declared_set, prefixes, withheld)]` per live peer run, in
    `held_records` order; None when a live peer's declaration is unreadable (contest unknown).
    Inside a `land_deadline.scope` it is built once per argument tuple, so the land_run's several claim
    readers share one `held_records` pass and one verdict, manifest and withheld read per run."""
    views = _scoped_peer_views()
    key = (str(root), exclude_session, cwd, None if minted_before is None else tuple(minted_before), exclude_run)
    if views is not None and key in views:
        return views[key]
    view = _build_peer_view(root, exclude_session, cwd, minted_before, exclude_run)
    if views is not None:
        views[key] = view
    return view


def _build_peer_view(
    root: Path, exclude_session: str, cwd: Optional[str], minted_before: Optional[tuple], exclude_run: Optional[str],
) -> Optional[list]:
    own_sizing, predecessors = _own_lineage(root, exclude_run)
    view: list = []
    for record in run_token.held_records(root):
        run_id = record["run_id"]
        if exclude_run is not None and run_id == exclude_run:
            continue
        if not _live_peer_run(root, record, exclude_session, cwd, minted_before):
            continue
        if run_id in predecessors and _lineage_sizing(root, run_id) == own_sizing:
            continue
        if resumed_run_released(root, run_id, exclude_run):
            continue
        declared, prefixes, unreadable = _manifest_declared(run_id, root)
        if unreadable and not declared and not prefixes:
            logging.getLogger(__name__).warning("live run %s: unreadable write declaration", run_id)
            return None
        view.append(
            (record["minted_at"], run_id, record.get("session_id"), frozenset(declared), prefixes, _withheld_paths(root, run_id))
        )
    return view


def _live_claim_scan(
    repo_root: Path,
    paths: Iterable[str],
    exclude_session: str,
    cwd: Optional[str],
    minted_before: Optional[tuple],
    exclude_run: Optional[str],
) -> Optional[dict]:
    """`{path: [(minted_at, run_id, session_id)]}` oldest first; None when contest unknown.
    The single `run_token.held_records` pass behind both claim readers; filters as `live_run_write_claims`."""
    root = Path(repo_root)
    wanted = {_norm(p) for p in paths if p}
    wanted.discard("")
    claims: dict = {}
    if not wanted:
        return claims
    view = _peer_view(root, exclude_session, cwd, minted_before, exclude_run)
    if view is None:
        return None
    # A peer's bare prefix (`writes_under`, as wide as `coordinator_core/`) never holds a path the
    # asking run names exactly: it would strand that run's own written files at checkpoint and empty
    # its review prep, for the peer token's whole TTL. An exact peer declaration still holds.
    own_exact = set(_manifest_declared(exclude_run, root)[0]) if exclude_run else set()
    for minted_at, run_id, sid, declared_set, prefixes, withheld in view:
        for path in wanted - withheld:
            if path in declared_set or (
                path not in own_exact and any(path.startswith(pre + "/") or path == pre for pre in prefixes)
            ):
                claims.setdefault(path, []).append((minted_at, run_id, sid))
    for entries in claims.values():
        entries.sort()
    return claims


def live_run_write_claims(
    repo_root: Path,
    paths: Iterable[str],
    *,
    exclude_session: str,
    cwd: Optional[str] = None,
    minted_before: Optional[tuple] = None,
    exclude_run: Optional[str] = None,
) -> Optional[dict]:
    """`{path: sorted peer session ids}` for each of `paths` a live peer run declares it writes.

    A peer run is a held token (`run_token.held_records`, listed once) that is fresh, whose
    non-empty `session_id` differs from `exclude_session` and is `touch_record.session_live`.
    `exclude_run` skips that run id before any other test, and each run in its `resumed_from` chain
    whose record names the same non-empty `sizing_rel` (a predecessor fire; a concurrent sibling of the
    same sizing still holds, and touch claims are not read here). `minted_before=(minted_at, run_id)`
    keeps only strictly senior runs. A path matches a declared path or sits under a declared
    prefix, unless the run recorded it with `workflow.revise withhold_paths`. Returns None (contest unknown) when a live peer's declaration is unparseable and
    yields no usable paths; {} when nothing is declared."""
    scan = _live_claim_scan(repo_root, paths, exclude_session, cwd, minted_before, exclude_run)
    if scan is None:
        return None
    return {p: sorted({sid for _, _, sid in e}) for p, e in scan.items()}


def halted_run_write_claims(
    repo_root: Path,
    paths: Iterable[str],
    *,
    exclude_run: Optional[str],
    now: Optional[float] = None,
    own_plan: Optional[str] = None,
    dirty=None,
    attributable=None,
) -> Optional[dict]:
    """`{path: sorted halted run ids}` for each of `paths` a halted, unresumed run's halt record
    names as withheld, plus (when `dirty`, a no-arg callable returning the set of paths dirty in
    the tree, is given) each path its manifest declares that is dirty now and carries hunks
    attributable to that run (`attributable(run_id, paths) -> held subset`, default a lazily built
    `halted_hunk_attribution.reader`). `dirty` is called at most
    once per call, and only when an unreleased halted run declares a candidate; a halted run that
    withheld nothing and left nothing dirty holds nothing.

    The withheld set is `halted_hold_release.held_paths(root, run_id, exclude_run, own_plan)`: empty
    for a run it frees (superseded sizing, same sizing/deliverable as `exclude_run`, or `own_plan`
    landing), so a freed run claims nothing from its record either.

    Only record dirs whose run-id stamp is within `run_token.TTL_SECONDS` of `now` are read; a name
    with no stamp, or an older one, costs no IO. `exclude_run` is skipped. No session filter."""
    import time

    from coordinator_core.ops.dispatch_emit import halted_hold_release, halted_run, stopped_runs

    root = Path(repo_root)
    wanted = {_norm(p) for p in paths if p}
    wanted.discard("")
    claims: dict = {}
    if not wanted:
        return claims
    moment = time.time() if now is None else now
    try:
        with os.scandir(root / RECORD_DIR_ROOT) as entries:
            names = sorted(e.name for e in entries if e.is_dir())
    except OSError:
        return claims
    with halted_hold_release.blob_scope(root):
        return _halted_claims(
            root, wanted, names, exclude_run, moment, own_plan, dirty, attributable, halted_hold_release, stopped_runs,
        )


def _index_clear(root: Path, run_id: str, wanted: set) -> bool:
    """True when `run_id`'s fresh `halted_withheld_index` entry shows neither its withheld paths nor its
    declared footprint cover any of `wanted`, and it left no `commit-request.txt` (the footprint the entry
    does not carry): such a run claims none of them, so its marker is never opened. Absent, stale or
    covering entries are False, and the caller reads the marker."""
    from coordinator_core.git import call_io_scope
    from coordinator_core.ops.dispatch_emit import run_ownership

    try:
        entry = _fresh_entry(root, run_id)
        if entry is None or call_io_scope.read_bytes(root / RUN_DIR_ROOT / run_id / COMMIT_REQUEST_FILE) is not None:
            return False
    except (OSError, ValueError):
        return False
    return not any(run_ownership._covers(w, c) for w in (*entry.paths, *entry.declared) for c in wanted)


def _halted_claims(
    root: Path, wanted: set, names: list, exclude_run: Optional[str], moment: float, own_plan: Optional[str],
    dirty, attributable, halted_hold_release, stopped_runs,
) -> dict:
    """`halted_run_write_claims` body, run inside one `halted_hold_release.blob_scope` whose blobs are
    prefetched for every in-window halted run in a single git spawn."""
    from coordinator_core.ops.dispatch_emit import halted_run

    claims: dict = {}
    halted_ids = []
    for run_id in names:
        if exclude_run is not None and run_id == exclude_run:
            continue
        stamp = stopped_runs._stamp_epoch(run_id)
        if stamp is None or moment - stamp > run_token.TTL_SECONDS:
            continue
        if _index_clear(root, run_id, wanted):
            continue
        if _halted(root, run_id):
            halted_ids.append(run_id)
    markers: dict = {}

    def strict_marker(run_id: str):
        # One parse per run for this pass; UnreadableHaltRecord is not cached, it re-raises.
        if run_id not in markers:
            markers[run_id] = halted_run.read_strict_run(record_dir(root, run_id))
        return markers[run_id]

    plans = []
    for run_id in halted_ids:
        try:
            rdir = record_dir(root, run_id)
            marker = strict_marker(run_id)
        except Exception:  # noqa: BLE001 -- unreadable record: read (and held) on demand
            continue
        if marker is not None:
            plans.append((rdir, marker))
    halted_hold_release.prefetch_kept_blobs(root, plans)
    for run_id in halted_ids:
        try:
            if _fresh_entry(root, run_id) is None:
                strict_marker(run_id)
        except halted_run.UnreadableHaltRecord as exc:
            for path in wanted:
                claims.setdefault(path, set()).add(exc.reason)
            continue
        held = set(halted_hold_release.held_paths(root, run_id, exclude_run, own_plan))
        freed: set = set()
        if held & wanted:
            try:
                rdir = record_dir(root, run_id)
                marker = strict_marker(run_id)
                if marker is not None:
                    freed = {_norm(p) for p in halted_hold_release.kept_at_head_paths(root, rdir, marker)}
            except Exception:  # noqa: BLE001 -- unreadable record: nothing freed, the hold stands
                freed = set()
            held = held - freed
        if dirty is not None:
            declared, prefixes, _unreadable = _manifest_declared(run_id, root)
            if declared or prefixes:
                declared_set = set(declared)
                hot = [p for p in wanted if p not in held and p not in freed and (
                    p in declared_set or any(p == pre or p.startswith(pre + "/") for pre in prefixes))]
                if hot:
                    if attributable is None:
                        from coordinator_core.ops.dispatch_emit import halted_hunk_attribution

                        attributable = halted_hunk_attribution.reader(root, exclude_run, landing_plan=own_plan)
                    held = held | attributable(run_id, (set(hot) & dirty()) - held)
        for path in wanted & held:
            claims.setdefault(path, set()).add(run_id)
    return {p: sorted(s) for p, s in claims.items()}


def halted_withheld_paths(root: Path, run_id: str) -> set:
    """Normalized paths a halted run's marker records as withheld (hunks it left in the tree);
    empty when the marker has none or is absent; `UnreadableHaltRecord` when it is corrupt."""
    from coordinator_core.ops.dispatch_emit import halted_run

    withheld = halted_run.halted_withheld_strict(record_dir(root, run_id))
    return {n for n in (_norm(p) for p in withheld) if n}


def write_reconciled(run_dir: Path, sizing_rel: str, deliverable_id: str, shas: Iterable[str]) -> Path:
    """Write the run-local terminal marker naming the commits that shipped the run by hand."""
    from coordinator_core.atomic_replace import atomic_write_bytes

    target = Path(run_dir) / RECONCILED_FILE
    body = {"sizing_rel": _norm(sizing_rel), "deliverable_id": deliverable_id, "shas": list(shas)}
    atomic_write_bytes(target, json.dumps(body, sort_keys=True).encode("utf-8"), preserve_mode=False)
    return target
