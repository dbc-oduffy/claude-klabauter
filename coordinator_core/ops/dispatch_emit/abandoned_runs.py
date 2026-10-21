"""Abandoned-run contract: ``dispatch.reap_abandoned_runs`` lands the ``state/warp/<run_id>/``
record dirs of ask runs that died before any terminal or halted commit, and the stranded plan,
handoff, sidecar, delivery note or sizing of a run whose record dir already landed.

Params: ``{}`` or ``{"dry_run": bool}``.
Reply: ``{ok, reaped: [run_id], sizings: [rel], artifacts: {run_id: [rel]}, committed_sha | null,
skipped: [{run_id, reason, path?}]}``.

A run is ended when it has changed top-level record files or a dirty artifact that
``run_artifacts.runs_of_artifacts`` maps to it (newest mtime then comes from those artifacts), no live or claimed run token, and
either its newest record file is older than ``TOKENLESS_QUIET_SECONDS`` (``run_token.TTL_SECONDS``
is the outer bound), or older than ``QUIET_SECONDS`` and the run is superseded by a later record dir binding the
same sizing, halt-marked, or carries a tracked top-level file modified after its commit. Each
ended run lands with its ``run_artifacts`` candidates (sizing, baton, plan, sidecars, slice
sizings, delivery note).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Mapping, Optional

from coordinator_core.git.run import run_git
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops.dispatch_emit import run_artifacts, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, halted_record_message
from coordinator_core.ops.dispatch_emit.worktree_snapshot import record_dir_changes
from coordinator_core.ops.dispatch_emit.halted_run import _STAMP, HALTED_FILE, STATUS_RESUMED
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import STATUS_HALTED
from coordinator_core.session.record_homes import home_dir

OP_NAME = "dispatch.reap_abandoned_runs"
HALTED_KIND = "abandoned"
SIZING_ROOT = Path(home_dir("", "sizings")).as_posix()
RUN_FILE = "run.json"
QUIET_SECONDS = 600
TOKENLESS_QUIET_SECONDS = 3600
STATUS_ROOTS = [RECORD_DIR_ROOT, SIZING_ROOT, Path(home_dir("", "handoffs")).as_posix(),"docs/plans", "docs/delivery"]


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
) -> list[str]:
    """Sorted run ids with a changed file directly under ``<record_root>/<run_id>/`` or named in
    ``artifact_runs`` (runs owning a dirty plan, handoff, sidecar, delivery note or sizing), absent
    from ``live_run_ids`` (live AND claimed tokens), and ended: newest mtime older than the token
    TTL, or older than ``QUIET_SECONDS`` when the run is superseded (a key of ``later_stamp_of``),
    halt-marked (in ``halted``) or has a tracked top-level file modified after its commit (in
    ``late_written``). Both discovery routes share one ``ended`` rule. A run id with no
    ``newest_mtime`` entry is not abandoned. Pure."""
    prefix = record_root.rstrip("/") + "/"
    superseded = later_stamp_of or {}
    halted = halted or set()
    late_written = late_written or set()
    candidates: set[str] = set(artifact_runs or ())
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
        if age > run_token.TTL_SECONDS:
            return True
        # A run mid-flight always holds a token, so an hour of tokenless quiet means it ended.
        if age > TOKENLESS_QUIET_SECONDS:
            return True
        return age > QUIET_SECONDS and (run_id in superseded or run_id in halted or run_id in late_written)

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
    root = main_worktree_root(Path(repo_root))
    base: dict = {"ok": True, "reaped": [], "sizings": [], "committed_sha": None, "skipped": []}

    status = _status(root)
    if status is None:
        return {**base, "ok": False, "error": "git status over the record and sizing roots failed"}
    snapshot, tracked = status
    base["artifacts"] = {}
    records = _token_records(root)
    live_ids = {r["run_id"] for r in records}

    files: dict[str, list[str]] = {}
    deleted: list[str] = []
    newest: dict[str, float] = {}
    record_runs = {
        rel[len(RECORD_DIR_ROOT) + 1:].split("/", 1)[0]
        for rel in snapshot.changed
        if rel.startswith(RECORD_DIR_ROOT + "/")
    }
    artifact_of = run_artifacts.runs_of_artifacts(root, snapshot.changed)
    artifact_runs = set(artifact_of.values())
    run_ids = record_runs | artifact_runs
    for run_id in sorted(run_ids):
        present, gone = record_dir_changes(root, f"{RECORD_DIR_ROOT}/{run_id}", snapshot.changed)
        files[run_id] = present
        deleted += gone
        mtimes = []
        for rel in present:
            try:
                mtimes.append((root / rel).stat().st_mtime)
            except OSError:
                pass
        if not mtimes:
            for rel, owner in artifact_of.items():
                if owner == run_id:
                    try:
                        mtimes.append((root / rel).stat().st_mtime)
                    except OSError:
                        pass
        if mtimes:
            newest[run_id] = max(mtimes)

    now = time.time()
    held_sizings = {
        str(r.get("sizing_rel") or "").replace("\\", "/") for r in records if _token_active(root, r, newest, now)
    } - {""}

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
    )
    base["skipped"] = [{"run_id": r, "reason": "live-or-recent"} for r in sorted(run_ids) if r not in reaped]
    if not reaped:
        return base

    paths: list[str] = []
    sizings: list[str] = []
    artifact_gone: list[str] = []
    for run_id in reaped:
        paths += files[run_id]
        sizing = sizing_of.get(run_id)
        if sizing and sizing in held_sizings:  # the record files still land; the graph beyond them waits
            base["skipped"].append({"run_id": run_id, "reason": "sizing-held-by-live-token", "path": sizing})
            continue
        artifacts = run_artifacts.candidate_paths(root, run_id)
        outside = run_artifacts.RunArtifacts(
            run_id=run_id,
            by_class={k: v for k, v in artifacts.by_class.items() if k != "record-dir"},
            record_dirs=(),
            root=root,
        )
        landing = run_artifacts.landable(root, outside, snapshot.changed, snapshot.ignored, session_id=session_id)
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
    gone = [d for d in deleted if d.split("/")[len(RECORD_DIR_ROOT.split("/"))] in reaped] + artifact_gone
    base["reaped"], base["sizings"] = reaped, sizings
    if params.get("dry_run") is True:
        return base

    commit_params: dict = {"paths": paths + sizings, "message": reap_message(reaped)}
    if gone:
        commit_params["deleted_paths"] = gone
        commit_params["declared_reverts"] = gone
    if session_id:
        commit_params["session_id"] = session_id
    try:
        reply = reentrant_dispatch("ceremony.commit_v2", commit_params, repo_root=root)
    except OpUnavailableError:
        return {**base, "ok": False, "error": "ceremony.commit_v2 is not registered"}
    if not isinstance(reply, dict) or reply.get("error") or not reply.get("committed"):
        return {**base, "ok": False, "error": f"commit_v2 landed nothing: {reply!r}"}
    return {**base, "committed_sha": reply.get("sha")}
