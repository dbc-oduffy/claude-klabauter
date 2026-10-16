"""Halt marker for a sizing run: write it, find the halted run a refire resumes, mark it taken.

The marker is `halted.json` in the run's record dir. It is rewritten to `resumed`, never
unlinked: terminal_commit drops record-dir deletions, so an unlinked marker would stay an
uncommitted tracked deletion. Stdlib plus the atomic writer; no subprocess, no git.

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
    HALT_BLOCKING_FIELD, HALT_PLAN_FIELD, RECORD_DIR_ROOT, RUN_DIR_ROOT,
)
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import STATUS_HALTED

HALTED_FILE = "halted.json"
STATUS_RESUMED = "resumed"

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
) -> Path:
    """Write the `halted` marker into `record_dir` (created if absent); return its path.

    `plan` and the blocking key are written only when given, so other halts' markers are unchanged.
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
    return _write(Path(record_dir), payload)


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


def mark_resumed(record_dir: Path) -> bool:
    """Rewrite a `halted` marker as `resumed`; False when no halted marker is present."""
    marker = _read(Path(record_dir))
    if marker is None or marker.get("status") != STATUS_HALTED:
        return False
    _write(Path(record_dir), {**marker, "status": STATUS_RESUMED})
    return True
