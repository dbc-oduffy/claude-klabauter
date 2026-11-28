"""Parity oracle: none (net-new section; covered by tests/test_sizings_and_workflow_runs_sections.py).

Section porter — WorkflowRun (envelope key: ``workflow_runs``).

One row per run record directory under ``state/warp/`` (or, for an in-flight run, ``scratch/warp/``) that carries ``run-stage.json`` and is
either non-terminal (``minted``/``staged``) or active within the last 7 days. ``sizing_rel``
comes from the run's ``run.json``; a run without ``run-stage.json`` (legacy) is skipped.
``outcome`` is always null. Emits nothing while the vendored schema pin lacks
``workflow-run.schema.json``, so emit never fails validation before the pin is re-vendored.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.contract.cockpit_schema.entities.workflow_run import WorkflowRun
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT, RUN_STAGE_FILE
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.validate import _entity_schema_path

ENTITY = "workflow-run"
_RECENT = timedelta(days=7)
_LIVE_STAGES = frozenset({"minted", "staged"})
_STATUS_BY_STAGE = {"landed": "closed", "halted": "halted"}


def _read_json(path: str) -> dict | None:
    try:
        with open(path, "rb") as f:
            data = json.loads(f.read())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _sig(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _project(
    run_id: str, run_dir: str, root: Path, stage_rec: dict, run_sig: tuple[int, int] | None
) -> tuple[dict | None, dict | None]:
    """Build and validate one row; returns ``(row, None)`` or ``(None, malformed)``."""
    run_rec = _read_json(run_dir + os.sep + "run.json") if run_sig is not None else None
    sizing_rel = (run_rec or {}).get("sizing_rel")
    stage = stage_rec.get("stage")
    row = {
        "run_id": run_id,
        "stage": stage,
        "status": _STATUS_BY_STAGE.get(stage, "live"),
        "sizing_rel": sizing_rel if isinstance(sizing_rel, str) and sizing_rel else None,
        "session_id": stage_rec.get("session_id"),
        "started_at": stage_rec.get("started_at"),
        "last_activity_at": stage_rec.get("last_activity_at"),
        "steps_planned": stage_rec.get("steps_planned"),
        "outcome": None,
    }
    try:
        WorkflowRun.model_validate(row)
    except ValidationError as exc:
        return None, {
            "path": (Path(run_dir) / RUN_STAGE_FILE).relative_to(root).as_posix(),
            "reason": f"fails WorkflowRun: {exc.error_count()} violation(s)",
            "frontmatter_keys": sorted(stage_rec.keys()),
        }
    return row, None


# root -> per-run memo. A warm entry is valid while the (mtime_ns, size) of run-stage.json (and of
# run.json, for a row built from it) is unchanged, so a warm engine re-reads only runs that moved.
# A cold engine adopts the on-disk snapshot instead: an entry whose run-dir mtime (free from the
# scandir) is unchanged is trusted with no file touched, because every writer replaces atomically
# and a replace moves the dir mtime. Such a ``pending`` entry gets its file sigs on the next call.
class _Entry:
    __slots__ = ("sig", "stage_rec", "last", "built", "run_sig", "row", "bad", "dm", "pending")

    def __init__(self, sig: tuple[int, int] | None, stage_rec: dict | None, dm: int = 0) -> None:
        self.sig = sig
        self.stage_rec = stage_rec
        self.last = _parse_ts(stage_rec.get("last_activity_at")) if stage_rec else None
        self.built = False
        self.run_sig: tuple[int, int] | None = None
        self.row: dict | None = None
        self.bad: dict | None = None
        self.dm = dm
        self.pending = False


class _RootMemo:
    __slots__ = ("entries", "absent", "snap", "dirty", "persisted_at")

    def __init__(self) -> None:
        self.entries: dict[str, _Entry] = {}
        self.absent: dict[str, int] = {}
        self.snap: dict = {}
        self.dirty = False
        self.persisted_at = 0.0


_MEMO: dict[str, _RootMemo] = {}
_MEMO_MAX_ROOTS = 8
_SNAP_REL = (".coordinator-local", "workflow-runs-index.json")
_SNAP_VERSION = 1
_PERSIST_EVERY_S = 5.0


def _fingerprint() -> list:
    sig = _sig(str(_entity_schema_path(ENTITY)))
    return [_SNAP_VERSION, sig[0] if sig else 0]


def _load_snapshot(root: Path) -> dict:
    """Snapshot entries by run key, or ``{}`` when absent, unreadable, or built for another schema."""
    data = _read_json(str(root.joinpath(*_SNAP_REL)))
    if not data or data.get("f") != _fingerprint() or not isinstance(data.get("e"), dict):
        return {}
    return data["e"]


def _persist(root: Path, memo: _RootMemo, keyed: dict[str, str]) -> None:
    """Write the snapshot atomically when ``.coordinator-local`` exists; a failed write is silent."""
    if not root.joinpath(_SNAP_REL[0]).is_dir():
        return
    out: dict[str, list] = {k: [dm, 0] for k, dm in memo.absent.items()}
    for stage_path, ent in memo.entries.items():
        key = keyed.get(stage_path)
        if key is None or ent.stage_rec is None:
            continue
        sr = ent.stage_rec
        out[key] = [ent.dm, 1, sr.get("stage"), sr.get("last_activity_at"), ent.row, ent.bad]
    try:
        atomic_write_bytes(
            root.joinpath(*_SNAP_REL),
            json.dumps({"f": _fingerprint(), "e": out}, separators=(",", ":")).encode("utf-8"),
        )
    except Exception:
        return
    memo.dirty = False
    memo.persisted_at = time.monotonic()


def _from_snapshot(snap_row: list, dm: int) -> _Entry:
    ent = _Entry(None, {"stage": snap_row[2], "last_activity_at": snap_row[3]}, dm)
    ent.row, ent.bad = snap_row[4], snap_row[5]
    ent.built = ent.row is not None or ent.bad is not None
    ent.pending = True
    return ent


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    if not _entity_schema_path(ENTITY).exists():
        return [], []
    root = ctx.subprocess_root if ctx.subprocess_root is not None else ctx.repo_root
    now = _parse_ts(ctx.observed_at) or datetime.now(timezone.utc)
    run_dirs: dict[str, tuple[str, int, str]] = {}
    # An in-flight run's record sits only under scratch/warp (ask_contract.record_dir); state wins.
    for rel in (RECORD_DIR_ROOT, RUN_DIR_ROOT):
        try:
            for e in os.scandir(Path(root) / rel):
                if e.name not in run_dirs and e.is_dir():
                    run_dirs[e.name] = (e.path, e.stat().st_mtime_ns, f"{rel}/{e.name}")
        except OSError:
            continue

    rkey = str(root)
    memo = _MEMO.get(rkey)
    if memo is None:
        memo = _RootMemo()
        if ctx.subprocess_root is None:
            memo.snap = _load_snapshot(Path(root))
    prev, prev_absent, snap = memo.entries, memo.absent, memo.snap
    fresh: dict[str, _Entry] = {}
    absent: dict[str, int] = {}
    keyed: dict[str, str] = {}
    records: list[dict] = []
    malformed: list[dict] = []
    changed = False
    for run_id in sorted(run_dirs):
        run_dir, dm, key = run_dirs[run_id]
        stage_path = run_dir + os.sep + RUN_STAGE_FILE
        ent = prev.get(stage_path)
        if ent is None:
            if prev_absent.get(key) == dm:
                absent[key] = dm
                continue
            srow = snap.get(key)
            if srow is not None and srow[0] == dm:
                if srow[1] == 0:
                    absent[key] = dm
                    continue
                ent = _from_snapshot(srow, dm)
        if ent is not None and ent.pending and ent.dm != dm:
            ent = None
        elif ent is not None and ent.pending and ent is prev.get(stage_path):
            ent.sig = _sig(stage_path)
            ent.run_sig = _sig(run_dir + os.sep + "run.json") if ent.built else None
            ent.pending = ent.sig is None
            if ent.sig is None:
                ent = None
        if ent is not None and not ent.pending and _sig(stage_path) != ent.sig:
            ent = None
        if ent is None:
            sig = _sig(stage_path)
            if sig is None:
                absent[key] = dm
                changed = True
                continue
            ent = _Entry(sig, _read_json(stage_path), dm)
            changed = True
        ent.dm = dm
        fresh[stage_path] = ent
        keyed[stage_path] = key
        stage_rec = ent.stage_rec
        if stage_rec is None:
            continue
        if stage_rec.get("stage") not in _LIVE_STAGES and (ent.last is None or now - ent.last > _RECENT):
            continue
        if ent.pending:
            if ent.row is None and ent.bad is None:
                ent = _Entry(_sig(stage_path), _read_json(stage_path), dm)
                fresh[stage_path] = ent
                stage_rec = ent.stage_rec
                changed = True
                if stage_rec is None:
                    continue
            elif ent.bad is not None:
                malformed.append(dict(ent.bad))
                continue
            else:
                records.append(dict(ent.row))
                continue
        run_sig = _sig(run_dir + os.sep + "run.json")
        if not ent.built or ent.run_sig != run_sig:
            ent.row, ent.bad = _project(run_id, run_dir, Path(root), stage_rec, run_sig)
            ent.run_sig, ent.built = run_sig, True
            changed = True
        if ent.bad is not None:
            malformed.append(dict(ent.bad))
        elif ent.row is not None:
            records.append(dict(ent.row))
    memo.entries, memo.absent, memo.snap = fresh, absent, {}
    memo.dirty = memo.dirty or changed or len(fresh) + len(absent) != len(prev) + len(prev_absent)
    if memo.dirty and ctx.subprocess_root is None and time.monotonic() - memo.persisted_at >= _PERSIST_EVERY_S:
        _persist(Path(root), memo, keyed)
    _MEMO.pop(rkey, None)
    _MEMO[rkey] = memo
    while len(_MEMO) > _MEMO_MAX_ROOTS:
        del _MEMO[next(iter(_MEMO))]
    return records, malformed
