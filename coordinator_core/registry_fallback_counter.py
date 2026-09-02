"""coordinator_core.registry_fallback_counter — append-only counter for op
resolutions that missed `OP_MODULE_MAP` and escalated to a full import sweep.

WHY THIS EXISTS. `ipc.py::_lazy_import_and_lookup` resolves an op through
`OP_MODULE_MAP` in one targeted import (measured: +55 sys.modules entries,
31ms). When the map has no entry for a method, or has one that did not
actually register it, resolution falls through to a full
`coordinator_core.ops._eager_import_all()` — 233 op modules, ~760 sys.modules
entries, measured 468.5ms against a 72.9ms bare interpreter on 2026-08-23.
That fallback is deliberate and correct: it is the never-worse-than-today
guarantee, degrading a stale map to slow-but-right rather than to a broken
dispatch.

It was also SILENT. Both fallback stages logged at `debug` level and nothing
counted them, so a single drifted map entry made every affected dispatch pay
~390ms extra with no signal anywhere — on a repo whose brightline is 500ms
end-to-end and which treats one process over 200ms as a defect. Correctness
was insured; performance had a trapdoor with no alarm on it. This module is
the alarm.

NEGATIVE SPEC — what this is deliberately NOT:

  - NOT a guard. It blocks nothing, refuses nothing, and has no verdict. A
    fallback firing is a legitimate resolution that costs too much, not a
    violation.
  - NOT a replacement for the static oracles. `ops/tests/test_registry_map_sync.py`
    and `ops/tests/test_op_inventory_parity.py` catch map drift before it
    ships and remain the primary defence. This catches what reaches a live
    session anyway — an op registered at runtime, a distribution whose
    generated map is stale, a module that failed to import.
  - NOT read back by anything in this module or at either call site. Append
    only, exactly as `guard_advisory_counter` is; aggregation is a read-time
    concern for a separate reducer.
  - NOT widened past the op name. The method name identifies which entry to
    repair, which is the whole diagnostic purpose. No params, no payload, no
    caller identity, no session content.

SHAPE MIRRORS `coordinator_core/guard_advisory_counter.py` deliberately rather
than extending it: that module's docstring forbids widening its record shape
without a fresh PM ruling, and a dispatch-resolution miss is a different
signal from a guard firing. Same per-session-file design, and for the same
reason — a shared append target across the 50-plus sessions this box routinely
carries would be exactly the concurrency hazard the per-session split avoids.

CANNOT-BREAK-DISPATCH CONTRACT: this module does not swallow its own
exceptions. `record_registry_fallback` may raise (unwritable directory, disk
full). The call site in `ipc.py` wraps it in `try/except Exception` and
continues — an op must never fail to resolve because its telemetry could not
be written. Cost is irrelevant on this path by construction: it is only ever
reached by a call that is already paying for a 233-module import.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.subagent_sandbox import resolve_git_root_cheap
from coordinator_core.session.machinery_paths import share_dir as _share_dir

_FALLBACK_COUNTS_FILENAME = "registry-fallback-counts.jsonl"

#: The one escalation stage counted: the ops-wide sweep.
#:
#: There is a second stage in `_lazy_import_and_lookup` — the hooks-scoped
#: escalation — and it is deliberately NOT emitted here. Every `hooks.*` key
#: in `OP_MODULE_MAP` maps to the shared "coordinator_core.hooks" package, so
#: the targeted step-1 import is always a no-op for them and that stage is
#: their DESIGNED resolution path, measured firing on 100% of `hooks.*`
#: dispatches. Since hooks fire on every tool call across every session on the
#: box, counting it would write a record per tool call and bury the ops-wide
#: cliff this file exists to surface. A `hooks.*` op that stage fails to
#: resolve still falls through to the ops-wide sweep, which does count.
STAGE_SAFE_FALLBACK = "safe-fallback"


def record_registry_fallback(
    method: str,
    stage: str,
    session_id: str,
    mapped: bool,
    cwd: Optional[str] = None,
) -> None:
    """Append one `{"op", "stage", "mapped", "at"}` record for a resolution
    that missed the targeted-import fast path.

    `mapped` distinguishes the two causes, which want different repairs: a
    method absent from `OP_MODULE_MAP` entirely (`False` — the map needs the
    entry) from one present whose targeted import did not register it
    (`True` — the entry is stale or the module is failing to import, and the
    second is far more serious because it is silent everywhere else too).

    No-op (not an error) when `session_id` is empty/unresolvable, or when the
    git root cannot be resolved — the same posture `guard_advisory_counter`
    takes, and for the same reason: inventing a fallback shared path would
    recreate the cross-session concurrency hazard the per-session file exists
    to avoid.

    May raise on an actual write failure; the caller is responsible for the
    swallow-and-continue (see module docstring's CANNOT-BREAK-DISPATCH
    CONTRACT).
    """
    if not session_id:
        return
    git_root = resolve_git_root_cheap(cwd)
    if not git_root:
        return
    path = Path(_share_dir(git_root, session_id)) / _FALLBACK_COUNTS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "op": method,
        "stage": stage,
        "mapped": mapped,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
