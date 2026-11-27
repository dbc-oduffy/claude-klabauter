"""Abandoned-run contract: ``dispatch.reap_abandoned_runs`` lands the ``state/warp/<run_id>/``
record dirs of ask runs that died before any terminal or halted commit, and the stranded plan,
handoff, sidecar, delivery note or sizing of a run whose record dir already landed.

Params: ``{}``, ``{"dry_run": bool}``, ``{"run_ids": [str]}`` (reap only those; combinable with dry_run),
``{"correct_closed": true}`` (rewrite each stored ``shipped`` outcome whose run's ``halted.json`` reads
``closed`` to ``closed`` and commit only those; no reap; reply ``{ok, corrected: [run_id], committed_sha}``).
Reply: ``{ok, reaped: [run_id], sizings: [rel], artifacts: {run_id: [rel]}, committed_sha | null,
skipped: [{run_id, reason, path?}]}``; a commit that loses the ref CAS is re-called on the new HEAD
(bounded) and an overlap or exhaustion replies ``{ok: False, refused: <ref_cas_retry kind>, error}``.

A run is ended when it has changed top-level record files or a dirty artifact that
``run_artifacts.runs_of_artifacts`` maps to it (newest mtime then comes from those artifacts), no run token with activity inside
``TOKENLESS_QUIET_SECONDS`` (an idle token is released, and its live-run index entry dropped,
when its run is reaped), and either its newest record file is older than ``TOKENLESS_QUIET_SECONDS`` (``run_token.TTL_SECONDS``
is the outer bound), or started more than ``ORPHAN_WALL_SECONDS`` ago with no live session, token
or stage, or older than ``QUIET_SECONDS`` and the run is superseded by a later record dir binding the
same sizing, halt-marked, or carries a tracked top-level file modified after its commit. Each
ended run lands with its ``run_artifacts`` candidates (sizing, baton, plan, sidecars, slice
sizings, delivery note).
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Mapping, Optional

from coordinator_core.git.ref_cas_retry import commit_v2_retrying
from coordinator_core.git.run import run_git
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops.dispatch_emit import process_artifact_policy, run_artifacts, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, halted_record_message
from coordinator_core.ops.dispatch_emit.worktree_snapshot import record_dir_changes
from coordinator_core.ops.dispatch_emit.halted_run import _STAMP, HALTED_FILE, STATUS_CLOSED, STATUS_RESUMED
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import STATUS_HALTED
from coordinator_core.session.record_homes import home_dir
from coordinator_core.warm.live_runs import owner_live as _owner_live, run_owner_holds, run_stage_live

OP_NAME = "dispatch.reap_abandoned_runs"
HALTED_KIND = "abandoned"
SIZING_ROOT = Path(home_dir("", "sizings")).as_posix()
RUN_FILE = "run.json"
QUIET_SECONDS = 600
TOKENLESS_QUIET_SECONDS = 3600
ORPHAN_WALL_SECONDS = 14400
STATUS_ROOTS = [
    RECORD_DIR_ROOT, SIZING_ROOT, Path(home_dir("", "handoffs")).as_posix(), "docs/plans", "docs/delivery",
    run_artifacts.EVIDENCE_ROOT,
]
HELD_MARKER_ROOT = ".coordinator-local/reap-held"
RUN_STAGE_FILE = "run-stage.json"
MINTED_STAGE = "minted"


def abandoned_run_ids(
    changed: set[str],
    live_run_ids: set[str],
    record_root: str,
    newest_mtime: dict[str, float],
    now: float,
    *,
    later_stamp_of: Optional[Mapping[str, str]] = None,
    halted: Optional[set[str]] = None,
    late_written: Optional[set[str]] = None,
    artifact_runs: Optional[set[str]] = None,
    minted: Optional[set[str]] = None,
    past_wall: Optional[set[str]] = None,
) -> list[str]:
    """Sorted run ids with a changed file directly under ``<record_root>/<run_id>/`` or named in
    ``artifact_runs`` (runs owning a dirty plan, handoff, sidecar, delivery note or sizing), absent
    from ``live_run_ids`` (live AND claimed tokens), and ended: newest mtime older than the token
    TTL, or older than ``QUIET_SECONDS`` when the run is superseded (a key of ``later_stamp_of``),
    halt-marked (in ``halted``) or has a tracked top-level file modified after its commit (in
    ``late_written``), or a ``minted`` run (never fired; a candidate without a changed file)
    quiet for ``QUIET_SECONDS``, or a ``past_wall`` run (started more than ``ORPHAN_WALL_SECONDS``
    ago) whatever its newest mtime. A run id with no ``newest_mtime`` entry is not abandoned. Pure."""
    prefix = record_root.rstrip("/") + "/"
    past_wall = past_wall or set()
    superseded = later_stamp_of or {}
    halted = halted or set()
    late_written = late_written or set()
    minted = minted or set()
    candidates: set[str] = set(artifact_runs or ()) | minted
    for rel in changed:
        if not rel.startswith(prefix):
            continue
        run_id, sep, leaf = rel[len(prefix):].partition("/")
        if run_id and sep and leaf and "/" not in leaf:
            candidates.add(run_id)

    def ended(run_id: str) -> bool:
        if run_id not in newest_mtime:
            return False
        age = now - newest_mtime[run_id]
        if age > run_token.TTL_SECONDS or run_id in past_wall:
            return True
        # A run mid-flight always holds a token, so an hour of tokenless quiet means it ended.
        if age > TOKENLESS_QUIET_SECONDS:
            return True
        return age > QUIET_SECONDS and (
            run_id in minted or run_id in superseded or run_id in halted or run_id in late_written
        )

    return sorted(run_id for run_id in candidates - live_run_ids if ended(run_id))


def reap_message(run_ids: list[str]) -> str:
    """Commit message for the reaper's one commit: the halted-record form with
    ``Halted-By: abandoned``. Pure."""
    return halted_record_message(", ".join(run_ids), {"halted": HALTED_KIND})


def _token_records(repo_root: Path) -> list[dict]:
    """Every live record plus every claimed one (a run mid terminal-commit). One directory
    listing each; no spawn."""
    return run_token.held_records(repo_root)


def _token_active(root: Path, record: dict, newest: Mapping[str, float], now: float) -> bool:
    """A token holds its sizing only while its run shows activity inside ``TOKENLESS_QUIET_SECONDS``:
    the newest of its mint time, its token files and its changed record files."""
    run_id = record["run_id"]
    stamps = [newest.get(run_id, 0.0)]
    minted = record.get("minted_at")
    if isinstance(minted, (int, float)):
        stamps.append(float(minted))
    for claimed in (False, True):
        path = run_token._record_path(root, run_id, claimed=claimed)
        try:
            stamps.append(path.stat().st_mtime if path is not None else 0.0)
        except OSError:
            pass
    return now - max(stamps) <= TOKENLESS_QUIET_SECONDS


def _run_sizing(run_dir: Path) -> Optional[str]:
    """The sizing a run's ``run.json`` names, repo-relative, else ``None``."""
    try:
        data = json.loads((run_dir / RUN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rel = data.get("sizing_rel") if isinstance(data, dict) else None
    return rel.replace("\\", "/") if isinstance(rel, str) and rel else None


def _status(root: Path):
    """One scoped status read over ``STATUS_ROOTS``: ``(snapshot, tracked)`` where ``tracked`` is
    the changed paths git already knows (modified, added, unmerged), or ``None`` on failure."""
    from coordinator_core.ops.dispatch_emit.worktree_snapshot import Snapshot

    roots = [r.rstrip("/") for r in STATUS_ROOTS]
    result = run_git(
        [
            "-C", str(root), "--no-optional-locks", "status",
            "--porcelain=v2", "-z", "--no-renames", "--untracked-files=all",
            "--ignored=matching", "--", *[f":(literal){r}/" for r in roots],
        ],
        binary=True,
    )
    if not result.ok:
        return None
    changed: set = set()
    tracked: set = set()
    ignored: list = []
    for raw in result.stdout_bytes.split(b"\0"):
        if len(raw) < 3:
            continue
        kind = raw[:1]
        fields = {b"1": 8, b"u": 10, b"?": 1, b"!": 1}.get(kind)
        if fields is None:
            continue
        path = raw.split(b" ", fields)[fields].decode("utf-8", "surrogateescape")
        if kind == b"!":
            ignored.append(path)
            continue
        changed.add(path)
        if kind != b"?":
            tracked.add(path)
    return Snapshot(changed, ignored, set(), roots), tracked


def _held_marker(root: Path, run_id: str) -> Path:
    return root / HELD_MARKER_ROOT / f"{run_id}.json"


def _already_reaped(root: Path, run_id: str, newest: Optional[float], changed: set[str]) -> bool:
    """True when this run was reaped before and nothing new has landed in it: its tracked
    ``outcome.json`` reads ``abandoned`` or ``closed`` and is itself committed (not in ``changed``), or a non-host
    repo's held-back marker is at least as new as the run's newest file. Two stats and one read."""
    outcome = run_artifacts.outcome_rel(run_id)
    if outcome not in changed and run_artifacts.read_tracked_outcome(root, run_id).get("state") in ("abandoned", STATUS_CLOSED):
        return True
    try:
        held_at = _held_marker(root, run_id).stat().st_mtime
    except OSError:
        return False
    return newest is not None and newest <= held_at


def _minted_runs(root: Path, only: Optional[set[str]], now: float) -> dict[str, float]:
    """``{run_id: last_activity_epoch}`` for record dirs whose ``run-stage.json`` stage is ``minted``
    and quiet beyond ``QUIET_SECONDS``. One ``scandir`` of the record root and one small file read per
    dir; no spawn. ``only`` restricts the scan to those ids."""

    out: dict[str, float] = {}
    try:
        with os.scandir(root / RECORD_DIR_ROOT) as it:
            entries = [e for e in it if e.is_dir() and (only is None or e.name in only)]
    except OSError:
        return out
    for entry in entries:
        stage_path = Path(entry.path) / RUN_STAGE_FILE
        try:
            data = json.loads(stage_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("stage") != MINTED_STAGE:
                continue
            stamp = data.get("last_activity_at")
            try:
                at = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
            except (AttributeError, ValueError):
                at = stage_path.stat().st_mtime
        except (OSError, ValueError):
            continue
        if now - at > QUIET_SECONDS:
            out[entry.name] = at
    return out


def _top_level_run(rel: str) -> Optional[str]:
    prefix = RECORD_DIR_ROOT + "/"
    if not rel.startswith(prefix):
        return None
    run_id, sep, leaf = rel[len(prefix):].partition("/")
    return run_id if run_id and sep and leaf and "/" not in leaf else None


def _stamp_of(run_id: str) -> Optional[str]:
    matches = _STAMP.findall(run_id)
    return matches[-1] if matches else None


def _later_binding_runs(root: Path, sizing_of: Mapping[str, str]) -> dict[str, str]:
    """``{run_id: later_run_id}`` for each run whose sizing a later-stamped record dir also binds.
    One ``scandir`` of the record root; ``run.json`` is read only for later dirs sharing the
    sizing's ``warp-<stem>-`` prefix or an ``ask-`` prefix."""
    if not sizing_of:
        return {}
    try:
        with os.scandir(root / RECORD_DIR_ROOT) as it:
            names = sorted(e.name for e in it if e.is_dir())
    except OSError:
        return {}
    cache: dict[str, Optional[str]] = {}
    out: dict[str, str] = {}
    for run_id, sizing in sizing_of.items():
        mine = _stamp_of(run_id)
        if mine is None:
            continue
        stem_prefix = f"warp-{Path(sizing).stem}-"
        for name in names:
            tail = _stamp_of(name)
            if name == run_id or tail is None or tail <= mine:
                continue
            if not (name.startswith(stem_prefix) or name.startswith("ask-")):
                continue
            if name not in cache:
                cache[name] = _run_sizing(root / RECORD_DIR_ROOT / name)
            if cache[name] == sizing:
                out[run_id] = name
                break
    return out


TERMINAL_STATES = ("landed", "shipped")
_SIZING_STATUS = re.compile(r"^status:\s*([A-Za-z_-]+)", re.MULTILINE)
_SIZING_RUN_ID = re.compile(r"^\s+run_id:\s*[\"']?([^\"'\s#]+)", re.MULTILINE)
_WORKFLOW_SUFFIX = ".workflow"


def _bare(run_id: str) -> str:
    return run_id[: -len(_WORKFLOW_SUFFIX)] if run_id.endswith(_WORKFLOW_SUFFIX) else run_id


def _sizing_ships_run(root: Path, run_id: str) -> bool:
    """True when the sizing the run's ``run.json`` (else ``manifest.json``) names reads ``status: shipped``
    and carries this run's id, with or without the emitted script's ``.workflow`` suffix. Trap: a run
    that landed through a workflow-suffixed id can leave ``run-stage.json`` at ``staged`` with no
    outcome file, so the sizing is the only record of the landing. One or two small reads; no spawn."""
    rec = root / RECORD_DIR_ROOT / run_id
    sizing = _run_sizing(rec)
    if sizing is None:
        try:
            data = json.loads((rec / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        rel = data.get("sizing_rel") if isinstance(data, dict) else None
        sizing = rel.replace("\\", "/") if isinstance(rel, str) and rel else None
    if sizing is None:
        return False
    try:
        text = (root / sizing).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    status = _SIZING_STATUS.search(text)
    if status is None or status.group(1) != "shipped":
        return False
    return any(_bare(m) == _bare(run_id) for m in _SIZING_RUN_ID.findall(text))


def _halted_closed(root: Path, run_id: str) -> bool:
    try:
        data = json.loads((root / RECORD_DIR_ROOT / run_id / HALTED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("status") == STATUS_CLOSED


def _mislabeled_closed(root: Path) -> list[str]:
    """Run ids whose tracked outcome reads ``shipped`` while the run's ``halted.json`` reads ``closed``:
    the outcomes an earlier reaper mislabeled. One scandir plus two small reads per evidence dir."""
    try:
        with os.scandir(root / run_artifacts.EVIDENCE_ROOT) as it:
            names = sorted(e.name for e in it if e.is_dir())
    except OSError:
        return []
    return [
        r for r in names
        if run_artifacts.read_tracked_outcome(root, r).get("state") == "shipped" and _halted_closed(root, r)
    ]


def _correct_closed(root: Path, params: dict, session_id: Optional[str]) -> dict:
    """Rewrite every mislabeled ``shipped`` outcome to ``closed`` (``sizing_rel`` included) and commit
    only those files; ``dry_run`` reports without writing."""
    from coordinator_core.warm.entry_seam import OpUnavailableError, reentrant_dispatch

    runs = _mislabeled_closed(root)
    base: dict = {"ok": True, "corrected": runs, "committed_sha": None}
    if not runs or params.get("dry_run") is True:
        return base
    paths = [p for r in runs if (p := run_artifacts.write_tracked_outcome(root, r, STATUS_CLOSED))]
    commit_params: dict = {"paths": paths, "message": f"warp: correct shipped outcome to closed for {', '.join(runs)}"}
    if session_id:
        commit_params["session_id"] = session_id
    try:
        reply, refused = commit_v2_retrying(root, root, commit_params, reentrant_dispatch)
    except OpUnavailableError:
        return {**base, "ok": False, "error": "ceremony.commit_v2 is not registered"}
    if refused:
        return {**base, "ok": False, "refused": refused}
    if not isinstance(reply, dict) or reply.get("error") or not reply.get("committed"):
        if isinstance(reply, dict) and reply.get("nothing_to_commit"):
            return base
        return {**base, "ok": False, "error": f"commit_v2 landed nothing: {reply!r}"}
    return {**base, "committed_sha": reply.get("sha")}


def _terminal_state(root: Path, run_id: str) -> Optional[str]:
    """``landed``, ``shipped`` or ``closed`` when the run's own records say it ended that way, else
    ``None``: the outcome file (the archive's source), the ``halted.json`` status (``closed`` is a halt
    that will never land and stays ``closed``, never ``shipped``), ``run-stage.json``, then the run's
    sizing. In-process reads only. An ended run is never ``abandoned``."""
    state = run_artifacts.read_tracked_outcome(root, run_id).get("state")
    if state == "shipped" and _halted_closed(root, run_id):
        return STATUS_CLOSED
    if state in TERMINAL_STATES or state == STATUS_CLOSED:
        return state
    rec = root / RECORD_DIR_ROOT / run_id
    for name, key in (("halted.json", "status"), (RUN_STAGE_FILE, "stage")):
        try:
            data = json.loads((rec / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        value = data.get(key) if isinstance(data, dict) else None
        if value in TERMINAL_STATES:
            return value
        if name == "halted.json" and value == STATUS_CLOSED:
            return STATUS_CLOSED
    return "shipped" if _sizing_ships_run(root, run_id) else None


def _halt_marked(root: Path, run_id: str) -> bool:
    try:
        data = json.loads((root / RECORD_DIR_ROOT / run_id / HALTED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("status") in (STATUS_HALTED, STATUS_RESUMED)


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.reap_abandoned_runs": one status read, at most one commit.

    Lands every abandoned run's top-level record files plus the dirty sizing its ``run.json``
    names (unless a live or claimed run token names it) as one ``ceremony.commit_v2`` call."""
    from coordinator_core.git.commit_trailers import _UUID_RE
    from coordinator_core.warm.entry_seam import OpUnavailableError, reentrant_dispatch

    if repo_root is None:
        return {"error": f"{OP_NAME} requires repo_root"}
    params = params if isinstance(params, dict) else {}
    wanted = params.get("run_ids")
    if wanted is not None and not (isinstance(wanted, list) and wanted and all(isinstance(r, str) and r for r in wanted)):
        return {"error": f"{OP_NAME}: run_ids must be a non-empty list of run id strings"}
    root = main_worktree_root(Path(repo_root))
    if params.get("correct_closed") is True:
        sid = params.get("session_id")
        return _correct_closed(root, params, sid if isinstance(sid, str) and _UUID_RE.fullmatch(sid) else None)
    base: dict = {"ok": True, "reaped": [], "sizings": [], "committed_sha": None, "skipped": []}

    status = _status(root)
    if status is None:
        return {**base, "ok": False, "error": "git status over the record and sizing roots failed"}
    snapshot, tracked = status
    base["artifacts"] = {}
    records = _token_records(root)

    files: dict[str, list[str]] = {}
    deleted: list[str] = []
    newest: dict[str, float] = {}
    oldest: dict[str, float] = {}
    record_runs = {
        rel[len(RECORD_DIR_ROOT) + 1:].split("/", 1)[0]
        for rel in snapshot.changed
        if rel.startswith(RECORD_DIR_ROOT + "/")
    }
    artifact_of = run_artifacts.runs_of_artifacts(root, snapshot.changed)
    artifact_runs = set(artifact_of.values())
    run_ids = record_runs | artifact_runs
    changed_of: dict[str, set[str]] = {}
    for rel in snapshot.changed:
        owner = _top_level_run(rel)
        if owner is not None:
            changed_of.setdefault(owner, set()).add(rel)
    artifacts_of_run: dict[str, list[str]] = {}
    for rel, owner in artifact_of.items():
        artifacts_of_run.setdefault(owner, []).append(rel)
    for run_id in sorted(run_ids):
        present, gone = record_dir_changes(root, f"{RECORD_DIR_ROOT}/{run_id}", changed_of.get(run_id, set()))
        files[run_id] = present
        deleted += gone
        mtimes = []
        for rel in present:
            try:
                mtimes.append((root / rel).stat().st_mtime)
            except OSError:
                pass
        if not mtimes:
            for rel in artifacts_of_run.get(run_id, ()):
                try:
                    mtimes.append((root / rel).stat().st_mtime)
                except OSError:
                    pass
        if mtimes:
            newest[run_id] = max(mtimes)
            oldest[run_id] = min(mtimes)

    now = time.time()
    # A run's start is its oldest changed file; a tracked file older than that only understates the age.
    past_wall = {r for r, at in oldest.items() if now - at > ORPHAN_WALL_SECONDS}
    for run_id in sorted(run_ids):
        if _already_reaped(root, run_id, newest.get(run_id), snapshot.changed):
            run_ids.discard(run_id)
            artifact_runs.discard(run_id)
            newest.pop(run_id, None)
    minted = {
        r: at for r, at in _minted_runs(root, set(wanted) if wanted else None, now).items()
        if not _already_reaped(root, r, at, snapshot.changed)
    }
    for run_id, at in minted.items():
        newest.setdefault(run_id, at)
    run_ids |= set(minted)
    # Past the wall a record file's mtime no longer holds a token; only the token's own activity or a live session does.
    token_newest = {r: at for r, at in newest.items() if r not in past_wall}
    active = [
        r for r in records
        if _token_active(root, r, token_newest, now) or (r["run_id"] in past_wall and _owner_live(r.get("session_id")))
    ]
    live_ids = {r["run_id"] for r in active}
    workflow_live = {r for r in run_ids - live_ids if run_stage_live(root, r)}
    live_ids |= workflow_live
    idle_tokens = {r["run_id"] for r in records} - live_ids
    held_sizings = {str(r.get("sizing_rel") or "").replace("\\", "/") for r in active} - {""}

    session_id = params.get("session_id")
    if not (isinstance(session_id, str) and _UUID_RE.fullmatch(session_id)):
        session_id = None

    sizing_of: dict[str, str] = {}
    halted: set[str] = set()
    for run_id in sorted(run_ids - live_ids):
        sizing = _run_sizing(root / RECORD_DIR_ROOT / run_id)
        if sizing:
            sizing_of[run_id] = sizing
        if _halt_marked(root, run_id):
            halted.add(run_id)
    late_written = {run_id for rel in tracked if (run_id := _top_level_run(rel)) is not None}
    reaped = abandoned_run_ids(
        snapshot.changed, live_ids, RECORD_DIR_ROOT, newest, now,
        later_stamp_of=_later_binding_runs(
            root, {r: s for r, s in sizing_of.items() if r in newest and now - newest[r] > QUIET_SECONDS}
        ),
        halted=halted,
        late_written=late_written,
        artifact_runs=artifact_runs,
        minted=set(minted),
        past_wall=past_wall,
    )
    if wanted is not None:
        reaped = [r for r in reaped if r in set(wanted)]
        run_ids &= set(wanted)
    # Asked only of runs already selected, so a call with nothing to reap pays no liveness probe.
    # A never-fired minted run has no stage to protect; its submitting session may outlive it.
    owner_held = {r for r in reaped if r not in minted and run_owner_holds(root, r)}
    reaped = [r for r in reaped if r not in owner_held]
    workflow_live |= owner_held
    base["skipped"] = [
        {"run_id": r, "reason": "live-workflow" if r in workflow_live else "live-or-recent"}
        for r in sorted(run_ids)
        if r not in reaped
    ]
    if not reaped:
        return base

    terminal = {r: t for r in reaped if (t := _terminal_state(root, r))}
    paths: list[str] = []
    sizings: list[str] = []
    artifact_gone: list[str] = []
    ignored_state: list[str] = []
    contain_cache: dict[str, bool] = {}
    changed_norm = set(snapshot.changed)
    ignored_norm = {p.replace("\\", "/") for p in snapshot.ignored}
    for run_id in reaped:
        kept, state_files = run_artifacts.split_run_state(files.get(run_id, []))
        paths += kept
        ignored_state += state_files
        if params.get("dry_run") is not True and process_artifact_policy.hosts_coordinator_state(root):  # a dry run writes nothing
            if run_id in terminal:
                outcome = run_artifacts.write_tracked_outcome(root, run_id, terminal[run_id])
            else:
                outcome = run_artifacts.write_tracked_outcome(root, run_id, "abandoned", halt_kind=HALTED_KIND)
            if outcome and outcome not in paths:
                paths.append(outcome)
        sizing = sizing_of.get(run_id)
        if sizing and sizing in held_sizings:  # the record files still land; the graph beyond them waits
            base["skipped"].append({"run_id": run_id, "reason": "sizing-held-by-live-token", "path": sizing})
            continue
        artifacts = run_artifacts.candidate_paths(root, run_id, contain_cache=contain_cache)
        outside = run_artifacts.RunArtifacts(
            run_id=run_id,
            by_class={k: v for k, v in artifacts.by_class.items() if k != "record-dir"},
            record_dirs=(),
            root=root,
        )
        landing = run_artifacts.landable(
            root, outside, changed_norm, ignored_norm, session_id=session_id, prenormalized=True
        )
        extra = [p for p in landing.present if p not in paths and p not in sizings]
        for path, why in sorted(landing.withheld.items()):
            base["skipped"].append({"run_id": run_id, "reason": why, "path": path})
        artifact_gone += [p for p in landing.deleted if p not in artifact_gone]
        for path in extra:
            if outside.class_of(path) == "sizing":
                sizings.append(path)
            else:
                paths.append(path)
        if landing.present:
            base["artifacts"][run_id] = list(landing.present)
    gone = run_artifacts.split_run_state(
        [d for d in deleted if d.split("/")[len(RECORD_DIR_ROOT.split("/"))] in reaped] + artifact_gone
    )[0]
    if ignored_state:
        base["ignored_excluded"] = sorted(set(ignored_state))
    base["reaped"], base["sizings"] = reaped, sizings
    if params.get("dry_run") is True:
        return base

    if not process_artifact_policy.hosts_coordinator_state(root):
        held_back = []
        for run_id in reaped:
            for path in [*files.get(run_id, ()), *base["artifacts"].get(run_id, ())]:
                if path not in held_back:
                    held_back.append(path)
                    base["skipped"].append(
                        {"run_id": run_id, "reason": process_artifact_policy.WITHHELD_REASON, "path": path}
                    )
        markers = []
        for run_id in reaped:
            try:
                _held_marker(root, run_id).parent.mkdir(parents=True, exist_ok=True)
                _held_marker(root, run_id).write_text(json.dumps({"run_id": run_id, "held_at": now}), encoding="utf-8")
            except OSError:
                continue  # no marker: the run is re-reaped next call, never reported as held
            markers.append(f"{HELD_MARKER_ROOT}/{run_id}.json")
        process_artifact_policy.exclude_locally(root, [*held_back, *markers])
        return base

    from coordinator_core.ops.dispatch_emit import run_close

    for run_id in reaped:
        if run_id in idle_tokens:
            run_token.discard(root, run_id)
        if run_id not in halted and run_id not in terminal:
            run_close.close_builds_held_after(root, run_id, "research-run-reaped")

    from coordinator_core.ops.dispatch_emit import landing_integrity
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import run_halted_trailer_lines

    abandoned = [r for r in reaped if r not in terminal]
    message = reap_message(abandoned) if abandoned else f"warp: record ended run artifacts for {', '.join(reaped)}"
    bases = [b for run_id in abandoned for b in landing_integrity.closing_bases(root, run_id, {})]
    for line in run_halted_trailer_lines(bases):
        message += "\n" + line
    commit_params: dict = {
        "paths": paths + sizings,
        "message": message,
        "ignored_untracked": snapshot.ignored_among(paths + sizings),
    }
    if gone:
        commit_params["deleted_paths"] = gone
        commit_params["declared_reverts"] = gone
    if session_id:
        commit_params["session_id"] = session_id
    try:
        reply, refused = commit_v2_retrying(root, root, commit_params, reentrant_dispatch)
    except OpUnavailableError:
        return {**base, "ok": False, "error": "ceremony.commit_v2 is not registered"}
    if refused:
        error = reply.get("error") if isinstance(reply, dict) else None
        return {**base, "ok": False, "refused": refused, "error": f"commit_v2 lost the ref CAS ({refused}): {error}"}
    if not isinstance(reply, dict) or reply.get("error") or not reply.get("committed"):
        if isinstance(reply, dict) and reply.get("nothing_to_commit"):
            return base  # the reap's records already match HEAD: nothing left to commit is success
        return {**base, "ok": False, "error": f"commit_v2 landed nothing: {reply!r}"}
    return {**base, "committed_sha": reply.get("sha")}
