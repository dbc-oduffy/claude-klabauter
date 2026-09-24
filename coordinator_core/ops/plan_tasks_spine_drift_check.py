"""
coordinator_core.ops.plan_tasks_spine_drift_check — JSON-RPC
"plan.tasks.spine_drift_check" operation.

Purpose: expose a read-only check for "does this plan's `## Tasks` spine
have a covering commit" without mutating anything. Report-only by
architectural boundary (DR-263), mirroring `coordinator_core.ops.
cascade_backstop_sweep`'s own "reports but never flips" posture: this op
never writes the plan file.

EVIDENCE JOIN DELETED (P153-C22, `docs/plans/2026-09-22-spawn-budget-and-
census.md`, R1, kill means kill forever — DR-344 §6): this op's only
evidence source was the shared chunk-evidence `Deliverable-Id`-trailer/
subject-chunk-id `git log` join (`_committed_chunk_shas`, relocated here
per C4 2026-08-20 "the close ceremony stops paying for the join", Gap 1),
whose own cold cost was measured at 1010-1544ms process time across
fixtures (`docs/research/2026-09-11-spine-drift-check-affordability.md`;
`docs/research/spike-verdicts/2026-09-22-evidence-join-range-bound.md`) --
well over DR-344's 500ms kill bar. Spike S3 (P153-C21) measured every
range-bound candidate that could cut that walk: a date-`--since` bound
still lands over 500ms on genuinely early-anchored plans (the mandated R1
fixture, 1359ms), and a `merge-base`-only bound is fast but silently drops
every genuinely committed chunk from before the branch point -- verdict
`not-viable`, no further range-bound candidate closes both gaps at once on
this repo's actual commit-date distribution (see that verdict's "Why no
further candidate helps" section). Per DR-344 §6's kill-bar disposition
(no refactor lane, no suspension, no reinstatement), the whole evidence-
join closure this op carried since C4/Gap-1 is deleted outright, not
narrowed or rebounded -- `_handler` below never computes `join_provenance`
or `drifted_rows` from commit evidence any more, and reports every
commit-required `open` row `"unknown"`, permanently, via
`evidence_reason`. The requirement question the verdict leaves open (does
the chunk-evidence join get re-approached by a non-range-bounded
mechanism, e.g. an index, sized and spiked as its own plan) is R1's, not
this row's -- resurrecting the join here would be exactly the "rewrite the
join" this row's own body forbids.

REUSE, not reimplementation: `_parse_spine_rows`, `_all_spine_ids`,
`_plan_deliverable_id` and `_row_disposition` are still direct calls into
`close_out_and_stamp`'s own private helpers, via the deferred `coas`
reference (see IMPORT CYCLE below) -- unaffected by the join deletion,
since they read the spine, not the tree.

IMPORT CYCLE (Review, 2026-08-21 -- do not revert this to a module-level
import): `close_out_and_stamp.py` itself imports from `coordinator_core.
ops.*` in several places (ceremony, plan_status_transition,
handoff_close_origin_stub, fleet._common). A module-level
`from coordinator_core.execute_plan_assemble.close_out_and_stamp import
(...)` here therefore creates a real cycle whenever `close_out_and_stamp`
happens to be the FIRST of the two imported: close_out_and_stamp ->
coordinator_core.ops -> the package's own eager-import loop -> this module
-> back into a partially-initialized close_out_and_stamp, which raises
`ImportError: cannot import name ... from partially initialized module`.
`coordinator_core/ops/__init__.py`'s eager-import loop CATCHES that
ImportError so nothing fails loudly -- this op simply never registers:
present in the source tree, absent from `plan.tasks.spine_drift_check`'s
own registry entry, exactly the "reachable by name" failure this op's own
dispatch brief warned against, arriving by an import-order route neither
the brief nor the first review pass anticipated. `_coas()` below defers
the import to CALL time instead: by the time a real request reaches
`_handler`, `close_out_and_stamp` has always finished importing elsewhere
first, so the cycle never has a chance to fire. See
`test_registers_when_close_out_and_stamp_imports_first` (this module's own
test file) for the regression pin -- it deliberately imports
`close_out_and_stamp` BEFORE `coordinator_core.ops`, in a fresh subprocess,
and asserts this op is still in the registry.

Self-registration: importing this module calls
register_op("plan.tasks.spine_drift_check") as a side-effect. Added to
coordinator_core/ops/__init__.py's eager-import table so registration fires
at start_server() time.

NEGATIVE-SPEC:
  - Does NOT write, anywhere, under any code path. No `locked_rmw`, no
    `_stamp_rows_in_body`, no frontmatter mutation of any kind.
  - Does NOT call `git log`, or any other git subprocess, for commit
    evidence -- the evidence join is deleted, not rebounded (see EVIDENCE
    JOIN DELETED above).
  - Does NOT touch `spine_read.py` or `emit.py`.

`drift_status` is now `"unknown"` for every commit-required `open` row,
`"no_open_rows"` when the spine has none, or `"error"` on a genuine
failure (unreadable plan, malformed spine) -- `"drift_detected"` and
`"verified_no_drift"` are dead states this op can no longer produce (kept
as named constants only because a caller may still branch on them; they
simply never fire again).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import main_worktree_root

SCHEMA_VERSION = 1

#: `drift_status` values this op can report. `DRIFT_STATUS_DRIFT_DETECTED`/
#: `DRIFT_STATUS_VERIFIED_NO_DRIFT` are retained as named constants for
#: caller-contract stability but can no longer fire -- see module
#: docstring, EVIDENCE JOIN DELETED.
DRIFT_STATUS_DRIFT_DETECTED = "drift_detected"
DRIFT_STATUS_VERIFIED_NO_DRIFT = "verified_no_drift"
DRIFT_STATUS_UNKNOWN = "unknown"
DRIFT_STATUS_NO_OPEN_ROWS = "no_open_rows"
DRIFT_STATUS_ERROR = "error"

#: Reason string attached to every commit-required `open` row this op can
#: no longer evaluate, now that the evidence join is deleted (P153-C22).
_EVIDENCE_JOIN_DELETED_REASON = (
    "no mechanical evidence source available -- the chunk-evidence "
    "commit-log join was deleted per DR-344's kill bar (P153-C22, "
    "docs/research/spike-verdicts/2026-09-22-evidence-join-range-bound.md, "
    "verdict: not-viable)"
)

_COAS_MODULE: Any = None


def _coas() -> Any:
    """Deferred import of `close_out_and_stamp` -- returns the module,
    cached in a module-level global after the first call. Called ONLY from
    inside `_handler` (never at this module's own import time) -- see the
    module docstring's "IMPORT CYCLE" paragraph for why a top-level import
    here is unsafe. `sys.modules` already caches the underlying import
    after the first real one anywhere in the process, so this adds no
    meaningful cost beyond the first call; the module-level cache below is
    purely to avoid a dict lookup+attribute walk through `sys.modules` on
    every request, not a correctness requirement."""
    global _COAS_MODULE
    if _COAS_MODULE is None:
        import coordinator_core.execute_plan_assemble.close_out_and_stamp as _mod

        _COAS_MODULE = _mod
    return _COAS_MODULE


def _open_spine_rows(rows: list[Any], coas: Any) -> list[dict]:
    """Non-`deferred`, `id`-bearing rows whose disposition reads `open`
    (D1 schema default, via `coas._row_disposition`) — the exact
    population `_auto_resolve_committed_open_rows` (`close_out_and_
    stamp.py`, AC8) scans for its write-side counterpart, restated here
    read-only. `coas` is the deferred-imported module (see `_coas()`),
    passed in rather than imported at this function's own module level."""
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("deferred", False):
            continue
        if not row.get("id"):
            continue
        if coas._row_disposition(row) == coas._OPEN:
            out.append(row)
    return out


@register_op("plan.tasks.spine_drift_check")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.tasks.spine_drift_check" handler — read-only.

    Required params:
        plan_path (str) — path to the plan, absolute or relative to the
                           worktree root.

    Returns:
        {
          "exit_code": 0,
          "schema_version": 1,
          "plan_path": <worktree-relative posix path>,
          "deliverable_id": <str or None>,
          "open_row_count": <int>,          # commit-required rows still `open`
          "drifted_rows": [],               # always empty -- see below
          "drifted_row_count": 0,
          "join_provenance": None,          # evidence join deleted (P153-C22)
          "evidence_available": False,
          "evidence_reason": <str, present whenever open_row_count > 0>,
          "drift_status": "unknown" | "no_open_rows" | "error",
        }

    With the evidence join deleted (module docstring, EVIDENCE JOIN
    DELETED), this op can no longer confirm OR rule out drift for any
    commit-required `open` row — every such row is reported inside
    `"unknown"` (never `"drift_detected"`/`"verified_no_drift"`), which is
    also this op's own long-documented safe direction: under-reporting
    drift is acceptable, a false "verified clean" is not.
      - `"unknown"` — `open_row_count` is nonzero; `drifted_rows` is always
        `[]` here, and `evidence_reason` names the deleted join.
      - `"no_open_rows"` — nothing in this plan's spine is `open` and
        commit-required; there was nothing to check.
      - `"error"` — `exit_code` is `1` and `error` names what failed
        (unreadable plan or malformed spine); every other field in this
        shape is meaningless.
    """
    if repo_root is None:
        return {
            "exit_code": 1,
            "error": "plan.tasks.spine_drift_check: repo_root is required (no founding root available)",
            "drift_status": DRIFT_STATUS_ERROR,
        }

    plan_path = (params or {}).get("plan_path")
    if not isinstance(plan_path, str) or not plan_path.strip():
        return {
            "exit_code": 1,
            "error": "plan.tasks.spine_drift_check: params.plan_path is required",
            "drift_status": DRIFT_STATUS_ERROR,
        }

    coas = _coas()

    worktree_root = main_worktree_root(repo_root)
    candidate = Path(plan_path)
    plan_file = candidate if candidate.is_absolute() else worktree_root / candidate

    try:
        text = plan_file.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "exit_code": 1,
            "error": f"plan.tasks.spine_drift_check: could not read {plan_path}: {exc}",
            "drift_status": DRIFT_STATUS_ERROR,
        }

    try:
        plan_path_rel = plan_file.resolve().relative_to(worktree_root.resolve()).as_posix()
    except ValueError:
        plan_path_rel = plan_path

    rows, rows_error = coas._parse_spine_rows(text, plan_path_rel)
    if rows_error is not None:
        return {"exit_code": 1, "error": rows_error, "drift_status": DRIFT_STATUS_ERROR}
    if rows is None:
        # Belt-and-braces (Review, 2026-08-21): `_parse_spine_rows`'s own
        # contract pairs `rows=None` with a non-None `error` on every path
        # (MALFORMED spine) -- the branch above already excludes that case
        # at runtime. This is a second, explicit check on the SAME
        # contract rather than a cast or a suppressed type-checker
        # warning, so a future change to that contract fails loud here
        # instead of silently reaching `_open_spine_rows(None, coas)`.
        return {
            "exit_code": 1,
            "error": f"{plan_path_rel}: _parse_spine_rows returned no rows and no error",
            "drift_status": DRIFT_STATUS_ERROR,
        }

    open_rows = _open_spine_rows(rows, coas)
    deliverable_id = coas._plan_deliverable_id(text)

    if not open_rows:
        return {
            "exit_code": 0,
            "schema_version": SCHEMA_VERSION,
            "plan_path": plan_path_rel,
            "deliverable_id": deliverable_id,
            "open_row_count": 0,
            "drifted_rows": [],
            "drifted_row_count": 0,
            "join_provenance": None,
            "evidence_available": None,
            "drift_status": DRIFT_STATUS_NO_OPEN_ROWS,
        }

    return {
        "exit_code": 0,
        "schema_version": SCHEMA_VERSION,
        "plan_path": plan_path_rel,
        "deliverable_id": deliverable_id,
        "open_row_count": len(open_rows),
        "drifted_rows": [],
        "drifted_row_count": 0,
        "join_provenance": None,
        "evidence_available": False,
        "evidence_reason": _EVIDENCE_JOIN_DELETED_REASON,
        "drift_status": DRIFT_STATUS_UNKNOWN,
    }
