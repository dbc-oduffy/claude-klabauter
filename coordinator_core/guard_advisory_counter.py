"""coordinator_core.guard_advisory_counter — append-only advisory-fire counter.

The minimum signal DR-277's "we can always revert" needs (docs/plans/
2026-08-06-apply-guard-class-census.md, chunk C21): a COUNT, not a log, of
every true advisory-class guard firing. Guard name and UTC timestamp only —
no payload, no command text, no file path, no session content. Do not widen
this record shape without a fresh PM ruling; C21's own plan body is written
to give a later reader something to argue against.

Written to a per-session file, `state/subagent-share/<session_id>/
advisory-fire-counts.jsonl`, mirroring the fleet's existing subagent-share
convention (`bash_guards/bump_outside_repo_write.py::_sandbox_root`,
`bash_guards/bump_foreign_repo_write.py::_sandbox_root_hint`) — a shared
append target across the six-plus sessions this repo routinely carries at
once would be exactly the concurrency hazard this per-session design exists
to avoid. Aggregation across sessions is a read-time concern (a future
reader/reducer over the per-session files), not this module's.

Two call sites:
  - `write_guards/engine.py::evaluate`, the advisory-phase `return out` (the
    legacy `aggregate=False` default). Since `docs/plans/2026-08-06-windows-
    hot-path-less-work-per-interpreter.md` chunk C6, that same function's
    opt-in `aggregate=True` branch calls this recorder in a loop, once per
    RETURNED advisory — so THIS call site can append more than one record
    per `evaluate()` invocation when a caller opts in. See that function's
    own docstring for the arity note; a reader of `advisory-fire-counts.jsonl`
    should not assume "at most one record per triggering call" holds for
    every session.
  - `bash_guards/dispatch.py::evaluate_payload_json`, the `return out` /
    `return emitted` exits — gated by the caller on `not fail_closed` since
    those are also the exit for an annotated hard-deny envelope. Still a
    single-advisory-return seam.

CANNOT-BREAK-THE-GUARD CONTRACT: this module does not itself swallow
exceptions — `record_advisory_fire` may raise (unresolvable git root,
unwritable directory, disk full). Both call sites wrap the call in their
own `try/except Exception: pass`, placed AFTER the guard's own return value
is already decided, so a failed write can only ever fail to produce a
record; it never turns an advisory into a deny and never reaches the
caller.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.subagent_sandbox import resolve_git_root

_COUNTS_FILENAME = "advisory-fire-counts.jsonl"


def record_advisory_fire(guard_name: str, session_id: str, cwd: Optional[str] = None) -> None:
    """Append one `{"guard", "at"}` record for a true advisory-class firing.

    No-op (not an error) when `session_id` is empty/unresolvable — the same
    fail-closed-never-grant posture the in-session unlock seam already
    applies to this exact payload field in both engines
    (`write_guards/engine.py:173-174`, `bash_guards/dispatch.py:692-694`).
    Does not invent a fallback shared path on an unresolvable git root
    either — that recreates the concurrency hazard this design avoids, so
    this, too, degrades to a silent no-op rather than a write.

    May raise on an actual write failure (unwritable directory, disk full);
    callers are responsible for the swallow-and-continue (see module
    docstring).
    """
    if not session_id:
        return
    git_root = resolve_git_root(cwd)
    if not git_root:
        return
    path = Path(git_root) / "state" / "subagent-share" / session_id / _COUNTS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"guard": guard_name, "at": datetime.now(timezone.utc).isoformat()}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
