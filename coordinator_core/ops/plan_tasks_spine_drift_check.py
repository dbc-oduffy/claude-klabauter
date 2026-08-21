"""
coordinator_core.ops.plan_tasks_spine_drift_check — JSON-RPC
"plan.tasks.spine_drift_check" operation.

Purpose: `close_out_and_stamp.py` already carries a hardened oracle for
"does this plan's `## Tasks` spine have a covering commit" —
`_committed_chunk_shas` (the Deliverable-Id-trailer/subject-chunk-id join,
scarred against a named 2026-07-27 cross-plan chunk-id collision) and
`_committed_id_covers_spine_id` (sub-chunk-suffix coverage). Today the only
way to SEE that a spine row still reads `disposition: open` while the tree
already shipped its work is to run `close_out_and_stamp` itself — a
mutating call. A fireable wave-map (`spine_read.read_spine` ->
`wave_map.build_waves`) will happily re-dispatch an already-landed chunk in
the meantime, silently. This op exposes the SAME oracle read-only, so a
spine can be checked against the tree without stamping anything.

Backlog record: state/bug-backlog/2026-08-21-spine-drift-is-invisible-
between-execute-and-emit-a1c4e7b20d13.yaml
Sizing object: state/sizings/2026-08-21-a-spine-that-disagrees-with-the-
tree-sho.yaml

REUSE, not reimplementation: every join/coverage decision below is a direct
call into `close_out_and_stamp`'s own private helpers (`_parse_spine_rows`,
`_all_spine_ids`, `_plan_deliverable_id`, `_committed_chunk_shas`,
`_committed_id_covers_spine_id`, `_row_disposition`) — the identical pattern
`coordinator_core.ops.cascade_baton_rows` already uses to reuse this same
module's helpers from a second call site. Nothing here re-derives the
trailer/subject join, opens a second commit ledger, or relaxes
`_committed_chunk_shas`'s exact-equality Deliverable-Id join.

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

Report-only by architectural boundary (DR-263), mirroring
`coordinator_core.ops.cascade_backstop_sweep`'s own "reports but never
flips" posture: this op never writes the plan file, never calls
`_auto_resolve_committed_open_rows` (the existing WRITE-side consumer of
this same evidence — `close_out_and_stamp.close_out_and_stamp` itself
remains the only entrypoint that flips a row), and never stamps anything.

Brightline (`docs/decisions/DR-344-*`): exactly ONE batched `git log` call
per invocation — `_committed_chunk_shas`, called once — never one per open
row. When the spine has no commit-required OR no open rows at all, the git
call is skipped entirely (nothing to check against).

KNOWN, NAMED, out-of-remit cost this op INHERITS rather than introduces
(Review, 2026-08-21): `_committed_chunk_shas`'s own `_chunk_evidence_log_
lines` query measured 4.5-6.4s wall / 469-719ms in-process-CPU on a
15,988-line log range in this repo -- over the 500ms brightline
(`docs/decisions/DR-344-*`) and the >2s CLAUDE.md forbids outright. This
predates this op (it is `close_out_and_stamp`'s own existing query, reused
verbatim per this module's REUSE mandate) and also implicates the existing
mutating close-out path, not merely this read-only one -- fixing it here
would be a narrow, load-bearing perf change to shared machinery, above
this op's remit. NOT fixed here; surfaced by the reviewing EM to the PM
instead. Do not narrow the log range or otherwise perf-patch this from
inside this module without that decision.

Self-registration: importing this module calls
register_op("plan.tasks.spine_drift_check") as a side-effect. Added to
coordinator_core/ops/__init__.py's eager-import table so registration fires
at start_server() time.

NEGATIVE-SPEC:
  - Does NOT write, anywhere, under any code path. No `locked_rmw`, no
    `_stamp_rows_in_body`, no frontmatter mutation of any kind.
  - Does NOT re-derive the Deliverable-Id trailer/subject-chunk-id join, or
    the sub-chunk-suffix coverage match — both are imported and called
    exactly as `close_out_and_stamp.py` itself calls them.
  - Does NOT call `_commit_subject` per drifted row (a covering commit's
    subject) — that is a SECOND git spawn per row, which the brightline
    forbids; only the sha `_committed_chunk_shas` already captured in its
    one batched call is reported.
  - Does NOT touch `spine_read.py` or `emit.py`.
  - Does NOT perf-patch `_chunk_evidence_log_lines`/`_committed_chunk_shas`
    from inside this module -- see the KNOWN cost paragraph above.

Known narrowing vs. the full oracle: `evidence_available` is `True`, and
`drift_status` can be `"drift_detected"`/`"verified_no_drift"`, only for
the exact-equality Deliverable-Id-trailer join (`JOIN_PROVENANCE_JOINED`)
— this op does NOT recognize `_committed_chunk_shas`'s own Session-Id-
scoped fallback leg (`JOIN_PROVENANCE_SESSION_FALLBACK_PARTIAL` in
`_determine_shipped`) as evidence, nor the sibling-repo/`disposition_ref`
unions `_determine_shipped` layers on afterward. A row this fallback alone
covers is therefore reported `"unknown"` rather than as drift — a false
negative, never a false positive, matching this module's own documented
"false-negative-over-false-positive" posture throughout.

Stated plainly because the failure direction matters (Review, 2026-08-21):
a row covered ONLY by the Session-Id fallback or a sibling-repo/
`disposition_ref` union lands in the `"unknown"` bucket, NEVER in
`"verified_no_drift"`. Under-reporting drift (a real drift this narrowing
misses) is the safe, accepted direction; silently reporting such a row as
verified-clean would not be — `"unknown"` is the only bucket this
narrowing is allowed to produce for evidence it cannot see. A future
widening that recognizes those legs must preserve that: promote a
newly-recognized row into `"drift_detected"`/`"verified_no_drift"` only
when it is ACTUALLY checked, never by relabeling `"unknown"` wholesale.

Measurement basis for the `drift_status` three-way split (2026-08-21, over
600 commits on `work/machine-a/2026-08-18to20`): only ~6% of commits carry a
chunk-id subject (`C1:`, `C1,C2:`) at all; when one IS present its
`Deliverable-Id` trailer is essentially always there too (the join is
trustworthy when it fires), but its RECALL across the corpus is very low.
The join can confirm a row shipped; it can never confirm a row did not —
so `"unknown"` (join found nothing to compare against) is the
overwhelmingly common outcome, not an edge case, and must never collapse
into the same signal as `"verified_no_drift"` (join found real evidence
and no open row was covered by it).

`drift_status` is set on EVERY return path, including every error/
`exit_code: 1` early return (Review, 2026-08-21 dogfooding across 254 real
plans: 3 came back with `drift_status` absent -- read externally as
`None` by any `dict.get`-style caller, an unhandled fifth shape outside
the documented four states, and precisely how "unknown" quietly becomes
"clean" downstream if left unstated). `DRIFT_STATUS_ERROR` names that
fifth state explicitly rather than leaving it an implicit key-absence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import main_worktree_root

SCHEMA_VERSION = 1

#: Five-way `drift_status` this op reports (2026-08-21, measurement-driven
#: widening -- only ~6% of commits on this branch carry a chunk-id subject
#: at all; when one is present its `Deliverable-Id` trailer is reliable,
#: but the JOIN'S RECALL is low). The oracle can confirm a row shipped; it
#: can never confirm a row did NOT ship. Collapsing "no evidence either
#: way" into the same `[]` as "checked, genuinely clean" would recreate
#: exactly the false-green this whole area exists to end -- a caller must
#: be able to tell the two apart without inspecting `join_provenance`
#: itself. Values are plain english labels for `join_provenance`'s own
#: four-state enum (`close_out_and_stamp.py`), not a competing vocabulary,
#: plus `"error"`/`"no_open_rows"` for this op's own two non-oracle exits.
DRIFT_STATUS_DRIFT_DETECTED = "drift_detected"
DRIFT_STATUS_VERIFIED_NO_DRIFT = "verified_no_drift"
DRIFT_STATUS_UNKNOWN = "unknown"
DRIFT_STATUS_NO_OPEN_ROWS = "no_open_rows"
DRIFT_STATUS_ERROR = "error"

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


def _join_provenance(join_stats: Any, coas: Any) -> str:
    """Classifies an already-computed `DeliverableJoinStats` into one of
    the four join-provenance states `close_out_and_stamp._determine_
    shipped` itself reports — mirrors that function's own branching
    verbatim (same field reads, same order) since the classification is
    not exposed as a standalone callable there. Does NOT recompute
    anything `_committed_chunk_shas` didn't already capture in its one
    call — this only reads the `DeliverableJoinStats` it returned. `coas`
    is the deferred-imported module (see `_coas()`)."""
    if not join_stats.attempted:
        return coas.JOIN_PROVENANCE_NO_JOIN_KEY
    if join_stats.trailered_commit_count == 0:
        return coas.JOIN_PROVENANCE_NO_JOIN_CANDIDATES
    if join_stats.matched_commit_count > 0:
        return coas.JOIN_PROVENANCE_JOINED
    return coas.JOIN_PROVENANCE_KEY_MISMATCH


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
          "drifted_rows": [
            {"chunk_id": ..., "covering_sha": ...}, ...
          ],
          "drifted_row_count": <int>,
          "join_provenance": <one of close_out_and_stamp's four values, or None>,
          "evidence_available": <bool or None>,
          "drift_status": "drift_detected" | "verified_no_drift" | "unknown"
                           | "no_open_rows" | "error",
        }

    A `drifted_rows` entry names an `open` spine row whose chunk-id is
    COVERED (`_committed_id_covers_spine_id`) by a commit the SAME
    Deliverable-Id-trailer join `close_out_and_stamp` itself trusts
    (`_committed_chunk_shas`) — i.e. a row the mutating close-out would
    auto-resolve to `coded` today, had it run.

    `drift_status` is the field callers should branch on, and is set on
    EVERY return path — a caller must never infer it from key-absence:
      - `"drift_detected"` — `drifted_rows` is non-empty: real signal, a
        row this plan's own oracle would auto-resolve today.
      - `"verified_no_drift"` — the join found real evidence for this plan
        (`join_provenance == "joined"`) and no open row was covered by it.
        A genuine negative, backed by a lookup — NOT "we found nothing to
        check" (see `"unknown"` below).
      - `"unknown"` — the join found NOTHING to compare against at all
        (`join_provenance` one of `no_join_key`/`no_join_candidates`/
        `key_mismatch`) — the OVERWHELMINGLY COMMON case on this branch
        (only ~6% of commits carry a chunk-id subject at all; absence of a
        match is NOT evidence of absence of shipped work). `drifted_rows`
        is always `[]` here too, but that `[]` means "no evidence either
        way", not "checked and clean".
      - `"no_open_rows"` — nothing in this plan's spine is `open` and
        commit-required; there was nothing to check.
      - `"error"` — `exit_code` is `1` and `error` names what failed
        (unreadable plan, malformed spine, or the git-log query itself
        failing); every other field in this shape is meaningless.

    `evidence_available` (`join_provenance == "joined"`) is retained
    alongside `drift_status` for callers that want the raw provenance
    value rather than the five-way label.
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

    spine_ids = coas._all_spine_ids(rows)
    query_ok, _committed, committed_shas, join_stats = coas._committed_chunk_shas(
        worktree_root,
        deliverable_id,
        spine_ids,
        plan_text=text,
        plan_path_rel=plan_path_rel,
    )
    if not query_ok:
        return {
            "exit_code": 1,
            "error": (
                f"{plan_path_rel}: git-log query for landed chunk commits failed -- "
                "cannot determine spine drift mechanically"
            ),
            "drift_status": DRIFT_STATUS_ERROR,
        }

    join_provenance = _join_provenance(join_stats, coas)
    evidence_available = join_provenance == coas.JOIN_PROVENANCE_JOINED

    drifted_rows: list[dict[str, Any]] = []
    if evidence_available:
        for row in open_rows:
            chunk_id = str(row["id"])
            sha = next(
                (
                    committed_shas[committed_id]
                    for committed_id in committed_shas
                    if coas._committed_id_covers_spine_id(committed_id, chunk_id)
                ),
                None,
            )
            if sha is not None:
                drifted_rows.append({"chunk_id": chunk_id, "covering_sha": sha})

    if drifted_rows:
        drift_status = DRIFT_STATUS_DRIFT_DETECTED
    elif evidence_available:
        drift_status = DRIFT_STATUS_VERIFIED_NO_DRIFT
    else:
        drift_status = DRIFT_STATUS_UNKNOWN

    result: dict[str, Any] = {
        "exit_code": 0,
        "schema_version": SCHEMA_VERSION,
        "plan_path": plan_path_rel,
        "deliverable_id": deliverable_id,
        "open_row_count": len(open_rows),
        "drifted_rows": drifted_rows,
        "drifted_row_count": len(drifted_rows),
        "join_provenance": join_provenance,
        "evidence_available": evidence_available,
        "drift_status": drift_status,
    }
    if not evidence_available:
        result["evidence_reason"] = coas._JOIN_PROVENANCE_REASON.get(join_provenance)
    return result
