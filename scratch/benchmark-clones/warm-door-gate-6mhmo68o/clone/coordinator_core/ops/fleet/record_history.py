"""
coordinator_core.ops.fleet.record_history -- "fleet.record_history" op.

Purpose: aggregate `coordinator_core.ops.record_history.derive_across_roots`'s
per-type, git-derived transition history across every registered ACTIVE
sibling, mirroring `coordinator_core.ops.fleet.work_state`'s own structure
(same `@register_op` placement, same `(params, repo_root=None)` handler
signature, same reuse of `_resolve_active_sibling_paths` for root
resolution) rather than inventing a second fan-out shape.

The handler is thin by construction: validate `record_type` (raising the
same `UnsupportedRecordTypeError` `records.history` itself raises, so an
unsupported type reads identically through both seams), resolve the active
sibling roots, call `derive_across_roots`, and return its dict unchanged --
no re-wrapping, no key rename, no envelope. `derive_across_roots`'s own
shape (`record_type`, `queried_root_count`, `roots_walked`,
`roots_skipped`, `repos`) is already the contract this op measures against.

`derive_across_roots` and `supported_record_types` are imported from
`coordinator_core.ops.record_history`, never copied -- this module adds no
git-log derivation logic of its own.

CLASSIFICATION: `OpClass.COMPUTE_ONLY` -- reads git history and the
machine-local registry, persists nothing, and returns its aggregated
answer verbatim, the same classification `fleet.work_state` and
`records.history` both carry.

SCOPE: `"none"`, matching `fleet.work_state` -- this op is fleet-generic,
not scoped to the calling repo's own tree; `repo_root` (engine-injected,
always `None` for scope `"none"`) is unused.

Purpose vs. mechanism (intent, not just behaviour). This op is an
EM/agent-facing aggregate, reachable in-repo via `coordinator-invoke`
exactly the way `tracker.render_status` is. It is **NOT**
Example-cockpit-repo's sanctioned query surface -- whether it becomes a third
named instance of that carve-out is PENDING the DR-287 ruling and is not
settled by this module or by registering it. This is the same
intent-not-mechanism line `docs/reference/sovereign-tracker-consumption-
contract.md` draws for `tracker.render_status`, which that doc calls "the
single most likely misreading of this seam" -- stated here in the same
terms so a reader arriving at this op via `coordinator-invoke` finds the
intent written down rather than having to infer it from the mechanism
alone.

Spec backlink: docs/plans/2026-08-20-a-counted-fleet-answer-for-record-history.md,
chunk C1.

Negative-spec:
    - Does NOT copy or re-derive any of `derive_across_roots`'s or
      `derive_type_history`'s own git-log parsing, pairing, or rename-chain
      logic -- this module's only job is per-repo-set root resolution plus
      a single pass-through call.
    - Does NOT re-wrap, rename a key in, or otherwise alter
      `derive_across_roots`'s returned dict -- AC2 is a shape-preservation
      bar and the aggregate's own shape is already the measured contract.
    - Is NOT example-cockpit-repo's sanctioned fleet query surface. Do not treat
      a successful `coordinator-invoke` call against this op as evidence
      that question has been settled -- it has not; see "Purpose vs.
      mechanism" above.
    - Does NOT fork a second registry parser or add a directory-scan
      fallback -- `_resolve_active_sibling_paths` (reused from
      `fleet.work_state`) is the ONE root-resolution path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet.work_state import _resolve_active_sibling_paths
from coordinator_core.ops.record_history import (
    UnsupportedRecordTypeError,
    derive_across_roots,
    supported_record_types,
)


def _require_supported(record_type: str) -> None:
    """Validate `record_type` up front, raising the SAME
    `UnsupportedRecordTypeError` `records.history` raises via
    `record_history._require_supported` -- so an unsupported type reads
    identically through both seams. Checked here explicitly (rather than
    left to `derive_across_roots`'s own per-walked-root call into
    `derive_type_history`) because an empty/all-skipped root set would
    otherwise let an unsupported type through silently: zero walked roots
    means `derive_type_history` -- and therefore its own validation -- is
    never reached at all.
    """
    supported = supported_record_types()
    if record_type not in supported:
        raise UnsupportedRecordTypeError(record_type, sorted(supported))


def build_fleet_record_history(record_type: str) -> dict:
    """Aggregate `derive_across_roots(record_type=...)` across every active,
    deduped sibling (per `_resolve_active_sibling_paths`).

    Validates `record_type` first (`_require_supported`, above) and returns
    `derive_across_roots`'s result dict unchanged.
    """
    _require_supported(record_type)
    roots = _resolve_active_sibling_paths()
    return derive_across_roots(roots, record_type)


@register_op("fleet.record_history")
def _fleet_record_history(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "fleet.record_history" handler.

    Params: `record_type` (required) -- an unsupported type raises the same
    `UnsupportedRecordTypeError` `records.history` itself raises, so both
    seams read identically for an unsupported type. Scope `"none"` means
    `repo_root` arrives as `None` (unused; this op is fleet-generic, not
    scoped to the calling repo's own tree).

    Returns: `build_fleet_record_history(record_type)`'s result verbatim.
    """
    record_type = params.get("record_type")
    if not record_type:
        raise ValueError(
            "fleet.record_history requires 'record_type'; supported: "
            + ", ".join(sorted(supported_record_types()))
        )
    return build_fleet_record_history(record_type)
