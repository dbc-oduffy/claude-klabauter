"""Halt marker for a sizing run: write it, find the halted run a refire resumes, mark it taken.

The marker is `halted.json` in the run's record dir. It is rewritten to `resumed`, never
unlinked: terminal_commit drops record-dir deletions, so an unlinked marker would stay an
uncommitted tracked deletion. The marker may carry `run_base_sha` (the halted run's review base,
read by `halted_run_base`); `halted_ask_run_for` finds a halted `ask-*` run naming a sizing.
`revised_at` is stamped by `mark_revised` when workflow.revise lands on a halted run and read by `revised_halt`. Stdlib plus the atomic writer; no subprocess, no git.

Accepted residual: two concurrent refires of one halted sizing can both read `halted` before
either rewrites it.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALT_BLOCKING_FIELD, HALT_BLOCKING_ITEMS_FIELD, HALT_PLAN_FIELD, RECORD_DIR_ROOT, RUN_DIR_ROOT,
)
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import STATUS_HALTED
from coordinator_core.ops.dispatch_emit.halt_refire import is_exit_next_action, is_refire_next_action

HALTED_FILE = "halted.json"
STATUS_RESUMED = "resumed"
STATUS_LANDED = "landed"
HALT_RUN_BASE_FIELD = "run_base_sha"
HALT_REASON_FIELD = "reason"
HALT_RUN_ARTIFACTS_FIELD = "run_artifacts"
REVISED_AT_FIELD = "revised_at"
HALT_NEXT_ACTION_FIELD = "next_action"
_SHA40 = re.compile(r"[0-9a-f]{40}")
_DATE_PREFIX = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

#: The `<%Y%m%dT%H%M%S>-<hex6>` tail of a run id; without it a sibling sizing whose stem extends
#: this one's (`foo` vs `foo-slice-1`) shares the `warp-<stem>-` prefix and shadows the newest dir.
_STAMP = re.compile(r"\d{8}T\d{6}-[0-9a-f]{6}")


def _norm(sizing_rel: str) -> str:
    return PurePosixPath(sizing_rel.replace("\\", "/")).as_posix()


def _read(record_dir: Path) -> Optional[dict]:
    try:
        data = json.loads((record_dir / HALTED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(record_dir: Path, payload: dict) -> Path:
    target = record_dir / HALTED_FILE
    atomic_write_bytes(target, (json.dumps(payload, indent=2) + "\n").encode("utf-8"))
    return target


def write_halt_marker(
    record_dir: Path,
    *,
    halted: str,
    sizing_rel: Optional[str],
    plan_rel: Optional[str] = None,
    blocking: Optional[tuple] = None,
    run_base_sha: Optional[str] = None,
    blocking_items: Optional[list] = None,
    reason: Optional[str] = None,
    next_action: Optional[dict] = None,
) -> Path:
    """Write the `halted` marker into `record_dir` (created if absent); return its path.

    `plan`, the blocking key, the blocking items, the reason, the next_action and the base are written only when given (the base
    only when 40-hex; next_action only when `halt_refire.is_refire_next_action` or `is_exit_next_action` accepts it), so other halts' markers are unchanged.
    """
    payload = {
        "status": STATUS_HALTED,
        "halted": halted,
        "sizing_rel": _norm(sizing_rel) if sizing_rel else None,
    }
    if plan_rel:
        payload[HALT_PLAN_FIELD] = _norm(plan_rel)
    if blocking is not None:
        payload[HALT_BLOCKING_FIELD] = list(blocking)
    if blocking_items is not None:
        payload[HALT_BLOCKING_ITEMS_FIELD] = list(blocking_items)
    if reason:
        payload[HALT_REASON_FIELD] = reason
    if next_action is not None and (is_refire_next_action(next_action) or is_exit_next_action(next_action)):
        payload[HALT_NEXT_ACTION_FIELD] = next_action
    if run_base_sha and _SHA40.fullmatch(run_base_sha):
        payload[HALT_RUN_BASE_FIELD] = run_base_sha
    return _write(Path(record_dir), payload)


def halted_run_base(record_dir: Path) -> Optional[str]:
    """The base sha the halted run recorded in its marker, else None (absent, unreadable, not 40-hex)."""
    marker = _read(Path(record_dir))
    value = marker.get(HALT_RUN_BASE_FIELD) if marker else None
    return value if isinstance(value, str) and _SHA40.fullmatch(value) else None


def halted_ask_run_for(repo_root: Path, sizing_rel: str) -> Optional[str]:
    """Return the newest halted `ask-<stamp>` run naming `sizing_rel`, else None.

    Only ask dirs stamped on or after the sizing filename's YYYY-MM-DD prefix are read (None for an
    undated name), and a `warp-<stem>-*` dir stamped later supersedes the ask run.
    """
    norm = _norm(sizing_rel)
    stem = PurePosixPath(norm).stem
    dated = _DATE_PREFIX.match(stem)
    if dated is None:
        return None
    floor = "".join(dated.groups())
    prefix = f"warp-{stem}-"
    root = Path(repo_root) / RECORD_DIR_ROOT
    asks = []
    newest_warp = ""
    try:
        with os.scandir(root) as it:
            for entry in it:
                name = entry.name
                if name.startswith("ask-"):
                    stamp = name[4:]
                    if _STAMP.fullmatch(stamp) and stamp[:8] >= floor and entry.is_dir():
                        asks.append(stamp)
                elif name.startswith(prefix):
                    stamp = name[len(prefix):]
                    if _STAMP.fullmatch(stamp) and entry.is_dir() and stamp > newest_warp:
                        newest_warp = stamp
    except OSError:
        return None
    for stamp in sorted(asks, reverse=True):
        if stamp < newest_warp:
            return None
        name = f"ask-{stamp}"
        marker = _read(root / name)
        if marker and marker.get("status") == STATUS_HALTED and marker.get("sizing_rel") == norm:
            return name if safe_id(name) else None
    return None


def halted_run_for(repo_root: Path, sizing_rel: str) -> Optional[str]:
    """Return the run id a refire of `sizing_rel` resumes, else None.

    Only the lexically greatest `warp-<stem>-*` dir counts (run ids embed %Y%m%dT%H%M%S): a
    newer unmarked run means the halted one was superseded.
    """
    prefix = f"warp-{PurePosixPath(_norm(sizing_rel)).stem}-"
    root = Path(repo_root) / RECORD_DIR_ROOT
    newest = None
    try:
        with os.scandir(root) as it:
            for entry in it:
                if entry.name.startswith(prefix) and _STAMP.fullmatch(entry.name[len(prefix):]) and entry.is_dir():
                    if newest is None or entry.name > newest:
                        newest = entry.name
    except OSError:
        return None
    if newest is None or not safe_id(newest):
        return None
    marker = _read(root / newest)
    if (
        marker is None
        or marker.get("status") != STATUS_HALTED
        or marker.get("sizing_rel") != _norm(sizing_rel)
    ):
        return None
    return newest


#: The `rebind_blocker` reason for a recorded emitter that is no longer a live session.
EMITTER_GONE = "emitter-gone"


def recorded_emitter(repo_root: Path, run_id: str) -> Optional[str]:
    """The session id the halted run's emission receipt names, else None (no script, no receipt,
    unreadable, or no session recorded)."""
    from coordinator_core.ops.dispatch_emit.emission_receipt import emission_receipt_path

    script = Path(repo_root) / RUN_DIR_ROOT / f"{run_id}.workflow.mjs"
    try:
        receipt = json.loads(emission_receipt_path(script).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    recorded = receipt.get("session_id") if isinstance(receipt, dict) else None
    return recorded if recorded and isinstance(recorded, str) else None


def rebind_blocker(repo_root: Path, run_id: str, session_id: str) -> Optional[str]:
    """Return a reason `session_id` must not take the halted run's script path, else None.

    None when the run has no script or receipt, the receipt names `session_id` or no session, or
    the recorded session is live. `EMITTER_GONE` when the recorded session is not live. One
    receipt read, one `session_live` call, no spawn.
    """
    from coordinator_core.session.liveness import session_live

    recorded = recorded_emitter(repo_root, run_id)
    if recorded is None or recorded == session_id:
        return None
    return None if session_live(recorded) else EMITTER_GONE


def record_withheld(record_dir: Path, withheld: dict) -> bool:
    """Stamp ``run_artifacts.withheld`` (``{path: reason}``) on a ``halted`` marker so a refire can
    tell product the halt left dirty on purpose from product it never touched; False and no write
    when nothing was withheld or the marker is absent or not halted."""
    marker = _read(Path(record_dir))
    if not withheld or marker is None or marker.get("status") != STATUS_HALTED:
        return False
    _write(Path(record_dir), {**marker, HALT_RUN_ARTIFACTS_FIELD: {"withheld": dict(sorted(withheld.items()))}})
    return True


def record_resume_stage(record_dir: Path, stage: dict) -> bool:
    """Stamp ``next_action.resume.stage`` (the kept.json pointer) on a ``halted`` marker whose
    ``next_action`` is a refire envelope; False and no write otherwise."""
    marker = _read(Path(record_dir))
    if marker is None or marker.get("status") != STATUS_HALTED:
        return False
    action = marker.get(HALT_NEXT_ACTION_FIELD)
    if not is_refire_next_action(action):
        return False
    stamped = {**action, "resume": {**action["resume"], "stage": dict(stage)}}
    _write(Path(record_dir), {**marker, HALT_NEXT_ACTION_FIELD: stamped})
    return True


def halted_withheld(record_dir: Path) -> dict:
    """``{path: reason}`` the halt recorded as withheld; empty when absent or malformed."""
    marker = _read(Path(record_dir))
    arts = marker.get(HALT_RUN_ARTIFACTS_FIELD) if marker else None
    raw = arts.get("withheld") if isinstance(arts, dict) else None
    return {k: v for k, v in raw.items() if isinstance(k, str)} if isinstance(raw, dict) else {}


def mark_revised(record_dir: Path, at: str) -> bool:
    """Stamp `revised_at` on a `halted` marker; False and no write when it is absent or not halted."""
    marker = _read(Path(record_dir))
    if marker is None or marker.get("status") != STATUS_HALTED:
        return False
    _write(Path(record_dir), {**marker, REVISED_AT_FIELD: at})
    return True


def revised_halt(record_dir: Path) -> Optional[str]:
    """The `revised_at` value while the marker is `halted`, else None. One marker read."""
    marker = _read(Path(record_dir))
    if marker is None or marker.get("status") != STATUS_HALTED:
        return None
    value = marker.get(REVISED_AT_FIELD)
    return value if isinstance(value, str) and value else None


def mark_resumed(record_dir: Path) -> bool:
    """Rewrite a `halted` marker as `resumed`; False when no halted marker is present."""
    marker = _read(Path(record_dir))
    if marker is None or marker.get("status") != STATUS_HALTED:
        return False
    _write(Path(record_dir), {**marker, "status": STATUS_RESUMED})
    return True


def mark_landed(record_dir: Path, sha: str) -> bool:
    """Rewrite a `halted` marker as `landed` at `sha`; False and no write when no halted marker is present.

    A `landed` marker is skipped by `halted_run_for` / `halted_ask_run_for`, so a refire no longer resumes it.
    """
    marker = _read(Path(record_dir))
    if marker is None or marker.get("status") != STATUS_HALTED:
        return False
    _write(Path(record_dir), {**marker, "status": STATUS_LANDED, "sha": sha})
    return True
