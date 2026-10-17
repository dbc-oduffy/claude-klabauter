"""Resume snapshot: the plan row ids the gate read as not to run on a re-fire.

The gate writes `<repo>/scratch/warp/<run_id>/resume-snapshot.json`; the stage reads it so a
re-plan's added rows are staged while landed or closed rows stay dropped. Leaf module: no
subprocess, no import from terminal_commit or ask_compose.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional

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


def write(repo_root: Path, run_id: str, plan_rel: str, not_to_run: Iterable[str]) -> Optional[Path]:
    """Write the snapshot; return its path, or None (nothing written) for an unsafe run_id or an unwritable run dir."""
    path = _snapshot_path(repo_root, run_id)
    if path is None:
        return None
    body = {"plan": plan_rel, "not_to_run": sorted(set(not_to_run))}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return None
    return path


def read(repo_root: Path, run_id: str, plan_rel: str) -> Optional[frozenset]:
    """The not-to-run row ids; None when absent, unreadable, malformed or naming another plan."""
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
    return frozenset(rows)


def clear(repo_root: Path, run_id: str) -> None:
    """Delete a stale snapshot for the run; a missing file or unsafe id is a no-op."""
    path = _snapshot_path(repo_root, run_id)
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass
