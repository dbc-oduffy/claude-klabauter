"""
coordinator_core.ops.handoff_close_origin_stub — "handoff.close_origin_stub" op.

Purpose: Python port + join-fix of DoE-claude's close-origin-stub-on-ship.sh
(Port of: close-origin-stub-on-ship.sh, DoE 394c8b64, 2026-07-19). A
``kind: spinoff``/``spinoff-roadmap``
origin stub in ``state/handoffs/`` is authored ``deployment_state:
ready_to_fire`` and its work is often executed through a SEPARATE plan/baton
— the origin stub's own frontmatter is never touched again, so after the
work ships the origin stub is left ``ready_to_fire`` forever, wrongly
advertising shipped work as a live pickup-able target. This op closes that
stub at ``/workstream-complete`` time by joining on the ``(roadmap_id,
stub_id)`` pair the governing plan / consumed handoff / roadmap-baton
ancestor already carries.

Spec backlink: cross-repo/archive/2026-07-14-claude-klabauter-em-wsc-close-origin-stub-join-and-session-shape-pickup-immutability.md
Also mirrors: coordinator-handoff-archive.sh (DoE c47b0268, 2026-07-19) ``--stamp-only`` path
DR:            DR-059 (break-class script-surface bugs route to claude-klabauter migration,
                docs/decisions/2026-07-14-break-class-script-surface-bugs-route-to-claude-klabauter-migration.md)
M1 spec backlink (in_flight liveness-gated admission + legible skip):
                cross-repo/inbox/2026-08-01-example-cockpit-repo-em-close-origin-stub-skips-in-flight.md

The join-fix (the whole point of the port — no bash analog): the bash only
ever reads ``roadmap_id``/``stub_id`` off the frontmatter of the *governing
plan* or the *consumed handoff* directly. In a roadmap-stub-routed execution
chain those ids live on the **roadmap baton** (``kind: spinoff-roadmap``)
itself, not on the plan/handoff that executes the stub's work — so
``--handoff $WSC_CONSUMED_HANDOFF`` dead-ends with "no (roadmap_id,stub_id)
in inputs" whenever the handoff doesn't directly carry the ids. This op adds
a resolution leg: it composes ``coordinator_core.dag.walk_forward``
(the SAME cycle-safe DFS primitive ``handoff_lineage_ancestry.py`` uses) over
``edge_kinds={'predecessor', 'origin_handoff'}`` — unified, unlike
``handoff_lineage_ancestry.py``'s deliberate ``origin_handoff``-only namespace
isolation — to walk ancestors from the supplied handoff and stop at (use) the
FIRST ancestor whose frontmatter has ``kind`` in ``{spinoff, spinoff-roadmap}``
AND both ``roadmap_id``/``stub_id`` present ("closest baton wins").

A further, additive leg (``join_source: "deliverable_id"``): the
``(roadmap_id, stub_id)`` pair is carried by only a small minority of plans
on disk (3 of ~22 at authoring time), while ``deliverable_id`` is carried by
every artifact in this chain (plan, execution handoff, origin stub) per
``docs/plans/2026-08-03-deliverable-id-carry-plan-handoff-agree.md``
(``status: implemented``). When the caller-supplied plan/handoff carries a
``deliverable_id``, this leg scans ``state/handoffs/`` for the origin stub
carrying the SAME ``deliverable_id`` and, if found, reads THAT stub's own
``(roadmap_id, stub_id)`` pair — which a baton-kind stub always carries — so
the resolved pair flows through the existing pair-keyed scan/close pipeline
unchanged. This leg is PURELY ADDITIVE fallback: the ``(roadmap_id,
stub_id)`` pair join stays primary and unchanged, and a pair this leg
resolves that the direct/baton-walk legs already resolved is deduped by
pair value (see ``_record``), never double-processed or given precedence.

A fourth, additive leg (``join_source: "closes_stubs"``): the
``deliverable_id`` leg is one-plan-to-N-handoffs and does not fix
plan-to-N-origins — a MERGED plan absorbing two or more pre-existing roadmap
stubs (each authored, with its own ``(roadmap_id, stub_id)``, before the
merge) still closes only ONE of those origins via ``deliverable_id``, because
that leg matches whichever single origin stub happens to carry the SAME
``deliverable_id``; the join is direction-dependent, not fixed, for the
merged case. Rather than widening ``stub_id`` to a list — wrong, because
``stub_id`` is IDENTITY, not a pointer (``roadmap_dag_node.py``: "Logical
identity: (repo, roadmap_id, stub_id)"), and ``gate_eval.py``'s
``_TypedHandoffIndex`` keys ``by_stub_id`` on that scalar with documented
last-write-wins collision behaviour, so widening it would change the emitted
bytes of ``coordinator/cockpit-contract/schema/*.json`` (a hard external
dependency per this repo's CLAUDE.md) for what is only an authorship gap —
the PLAN itself may instead carry ``closes_stubs: [{roadmap_id, stub_id},
...]``, ABSENT BY DEFAULT (absence is today's behaviour exactly). Each entry
is read directly off the plan's own frontmatter (no scan, no indirection —
the plan asserts the pair itself) and fed into the same pairs loop as its own
``(roadmap_id, stub_id, "closes_stubs")`` leg, so every named origin flows
through the existing pair-keyed scan/close pipeline unchanged. This is NOT a
fifth half-populated join key of the kind this module otherwise guards
against — the pathology this module exists to fix is a key CONSUMERS DEPEND
ON going unpopulated (``plan:``, ``roadmap_id``, ``Resolves:``);
``closes_stubs`` is an assertion authored only in the rare merged-plan case,
required by no consumer, whose absence falls back to the existing scalar
pair — additive fallback, never a second source of truth. It is plan
frontmatter, not handoff frontmatter, and is deliberately absent from
``handoff.schema.json``.

A fifth, additive leg (``join_source: "predecessor_handoff"``): all four legs
above are PAIR-keyed — they resolve a ``(roadmap_id, stub_id)`` value that
then flows through the pair-keyed ``_scan_matches``/``_try_close`` pipeline.
That pipeline is structurally inert for a NON-ROADMAP ``kind: spinoff``
origin stub, which carries neither ``roadmap_id`` nor ``stub_id`` at all
(measured: every ``kind: spinoff*`` stub at ``deployment_state:
ready_to_fire`` in ``state/handoffs/`` at authoring time carried neither
field) — such a stub can never be a match target for any of the four legs,
however many of them run, and is left ``ready_to_fire``/``pickup_ready: true``
forever: precisely the pathology this module's own opening paragraph exists
to fix. The one edge that DOES name such a stub is the executing plan's own
``predecessor_handoff:`` frontmatter field — a PATH under
``state/handoffs/``, not a pair. This leg is therefore PATH-keyed, not
pair-keyed: it resolves that path directly (``_predecessor_handoff_stub``)
and feeds it straight to ``_try_close`` (which is already path-keyed — it
takes ``stub_path`` and uses ``roadmap_id``/``stub_id`` only for reporting,
never for resolution), bypassing ``_scan_matches`` entirely rather than
manufacturing a synthetic pair to rejoin against a scan that would never
have found this stub. Negative-spec: this leg reads ``predecessor_handoff``
off the PLAN only, never the consumed handoff (no equivalent field exists
on ``handoff.schema.json``) and never ``archive/handoffs/`` (only the
``repair-archived-*`` verbs reach the archive; an already-archived stub is
out of scope for an in-place, stamp-only close). Deduped against every
pair-resolved leg above by STUB PATH (not pair value, since this leg's
target stub may carry no pair to dedupe against) — a stub reachable by both
a pair and ``predecessor_handoff`` closes exactly once. Counted separately
from ``pairs_resolved`` via its own ``stubs_resolved`` field (see
``_handler``'s Returns docstring) — folding it into ``pairs_resolved`` would
retroactively change an existing machine-readable contract for a value that
was never a pair.

Trust boundary (documented, load-bearing, preserved verbatim from bash):
this op trusts the plan's/handoff's/baton's self-asserted ``(roadmap_id,
stub_id)`` as an honest complete claim — it does NOT verify the plan actually
satisfied the stub's acceptance criteria. A premature close is bounded and
self-correcting (a later re-pickup of still-open work reopens the stub). The
``deliverable_id``, ``closes_stubs``, and ``predecessor_handoff`` legs
inherit this SAME posture verbatim: each trusts its self-asserted claim
exactly as the other legs trust a self-asserted ``(roadmap_id, stub_id)``
pair — no additional verification is layered onto any one leg alone. Any of
the five legs behaving differently from the others on the same trust
question would itself be the defect; a caller must not be able to tell
which leg resolved a stub from close-precision alone.

Compose, don't reimplement (mirrors ``handoff_ship_archive.py``'s composition
shape exactly — same three primitives, same in-process call convention):
  1. ``handoff.has_live_children`` guard (``handoff_children._handoff_has_live_children``,
     called in-process, not a second JSON-RPC round trip) — the live-children
     guard runs UNCONDITIONALLY before any stamp is attempted. Tri-state:
     exit_code 1 (safe) → proceed; exit_code 0 (has live children) or 2
     (indeterminate/fail-closed) → do NOT stamp (mirrors
     coordinator-handoff-archive.sh's guard contract exactly — retention is
     never treated as an error).
  2. ``handoff.stamp`` (``handoff_stamp._handler``) — stamps ``shipped_in:
     <sha>`` when an optional ``sha`` param is supplied and the field is
     absent (idempotent). Skipped entirely when no ``sha`` is supplied
     (graceful partial, same choice ``handoff_ship_archive.py`` makes — see
     its negative-spec "Does NOT fall back to a branch-tip SHA").
  3. ``handoff.transition`` ``ship`` verb (``handoff_transition._ship``) —
     deployment_state → shipped, in place, NO git mv (mirrors the bash's
     ``coordinator-handoff-archive.sh <stub> --stamp-only`` contract exactly:
     the stub stays in ``state/handoffs/`` for the existing archival paths
     — ``fleet.archive_shipped_handoffs`` / ``session.boot_sweep`` — to pick
     up later once a ``shipped_in`` lands).

Self-registration: importing this module calls
``register_op("handoff.close_origin_stub", _handler)`` as a side-effect.
Add this module to ``coordinator_core/ops/__init__.py`` to trigger
registration at start_server() time.

Scope model: ``common_dir`` (same-repo only, like ``handoff.stamp``,
``handoff.transition``, ``handoff.lineage_ancestry``) — this op only ever
operates on the calling repo's own ``state/handoffs/`` tree (both the
plan/handoff input and the origin-stub scan target are same-repo).
``repo_root`` arrives as ``<worktree>/.git``; the worktree is derived via
``main_worktree_root(repo_root)`` (tests below call ``_handler`` directly
with an explicit ``repo_root``, bypassing the router entirely). Calibration
note: reason from the registry (``op_scopes.py`` / ``authz/classification.py``),
not from a source comment — deliberate non-registration is also a live
pattern in this codebase (see ``_repair_archived_shipped_in_handler`` in
``coordinator_core/ops/handoff_stamp.py``), so a comment claiming an entry
is missing is weak evidence in either direction; check the registries.

Negative-spec (hard-won):
  - Does NOT implement roll-up/derivation logic — that is the separate
    deferred lvv-09 cadence-sweep backstop. This is the exact-pair-join
    proactive path only (bash's own negative-spec, preserved).
  - Does NOT git mv / archive the closed stub — stamp-only, mirrors
    ``coordinator-handoff-archive.sh --stamp-only`` exactly. Archival is a
    separate, later concern (fleet.archive_shipped_handoffs / boot_sweep).
  - Does NOT fall back to a branch-tip SHA when no ``sha`` param is supplied
    and no session-derived sha resolves — mirrors ``handoff_ship_archive.py``'s
    explicit choice (its negative-spec: "Does NOT fall back to a branch-tip
    SHA"). Absence of evidence is not a fallback: a session-derived sha is
    used ONLY when the session's own commit set (via
    ``ops.session_commits :: resolve_session_commits``, C4/C5,
    docs/plans/2026-08-18-a-session-always-has-a-baton.md) contains a commit
    that touched the stub's own path — never a branch-tip guess, and never a
    commit that merely postdates the session. When ``session_id`` is
    supplied but resolves no such commit for a given stub, ``shipped_in`` is
    left unstamped for that stub exactly as it was before this leg existed
    (graceful partial, same choice ``handoff_ship_archive.py`` makes). An
    explicit ``sha`` param, when supplied, always wins over the
    session-derived resolution — it is a stronger, caller-asserted claim.
    ``session_id`` therefore is USED now (see ``_session_derived_sha``),
    closing the gap the port proposal's param contract left open (mirrors
    ``handoff.ship_and_archive``'s caller contract, not ``handoff.stamp``'s —
    ``handoff.stamp`` has no ``session_id`` param at all).
  - Does NOT walk the baton-walk leg off ``plan_path`` — plans
    (``docs/plans/*.md``) are not part of the handoff DAG (``walk_forward``
    only knows ``state/handoffs/`` + ``archive/handoffs/`` nodes). A
    plan-only call resolves exactly as the bash did (direct frontmatter read
    off the plan) — no regression.
  - Does NOT verify the plan/handoff/baton's claim against the stub's actual
    acceptance criteria (documented trust boundary above, preserved from bash).
  - Does NOT re-derive `main_worktree_root` inline — uses the shared helper
    (`coordinator_core.ops.fleet._common.main_worktree_root`), consistent
    with every other ``common_dir``-scoped op in this package.
  - (M1) Does NOT admit `in_flight` unconditionally — `/pickup` stamps
    `in_flight` the instant a roadmap baton is picked up, so the bare state
    alone cannot discriminate "someone is on this" from "someone was on
    this, and it shipped without the stub ever transitioning". Admission is
    gated on claim liveness (`_in_flight_eligible`); `ready_to_fire`/
    `awaiting_gate` stay unconditionally eligible and `shipped`/`abandoned`
    stay unconditionally excluded, unchanged.
  - (M1) Does NOT reimplement claim-liveness — composes the SAME
    ``handoff_claim_dir`` (``coordinator_core.ops.fleet._common``) +
    ``cs_claim_holder_live`` (``coordinator_core.liveness``) pairing
    ``handoff_reconcile.py``'s ``_ancestor_liveness_blocked`` uses, including
    its fail-closed-on-exception discipline (a liveness read that raises is
    treated as LIVE, never as eligible-to-close). No raw pid check, no
    inline claim-dir path re-derivation.
  - (M1) Does NOT collapse a deployment-state-filtered match into the same
    `"no-match"` skip reason as a genuine zero-candidate join — see
    `_scan_matches`'s own docstring for why the bash's mirrored behaviour
    changes here (a deliberate divergence, not a bug).
  - (M1, Finding 3) Does NOT special-case a structural (not transient)
    `cs_claim_holder_live` raise — e.g. `MissingPsutilError` on a Windows
    install without psutil (`coordinator_core/liveness.py`'s own docstring
    names this). The fail-closed branch treats it as LIVE, so on such an
    install every `in_flight` candidate with a claim dir permanently stays
    ineligible: the exact under-count this op exists to fix silently
    persists there. This repo treats Windows as first-class (`CLAUDE.md`),
    so this is a known, named gap, not a silent one — `excluded[].
    exclusion_reason: "liveness-read-failed"` (see `_scan_matches`) is the
    caller-visible signal that lets a ceremony reader on an affected
    platform tell "holder is live" apart from "liveness could not be
    determined", but does not itself close the gap; closing it would mean
    either shipping a psutil-free liveness primitive or accepting a
    narrower not-live default there, both out of this op's scope.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import List, Optional, Set, Tuple

from coordinator_core.dag import _read_meta, walk_forward
from coordinator_core.frontmatter.baton_class import kind_values_for_canonical
from coordinator_core.ipc import register_op
from coordinator_core.liveness import cs_claim_holder_live
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import handoff_claim_dir, main_worktree_root
from coordinator_core.ops.handoff_children import (
    CONCLUSION_EDGE_KINDS,
    _handoff_has_live_children as _live_children_guard,
)
from coordinator_core.ops.handoff_stamp import _handler as _stamp_handler
from coordinator_core.ops.handoff_transition import _ship
from coordinator_core.ops.session_commits import (
    resolve_session_commits as _resolve_session_commits_primitive,
)
from coordinator_core.wire_paths import rel_id

_LOG = logging.getLogger(__name__)


# Membership is EXPLICIT, not derived from `baton_class()`, and that is a
# Legacy values are retained PERMANENTLY, not time-boxed: sibling repos still
# The retired/successor pair is sourced from the canonical `_PRE_RENAME_ALIASES`
_BATON_KINDS = frozenset(
    {"spinoff"} | set(kind_values_for_canonical("roadmap-baton"))
)

def _is_baton_kind(kind: str | None) -> bool:
    """Origin-stub kinds this op is allowed to close (mirrors the bash's
    `spinoff|spinoff-roadmap` case match).

    C3 (baton-kind-vocabulary migration): retires the former
    `_BATON_KINDS = {"spinoff", "spinoff-roadmap"}` set in favor of the
    canonical `baton_class()` derivation (C2/D2) plus one explicit
    compatibility literal.

    FINDING — the original two-member set does not correspond to one
    `baton_class`: `spinoff` derives `deflection`, but `spinoff-roadmap`
    (D1's still-live pre-rename source name for `roadmap-baton`) derives
    `intention`. Preserved verbatim, not silently narrowed — report only,
    per this chunk's brief.
    """
    return kind in _BATON_KINDS

#: Deployment_state values UNCONDITIONALLY eligible for closure — no
#: from both this set and `_LIVENESS_GATED_DEPLOYMENT_STATE` below).
_UNCONDITIONAL_NON_TERMINAL_STATES = {"ready_to_fire", "awaiting_gate"}

#: (M1) The one deployment_state value admitted CONDITIONALLY, iff its claim
_LIVENESS_GATED_DEPLOYMENT_STATE = "in_flight"

_BATON_WALK_EDGE_KINDS: Set[str] = {"predecessor", "origin_handoff"}


def _err(msg: str) -> dict:
    _LOG.warning("handoff.close_origin_stub: %s", msg)
    return {
        "exit_code": 1,
        "closed": [],
        "skipped": [],
        "pairs_resolved": 0,
        "error": msg,
    }


def _read_pair(meta: dict) -> Optional[Tuple[str, str]]:
    rid = meta.get("roadmap_id")
    sid = meta.get("stub_id")
    rid_s = rid.strip() if isinstance(rid, str) else ""
    sid_s = sid.strip() if isinstance(sid, str) else ""
    if rid_s and sid_s:
        return (rid_s, sid_s)
    return None


def _resolve_input_path(
    raw_path: str, worktree: Path, allowed_roots: List[Path]
) -> Optional[Path]:
    if not raw_path:
        return None
    p = Path(raw_path)
    if not p.is_absolute():
        p = worktree / p
    return contained_path(p, allowed_roots)


def _direct_pair(
    raw_path: str, worktree: Path, allowed_roots: List[Path]
) -> Optional[Tuple[str, str]]:
    resolved = _resolve_input_path(raw_path, worktree, allowed_roots)
    if resolved is None or not resolved.is_file():
        return None
    return _read_pair(_read_meta(str(resolved)))


def _baton_walk_pair(
    handoff_resolved: Path, handoffs_dir: Path
) -> Optional[Tuple[str, str]]:
    walk = walk_forward(
        str(handoff_resolved),
        edge_kinds=_BATON_WALK_EDGE_KINDS,
        handoff_dir=str(handoffs_dir),
    )
    start_abs = os.path.abspath(str(handoff_resolved))
    for abs_path in walk["orderedPaths"]:
        if abs_path == start_abs:
            continue
        meta = walk["nodes"].get(abs_path, {})
        if not _is_baton_kind(meta.get("kind")):
            continue
        pair = _read_pair(meta)
        if pair is not None:
            return pair
    return None


CLOSE_BASIS_DELIVERY_PROOF = "delivery-proof"
CLOSE_BASIS_GUARD = "guard"


def _is_complete_delivery_proof(proof: Optional[dict]) -> bool:
    """True iff `proof` is a COMPLETE delivery proof, per the exact
    conditions this op's caller (`close_out_and_stamp.close_out_and_stamp`)
    established for its own `status_target == "implemented"` stamping
    decision:

      - ``deliverable_id`` is a non-empty string (the plan carries one).
      - ``missing_chunk_ids == []`` — every commit-required chunk id has
        covering evidence under the same oracle that gated this run's
        `implemented` stamp (empty, not merely absent/falsy — an absent key
        on `proof` is NOT the same claim as an explicitly-empty list, and is
        treated as incomplete). This is NOT a claim that every chunk id was
        individually committed AND trailered: `missing` is computed against
        a `committed` set unioned from Session-Id-scoped fallback,
        `disposition_ref` evidence, and sibling-repo scans, so one
        trailered commit can satisfy `matched_commit_count > 0` while other
        chunk ids are covered by non-trailer evidence.
      - ``status == "implemented"`` — the plan was stamped `status:
        implemented` this run (`close_out_and_stamp`'s own
        `status_target == "implemented"` gate).
      - ``commit_required_chunk_count`` is an int > 0 — the plan's spine
        actually had at least one commit-required row for the join to have
        run against (Finding 0, staff-eng review 2026-08-13: without this,
        a plan with zero commit-required spine rows manufactures a
        "complete" proof via the degenerate `_determine_shipped` branch
        above and closes a stub on zero delivery evidence). A missing,
        `None`, or non-int count is treated as NOT complete — fail safe to
        the guard, same as any other absent/wrong-typed field.

    An indeterminate/partial proof (any field absent, wrong-typed, or not
    exactly matching the above) is NOT a proof — falls back to the
    live-children guard with today's exact semantics, same as an absent
    `proof` altogether.
    """
    if not isinstance(proof, dict):
        return False
    deliverable_id = proof.get("deliverable_id")
    if not isinstance(deliverable_id, str) or not deliverable_id.strip():
        return False
    missing_chunk_ids = proof.get("missing_chunk_ids")
    if missing_chunk_ids != []:
        return False
    if proof.get("status") != "implemented":
        return False
    commit_required_chunk_count = proof.get("commit_required_chunk_count")
    if (
        not isinstance(commit_required_chunk_count, int)
        or isinstance(commit_required_chunk_count, bool)
        or commit_required_chunk_count <= 0
    ):
        return False
    return True


def _read_deliverable_id(meta: dict) -> Optional[str]:
    val = meta.get("deliverable_id")
    val_s = val.strip() if isinstance(val, str) else ""
    return val_s or None


def _deliverable_id_pair(
    deliverable_id: str, handoffs_dir: Path, worktree: Optional[Path] = None
) -> Optional[Tuple[str, str]]:
    if not handoffs_dir.is_dir():
        return None
    target = deliverable_id
    for p in sorted(handoffs_dir.glob("*.md")):
        if not p.is_file():
            continue
        meta = _read_meta(str(p))
        if not _is_baton_kind(meta.get("kind")):
            continue
        if _read_deliverable_id(meta) != target:
            continue
        pair = _read_pair(meta)
        if pair is not None:
            return pair
    return None


def _read_closes_stubs(meta: dict) -> List[Tuple[str, str]]:
    raw = meta.get("closes_stubs")
    if not isinstance(raw, list):
        return []
    pairs: List[Tuple[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        pair = _read_pair(entry)
        if pair is not None:
            pairs.append(pair)
    return pairs


def _read_predecessor_handoff(meta: dict) -> Optional[str]:
    val = meta.get("predecessor_handoff")
    val_s = val.strip() if isinstance(val, str) else ""
    return val_s or None


async def _predecessor_handoff_stub(
    plan_meta: dict, worktree: Path, handoffs_dir: Path, common_dir: Path
) -> Tuple[Optional[Path], Optional[Tuple[Path, Optional[str], str]]]:
    """predecessor_handoff path-keyed join leg (C2; see module docstring).

    Unlike the four pair-resolution legs above, ``predecessor_handoff``
    names the origin stub's own PATH directly — the one edge that survives
    for a non-roadmap ``kind: spinoff`` stub, which carries no
    ``roadmap_id``/``stub_id`` at all and so can never be a match target for
    the pair-keyed ``_scan_matches``/``_try_close`` pipeline (measured: all
    ``kind: spinoff*`` stubs at ``deployment_state: ready_to_fire`` in
    ``state/handoffs/`` at authoring time carried neither field). Since
    ``_try_close`` is already path-keyed — it takes ``stub_path`` directly,
    and uses ``roadmap_id``/``stub_id`` only for reporting, never for
    resolution — this leg feeds it a resolved stub path straight, bypassing
    ``_scan_matches`` entirely rather than manufacturing a pair to rejoin
    against a scan that would never have found this stub in the first place.

    Reads the plan's own ``predecessor_handoff`` ONLY — see this op's
    negative-spec: only the PLAN is a legitimate source (the executing plan
    is what names the stub it forked out of; a consumed handoff has no
    equivalent field on ``handoff.schema.json``).

    Resolves under ``state/handoffs/`` ONLY (``handoffs_dir``, not
    ``archive/handoffs/``) via the shared ``_resolve_input_path`` helper
    (path-containment, never hand-joined) — an already-archived stub is out
    of scope for this leg; only the ``repair-archived-*`` verbs reach
    ``archive/handoffs/``, and this op's own stamp-only close (in place, no
    ``git mv``) has no business touching an archived record.

    Returns:
      - ``(stub_path, None)`` — resolved to a readable, baton-kind
        (``_is_baton_kind``) stub that is state-eligible per
        ``_stub_state_eligibility`` (the SAME eligibility ladder
        ``_scan_matches`` uses — see that function's own docstring for why a
        second literal of the ladder is refused).
      - ``(None, (path, deployment_state, exclusion_reason))`` — resolved to
        a readable, baton-kind stub, but state-EXCLUDED (mirrors
        ``_scan_matches``'s ``filtered`` triple shape exactly, so the caller
        can report it as a skip rather than silently dropping it).
      - ``(None, None)`` — ``predecessor_handoff`` absent, unresolvable
        (does not exist / escapes ``handoffs_dir``), or resolves to a
        non-baton-kind record (e.g. a ``session-handoff`` — refused, never
        closed).
    """
    raw = _read_predecessor_handoff(plan_meta)
    if raw is None:
        return None, None
    resolved = _resolve_input_path(raw, worktree, [handoffs_dir])
    if resolved is None or not resolved.is_file():
        return None, None
    meta = _read_meta(str(resolved))
    if not _is_baton_kind(meta.get("kind")):
        return None, None
    eligible, deployment_state, exclusion_reason = await _stub_state_eligibility(
        resolved, meta, common_dir
    )
    if eligible:
        return resolved, None
    return None, (resolved, deployment_state, exclusion_reason)


async def _in_flight_eligible(
    stub_path: Path, common_dir: Path
) -> Tuple[bool, Optional[str]]:
    """M1 liveness gate for an `in_flight` candidate stub.

    Composes the same claim-liveness primitives `handoff_reconcile.py`'s
    `_ancestor_liveness_blocked` uses (`handoff_claim_dir` +
    `cs_claim_holder_live`) rather than reimplementing claim-dir resolution
    or a raw pid check. Unlike `_ancestor_liveness_blocked`, this op has no
    `reverse_membership`/`consumed_by` fallback legs to compose — those guard
    against a DIFFERENT hazard (unrelated live children of the ANCESTOR),
    which this op's own unconditional `_live_children_guard` already covers,
    downstream, in `_try_close`. This gate answers a narrower question:
    "is THIS stub's own claim holder still on it?"

    Returns (eligible, exclusion_reason) — exclusion_reason is None iff
    eligible is True; the caller (`_scan_matches`) folds a non-None reason
    into the `excluded[]` payload's `exclusion_reason` field so a ceremony
    reader can tell "holder live" apart from "liveness read failed" (see
    Finding 3, M1 review) instead of both collapsing into a bare
    `deployment_state: in_flight`.

    - No claim dir -> holder not live -> ELIGIBLE (True, None). The
      orphaned-after-ship case the M1 spec describes: the claim is released
      when the session ends.
    - Claim dir present, liveness read succeeds -> eligible iff the holder
      is NOT live; exclusion_reason "claim-live" when it is.
    - Claim dir present, liveness read RAISES (e.g. `MissingPsutilError` on
      a Windows install without psutil — see `liveness.cs_claim_holder_live`
      docstring) -> treated as LIVE -> NOT eligible (fail-closed: never stamp
      a stub whose holder-liveness could not be determined), exclusion_reason
      "liveness-read-failed". This is a narrower fail-closed than
      `_ancestor_liveness_blocked`'s own exception handling (which degrades
      to its consumed_by fallback leg on a raise) — this op has no such
      fallback leg to degrade to, so an indeterminate read here goes
      directly to "not eligible", per the M1 spec.
    """
    claim_dir = handoff_claim_dir(common_dir, stub_path)
    if not claim_dir.is_dir():
        return True, None
    try:
        holder_live = await asyncio.to_thread(cs_claim_holder_live, str(claim_dir))
    except Exception as exc:
        _LOG.warning(
            "handoff.close_origin_stub: cs_claim_holder_live raised for %s — "
            "treating as LIVE (fail-closed, NOT eligible to close): %s",
            claim_dir, exc,
        )
        return False, "liveness-read-failed"
    if holder_live:
        return False, "claim-live"
    return True, None


async def _stub_state_eligibility(
    stub_path: Path, meta: dict, common_dir: Path
) -> Tuple[bool, Optional[str], str]:
    deployment_state = meta.get("deployment_state")
    if deployment_state in _UNCONDITIONAL_NON_TERMINAL_STATES:
        return True, deployment_state, ""
    if deployment_state == _LIVENESS_GATED_DEPLOYMENT_STATE:
        eligible, exclusion_reason = await _in_flight_eligible(stub_path, common_dir)
        if eligible:
            return True, deployment_state, ""
        return False, deployment_state, exclusion_reason or "claim-live"
    return False, deployment_state, "state-not-eligible"


async def _scan_matches(
    handoffs_dir: Path, roadmap_id: str, stub_id: str, common_dir: Path
) -> Tuple[List[Path], List[Tuple[Path, Optional[str], str]]]:
    """Scan state/handoffs/*.md for eligible origin stubs matching the pair.

    Filters: kind in {spinoff, spinoff-roadmap} AND roadmap_id/stub_id match
    AND deployment_state eligible — `ready_to_fire`/`awaiting_gate`
    unconditionally, `in_flight` conditionally on `_in_flight_eligible`
    (M1, Leg A), `shipped`/`abandoned` never.

    (M1) DIVERGES from the bash it otherwise mirrors ("steps 2-5 exactly" no
    longer holds verbatim): the bash treats every deployment-state-excluded
    match identically to a genuine zero-candidate join. This op instead
    returns TWO lists — `matches` (eligible candidates) and `filtered`
    (matched kind+pair but excluded by the deployment_state/liveness gate,
    as `(stub_path, deployment_state, exclusion_reason)` triples) — so the
    caller (`_handler`) can surface a `"no-match-filtered-deployment-state"`
    skip reason distinct from a true `"no-match"` (Leg B; see module
    docstring). `exclusion_reason` is one of `"state-not-eligible"` (a
    terminal/unrecognized deployment_state, never liveness-gated),
    `"claim-live"`, or `"liveness-read-failed"` (Finding 3, M1 review — lets
    a ceremony reader distinguish "holder is live" from "liveness could not
    be determined", instead of both collapsing into a bare
    `deployment_state: in_flight`).
    """
    matches: List[Path] = []
    filtered: List[Tuple[Path, Optional[str], str]] = []
    if not handoffs_dir.is_dir():
        return matches, filtered
    for p in sorted(handoffs_dir.glob("*.md")):
        if not p.is_file():
            continue
        meta = _read_meta(str(p))
        if not _is_baton_kind(meta.get("kind")):
            continue
        pair = _read_pair(meta)
        if pair != (roadmap_id, stub_id):
            continue
        eligible, deployment_state, exclusion_reason = await _stub_state_eligibility(
            p, meta, common_dir
        )
        if eligible:
            matches.append(p)
        else:
            filtered.append((p, deployment_state, exclusion_reason))
    return matches, filtered


def _session_derived_sha(
    session_commits: Optional[List[dict]], stub_rel_path: str
) -> str:
    if not session_commits:
        return ""
    for commit in reversed(session_commits):
        if stub_rel_path in commit.get("touched_paths", ()):
            return commit["sha"]
    return ""


async def _try_close(
    stub_path: Path,
    worktree: Path,
    repo_root: Path,
    roadmap_id: Optional[str],
    stub_id: Optional[str],
    join_source: str,
    sha: str,
    guard_exclude: List[str],
    delivery_proof: Optional[dict] = None,
) -> Tuple[Optional[dict], Optional[dict]]:
    """Attempt to stamp-close one matched origin stub.

    `roadmap_id`/`stub_id` are Optional — every pair-resolution leg always
    supplies both (a baton-kind stub matched by pair always carries them),
    but the path-keyed `predecessor_handoff` leg (C2) may resolve a
    non-roadmap `kind: spinoff` stub carrying NEITHER field; this function
    never joins on them (`stub_path` alone is the resolution key), it only
    threads them through into `closed`/`skipped` reporting, so `None` here
    means "this stub carries no roadmap-origin identity" — an honest report,
    not a degraded one.

    Returns (closed_entry, skipped_entry) — exactly one is non-None.

    Composition order mirrors coordinator-handoff-archive.sh --stamp-only:
    live-children guard (unconditional, UNLESS a complete, stub-specific
    delivery proof is supplied — see below) -> optional shipped_in stamp ->
    ship verb (deployment_state: shipped, no git mv).

    `delivery_proof` (optional; PM ruling — see module docstring "Delivery-
    proof close" section): when `_is_complete_delivery_proof(delivery_proof)`
    is True AND the proof's own `deliverable_id` equals THIS STUB's own
    `deliverable_id` (`_read_deliverable_id` on the stub's frontmatter — a
    proof for deliverable A must never close a stub carrying deliverable B),
    the live-children guard is skipped entirely and the close proceeds on the
    proof alone. This is safe precisely because the close performed here is
    IN PLACE (`deployment_state -> shipped`, no `git mv`) — it cannot strand
    a dependent the way an archival move could; archival remains separately
    gated on liveness in `archive_handoffs.py`, untouched by this leg.
    `closed_entry["close_basis"]` records which path fired
    (`CLOSE_BASIS_DELIVERY_PROOF` vs `CLOSE_BASIS_GUARD`) so the reply stays
    auditable, mirroring 595a8b3cf977's guard-decline reason split.
    An absent/incomplete/mismatched proof falls back to the guard with
    today's exact semantics — unchanged.

    Latent-bug fix (discovered authoring this op, not present in the port
    proposal): the guard is now called with the conclusion-shaped
    `CONCLUSION_EDGE_KINDS` ({predecessor, additional_predecessors}), not the
    archival-shaped default — but that set still INCLUDES 'predecessor', and
    the baton-walk join leg (§2) reaches the baton precisely BECAUSE the
    supplied handoff_path's own `predecessor:` (or `origin_handoff:`, which
    the guard does NOT scan) points at it. Without excluding the
    caller-supplied handoff_path from the guard's live-set scan, the
    join-fix's own headline case (baton reached via `predecessor`) would
    ALWAYS guard-decline — the very handoff that proves the join is itself
    counted as a "live child" of the stub it just resolved, permanently
    defeating the fix this op exists to ship. `guard_exclude` carries the
    caller-supplied handoff_path (worktree-relative), mirroring
    coordinator-handoff-archive.sh's own `--exclude "$HANDOFF_FILE"`
    convention used elsewhere in the archival flow (chain mode) — the
    join-source handoff is understood to be this session's own now-obsolete
    continuity pointer, not independent "the fleet still needs this baton"
    signal. A genuinely unrelated live handoff still referencing the stub is
    NOT excluded and still correctly guard-declines (see module tests, C8).
    `guard_exclude` MUST carry absolute (already-resolved) path strings — see
    the caller in _handler for why a worktree-relative entry would silently
    fail to match.

    `forked_from` is deliberately EXCLUDED from the edge set this guard uses
    here (unlike the archival-shaped default; see `dag.CONTINUATION_EDGE_
    KINDS` for the general rationale) — the call-site-specific reason: a fork
    child of THIS stub cannot be its continuation, since `forked_from` is
    schema-legal only on `kind: spinoff`, which is required to carry
    `predecessor: none` (`frontmatter/schema_validate.py`'s
    `_cf_forked_from_spinoff_only` / `_cf_spinoff_predecessor_none`) — so the
    stub's real continuation is necessarily `predecessor`/`origin_handoff`-
    edged, which is the spine `_BATON_WALK_EDGE_KINDS` (this op's own
    baton-walk join leg) follows. Archival protection for a live
    `forked_from` child is unaffected — `archive_handoffs.py::_is_terminal`
    Check 3 keeps the full default.
    """
    rel = rel_id(stub_path, worktree)

    # a COMPLETE proof, stub-specific via `deliverable_id` equality, closes
    close_basis = CLOSE_BASIS_GUARD
    proof_applies = False
    if _is_complete_delivery_proof(delivery_proof):
        stub_deliverable_id = _read_deliverable_id(_read_meta(str(stub_path)))
        proof_deliverable_id = delivery_proof.get("deliverable_id")
        proof_deliverable_id_s = (
            proof_deliverable_id.strip()
            if isinstance(proof_deliverable_id, str)
            else proof_deliverable_id
        )
        if (
            stub_deliverable_id is not None
            and stub_deliverable_id == proof_deliverable_id_s
        ):
            proof_applies = True
            close_basis = CLOSE_BASIS_DELIVERY_PROOF

    if not proof_applies:
        # PLACE, no `git mv`, so `CONCLUSION_EDGE_KINDS` (not the archival-shaped
        guard_params: dict = {
            "candidate": str(stub_path),
            "edge_kinds": CONCLUSION_EDGE_KINDS,
        }
        if guard_exclude:
            guard_params["exclude"] = guard_exclude
        guard_res = await _live_children_guard(guard_params, repo_root)
        if guard_res.get("exit_code") != 1:
            # never an error. The two states demand OPPOSITE operator responses
            return None, {
                "roadmap_id": roadmap_id,
                "stub_id": stub_id,
                "reason": (
                    "guard-declined-live-children"
                    if guard_res.get("exit_code") == 0
                    else "guard-declined-indeterminate"
                ),
                "blocking_children": [
                    rel_id(Path(c), worktree) for c in guard_res.get("children", [])
                ],
                "guard_error": guard_res.get("error"),
            }

    if sha:
        stamp_res = await _stamp_handler(
            {"handoff_path": rel, "sha": sha, "kind": "ship-commit"}, repo_root
        )
        if stamp_res.get("exit_code") != 0:
            return None, {
                "roadmap_id": roadmap_id,
                "stub_id": stub_id,
                "reason": "mutation-failed",
            }

    ship_res = await asyncio.to_thread(_ship, rel, worktree, repo_root)
    if ship_res.get("exit_code") != 0:
        return None, {
            "roadmap_id": roadmap_id,
            "stub_id": stub_id,
            "reason": "mutation-failed",
        }

    return (
        {
            "stub_path": rel,
            "roadmap_id": roadmap_id,
            "stub_id": stub_id,
            "join_source": join_source,
            "close_basis": close_basis,
        },
        None,
    )


# JSON-RPC handler


@register_op("handoff.close_origin_stub")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "handoff.close_origin_stub" handler.

    Params:
        plan_path    (str, optional) — worktree-relative or absolute path to
                     the governing plan (must resolve under docs/plans/).
        handoff_path (str, optional) — worktree-relative or absolute path to
                     the consumed/execution handoff (must resolve under
                     state/handoffs/ or archive/handoffs/). Required for the
                     baton-walk join leg (§2) — plans are not DAG nodes.
        session_id   (str, optional) — when `sha` is absent, used to resolve
                     the session's own commit set (`ops.session_commits`)
                     and derive a per-stub shipping sha from the most recent
                     session commit that touched that stub's own path (see
                     `_session_derived_sha`). No such commit -> that stub's
                     shipped_in stamp is skipped exactly as it was before
                     this leg existed — never a branch-tip fallback (module
                     negative-spec).
        sha          (str, optional) — shipping-commit SHA to stamp as
                     shipped_in on each closed stub, when the field is
                     absent (idempotent). Takes precedence over any
                     session_id-derived sha. Absent (and no session-derived
                     sha resolves) -> shipped_in stamp is skipped (graceful
                     partial, mirrors handoff.ship_and_archive's
                     negative-spec).
        delivery_proof (dict, optional) — a completed delivery proof for the
                     CLOSING plan, letting a positive, complete delivery
                     proof close the origin stub directly instead of relying
                     on the live-children guard (PM ruling: bookkeeping must
                     not stay gated behind a skill invocation that may never
                     arrive). A proof is COMPLETE (see
                     `_is_complete_delivery_proof`) iff ALL of:
                       - `deliverable_id` is a non-empty string.
                       - `missing_chunk_ids == []` — every commit-required
                         chunk id has covering evidence under the same
                         oracle that gated this run's `implemented` stamp
                         (not a claim that every chunk id was individually
                         committed AND trailered — see `_is_complete_
                         delivery_proof`'s own docstring).
                       - `status == "implemented"` — the plan was stamped
                         `status: implemented` this run.
                       - `commit_required_chunk_count` is an int > 0 — the
                         plan's spine actually had at least one
                         commit-required row for the join to have run
                         against (Finding 0, staff-eng review 2026-08-13).
                     A complete proof closes a matched stub ONLY when the
                     proof's own `deliverable_id` equals THAT STUB's own
                     `deliverable_id` (`_read_deliverable_id` on the stub's
                     frontmatter) — stub-specific, never global: a proof for
                     deliverable A must never close a stub carrying
                     deliverable B. When it applies, the live-children guard
                     (`_try_close`) is skipped entirely for that stub — safe
                     because this close is IN PLACE (no `git mv`), so it
                     cannot strand a dependent; archival liveness gating is
                     untouched. Absent or incomplete (per any condition
                     above) -> current behaviour exactly, unchanged (falls
                     back to the live-children guard). Each `closed[]` entry
                     carries `close_basis` (`"delivery-proof"`|`"guard"`)
                     recording which path fired, for both the delivery-proof
                     and guard-fallback close of that same stub.

    At least one of plan_path/handoff_path is required — usage-error contract
    mirrors the bash's exit 2 (here: exit_code=1 structured error). A
    declined stamp or an ambiguous match are non-fatal surfaced states
    (exit_code 0, `pairs_resolved > 0`).

    `pairs_resolved == 0` (every join leg — direct pair / deliverable_id /
    closes_stubs — came up empty) is NOT itself always loud (AC2/AC14
    correction, 2026-08-04 — the audit's original per-closer scoring for
    THIS closer was wrong; see `state/audits/2026-08-04-terminal-state-
    closer-exit-code-caller-audit.md`'s corrected section). `post_commit_tail`
    calls this op once per (plan, consumed-handoff) touched by a close-out
    REGARDLESS of whether that artifact ever had a roadmap origin — a
    memo-sourced plan with no `roadmap_id` and no origin stub anywhere is a
    genuine zero-candidates negative, not a caller handing inputs that could
    not be read. Two outcomes are distinguished, both under
    `pairs_resolved == 0`:
      - Zero candidates (quiet, exit_code 0): every caller-supplied artifact
        resolved to a real file, and it simply carries no roadmap-origin
        linkage of any kind (no `roadmap_id`/`stub_id`, no partial pair, no
        malformed `closes_stubs`, and no `deliverable_id`-joined origin
        stub found). `pairs_resolved` stays 0; `no_candidates` is `true` so
        the distinction stays machine-readable.
      - Unjoinable inputs (LOUD, exit_code 1): a named `plan_path`/
        `handoff_path` did not resolve to a readable file at all, OR a
        resolved artifact carries partial/contradictory linkage (e.g. only
        one of `roadmap_id`/`stub_id` present, or every `closes_stubs` entry
        malformed) — a caller cannot otherwise distinguish "nothing to
        close" from "I could not read your inputs" (AC2's original
        motivation, preserved for this case).

    Returns:
        {
          "exit_code": 0,
          "closed": [{"stub_path", "roadmap_id", "stub_id",
                      "join_source": "direct"|"baton_walk"|"deliverable_id"|
                                      "closes_stubs"|"predecessor_handoff",
                      # "roadmap_id"/"stub_id" are None on a
                      # "predecessor_handoff"-joined entry when the closed
                      # stub is a non-roadmap `kind: spinoff` carrying
                      # neither field (see `_try_close`'s own docstring) —
                      # every other join_source always carries both.
                      # "close_basis" (delivery-proof threading, see
                      # `delivery_proof` param docs above): "delivery-proof"
                      # when a complete, stub-specific proof closed this
                      # stub without consulting the live-children guard at
                      # all; "guard" when the live-children guard itself
                      # read safe-to-close (today's exact pre-existing path).
                      "close_basis": "delivery-proof"|"guard"}],
          "skipped": [{"roadmap_id", "stub_id",
                       "reason": "no-match"|"no-match-filtered-deployment-state"|
                                 "ambiguous"|"guard-declined-live-children"|
                                 "guard-declined-indeterminate"|"mutation-failed",
                       # "excluded" is present ONLY on "no-match-filtered-
                       # deployment-state" (M1, Leg B) — the stub(s) that
                       # matched kind+pair but were excluded by the
                       # deployment_state/liveness gate, so a ceremony reader
                       # can distinguish this from a genuine zero-candidate join.
                       # "exclusion_reason" (Finding 3, M1 review) is one of
                       # "state-not-eligible"|"claim-live"|"liveness-read-failed"
                       # — distinguishes "holder live" from "liveness could
                       # not be determined" instead of collapsing both into
                       # the bare deployment_state.
                       "excluded": [{"stub_path", "deployment_state", "exclusion_reason"}],
                       # "blocking_children"/"guard_error" are present ONLY on
                       # "guard-declined-live-children"/"guard-declined-
                       # indeterminate" — the live-children guard's own
                       # `children` (worktree-relative here, absolute on the
                       # guard's own reply) and `error` fields, surfaced so an
                       # operator can tell "wait for these to resolve" apart
                       # from "the scan itself failed, investigate" instead of
                       # both reading as an opaque "guard-declined".
                       "blocking_children": [str],
                       "guard_error": Optional[str]}],
          "pairs_resolved": int,
          # count of resolved (roadmap_id, stub_id) pairs ONLY — an existing
          # machine-readable contract, deliberately NOT folded together with
          # the path-keyed predecessor_handoff leg below (see "stubs_resolved").
          "stubs_resolved": int,
          # count of stubs resolved via the predecessor_handoff leg (C2) —
          # kept SEPARATE from "pairs_resolved" (see above); a stub resolved
          # by BOTH a pair and predecessor_handoff is counted in both, but
          # closed exactly once (deduped in the direct-stubs close loop).
          "message": str,
        }
      or {"exit_code": 1, "closed": [], "skipped": [], "pairs_resolved": 0, "error": str}
      on a usage error (missing plan_path/handoff_path, or no repo_root).
      or {"exit_code": 0, "closed": [], "skipped": [], "pairs_resolved": 0,
          "no_candidates": true, "message": str}
      when every supplied artifact resolved cleanly but carries no
      roadmap-origin linkage at all — the quiet zero-candidates case above.
      or {"exit_code": 1, "closed": [], "skipped": [], "pairs_resolved": 0, "message": str}
      when a supplied artifact could not be resolved/read, or carries
      partial/contradictory linkage — the loud unjoinable-inputs case above.
    """
    plan_path = (params.get("plan_path") or "").strip()
    handoff_path = (params.get("handoff_path") or "").strip()
    session_id = (params.get("session_id") or "").strip()
    sha = (params.get("sha") or "").strip()
    delivery_proof = params.get("delivery_proof")
    if not isinstance(delivery_proof, dict):
        delivery_proof = None

    if not plan_path and not handoff_path:
        return _err(
            "at least one of 'plan_path'/'handoff_path' is required"
        )

    if repo_root is None:
        return _err(
            "repo_root is required "
            "(no founding root available — handler called without socket-authoritative common_dir)"
        )

    worktree = main_worktree_root(repo_root)
    handoffs_dir = worktree / "state" / "handoffs"
    archive_dir = worktree / "archive" / "handoffs"
    plans_dir = worktree / "docs" / "plans"

    session_commits: Optional[List[dict]] = None
    if not sha and session_id:
        try:
            session_commits = _resolve_session_commits_primitive(worktree, session_id)
        except (ValueError, RuntimeError) as exc:
            _LOG.warning(
                "handoff.close_origin_stub: session.commits primitive failed "
                "for session_id=%r — no session-derived sha available: %s",
                session_id, exc,
            )
            session_commits = None

    def _sha_for(stub_path: Path) -> str:
        if sha:
            return sha
        return _session_derived_sha(session_commits, rel_id(stub_path, worktree))

    pairs: List[Tuple[str, str, str]] = []
    seen: Set[Tuple[str, str]] = set()

    def _record(pair: Optional[Tuple[str, str]], join_source: str) -> None:
        if pair is None or pair in seen:
            return
        seen.add(pair)
        pairs.append((pair[0], pair[1], join_source))

    if plan_path:
        _record(_direct_pair(plan_path, worktree, [plans_dir]), "direct")

    plan_resolved: Optional[Path] = (
        _resolve_input_path(plan_path, worktree, [plans_dir]) if plan_path else None
    )

    handoff_resolved: Optional[Path] = None
    if handoff_path:
        handoff_resolved = _resolve_input_path(
            handoff_path, worktree, [handoffs_dir, archive_dir]
        )
        if handoff_resolved is not None and handoff_resolved.is_file():
            _record(_read_pair(_read_meta(str(handoff_resolved))), "direct")

    if handoff_resolved is not None and handoff_resolved.is_file():
        _record(_baton_walk_pair(handoff_resolved, handoffs_dir), "baton_walk")

    for resolved in (plan_resolved, handoff_resolved):
        if resolved is None or not resolved.is_file():
            continue
        deliverable_id = _read_deliverable_id(_read_meta(str(resolved)))
        if deliverable_id is None:
            continue
        _record(_deliverable_id_pair(deliverable_id, handoffs_dir, worktree), "deliverable_id")

    if plan_resolved is not None and plan_resolved.is_file():
        for pair in _read_closes_stubs(_read_meta(str(plan_resolved))):
            _record(pair, "closes_stubs")

    direct_stubs: List[Tuple[Path, str]] = []
    filtered_stubs: List[Tuple[Path, Optional[str], str]] = []
    if plan_resolved is not None and plan_resolved.is_file():
        stub_path, filtered_entry = await _predecessor_handoff_stub(
            _read_meta(str(plan_resolved)), worktree, handoffs_dir, repo_root
        )
        if stub_path is not None:
            direct_stubs.append((stub_path, "predecessor_handoff"))
        elif filtered_entry is not None:
            filtered_stubs.append(filtered_entry)

    guard_exclude: List[str] = []
    if handoff_resolved is not None and handoff_resolved.is_file():
        guard_exclude = [str(handoff_resolved)]

    if not pairs and not direct_stubs and not filtered_stubs:
        # REGARDLESS of whether that artifact ever had a roadmap origin, so
        unresolved = [
            raw
            for raw, resolved in (
                (plan_path, plan_resolved),
                (handoff_path, handoff_resolved),
            )
            if raw and (resolved is None or not resolved.is_file())
        ]

        contradictory: List[str] = []
        for resolved in (plan_resolved, handoff_resolved):
            if resolved is None or not resolved.is_file():
                continue
            meta = _read_meta(str(resolved))
            rid = meta.get("roadmap_id")
            sid = meta.get("stub_id")
            rid_s = rid.strip() if isinstance(rid, str) else ""
            sid_s = sid.strip() if isinstance(sid, str) else ""
            if bool(rid_s) != bool(sid_s):
                contradictory.append(rel_id(resolved, worktree))
                continue
            raw_closes = meta.get("closes_stubs")
            if (
                isinstance(raw_closes, list)
                and raw_closes
                and not _read_closes_stubs(meta)
            ):
                contradictory.append(rel_id(resolved, worktree))

        inspected = [
            rel_id(p, worktree)
            for p in (plan_resolved, handoff_resolved)
            if p is not None
        ]
        artifact_desc = ", ".join(inspected) if inspected else "(no artifact resolved)"

        if unresolved or contradictory:
            detail_bits = []
            if unresolved:
                detail_bits.append(f"unresolvable: {', '.join(unresolved)}")
            if contradictory:
                detail_bits.append(
                    f"partial/contradictory linkage: {', '.join(contradictory)}"
                )
            return {
                "exit_code": 1,
                "closed": [],
                "skipped": [],
                "pairs_resolved": 0,
                "message": (
                    f"no (roadmap_id,stub_id) resolvable from {artifact_desc} — "
                    "looked for a direct (roadmap_id,stub_id) pair, a "
                    "deliverable_id-joined origin stub, a closes_stubs "
                    "list, and a predecessor_handoff-joined origin stub; "
                    f"{'; '.join(detail_bits)}"
                ),
            }

        return {
            "exit_code": 0,
            "closed": [],
            "skipped": [],
            "pairs_resolved": 0,
            "no_candidates": True,
            "message": (
                f"no roadmap-origin linkage present on {artifact_desc} — "
                "nothing to do (checked: direct (roadmap_id,stub_id) pair, "
                "deliverable_id-joined origin stub, closes_stubs list, and "
                "predecessor_handoff-joined origin stub; none present, and no "
                "artifact carries partial/contradictory linkage)"
            ),
        }

    closed: List[dict] = []
    skipped: List[dict] = []
    attempted_stub_paths: Set[Path] = set()

    for roadmap_id, stub_id, join_source in pairs:
        matches, filtered = await _scan_matches(
            handoffs_dir, roadmap_id, stub_id, repo_root
        )

        if not matches:
            if filtered:
                skipped.append({
                    "roadmap_id": roadmap_id,
                    "stub_id": stub_id,
                    "reason": "no-match-filtered-deployment-state",
                    "excluded": [
                        {
                            "stub_path": rel_id(p, worktree),
                            "deployment_state": ds,
                            "exclusion_reason": reason,
                        }
                        for p, ds, reason in filtered
                    ],
                })
            else:
                skipped.append(
                    {"roadmap_id": roadmap_id, "stub_id": stub_id, "reason": "no-match"}
                )
            continue

        if len(matches) > 1:
            skipped.append(
                {"roadmap_id": roadmap_id, "stub_id": stub_id, "reason": "ambiguous"}
            )
            continue

        attempted_stub_paths.add(matches[0])
        closed_entry, skipped_entry = await _try_close(
            matches[0],
            worktree,
            repo_root,
            roadmap_id,
            stub_id,
            join_source,
            _sha_for(matches[0]),
            guard_exclude,
            delivery_proof,
        )
        if closed_entry is not None:
            closed.append(closed_entry)
        else:
            assert skipped_entry is not None
            skipped.append(skipped_entry)

    for stub_path, deployment_state, exclusion_reason in filtered_stubs:
        skipped.append({
            "roadmap_id": None,
            "stub_id": None,
            "reason": "no-match-filtered-deployment-state",
            "excluded": [
                {
                    "stub_path": rel_id(stub_path, worktree),
                    "deployment_state": deployment_state,
                    "exclusion_reason": exclusion_reason,
                }
            ],
        })

    stubs_resolved = len(direct_stubs)
    for stub_path, join_source in direct_stubs:
        if stub_path in attempted_stub_paths:
            continue
        attempted_stub_paths.add(stub_path)
        stub_meta = _read_meta(str(stub_path))
        stub_rid = stub_meta.get("roadmap_id")
        stub_sid = stub_meta.get("stub_id")
        stub_rid_s = stub_rid.strip() if isinstance(stub_rid, str) else None
        stub_sid_s = stub_sid.strip() if isinstance(stub_sid, str) else None
        closed_entry, skipped_entry = await _try_close(
            stub_path,
            worktree,
            repo_root,
            stub_rid_s or None,
            stub_sid_s or None,
            join_source,
            _sha_for(stub_path),
            guard_exclude,
            delivery_proof,
        )
        if closed_entry is not None:
            closed.append(closed_entry)
        else:
            assert skipped_entry is not None
            skipped.append(skipped_entry)

    message = (
        f"closed {len(closed)} origin stub(s); skipped {len(skipped)} of "
        f"{len(pairs)} resolved (roadmap_id,stub_id) pair(s) and "
        f"{stubs_resolved} resolved predecessor_handoff stub(s)"
    )
    return {
        "exit_code": 0,
        "closed": closed,
        "skipped": skipped,
        "pairs_resolved": len(pairs),
        "stubs_resolved": stubs_resolved,
        "message": message,
    }
