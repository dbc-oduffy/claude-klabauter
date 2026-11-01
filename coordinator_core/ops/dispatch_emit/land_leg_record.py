"""The run record's `land-legs.json`: what each land_run leg cost, written on a halt and on a landing.

`write` never raises and spawns nothing: a record failure must not change the landing reply. The file
sits in the run's record dir and is added to the local git exclude, so a landed call leaves no
untracked path behind (an in-flight run's `scratch/warp` dir is already git-ignored). It is not
`usage.json`, which is `sibling_hold`'s landed marker.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

FILE_NAME = "land-legs.json"

OUTCOME_HALT = "land-deadline"
OUTCOME_LANDED = "landed"


def write(
    root: Path, run_id: object, *, outcome: str, leg: Optional[str], bound_by: Optional[str], legs: list
) -> Optional[str]:
    """Write `<record_dir>/land-legs.json` atomically and return its repo-relative posix path.

    Returns None, writing nothing, when `run_id` is absent or unsafe or on any failure."""
    try:
        if not isinstance(run_id, str) or not run_id or "/" in run_id or "\\" in run_id:
            return None
        from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

        target = record_dir(root, run_id) / FILE_NAME
        body = {"outcome": outcome, "leg": leg, "bound_by": bound_by, "legs": list(legs)}
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{FILE_NAME}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(body, indent=2), encoding="utf-8", newline="\n")
        os.replace(tmp, target)
        rel = target.relative_to(Path(root)).as_posix()
        from coordinator_core.ops.dispatch_emit import process_artifact_policy

        process_artifact_policy.exclude_locally(root, [rel])
        return rel
    except Exception:
        return None
