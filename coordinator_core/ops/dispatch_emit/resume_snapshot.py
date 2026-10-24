"""Resume snapshot: the gate's view of a row resume, as two row-id sets.

`not_to_run` is the plan row ids the gate read as landed or closed; `resume_rows` is the ids it
handed the stage to run. The gate writes `<repo>/scratch/warp/<run_id>/resume-snapshot.json`; the
stage reads it so a re-plan's added rows are staged, landed or closed rows stay dropped, and a
resume row the re-plan retired is told apart from an unknown id. Leaf module: no subprocess, no
import from terminal_commit or ask_compose.

`reasons` is the gate's reason for each `not_to_run` id.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, NamedTuple, Optional

from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

SNAPSHOT_NAME = "resume-snapshot.json"


def _snapshot_path(repo_root: Path, run_id: str) -> Optional[Path]:
    """The snapshot path for `run_id`, or None when the id is unsafe or escapes RUN_DIR_ROOT."""
    if not isinstance(run_id, str) or not safe_id(run_id):
        return None
    root = Path(repo_root)
    run_dir = contained_path(root / RUN_DIR_ROOT / run_id, [root / RUN_DIR_ROOT])
    if run_dir is None:
        return None
    return run_dir / SNAPSHOT_NAME


class Snapshot(NamedTuple):
    """What the gate read in one file: `not_to_run` is the gate-time spine ids minus `resume_rows`;
    `resume_rows` is the gate's own resume rows (empty for a snapshot written before the key existed)."""

    not_to_run: frozenset
    resume_rows: frozenset
    kept: frozenset = frozenset()
    kept_run: Optional[str] = None
    held: tuple = ()
    reasons: Mapping[str, str] = MappingProxyType({})


def write(
    repo_root: Path,
    run_id: str,
    plan_rel: str,
    not_to_run: Iterable[str],
    resume_rows: Iterable[str],
    kept: Iterable[str] = (),
    kept_run: Optional[str] = None,
    held: Iterable = (),
    reasons: Mapping[str, str] = MappingProxyType({}),
) -> Optional[Path]:
    """Write the snapshot; return its path, or None (nothing written) for an unsafe run_id or an unwritable run dir."""
    path = _snapshot_path(repo_root, run_id)
    if path is None:
        return None
    body = {
        "plan": plan_rel,
        "not_to_run": sorted(set(not_to_run)),
        "resume_rows": sorted(set(resume_rows)),
    }
    if kept:
        body["kept"] = sorted(set(kept))
    if kept_run:
        body["kept_run"] = kept_run
    body["held"] = [{"id": i, "reason": r} for i, r in held]
    if reasons:
        body["reasons"] = {k: reasons[k] for k in sorted(reasons)}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return None
    return path


def read(repo_root: Path, run_id: str, plan_rel: str) -> Optional[Snapshot]:
    """The snapshot; None when absent, unreadable, malformed or naming another plan. A body without
    "resume_rows" reads `resume_rows=frozenset()`."""
    path = _snapshot_path(repo_root, run_id)
    if path is None:
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(body, dict) or body.get("plan") != plan_rel:
        return None
    rows = body.get("not_to_run")
    if not isinstance(rows, list) or not all(isinstance(r, str) for r in rows):
        return None
    resume = body.get("resume_rows", [])
    if not isinstance(resume, list) or not all(isinstance(r, str) for r in resume):
        return None
    kept = body.get("kept", [])
    if not isinstance(kept, list) or not all(isinstance(r, str) for r in kept):
        kept = []
    kept_run = body.get("kept_run")
    held_raw = body.get("held", [])
    held = tuple(
        (h["id"], h["reason"])
        for h in (held_raw if isinstance(held_raw, list) else [])
        if isinstance(h, dict) and isinstance(h.get("id"), str) and isinstance(h.get("reason"), str)
    )
    reasons_raw = body.get("reasons", {})
    reasons = MappingProxyType(
        {k: v for k, v in reasons_raw.items() if isinstance(k, str) and isinstance(v, str)}
        if isinstance(reasons_raw, dict)
        else {}
    )
    return Snapshot(
        frozenset(rows),
        frozenset(resume),
        frozenset(kept),
        kept_run if isinstance(kept_run, str) and kept_run else None,
        held,
        reasons,
    )


def clear(repo_root: Path, run_id: str) -> None:
    """Delete a stale snapshot for the run; a missing file or unsafe id is a no-op."""
    path = _snapshot_path(repo_root, run_id)
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass
