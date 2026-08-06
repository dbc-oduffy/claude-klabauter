"""
coordinator_core.ops.fleet.plan_handoffs — the "fleet.handoffs_for_plan" COMPUTE_ONLY op.

Purpose: given a plan's ``origin_plan_id``, enumerate every handoff that carries
it — live (``state/handoffs/*.md``) AND archived (``archive/handoffs/**/*.md``),
terminal AND non-terminal — with enough per-row detail (path, status,
deployment_state, shipped_in, claimed_by, and whether the file is live or
archived) for a repair/sweep op to act on. Nothing in the system previously
did the equivalent of ``SELECT * FROM handoffs WHERE origin_plan_id = X``
across both live and archived homes in one call; the 2026-07-26 repair
(commit 5280701e) closed the B-series of a campaign by naming each baton
individually and silently missed eight G-series batons in the identical
state, because there was no aggregation to catch the gap. ``origin_plan_id``
is already the right key (schema-registered in
``coordinator_core/frontmatter/schemas/handoff.schema.json``, identical
across every baton one plan minted) — this op is purely the missing
aggregation, not a new field.

Built entirely on top of ``coordinator_core.ops.ceremony.records_query
.query_records`` — the same in-process ``where=``-grammar read seam
``coordinator_core.ops.blocked`` and ``coordinator_core.ops.queue_family``
already reuse rather than re-parsing frontmatter themselves. Deliberately NOT
an extension of ``coordinator_core.ops.records_query`` (the frozen,
byte-parity ``records.query`` JSON-RPC op ported from ``query-records.js``)
— that module's ``_TYPE_TO_GLOB``/``_SYNTHETIC_TYPES`` set is a documented
port-fidelity surface with its own differential parity suite
(``test_records_query_parity.py``); adding a new union-of-two-types synthetic
type there for a need specific to plan-authored handoff aggregation would
graft a non-oracle concept onto a frozen contract. This op instead composes
TWO existing, unmodified ``query_records`` calls (``type="handoff"`` and
``type="handoff-archived"``, both ``where=origin_plan_id=<id>``) and merges
them — same "compose, don't extend the frozen grammar" posture
``coordinator_core.ops.blocked`` and ``queue_family`` already model.

Response envelope: the standard fleet.* ``build_dry_run_result`` (contract
§2.1) — this op has no act mode (pure read, mirrors ``memo.list``'s posture).
Each ``candidates[]`` entry carries the six frozen keys (``id``, ``title``,
``status``, ``family``, ``terminal_since``, ``note``) PLUS the caller-facing
detail this op exists to surface: ``deployment_state``, ``shipped_in``,
``claimed_by``, ``origin_plan_id``, and ``live`` (bool — True for
``state/handoffs/``, False for ``archive/handoffs/``). The frozen six are not
narrowed; extra keys are additive, matching how ``memo.list`` already stuffs
op-specific detail (``repo_key``, ``resolved``, ``canonical_to``, ...) into
its own candidates without violating the contract.

Spec backlink: cross-repo memo from claude-central-em, 2026-07-26 —
  "the B-series repair closed named batons one at a time; nothing enumerates
  what a given plan minted, so the G-series went unnoticed."
Wire contract: coordinator_core/contract/cockpit-invoke-producer-contract.md §2.1
Schema:        coordinator_core/frontmatter/schemas/handoff.schema.json (origin_plan_id)

Negative-spec:
  - Does NOT add a new field to the handoff schema — origin_plan_id already
    exists and is already populated by the minting op; this module only reads it.
  - Does NOT touch coordinator_core/ops/records_query.py or its
    _TYPE_TO_GLOB/_SYNTHETIC_TYPES tables — see module docstring above for why
    that frozen, JS-parity-gated surface is the wrong extension point.
  - Does NOT write, commit, or mutate anything — provably side-effect-free,
    same DR-208 five-question posture as memo.list.
  - Does NOT accept dry_run:false — this op has no act mode; a caller passing
    dry_run:false gets a setup-error envelope, not a silently-ignored flag.
  - Does NOT deduplicate a plan id that (incorrectly) appears in both live and
    archived homes for the SAME handoff path — that shape should not occur
    (a handoff is either live or archived, never both), and silently
    collapsing it would hide the very drift this op exists to surface.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony.records_query import query_records
from coordinator_core.ops.fleet._common import (
    build_dry_run_result,
    build_setup_error_result,
    check_repo_root,
    main_worktree_root,
)
from coordinator_core.session import claims

_MODE = "handoffs_for_plan"

# The two record_type homes a plan-minted handoff can be found in — live
# (state/handoffs/) and archived (archive/handoffs/). Order is preserved in
# the returned candidates (live first) so a repair op sees the still-open
# batons before the already-closed ones.
_PLAN_HANDOFF_TYPES = ("handoff", "handoff-archived")

# Output-shape key for the DR-084-resolved claim holder (see
# _candidate_from_record's docstring below). This module emits the claim
# holder's field name as a dict key in its candidate shape — that's OUTPUT
# data, never a frontmatter lookup, so it's a legitimate, greppable field
# name rather than a DR-084 single-accessor violation. query_records() does
# NOT normalize the dual claim-holder vocabulary for this field (only status
# and the claim-timestamp field get normalized), which is why
# _candidate_from_record() resolves it through
# claims.handoff_lifecycle().claim_holder() instead of reading it raw.
_CLAIMED_BY_KEY = "claimed_by"  # dr084: write-not-read, output dict key emitted to callers, not a frontmatter lookup


def _candidate_from_record(record: dict, *, record_type: str, plan_id: str) -> dict:
    """Build one candidate dict from a query_records() result.

    Carries the six frozen fleet.* candidate keys (contract §2.1) plus the
    per-row detail a repair op needs: deployment_state, shipped_in,
    claimed_by, origin_plan_id (echoed back for a caller that merges several
    plans' results), and live (True for "handoff", False for "handoff-archived").

    The claim-holder field is resolved through the DR-084 single accessor
    (``coordinator_core.session.claims.handoff_lifecycle().claim_holder()``)
    rather than a raw frontmatter-dict lookup of the field name by itself —
    ``query_records()`` does NOT normalize the dual claim-holder vocabulary
    this field can be recorded under (it only normalizes ``status`` and the
    claim-timestamp field from a body marker), so a legacy-vocabulary handoff
    whose holder is recorded under the OLD field name would otherwise
    silently resolve to a missing value here.
    """
    fm = record["frontmatter"]
    path = record["path"]
    title = fm.get("title") or Path(path).stem
    claimed_by = claims.handoff_lifecycle().claim_holder(fm) or None
    return {
        "id": path,
        "title": title,
        "status": fm.get("status"),
        "family": "handoff",
        "terminal_since": None,  # not materialised for handoffs (contract allows null)
        "note": None,
        "deployment_state": fm.get("deployment_state"),
        "shipped_in": fm.get("shipped_in"),
        _CLAIMED_BY_KEY: claimed_by,
        "origin_plan_id": plan_id,
        "live": record_type == "handoff",
    }


def _collect_plan_handoffs(worktree_root: Path, plan_id: str) -> list[dict]:
    """Union query_records("handoff", ...) and query_records("handoff-archived", ...)
    over the SAME where=origin_plan_id=<plan_id> filter, tagging each result with
    its source type. Mirrors the "collect per type, tag, merge" shape
    coordinator_core.ops.records_query._query_unattached_all already uses for its
    own multi-type union lens — same pattern, scoped to these two types instead
    of UNATTACHED_TYPES.

    A per-type query_records() failure (directory-scan error) degrades to []
    for that type only (query_records' own fail-open contract — see its
    docstring) rather than aborting the whole union; a repair op reading a
    partial result is strictly better than one reading none.
    """
    candidates: list[dict] = []
    where = f"origin_plan_id={plan_id}"
    for record_type in _PLAN_HANDOFF_TYPES:
        records = query_records(record_type, worktree_root, where=where, limit=0)
        for record in records:
            candidates.append(
                _candidate_from_record(record, record_type=record_type, plan_id=plan_id)
            )
    return candidates


@register_op("fleet.handoffs_for_plan")
async def _handoffs_for_plan(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "fleet.handoffs_for_plan" handler.

    Enumerate every handoff (live + archived) whose ``origin_plan_id`` matches
    the caller-supplied ``plan_id`` — the missing aggregation named in this
    op's module docstring.

    Params:
        dry_run (bool, required): must be True — this op has no act mode
                                   (pure read, mirrors memo.list's posture).
        plan_id (str, required):  the origin_plan_id value to look up. Callers
                                   holding only a plan FILE PATH resolve it to
                                   an origin_plan_id themselves first (a plan's
                                   OWN id is a cheap frontmatter read one call
                                   away via records.query type=plan — this op
                                   does not do that resolution itself, to keep
                                   its own contract to the one cheap lookup it
                                   exists for: handoffs by plan id, not
                                   plan-path resolution, which is records.query's
                                   job already).

    Returns:
        build_dry_run_result envelope (contract §2.1) with one candidate per
        matching handoff (live entries before archived, each source's own
        query_records() enumeration order otherwise preserved). Empty
        candidates[] (not an error) when plan_id has zero matching handoffs.
        build_setup_error_result (exit_code:1) on a missing/blank plan_id, a
        non-True dry_run, or an absent repo_root.
    """
    dry_run = params.get("dry_run")
    if dry_run is not True:
        return build_setup_error_result(
            _MODE, dry_run,
            "fleet.handoffs_for_plan: dry_run must be True — this op has no act mode",
        )

    plan_id = params.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id.strip():
        return build_setup_error_result(
            _MODE, dry_run,
            f"fleet.handoffs_for_plan: plan_id is required and must be a non-empty "
            f"string, got {plan_id!r}",
        )
    plan_id = plan_id.strip()

    if repo_root is None:
        return build_setup_error_result(
            _MODE, dry_run,
            "fleet.handoffs_for_plan: repo_root arg is None — common_dir not "
            "supplied by engine (check _OP_KEY_SCOPE = 'common_dir')",
        )
    common_dir = Path(repo_root)
    worktree_root = main_worktree_root(common_dir)

    d3_error = check_repo_root(params.get("repo_root"), common_dir)
    if d3_error:
        return build_setup_error_result(_MODE, dry_run, d3_error)

    try:
        candidates = _collect_plan_handoffs(worktree_root, plan_id)
    except SystemExit as exc:
        # query_records' own _parse_where raises SystemExit on an unparseable
        # where clause. plan_id is interpolated into an equality clause here
        # (never caller-supplied where syntax directly), so this should be
        # unreachable in practice — surfaced as a setup error rather than
        # letting a bare SystemExit escape the op handler.
        print(
            f"fleet.handoffs_for_plan: query_records where-clause parse failed "
            f"for plan_id={plan_id!r}: {exc}",
            file=sys.stderr,
        )
        return build_setup_error_result(
            _MODE, dry_run,
            f"fleet.handoffs_for_plan: internal where-clause parse failure for "
            f"plan_id={plan_id!r}",
        )

    return build_dry_run_result(_MODE, candidates)
