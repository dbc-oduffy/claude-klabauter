"""
coordinator_core.ops.deliverable_cascade — JSON-RPC "deliverable.cascade_terminal" operation.

Purpose: the SHARED, addressable cascade mechanism PM rulings R1/R1a call for — a plan
reaching `status: implemented` (or, later, C6b's second trigger: a handoff concluding
terminally-positive) advances every LIVE handoff joined to it by `deliverable_id`, in the
same operation, with zero additional EM-facing steps. This is the ONE implementation both
triggers call; per Addendum Q4, its home is `coordinator_core/ops/` (not a helper inside
`archive_stamp.py` or `plan_status_transition.py`) precisely so C6b (`post_commit_tail`) and
this chunk's trigger (`plan_status_transition.main()`, after a successful non-no-op flip) both
route through ONE addressable op — AC6i's idempotency/no-re-entry guarantee only holds when
there is exactly one entrypoint, not two implementations that happen to agree.

Delivers: AC6, AC6c, AC6e, AC6h, AC6g (row depth composed from the sibling
module `coordinator_core.ops.cascade_baton_rows` — see that module's own
docstring for the row-level join/write mechanism; this module's role in
AC6g is only to call it once per advanced candidate and fold its result
into this op's own response and provenance).
Spec backlink: docs/plans/2026-08-04-terminal-state-propagation-join-keys.md § C6 (R1, R1a)

Join key: `deliverable_id`, exact-string match against each live handoff's own frontmatter
field — NEVER the `plan:` pointer (1 of 80 live handoffs carries it; C12 retires it). No
fork-equivalence canonicalization (unlike `deliverable.rollup`) — C6's body does not call for
it and folding it in here would be undeclared scope creep on a join this plan's own table
names as the one everybody already carries.

Candidate surface: `state/handoffs/*.md` ONLY (flat, live corpus) — mirrors
`handoff_transition._resolve_path`'s own containment discipline (mutation verbs are live-only;
archived handoffs are out of scope for a mutation verb by long-standing convention, see
docs/problems/2026-07-08-op-family-path-containment-investigation.md § 4). A candidate is
"still advertising its work as live" when its `deployment_state` is NOT a member of
`lifecycle_constants.HANDOFF_TERMINAL_DEPLOYMENT`.

Per-target predicate (AC6h) — evaluated on EVERY candidate BEFORE any write is attempted,
closing the gap Anti-scope/R1 name: DR-217's discriminator is evaluated on the node that
itself transitions; this cascade fans OUT from a transitioning plan to a query-selected set,
so none of DR-217's three conjuncts is knowable from the plan's own transition — each is read
off the candidate itself, here:
  (a) claimed by a live session? — `claimed_by`/`consumed_by` (dual-vocab) resolved against
      `coordinator_core.liveness.resolve_live_session_ids()`, mirroring the established
      `archive_handoffs._is_terminal` consumed_by-liveness fallback idiom.
  (b) has a live successor/continuation? — composes the existing `handoff.has_live_children`
      resolver in-process (never re-derives reverse-membership) with THIS candidate as the
      query. `referenced=True` (exit_code 0) or an indeterminate/fail-closed read (exit_code 2)
      both refuse — an ambiguous "cannot tell" answer is never treated as a green light.
  (c) is its own work-state consistent with terminal? — `deployment_state` must be one of
      `{"ready_to_fire", "in_flight"}`. `awaiting_gate` is live-but-blocked-on-something-else,
      not "simply idle and safe to close" — it fails this leg and is named, not flipped.

Any leg failing REFUSES the candidate — named in the result's `refused` list, never silently
dropped, and no write of any kind is attempted for it (AC6h — "rather than flipping it").

Fixpoint iteration (chain convergence) — `_handler` re-runs the candidate loop while the
PREVIOUS pass advanced (or found already-advanced) at least one candidate, bounded at
`len(candidates)` passes (a chain of N candidates can never need more than N passes to fully
drain; the bound is a write-safety property, not a nicety). This exists because leg (b) is
evaluated once per candidate per SINGLE pass over the whole batch — on a chain A -> B sharing
one `deliverable_id` where both are live and B names A as predecessor, a single pass judges A
against B while B is still (from disk's point of view) live, refusing A in the same pass B
itself clears and advances. Iterating lets a later pass re-judge A once B is no longer a
candidate.

Re-judging A is NOT enough on its own, however: `handoff.has_live_children`'s own liveness
definition for a referencing child is keyed off that child's `status` frontmatter field
(`archival._is_terminal_or_archived_child`) — `_advance_one` below deliberately leaves `status`
untouched when it flips a candidate's `deployment_state` to `shipped` (see `build_ship_mutate`'s
own "status untouched" contract). Re-reading B fresh off disk on pass 2 therefore does NOT, by
itself, make leg (b) see B as gone — verified empirically: advancing B and then re-querying
`handoff.has_live_children` for A, with no other change, still returns `referenced=True`. Leg
(b) is NOT weakened to fix this (its `exit_code != 0` fail-closed arm, and its behaviour for any
successor NOT advanced by this same run, are both untouched) — instead, `_handler` accumulates
the resolved paths of every candidate THIS RUN has itself advanced (or found already-advanced)
and threads them through `_predicate_refusal`'s `exclude_children_check` param into
`handoff.has_live_children`'s own `exclude` param (an existing, documented scan-set-adjustment
knob, not a new backdoor) on every subsequent pass. This is scoped strictly to paths this same
invocation itself just proved terminal via its own authoritative `deployment_state` write — a
genuinely live successor sitting elsewhere on disk, or a successor some OTHER process advanced
outside this run, is never excluded and leg (b) still refuses on it exactly as before.

Write (only for a candidate that clears all three legs): reuses the SAME established
primitives every other terminal-ship path in this repo already composes — never re-derives
shipped_in resolution or the deployment_state flip:
  1. `archive_stamp.resolve_source_ship_sha(source_path, not_after=advanced_at)`, tried
     FIRST (2026-08-04) — the commit that landed the artifact whose terminal transition
     FIRED this cascade (the plan that reached `status: implemented`, or the terminal
     handoff C6b's trigger names) is honest, already-known evidence, stamped via
     `stamp_shipped_in(kind="ship-commit", sha=<that commit>)`. Only when this yields
     nothing does resolution fall back to:
  2. `archive_stamp.stamp_shipped_in(kind="scope-derived", allow_branch_tip_fallback=False,
     not_after=advanced_at)` — Position A self-derivation from the candidate's own
     `scope:` paths, guarded against an implausible (postdating-the-trigger) match by the
     same `not_after` param. A genuine no-commit-found result from EITHER leg is NOT an
     error (existing established contract) — it REFUSES this candidate (named: "no commit
     evidence resolvable for shipped_in") rather than flipping a handoff to `shipped` with
     no `shipped_in`, which the schema's post-cutoff cross-field rule
     (`_cf_shipped_in_required`) would reject anyway. See `_advance_one`'s own docstring
     for why source-first, never the reverse.
  3. `handoff_transition.build_ship_mutate` — the SAME mutate closure `_ship` itself calls,
     composed here (not re-invented) inside ONE extra `locked_rmw` pass that ALSO stamps this
     cascade's own provenance fields (`advanced_by`, `advanced_at`) atomically with the
     deployment_state flip — AC6e's provenance is written in the SAME write as the flip it
     describes, not a second racing write.

Idempotency/no-re-entry (AC6i): falls out of construction, not extra bookkeeping. A second
invocation over the same join-closure re-scans state/handoffs/ and finds every
previously-advanced candidate now `deployment_state:shipped` — i.e. no longer live — so it is
excluded from the candidate set before the predicate even runs; nothing is re-evaluated, nothing
is re-written. This op never calls itself and never re-invokes the cascade recursively on an
artifact it just advanced — re-entrancy across the TWO triggers (this chunk's plan trigger and
C6b's handoff trigger) is a property of trigger wiring, not of this op, and is C6b's to satisfy
by calling through this same entrypoint rather than duplicating it.

Failure posture (per C6's body, mirroring C2's "pairs_resolved=0 is never silent" discipline):
a cascade that resolves NO downstream artifact — zero candidates matched, OR candidates matched
but every one refused — is the C2 case, not success. `exit_code` is 1 whenever `advanced` is
empty, with `message` naming the deliverable_id and (when candidates existed) every refusal
already present in `refused`. A successful cascade with at least one advanced artifact is
`exit_code: 0` even when SOME candidates were refused — refusal-of-some is a correct, named
outcome per AC6h, not a failure of the cascade as a whole.

Self-registration: importing this module calls register_op("deliverable.cascade_terminal") as
a side-effect. Add this module to coordinator_core/ops/__init__.py's eager-import table to
trigger registration at start_server() time (done in this same chunk).

Negative-spec:
  - Does NOT widen `close_out_and_stamp`'s `stage_paths` — this op's writes are its own,
    reached from `plan_status_transition.main()` directly, never from `close_out_and_stamp`.
  - Does NOT touch `docs/plans/*.md` — the plan whose stamp fired this cascade is read
    (`deliverable_id` is supplied by the caller as a param, never re-derived from the plan file
    here) but never written by this op.
  - Does NOT scan `archive/handoffs/` — mutation targets are live-only (see Candidate surface
    above); a candidate that has already been archived is, by construction, no longer
    advertising its work as live and is outside this op's join surface.
  - Does NOT build a decision-record artifact or any confirmation step (R1a's anchor) — the
    caller's stamp IS the decision; this op is pure bookkeeping fan-out.
  - AC6g's baton-row depth (rows inside a roadmap-baton handoff's own body, joined via
    `_determine_shipped`/`_committed_chunk_ids`) IS implemented, but not IN this module — see
    `coordinator_core.ops.cascade_baton_rows.resolve_baton_rows`, called once per advanced
    candidate below (§ JSON-RPC handler). This module never re-derives that join itself.
  - Does NOT implement AC6f's retraction (C6d) or the second trigger's wiring into
    `post_commit_tail` (C6b) — both are explicitly out of this chunk's scope and are named as
    depending on THIS op's provenance shape (AC6e) rather than re-deriving it.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from coordinator_core.dag import _read_meta
from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field,
    rebuild,
    replace_fm_field,
    split_frontmatter,
)
from coordinator_core.frontmatter.schema_validate import (
    format_validation_errors,
    validate_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.liveness import resolve_live_session_ids
from coordinator_core.ops.cascade_baton_rows import resolve_baton_rows
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root

# Vendored handoff schema path — same file every other handoff-mutating op in this
# package validates against; each mutating module keeps its own local copy of this
# constant by established convention (see e.g. ops/handoff_transition.py) rather than
# importing another module's private symbol.
_SCHEMA_PATH: Path = (
    Path(__file__).parent.parent / "frontmatter" / "schemas" / "handoff.schema.json"
)

_LIVE_DEPLOYMENT_STATES = frozenset({"ready_to_fire", "in_flight"})


def _validate_fm(fm_text: str) -> list:
    """Parse fm_text as YAML and validate against the vendored handoff schema.

    Mirrors handoff_transition._validate_fm's contract exactly (own local copy per
    this package's established per-module convention).
    """
    try:
        fm_dict = yaml.safe_load(fm_text) or {}
    except Exception as exc:  # noqa: BLE001
        return [{"field": "(parse)", "error": f"YAML parse error in frontmatter: {exc}", "hint": ""}]
    return validate_frontmatter(fm_dict, _SCHEMA_PATH)


def _iso_now() -> str:
    """UTC ISO-8601 timestamp with a literal 'Z' suffix (no external tz dependency)."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _claimant(fm: dict) -> Optional[str]:
    """Dual-vocab (DR-084) read of the handoff's current claim holder, if any."""
    value = fm.get("claimed_by")
    if isinstance(value, str) and value.strip():
        return value.strip()
    value = fm.get("consumed_by")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


# ---------------------------------------------------------------------------
# Candidate collection — state/handoffs/*.md only (live, flat; see module docstring)
# ---------------------------------------------------------------------------


def _collect_live_candidates(worktree_root: Path, deliverable_id: str) -> tuple[List[dict], bool]:
    """Return ([{path, fm}, ...], scan_incomplete) for every LIVE handoff whose own
    `deliverable_id` frontmatter field exact-matches the query, and whose
    `deployment_state` is NOT in HANDOFF_TERMINAL_DEPLOYMENT (i.e. still advertises its
    work as live).

    scan_incomplete=True means state/handoffs/ could not be fully enumerated (permission
    denied) — the caller MUST treat this as "candidates may be missing," never as "this is
    the complete set."
    """
    base_dir = worktree_root / "state" / "handoffs"
    matches: List[dict] = []
    if not base_dir.is_dir():
        return matches, False

    try:
        entries = list(base_dir.iterdir())
    except OSError:
        return matches, True

    for path in entries:
        if path.suffix != ".md" or not path.is_file():
            continue
        try:
            fm = _read_meta(str(path))
        except Exception:  # noqa: BLE001 — quarantine an unreadable/malformed record
            continue
        if not fm:
            continue
        artifact_did = fm.get("deliverable_id")
        if not isinstance(artifact_did, str) or artifact_did.strip() != deliverable_id:
            continue
        deployment = fm.get("deployment_state")
        if deployment in HANDOFF_TERMINAL_DEPLOYMENT:
            continue
        matches.append({"path": path, "fm": fm})

    return matches, False


# ---------------------------------------------------------------------------
# AC6h per-target predicate — refuse-and-name, never silently flip
# ---------------------------------------------------------------------------


async def _predicate_refusal(
    candidate_path: Path,
    fm: dict,
    repo_root: Path,
    exclude_children_check: Optional[List[str]] = None,
) -> Optional[str]:
    """Evaluate the three-legged per-target predicate. Returns a human-readable
    refusal reason, or None when the candidate clears all three legs and is safe
    to advance.

    exclude_children_check (keyword-only by convention, positional-compatible for
    the two existing 3-positional-arg callers -- cascade_backstop_sweep.py and this
    module's own single-pass-era call sites): paths (any absolute-path spelling;
    resolved internally before comparison) to drop from leg (b)'s live-successor
    scan. THIS module's `_handler` is the only caller that ever supplies a non-empty
    value -- see its own docstring/module docstring "Fixpoint iteration" note for
    why. `cascade_backstop_sweep.py` never passes this (report-only, single pass,
    no candidate of its own scan has been written yet -- nothing to exclude), and
    its omission is exactly the pre-existing default (None -> no exclusion, byte-
    identical to this function's prior behaviour).
    """
    deployment = fm.get("deployment_state")

    # Leg (c) — own work-state consistent with terminal.
    if deployment not in _LIVE_DEPLOYMENT_STATES:
        return (
            f"own work-state (deployment_state={deployment!r}) is not consistent "
            "with terminal — live-but-blocked-on-something-else, not simply idle"
        )

    # Leg (a) — claimed by a live session.
    claimant = _claimant(fm)
    if claimant is not None:
        live_sids = resolve_live_session_ids()
        if claimant in live_sids:
            return f"claimed by live session {claimant!r} — refusing to advance out from under it"

    # Leg (b) — has a live successor/continuation.
    # Function-local import: composes the existing resolver rather than re-deriving
    # reverse-membership; mirrors ops/handoff_children.py's own function-local-import
    # discipline note for a sibling case (avoid pulling its transitive import set into
    # every eager-registration pass just for a defensive edge that today is acyclic).
    from coordinator_core.ops.handoff_children import CONCLUSION_EDGE_KINDS, _handoff_has_live_children

    # This leg is conclusion-shaped, not archival-shaped: the write it gates is
    # `deployment_state -> shipped` with `status` untouched and NO file move
    # (`build_ship_mutate`'s "status untouched" contract, above). Nothing is
    # archived here, so no origin pointer can be stranded — see
    # dag.CONTINUATION_EDGE_KINDS for the general archival-vs-conclusion
    # rationale (example-cockpit-repo-em, 2026-08-05, cross-repo/inbox/2026-08-05-
    # example-cockpit-repo-em-wsc-leg-b-counts-spinoffs-as-live-children.md).
    # Narrowed here explicitly rather than taking the op's archival-shaped
    # default (`_DEFAULT_EDGE_KINDS`).
    children_params: Dict[str, Any] = {
        "candidate": str(candidate_path),
        "edge_kinds": CONCLUSION_EDGE_KINDS,
    }
    if exclude_children_check:
        children_params["exclude"] = list(exclude_children_check)
    children_result = await _handoff_has_live_children(children_params, repo_root)
    children_exit = children_result.get("exit_code")
    if children_exit == 0:
        children = children_result.get("children") or []
        return f"has a live successor/continuation (referenced by: {children})"
    if children_exit != 1:
        # 2 (indeterminate) or any other unexpected value — fail-closed, never
        # treat "cannot tell" as a green light.
        return (
            "live-successor check indeterminate: "
            f"{children_result.get('error', 'unknown error')} — refusing (fail-closed)"
        )

    return None


# ---------------------------------------------------------------------------
# Write — shipped_in stamp + deployment_state flip + AC6e provenance, one locked_rmw
# ---------------------------------------------------------------------------


def _current_shipped_in(candidate_path: Path) -> Optional[str]:
    """Read the on-disk `shipped_in` value straight off disk, or None (absent/unreadable)."""
    try:
        text = candidate_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    split = split_frontmatter(text)
    if split is None:
        return None
    value = read_fm_field(split.fm_text, "shipped_in")
    return value if value not in (None, "null", "") else None


#: `_advance_one`'s own marker string for the benign-race/idempotency-floor
#: outcome — distinct from a genuine per-target refusal reason, so
#: `_handler` can route it to `already_advanced` rather than `refused`.
#: Review: coordinator:code-reviewer Finding 4.
_ALREADY_ADVANCED_MARKER = "already advanced (idempotent no-op)"


def _advance_one(
    candidate_path: Path,
    deliverable_id: str,
    advanced_at: str,
    repo_root: Path,
    source_path: str = "",
) -> tuple[bool, Optional[str]]:
    """Attempt to advance a single candidate that has already cleared the AC6h predicate.

    Returns (advanced, refusal_reason). Never raises — every failure mode is folded into
    a named refusal reason instead.

    shipped_in evidence priority (2026-08-04): the artifact that FIRED this cascade
    (`source_path` — a plan that reached `status: implemented`, or a handoff that
    concluded terminally-positive) is honest, already-known evidence of what shipped
    the deliverable — tried FIRST via `archive_stamp.resolve_source_ship_sha` (kind
    `"ship-commit"`, an explicit caller-supplied sha, never subject to the ownership
    guard — see that function's docstring for why). Position A's baton-`scope:`
    self-derivation (`kind="scope-derived"`) is the FALLBACK, tried only when the
    source yields nothing — never the other way around. This closes both halves of
    the incident this pass fixes: a baton whose own `scope:` resolves to no commit
    but whose firing source plainly has one no longer refuses (the false negative);
    and preferring the source over a coarse-directory `scope:` guess means the
    scope-derived path — and its own `not_after` plausibility guard, see
    `stamp_shipped_in`'s docstring — is reached far less often, where the risk of an
    implausible unrelated-commit match (the false positive) actually lives.
    """
    from coordinator_core.archive_stamp import resolve_source_ship_sha, stamp_shipped_in

    source_sha = resolve_source_ship_sha(
        source_path, not_after=advanced_at, worktree=main_worktree_root(repo_root)
    )
    if source_sha:
        outcome = stamp_shipped_in(
            str(candidate_path),
            kind="ship-commit",
            sha=source_sha,
        )
    else:
        outcome = stamp_shipped_in(
            str(candidate_path),
            kind="scope-derived",
            allow_branch_tip_fallback=False,
            not_after=advanced_at,
        )
    if outcome.exit_code != 0:
        return False, f"stamp_shipped_in failed: {outcome.error}"

    # Position A: a genuine no-commit-found result is an honest non-error that still
    # leaves shipped_in unset — the schema's post-cutoff cross-field rule
    # (_cf_shipped_in_required) requires shipped_in whenever deployment_state:shipped
    # is written, so an unresolved shipped_in here means the flip below cannot proceed.
    if not _current_shipped_in(candidate_path):
        return False, (
            "no commit evidence resolvable for shipped_in (source-derived: no evidence "
            "from source_path; scope-derived Position A: no branch-tip fallback) — "
            "deferred, not flipped"
        )

    _state: Dict[str, Any] = {"applied": False, "message": ""}

    def mutate(old_text: str) -> str:
        from coordinator_core.ops.handoff_transition import build_ship_mutate

        ship_mutate, ship_state = build_ship_mutate(str(candidate_path))
        after_ship = ship_mutate(old_text)
        if after_ship == old_text:
            # Already shipped (idempotency floor) — nothing to advance or provenance-stamp.
            _state["applied"] = False
            _state["message"] = ship_state["message"]
            return old_text

        split = split_frontmatter(after_ship)
        if split is None:
            raise MutateAbort(f"advance: no parseable YAML frontmatter after ship mutation for {candidate_path}")
        fm_text = split.fm_text

        # AC6e provenance — inserted (never left absent) in the SAME write as the flip
        # it describes, per this module's docstring.
        if read_fm_field(fm_text, "advanced_by") is not None:
            fm_text = replace_fm_field(fm_text, "advanced_by", deliverable_id)
        else:
            fm_text = insert_fm_field(fm_text, "advanced_by", deliverable_id, "deployment_state")
        if read_fm_field(fm_text, "advanced_at") is not None:
            fm_text = replace_fm_field(fm_text, "advanced_at", advanced_at)
        else:
            fm_text = insert_fm_field(fm_text, "advanced_at", advanced_at, "advanced_by")

        errors = _validate_fm(fm_text)
        if errors:
            details = format_validation_errors(errors)
            raise MutateAbort(f"advance: post-mutation schema validation failed: {details}")

        _state["applied"] = True
        _state["message"] = f"advanced {candidate_path} (deployment_state: shipped, advanced_by: {deliverable_id})"
        return rebuild(split, fm_text)

    try:
        locked_rmw(candidate_path, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return False, f"advance: handoff not found: {candidate_path}"
    except LockTimeout as exc:
        return False, f"advance: timed out waiting for file lock on {candidate_path}: {exc}"
    except MutateAbort as exc:
        return False, (exc.args[0] if exc.args else "advance: mutation aborted")

    return bool(_state["applied"]), None if _state["applied"] else _ALREADY_ADVANCED_MARKER


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------


@register_op("deliverable.cascade_terminal")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "deliverable.cascade_terminal" handler — the shared cascade entrypoint.

    Precondition on `repo_root`: this MUST be the git COMMON dir (the directory
    ending in `.git`), never the worktree root — `main_worktree_root(repo_root)`
    below takes `.parent`, which only derives the correct worktree root when
    `repo_root` IS the common dir. The router's own JSON-RPC dispatch supplies
    this correctly (C1b-ii convention, see `handoff_children._handoff_has_live_children`'s
    identical precondition note); a caller invoking this handler in-process directly
    (as `plan_status_transition._run_cascade` does) must resolve and pass the common
    dir itself, NOT a raw worktree/repo root — passing the wrong one does not raise,
    it silently scans `<repo_root>.parent/state/handoffs` and finds nothing (a real,
    previously-observed false negative; see `plan_status_transition`'s own
    `git_common_dir`-named binding and its rename note).

    Required params:
        deliverable_id (str) — the join key. Every live handoff whose own frontmatter
                                `deliverable_id` exact-matches this value is a candidate.
        source_kind    (str) — "plan" | "handoff" — the kind of terminal node that fired
                                this cascade. Purely a provenance/labeling input in THIS
                                chunk (only the plan trigger is wired); C6b wires the
                                "handoff" trigger through this SAME entrypoint.
        source_path    (str) — path of the terminal node that fired the cascade. Excluded
                                from its own candidate set when it happens to itself be a
                                live handoff (source_kind == "handoff") — self-advance
                                guard, needed by C6b, harmless no-op for C6's plan trigger.

    Optional params:
        at (str) — ISO-8601 timestamp for `advanced_at`. Defaults to now (UTC) when absent.

    Returns:
        {
          "exit_code": 0 | 1,
          "deliverable_id": ..., "source_kind": ..., "source_path": ...,
          "candidates_matched": <int>,
          "advanced": [{"handoff_path": ..., "message": ...,
                        "baton_rows_advanced": [{"row_id": ..., "message": ...}, ...],
                        "baton_rows_unresolved": [{"row_id": ..., "reason": ...}, ...],
                        "baton_rows_error": <str, present only on a genuine row-resolution
                                             failure -- see cascade_baton_rows.py>}, ...],
          "refused":  [{"handoff_path": ..., "reason": ...}, ...],
          "already_advanced": [{"handoff_path": ..., "reason": ...}, ...],
          "scan_incomplete": <bool>,
          "error": <str, present iff exit_code==1>,
        }

    Failure posture (see module docstring): exit_code is 1 whenever `advanced` is empty —
    zero candidates matched, or every candidate matched was refused — mirroring C2's
    "pairs_resolved=0 is never silent" discipline. Every per-target refusal is named in
    `refused` regardless of the aggregate outcome (AC6h).

    `already_advanced` is a DISTINCT list from `refused` (Review: coordinator:code-reviewer
    -- a candidate that cleared the AC6h predicate but turned out already-shipped by the
    time the write lock was acquired, e.g. a genuinely concurrent second trigger racing
    the identical join-closure, is a benign idempotency outcome, not a genuine AC6h
    predicate refusal — folding it into `refused` made the two indistinguishable to a
    caller reading AC6e's provenance/explainability contract).
    """
    import asyncio  # deferred: see _advance_one call site below for why it's needed

    deliverable_id: str = (params.get("deliverable_id") or "").strip()
    source_kind: str = (params.get("source_kind") or "").strip()
    source_path: str = (params.get("source_path") or "").strip()
    at: str = (params.get("at") or "").strip() or _iso_now()

    if not deliverable_id:
        return {
            "exit_code": 1,
            "deliverable_id": deliverable_id,
            "source_kind": source_kind,
            "source_path": source_path,
            "candidates_matched": 0,
            "advanced": [],
            "refused": [],
            "already_advanced": [],
            "scan_incomplete": False,
            "error": "deliverable.cascade_terminal: 'deliverable_id' is required",
        }
    if repo_root is None:
        return {
            "exit_code": 1,
            "deliverable_id": deliverable_id,
            "source_kind": source_kind,
            "source_path": source_path,
            "candidates_matched": 0,
            "advanced": [],
            "refused": [],
            "already_advanced": [],
            "scan_incomplete": False,
            "error": "deliverable.cascade_terminal: repo_root is required (no founding root available)",
        }

    worktree_root = main_worktree_root(repo_root)

    candidates, scan_incomplete = _collect_live_candidates(worktree_root, deliverable_id)

    # Self-advance guard (needed by C6b's handoff trigger; a no-op for C6's plan
    # trigger, whose source_path is never itself a handoff in this scan surface).
    # `source_path` arrives from `post_commit_tail._run_deliverable_cascade` as
    # `str(handoff_abs)` -- an OS-native absolute path, deliberately never
    # `.as_posix()`-normalized. Safe by construction: the comparison two lines
    # below is `Path(...).resolve()` identity via `contained_path`, not string
    # equality, so an OS-native path string is fine here on any platform,
    # including Windows -- do not "fix" this into a posix-normalized string
    # (Review: coordinator:code-reviewer Finding 5).
    if source_kind == "handoff" and source_path:
        try:
            resolved_source = contained_path(
                Path(source_path), [worktree_root / "state" / "handoffs"]
            )
        except Exception:  # noqa: BLE001
            resolved_source = None
        if resolved_source is not None:
            candidates = [c for c in candidates if c["path"].resolve() != resolved_source]

    advanced: List[dict] = []
    refused: List[dict] = []
    already_advanced: List[dict] = []

    # Fixpoint iteration (see module docstring "Fixpoint iteration (chain
    # convergence)") -- `pending` holds candidates not yet finally resolved;
    # a candidate leaves `pending` the moment it advances, is found
    # already-advanced, or a pass's write attempt fails for a reason other
    # than the predicate (all three are terminal outcomes for this run).
    # `refused` is provisional per-pass — REPLACED (never appended twice for
    # the same path) so a candidate refused in an early pass and advanced in
    # a later one appears ONLY in `advanced`, per AC6i provenance coherence.
    pending: List[dict] = list(candidates)
    refused_reason: Dict[str, str] = {}
    # Resolved (realpath) paths of every candidate THIS RUN has itself proven
    # terminal (advanced or found already-advanced) — threaded into leg (b)'s
    # live-successor scan on every subsequent pass so a chain converges
    # without weakening leg (b) for anything this run did NOT itself resolve.
    resolved_this_run: List[str] = []

    max_passes = len(candidates)
    passes_done = 0
    progressed = True
    while pending and progressed and passes_done < max_passes:
        progressed = False
        passes_done += 1
        still_pending: List[dict] = []

        for candidate in pending:
            candidate_path: Path = candidate["path"]

            # Re-read frontmatter fresh off disk each pass -- legs (a)/(c)
            # must judge CURRENT state, not a snapshot from before this run's
            # own earlier passes may have changed the corpus around it.
            fm = _read_meta(str(candidate_path)) or candidate["fm"]

            refusal = await _predicate_refusal(
                candidate_path, fm, repo_root, exclude_children_check=resolved_this_run
            )
            if refusal is not None:
                refused_reason[str(candidate_path)] = refusal
                still_pending.append(candidate)
                continue

            # asyncio.to_thread: _advance_one composes archive_stamp.stamp_shipped_in,
            # which itself does its own asyncio.run(...) internally (see that module's
            # existing convention) — calling it directly from inside this coroutine's
            # already-running event loop would raise RuntimeError. Off-loading to a
            # thread also keeps the event loop live during the blocking git-log/file-lock
            # work, per DR-212 D3.
            did_advance, write_refusal = await asyncio.to_thread(
                _advance_one, candidate_path, deliverable_id, at, repo_root, source_path
            )
            if did_advance:
                # AC6g depth: the candidate itself just advanced -- also resolve
                # any rows its OWN body carries (evidence-joined, never blanket;
                # see cascade_baton_rows.py's own docstring). Composed here, not
                # re-derived -- deciding WHETHER this candidate advances stays
                # entirely this module's per-target predicate (AC6h); row
                # resolution only ever runs for a candidate already decided.
                baton_rows = await asyncio.to_thread(
                    resolve_baton_rows, candidate_path, deliverable_id, at, repo_root
                )
                advanced.append(
                    {
                        "handoff_path": str(candidate_path),
                        "message": f"advanced (deployment_state: shipped, advanced_by: {deliverable_id})",
                        "baton_rows_advanced": baton_rows["advanced"],
                        "baton_rows_unresolved": baton_rows["unresolved"],
                        **(
                            {"baton_rows_error": baton_rows["error"]}
                            if baton_rows.get("error")
                            else {}
                        ),
                    }
                )
                refused_reason.pop(str(candidate_path), None)
                resolved_this_run.append(str(candidate_path.resolve()))
                progressed = True
            elif write_refusal == _ALREADY_ADVANCED_MARKER:
                already_advanced.append({"handoff_path": str(candidate_path), "reason": write_refusal})
                refused_reason.pop(str(candidate_path), None)
                resolved_this_run.append(str(candidate_path.resolve()))
                progressed = True
            else:
                refused_reason[str(candidate_path)] = write_refusal or "not advanced"
                still_pending.append(candidate)

        pending = still_pending

    # Every candidate still pending when the loop exits (no more progress, or
    # the pass bound was reached) is a genuine, final refusal.
    for candidate in pending:
        path_str = str(candidate["path"])
        refused.append(
            {"handoff_path": path_str, "reason": refused_reason.get(path_str, "not advanced")}
        )

    result = {
        "exit_code": 0 if advanced else 1,
        "deliverable_id": deliverable_id,
        "source_kind": source_kind,
        "source_path": source_path,
        "candidates_matched": len(candidates),
        "advanced": advanced,
        "refused": refused,
        "already_advanced": already_advanced,
        "scan_incomplete": scan_incomplete,
    }
    if not advanced:
        if not candidates:
            result["error"] = (
                f"deliverable.cascade_terminal: no live handoff in state/handoffs/ carries "
                f"deliverable_id={deliverable_id!r} — nothing to advance"
            )
        else:
            result["error"] = (
                f"deliverable.cascade_terminal: {len(candidates)} candidate(s) matched "
                f"deliverable_id={deliverable_id!r} but every one was refused — see 'refused'"
            )
    return result
