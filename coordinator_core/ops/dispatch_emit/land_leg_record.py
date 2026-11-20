"""The run record's `land-legs.json`: what each land_run leg cost, written on a halt and on a landing.

`write` never raises and spawns nothing: a record failure must not change the landing reply. The file
sits in the run's record dir and is added to the local git exclude, so a landed call leaves no
untracked path behind (an in-flight run's `scratch/warp` dir is already git-ignored). It is not
`usage.json`, which is `sibling_hold`'s landed marker.

The file is not outcome-free: `halted_run_landed` reads `outcome == landed` as proof a commit ran. So a
`landed` record is never replaced by a halt one, even by a halt worker that finishes late.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional

from coordinator_core.bounded_io import run_bounded

FILE_NAME = "land-legs.json"

OUTCOME_HALT = "land-deadline"
OUTCOME_LANDED = "landed"
#: A commit ran but carries none / only some of the run's measured code paths.
OUTCOME_LANDED_NO_CODE = "landed_no_code"
OUTCOME_PARTIAL = "partial"
COMMITTED_OUTCOMES = frozenset({OUTCOME_LANDED, OUTCOME_LANDED_NO_CODE, OUTCOME_PARTIAL})

#: Longest a caller waits on the record write; a drop returns None like any other failure.
WRITE_DEADLINE_SECS = 0.15

# One lock per target, held across os.replace: a stalled replace drops only that run's later writes,
# never another run's `landed` record.
_REPLACE_GUARDS: "dict[str, threading.Lock]" = {}
_REPLACE_GUARDS_LOCK = threading.Lock()


def _replace_guard(target: Path) -> threading.Lock:
    with _REPLACE_GUARDS_LOCK:
        return _REPLACE_GUARDS.setdefault(str(target), threading.Lock())


def _recorded_outcome(target: Path) -> Optional[str]:
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc.get("outcome") if isinstance(doc, dict) else None


def write(
    root: Path, run_id: object, *, outcome: str, leg: Optional[str], bound_by: Optional[str], legs: list,
    withheld_reasons: Optional[dict] = None,
) -> Optional[str]:
    """Write `<record_dir>/land-legs.json` atomically and return its repo-relative posix path.

    Returns None, writing nothing, when `run_id` is absent or unsafe, on any failure, or when the write
    has not finished within `WRITE_DEADLINE_SECS` (the worker completes late). An existing `landed`
    record is kept when `outcome` is not `landed`; the path is still returned."""
    try:
        if not isinstance(run_id, str) or not run_id or "/" in run_id or "\\" in run_id:
            return None
        from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

        target = record_dir(root, run_id) / FILE_NAME
        body = {"outcome": outcome, "leg": leg, "bound_by": bound_by, "legs": list(legs)}
        body["withheld_reasons"] = dict(withheld_reasons or {})

        def put() -> Optional[str]:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(f"{FILE_NAME}.{os.getpid()}.{threading.get_ident()}.tmp")
                tmp.write_text(json.dumps(body, indent=2), encoding="utf-8", newline="\n")
                with _replace_guard(target):
                    if outcome in COMMITTED_OUTCOMES or _recorded_outcome(target) not in COMMITTED_OUTCOMES:
                        os.replace(tmp, target)
                    else:
                        tmp.unlink(missing_ok=True)
                rel = target.relative_to(Path(root)).as_posix()
                from coordinator_core.ops.dispatch_emit import process_artifact_policy

                process_artifact_policy.exclude_locally(root, [rel])
                return rel
            except Exception:
                return None

        return run_bounded(f"{target}\0{outcome}", put, deadline=WRITE_DEADLINE_SECS, default=None)
    except Exception:
        return None
