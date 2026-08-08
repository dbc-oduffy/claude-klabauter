"""archival.py — Reverse-membership guard for the handoff archival pipeline.

Purpose: Provide the reverse-membership interface that determines whether a
handoff is still a live parent — i.e. whether any other handoff in the in-memory
frontmatter index names it as predecessor, additional_predecessors, or forked_from.
Replaces the query-records.js double-spawn with a pure in-memory walk over the
DAG index.

C0 authored the interface seam (signature, ChildRecord, module docstring) so C3
and C4 could be authored in parallel in Wave 2 against a frozen contract.
C4 fills the implementation body here.

Bug fix (2026-07-09): the referencedBy set returned by dag.referenced_by can
include archive-resident and/or terminal-status (consumed/superseded/abandoned)
children. A consumed parent whose only reverse-membership edge points at such a
child was reported as referenced=True forever, so it never archived — a
faithfully-ported bash bug. reverse_membership now excludes archive-resident
and terminal-status children from the "live" set on a POSITIVE classification
only; indeterminate frontmatter is retained (fail-closed). See
cross-repo/inbox/2026-07-09-claude-central-em-handoff-has-live-children-terminal-archived-exclusion.md.

Bug fix (2026-07-17): the terminal-status exclusion above (consumed/superseded/
abandoned) had no reference to `deployment_state`, so a `consumed` child whose
`deployment_state` was `in_flight` — still OPEN/unfinished work under
handoff_reconcile.py's widened _is_open semantics — was wrongly excluded from
the "live" set, letting a parent be archived while that immediate child still
named it as predecessor. `_is_terminal_or_archived_child` now retains a
`consumed` child as live when its `deployment_state == "in_flight"`;
`superseded`/`abandoned` remain terminal unconditionally. See
state/bug-backlog/2026-07-17-archival-reverse-membership-ignores-deployment-state.yaml
and state/review-trail/findings/2026-07-17-codereview-slicereconcile-open-dead-zone-coordinator-core-ops-handoff-reconcile-p.md
(Finding 2).

Spec backlink: docs/plans/2026-07-02-pcore-03-beachhead-coordinator-core.md
§ C0 (seam author), § C4 (full implementation).

Negative-spec:
  - Does NOT call ps -p / kill -0 / psutil.pid_exists on any stored PID
    (RAW-PID-LIVENESS floor; session liveness routes via liveness.py).
  - Does NOT scan the filesystem directly — callers supply the dag_index
    (list of paths) so the scan strategy is caller-controlled.
  - Does NOT traverse transitively — reverse_membership is a single-hop check
    (mirrors referencedBy in walk-handoff-dag.js, not walkForward).
  - Does NOT exclude a child on indeterminate/unparseable frontmatter — only a
    definitively terminal status or definitive archive-residency removes a
    child from the live set (fail-closed default; see _is_terminal_or_archived_child).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Set, Tuple

from coordinator_core.claim_state import resolve_claim_state
from coordinator_core.dag import _read_meta
from coordinator_core.dag import referenced_by as _referenced_by
from coordinator_core.lifecycle_constants import HANDOFF_ARCHIVAL_TERMINAL_STATUSES
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT

# Frontmatter status values that mark a handoff as terminal — a terminal child
# no longer counts as a "live" reference for the reverse-membership guard.
# Mixed status+deployment defensive set; SSOT'd via lifecycle_constants per DR-084 C3.
_TERMINAL_STATUSES: Set[str] = set(HANDOFF_ARCHIVAL_TERMINAL_STATUSES)


# ---------------------------------------------------------------------------
# Shared data-classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChildRecord:
    """A single forward-edge (child) relationship in the handoff DAG.

    Attributes:
        path:      Repo-relative path to the handoff file that names the
                   parent (node_path) as a live predecessor/ancestor.
        edge_kind: The frontmatter field that created this edge; one of
                   "predecessor", "additional_predecessors", or "forked_from"
                   (mirrors EDGE_KIND_META in coordinator_core/dag.py and
                   walk-handoff-dag.js — see D4 dual-homed SSOT note).
    """

    path: str
    edge_kind: str


# ---------------------------------------------------------------------------
# Live-children exclusion predicate
# ---------------------------------------------------------------------------


def _is_terminal_or_archived_child(path: str) -> bool:
    """Return True iff path should be excluded from the "live children" set.

    Three POSITIVE exclusion rules, any one sufficient on its own:
      1. archive-resident: path lives under a consecutive .../archive/handoffs/...
         segment pair (checked on Path(path).parts, not a substring match — a
         substring check would false-match a repo literally named "archive").
      2. terminal-status: the child's frontmatter status is one of
         _TERMINAL_STATUSES (consumed, superseded, abandoned), read via
         dag._read_meta — EXCEPT a `consumed` child whose `deployment_state`
         is `in_flight`, which is still OPEN/unfinished work and is therefore
         NOT excluded (see the `consumed`-branch deployment_state check below).
         `superseded` and `abandoned` are terminal unconditionally, regardless
         of `deployment_state`.
      3. terminal-deployment-state (DR-084): the child's frontmatter
         `deployment_state` is one of HANDOFF_TERMINAL_DEPLOYMENT (shipped,
         abandoned, continued, closed), read via dag._read_meta — regardless
         of `status`. DR-084 moved terminality onto the deployment_state axis
         and narrowed `status` to open|claimed only; a handoff can now carry
         `status: open` with a terminal `deployment_state` (e.g. the
         close-handoff verb stamps `deployment_state: closed` +
         `closed_reason` while leaving `status: open`), and rule 2 alone never
         catches that shape. `in_flight` is NOT a member of
         HANDOFF_TERMINAL_DEPLOYMENT, so this rule cannot resurrect a
         consumed/claimed+in_flight child that the rule-2 carve-out below
         deliberately retains as live. This is the drift the rule-2 docstring's
         lockstep warning predicted, and it fired: DR-084 introduced exactly
         the additional deployment_state vocabulary that warning anticipated,
         without this predicate being extended to read it.

    Fail-closed default: if none of the three rules positively fires —
    including when _read_meta returns {} (unreadable/unparseable frontmatter)
    or both the status and deployment_state keys are absent or unrecognized —
    this returns False (child is RETAINED, i.e. still counted as live).  Only
    a definitive terminal status, definitive terminal deployment_state, or
    definitive archive-residency removes a child from the live set;
    indeterminacy must never cause a parent to look archivable.

    Review: code-reviewer F2 — the archive-residency check (rule 1) assumes
    callers scan the conventional archive/handoffs/ tree; a future caller
    supplying a differently-shaped archive convention (e.g. a symlinked mount
    whose resolved path doesn't literally contain "handoffs" as the immediate
    child of "archive") must re-verify this predicate before relying on it.
    """
    parts = Path(path).parts
    for i in range(len(parts) - 1):
        if parts[i] == "archive" and parts[i + 1] == "handoffs":
            return True

    meta = _read_meta(path)
    status = meta.get("status") if meta else None
    # Review: code-reviewer F1 — normalize case/whitespace so `status: Consumed`/
    # `CONSUMED` are recognized; fail-closed default (None/absent) is untouched
    # since (None or "") == "".
    normalized_status = (status or "").strip().lower()

    # Rule 3 (DR-084): a definitive terminal deployment_state excludes the
    # child regardless of `status`. Checked ahead of the status-only early
    # return below so a `status: open` + terminal `deployment_state` child
    # (the close-handoff verb's shape) is still caught. `in_flight` is not a
    # member of HANDOFF_TERMINAL_DEPLOYMENT, so this cannot fire for the
    # consumed/claimed+in_flight carve-out case handled further down.
    normalized_deployment_state = (
        (meta.get("deployment_state") or "").strip().lower() if meta else ""
    )
    if normalized_deployment_state in HANDOFF_TERMINAL_DEPLOYMENT:
        return True

    if normalized_status not in _TERMINAL_STATUSES:
        return False

    if normalized_status in ("consumed", "claimed"):
        # Review: code-reviewer F2 (2026-07-17 reconcile-open-dead-zone review,
        # bug-backlog state/bug-backlog/2026-07-17-archival-reverse-membership-
        # ignores-deployment-state.yaml) — a consumed/claimed+in_flight child is
        # still OPEN/unfinished work under handoff_reconcile.py's widened _is_open
        # semantics, the exact archive-complement of ops/fleet/archive_handoffs.py's
        # _is_terminal Branch A2 (deployment_state != "in_flight" hard exclusion for
        # status==consumed/claimed).  Treating a consumed/claimed+in_flight child
        # as terminal here would let a parent be archived by
        # fleet.archive_completed_handoffs' Check 3 (this module's
        # reverse_membership) while the child still names it as
        # predecessor/additional_predecessors/forked_from.  This is the interim
        # forward-compatible subset of the fuller example-doctrine-repo lvv-04/C3 archive-safe
        # predicate (lifecycle-vocab roadmap) — just the in_flight hard exclusion,
        # mirroring archive_handoffs.py's Check A2 negative-spec: DR-084 renamed
        # status consumed->claimed (dual-tolerant read window); if example-doctrine-repo lvv-04/C3
        # introduces additional non-terminal deployment_state values that can
        # co-occur with status:consumed/claimed, this exclusion must be extended
        # in lockstep — or inverted to a terminal-state allowlist — or this
        # predicate will silently treat them as terminal (live-excluding) again.
        # Does NOT apply to `superseded`/`abandoned` — those remain terminal
        # regardless of deployment_state.
        deployment_state = (meta.get("deployment_state") or "").strip().lower() if meta else ""
        if deployment_state == "in_flight":
            return False

    return True


# ---------------------------------------------------------------------------
# Public interface (seam)
# ---------------------------------------------------------------------------


def reverse_membership(
    node_path: str,
    dag_index: List[str],
    *,
    exclude: Optional[List[str]] = None,
    edge_kinds: Optional[Set[str]] = None,
) -> frozenset:
    """Return the set of handoff paths that still name node_path as a live parent.

    Reverse-membership contract: a handoff H is a member of the returned set
    iff H's frontmatter contains node_path in at least one of the edge-kind
    fields defined in EDGE_KIND_META (predecessor, additional_predecessors,
    forked_from).  "Live" means H is present in dag_index (the caller-supplied
    scan set, which typically includes both state/handoffs/ and archive/handoffs/).

    This is a single-hop membership test, NOT transitive reachability.  Mirrors
    the referencedBy primitive in walk-handoff-dag.js, replacing a node
    double-spawn with an in-memory walk.

    Args:
        node_path:   Absolute or repo-relative path of the handoff node whose
                     children we are enumerating (the "parent" in DAG terms).
                     Resolved to an absolute path before comparison.
        dag_index:   The in-memory scan set: list[str] of absolute or resolvable
                     handoff paths to check for references to node_path.
                     Corresponds to the combined live+archived handoff set
                     obtained via query-records.js (both --type handoff and
                     --type handoff-archived).
        exclude:     Optional list of paths to drop from the scan set before
                     checking references.  Mirrors an --exclude flag.
        edge_kinds:  Optional set of edge-kind names to follow when checking
                     for references.  Defaults to all three kinds when None.
                     Valid names: "predecessor", "additional_predecessors",
                     "forked_from".

    Returns:
        frozenset of absolute handoff paths (str) that reference node_path as a
        live parent.  Terminal-status (consumed/superseded/abandoned) and
        archive-resident children are excluded from this set — they no longer
        count as "live" references, so a parent whose only referencing children
        are terminal/archived is reported as having zero live children (safe
        to archive).  Exclusion is fail-closed on indeterminate frontmatter:
        a child with unparseable/absent status is retained (counted as live),
        never excluded.  See _is_terminal_or_archived_child.
        Empty frozenset if node_path has no live referencing nodes.

    Raises:
        ValueError: when dag_index is empty — the caller must supply a non-empty
                    scan set.  Fail-closed: an empty index cannot prove the
                    absence of children.
        TypeError:  when dag_index is not iterable.
    """
    # C4 implementation: thin delegation to coordinator_core.dag.referenced_by,
    # which provides the same single-hop reverse-membership semantics as the
    # walk-handoff-dag.js referencedBy primitive.
    # (dag.py now ships in the same package; import is hoisted to module top — W8.)

    # Validate dag_index — fail-closed on empty
    if dag_index is None:
        raise ValueError(
            "reverse_membership: dag_index is None; cannot determine children"
        )
    live_paths: List[str] = list(dag_index)  # type: ignore[arg-type]
    if not live_paths:
        raise ValueError(
            "reverse_membership: empty dag_index; cannot determine children "
            "(mirrors handoff-has-live-children.sh:196-199 fail-closed guard)"
        )

    # Delegate to dag.referenced_by — single-hop, edge-kind-aware, exclude-aware
    result = _referenced_by(
        target=os.path.abspath(node_path),
        live_set=live_paths,
        edge_kinds=edge_kinds,   # None → dag.py default (all three kinds)
        exclude=exclude,
    )

    # Exclude terminal-status and archive-resident children from the live set —
    # fail-closed on indeterminate frontmatter (see _is_terminal_or_archived_child).
    live_children = [
        c for c in result["referencedBy"] if not _is_terminal_or_archived_child(c)
    ]
    return frozenset(live_children)


# ---------------------------------------------------------------------------
# DR-242 predicate: was this handoff EVER claimed or shipped?
# ---------------------------------------------------------------------------
#
# Relocated (2026-08-06) from `coordinator_core.tests._baton_dag_oracle` — that
# module is a differential ORACLE for coordinator_core.dag's pointer-resolution
# job (C6), and `claimed_or_shipped_at_path` had accreted onto it as a sixth
# production import site despite having nothing to do with that oracle's
# actual job (it inspects a candidate parent's OWN frontmatter; it never reads
# a child-referencing field, so it never participates in any pointer-DAG
# comparison). Six production modules (archive_stamp, baton_assemble x2,
# baton_assemble/apply, ops/baton_drift_sweep, ops/handoff_archive_transition,
# coordinator/bin/handoff-archive-transition.py) were importing a predicate
# out of a package literally named `tests` — if that directory is ever
# excluded from an install/packaging payload (the ordinary thing to do with a
# tests directory), every one of those sites breaks at import time, on the
# handoff-supersession hot path. See DR-242's own decision doc and this
# module's `reverse_membership`/`_is_terminal_or_archived_child` above, which
# this predicate is a sibling of (both gate handoff-archival mutations) but
# does NOT reuse — see the negative-spec below.
#
# `_frontmatter`/`_field` immediately below are a DELIBERATE, byte-for-byte
# duplicate of `_baton_dag_oracle._frontmatter`/`_field`'s hand-rolled
# regex-based frontmatter reader, not a delegation to
# `coordinator_core.frontmatter.primitives.split_frontmatter` or
# `coordinator_core.dag._read_meta` (both already available in this package
# and used elsewhere in this repo). This is a relocation, not a redesign: the
# oracle module's own copy of these two helpers stays in place, unchanged,
# because IT still needs its own independent frontmatter parsing for its
# actual job — the C6 differential comparison against coordinator_core.dag's
# pointer resolution (`build_children_index`, exercised by
# test_c6_pointer_normalization.py). That independence claim was never about
# `claimed_or_shipped`/`claimed_or_shipped_at_path` (neither is compared
# against a second implementation anywhere), so this predicate's own logic is
# single-sourced here and re-exported from the oracle module for its existing
# test importers — see `_baton_dag_oracle.py`'s own note at the re-export
# site. The two `_frontmatter`/`_field` copies are intentional, independently
# maintained duplicates serving two unrelated consumers (this production gate
# vs. that differential oracle), not accidental drift — a future fix to one
# is not automatically owed to the other.


_CLAIMED_STATUS_VALUES: Tuple[str, ...] = ("claimed", "consumed", "superseded")


def _frontmatter(path: str) -> str:
    """Return a document's raw frontmatter text, or "" when it has none.

    Byte-for-byte duplicate of `coordinator_core.tests._baton_dag_oracle._frontmatter`
    — see this section's header comment for why this is a deliberate duplicate
    rather than a shared import. Tolerates a leading preamble of blank lines
    and/or HTML comments before the opening `---` (handoffs carrying one are a
    supported shape; see `frontmatter/tests/test_parity_handoff_ops.py`'s
    `_PREAMBLE_FIXTURE`).
    """
    text = open(path, encoding="utf-8", errors="replace").read()

    body = text
    while True:
        stripped = body.lstrip()
        if stripped.startswith("<!--"):
            end = stripped.find("-->")
            if end == -1:
                return ""
            body = stripped[end + 3:]
            continue
        body = stripped
        break

    if not body.startswith("---"):
        return ""
    return body.split("\n---", 1)[0][4:]


def _field(fm: str, key: str) -> str:
    """Byte-for-byte duplicate of `_baton_dag_oracle._field` — see this
    section's header comment."""
    m = re.search(r"^%s:[ \t]*(.*)$" % re.escape(key), fm, re.M)
    if not m:
        return ""
    v = m.group(1).strip().strip("\"'")
    return "" if v in ("none", "null", "~", "") else v


def claimed_or_shipped(fm: str) -> bool:
    """DR-242: was this handoff EVER claimed (picked up by a session) or shipped
    (reached a terminal `deployment_state`)? `fm` is a raw frontmatter blob as
    returned by `_frontmatter` — this function does no file I/O itself, so it is
    directly unit-testable against a literal frontmatter string.

    Claimed, any vocabulary (DR-084 rename, commit 92c90205 — old-vocabulary
    archived records are still live-read per handoff-archived.schema.json's
    documented read-tolerance carve-out, so all three must be checked, not
    just the current live one):
      - `status` in {"claimed", "consumed", "superseded"} — "superseded" is
        as terminal a status as a record can carry (it was, by definition,
        already superseded), OR
      - `claimed_at` / `claimed_by` non-empty (new vocabulary), OR
      - `consumed_at` / `consumed_by` non-empty (retired vocabulary).

    Shipped — reached a terminal `deployment_state`, or carries `shipped_in`
    (a caller-supplied ship commit implies the baton was, at minimum, resolved):
      - `deployment_state` in `lifecycle_constants.HANDOFF_TERMINAL_DEPLOYMENT`
        — {"shipped", "abandoned", "continued", "closed"}, dual-vocabulary:
        "abandoned" is the DR-084 old term for what "continued"/"closed" now
        express, and a record carrying it was terminally disposed of just as
        definitively, OR
      - `shipped_in` non-empty.

    Negative-spec (DR-242 § 2): a `predecessor`/`predecessor_id` pointer FROM a
    later-dated child is not evidence of anything about the node this predicate
    inspects — it names an entirely different record. This predicate never reads
    child-referencing fields; it looks ONLY at the candidate parent's own
    frontmatter, by construction, so a caller cannot accidentally launder a
    successor-named-child check through it. It also does NOT reuse
    `_is_terminal_or_archived_child`/`reverse_membership` above — those answer
    "does this handoff still have live children", a different question this
    predicate must not be conflated with.
    """
    status = _field(fm, "status")
    if status in _CLAIMED_STATUS_VALUES:
        return True
    if _field(fm, "claimed_at") or _field(fm, "claimed_by"):
        return True
    if _field(fm, "consumed_at") or _field(fm, "consumed_by"):
        return True
    deployment_state = _field(fm, "deployment_state")
    if deployment_state in HANDOFF_TERMINAL_DEPLOYMENT:
        return True
    if _field(fm, "shipped_in"):
        return True
    return False


def claimed_or_shipped_at_path(path: str) -> bool:
    """Path-based convenience wrapper over `claimed_or_shipped` for callers (e.g.
    a succession-stamping site) that hold a handoff path rather than an
    already-extracted frontmatter blob. Reads `path` directly — no corpus scan,
    no `root` argument — so a caller checking a single candidate parent does not
    need to build a full children index first. A path with no frontmatter block
    (unreadable, malformed, or missing entirely) is NOT claimed-or-shipped on
    the frontmatter half alone — but see the ledger-first widening below.

    Ledger-first widening (C10, docs/plans/2026-08-07-claim-state-ledger-first-
    authoritative-read.md, AC15): the CLAIM half of this predicate additionally
    consults `coordinator_core.claim_state.resolve_claim_state`, which is
    ledger-first with frontmatter-mirror fallback. This WIDENS what counts as
    claimed-or-shipped — a handoff whose ledger still holds a live claim but
    whose tracked-frontmatter mirror reverted (the branch-switch-revert desync
    that module's docstring documents) is now also reported True, closing the
    gap where `claimed_or_shipped`'s frontmatter-only reads let a fully-worked
    baton look unclaimed and get archived/superseded out from under its own
    claim. It never RELAXES: every input that returned True before this change
    (via `claimed_or_shipped`'s frontmatter checks — claim vocabulary, shipped
    `deployment_state`, or `shipped_in`) still returns True here first, before
    the ledger is even consulted. The SHIPPED half (`deployment_state`,
    `shipped_in`) has no ledger counterpart and is unaffected — it stays
    exactly the frontmatter-only check `claimed_or_shipped` already performs.
    Do NOT touch `shipped_in` here (DR-096: `archive_stamp.stamp_shipped_in`
    is its sole writer).

    Any error resolving the ledger side (missing git dir, unreadable claim
    record, etc.) degrades to the pre-widening frontmatter-only answer —
    fail-closed on the widening, never fail-open."""
    try:
        fm = _frontmatter(path)
    except OSError:
        return False
    if claimed_or_shipped(fm):
        return True
    try:
        # Review: coordinator:code-reviewer (slice A, P3) — renamed from
        # `claim_state` to avoid shadowing the sibling module
        # `coordinator_core.claim_state` imported one line above this
        # function.
        state = resolve_claim_state(path)
    except Exception:
        return False
    return state.holder is not None
