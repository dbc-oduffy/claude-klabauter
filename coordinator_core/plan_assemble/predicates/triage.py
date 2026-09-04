"""
coordinator_core.plan_assemble.predicates.triage — the `gates.triage.*`
Layer 0 leaf readers, Branch A of the `plan-assemble` contract.

Purpose: every row below answers one Branch A audit line (`:30`-`:50`) by
reading off the already-constructed `PredicateContext` — the sizing
object's presence and frontmatter fields, the resolved route, and the
one-bit caller signals (`:32`'s arrival split) — plus, where a row's own
contract line names an explicit grep or path check (`:40`'s cross-repo
inbox provenance, `:50`'s handoff-body grep), exactly that read and no
other. No row here reimplements the sizing lobby's route resolution; every
route-shaped field SURFACES `context.resolved_route` rather than
re-deriving it.

Namespace: every field this module returns nests under `gates.triage.*`,
including the XL-exit shape that survives contract line `:34` under its
renamed home `gates.triage.roadmap_precondition.*` (see below).

Negative-spec:
  - Does NOT emit anything for `:34`'s XL-exit arm (`workstream_count` /
    `goal_fk_present`). DoE's reply memo withdrew it in full; per the
    plan's Layer partition ("Emitting nothing"), this row is silent, not
    `undetermined` — there is no field name for a reader to accidentally
    resurrect.
  - Does NOT use the `gates.triage.xl_exit.*` namespace for anything. That
    key names a shape the schema says the engine never populates; the one
    surviving arm of `:34` (the clean-route disqualification check) lives
    at `gates.triage.roadmap_precondition.*` instead, so a future reader
    grepping for `xl_exit` finds nothing to extend.
  - Does NOT compute `:43` (the express-lane carve-out). It is vacuous by
    construction — `sizing_assemble:670`'s `if express_lane:` returns
    before this predicate is ever reached — so no field, no `undetermined`
    sentinel, nothing.
  - Does NOT decide `:59`'s `multi-stakeholder` judgment arm or any other
    `U`-classified disposition. `:59` itself is Layer 2 composition
    (chunk C12) and does not live in this module at all.
  - Does NOT re-derive `gates.triage.route` from the sizing object's other
    fields — it surfaces `context.resolved_route` verbatim, exactly as the
    contract's `:33` source column requires.
  - Does NOT backfill a missing `sizing_object.intent`/`.estimate`/
    `.appetite` field, and does NOT normalise a present one — each is
    surfaced verbatim off the sizing object's frontmatter or is `None`.
  - Does NOT build a duplicate claim-grant resolver for `:42`. It reads the
    same claim-entry signal off `context.caller_flags["claim_grant"]` the
    contract says the existing envelope already carries; an absent key
    emits `undetermined`, never a guess.
  - Does NOT reimplement any part of `sizing_disposition.compute_sizing_
    disposition` for `admission`'s SIZING axis (execution/sized/unsized).
    No plan-FK glob, no precedence rule, no deliverable-inheritance check
    is duplicated here — `admission` calls the shared predicate and returns
    its verdict verbatim, the same posture `:33`'s `route` already takes
    toward `context.resolved_route`. See `admission`'s own docstring for
    why an explicit `--sizing-object` bypasses the plan-FK precedence
    rather than merely out-ranking it inside the same call.

Spec backlink: pln-plan-assemble-wave-2-the-predi-fad89b, chunk C2
Spec backlink: pln-plan-assemble-admits-instead-o-e441e3, chunk C1
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from coordinator_core import sizing_disposition
from coordinator_core.memo_corpus import memo_corpus_root
from coordinator_core.pickup_assemble import resolve_archived_basename
from coordinator_core.plan_assemble.predicates import PredicateContext, undetermined

#: `:38`'s static route -> sizing-wall disposition table. No disk read.
_SIZING_WALL_DISPOSITION: dict[str, str] = {
    "plan": "route_to_plan",
    "spec-dispatch": "route_to_plan",
    "shape": "route_to_shape",
    "roadmap": "route_to_roadmap",
    "pm-decision": "route_to_pm_decision",
    "dispatch": "route_to_dispatch",
}

#: `:34`'s clean-route disqualifying set — a route in this set means `plan`
#: is not the room for this pass.
_ROADMAP_PRECONDITION_DISQUALIFYING_ROUTES: frozenset[str] = frozenset(
    {"shape", "roadmap", "pm-decision"}
)

#: `:50`'s plan-trigger phrase ladder — a handoff body is read as
#: prescribing a plan if any of these case-insensitive substrings appear.
#: Deliberately a short, explicit list rather than a single regex knob: a
#: future addition is a one-line change, not a rewrite of this function.
_PLAN_TRIGGER_PHRASES: tuple[str, ...] = (
    "prescribes a plan",
    "requires a plan",
    "needs a plan",
    "trigger a plan",
    "triggers a plan",
    "route to plan",
)


def sizing_object_present(context: PredicateContext) -> dict[str, Any]:
    """`:30` -> `gates.triage.sizing_object.present`, `.path`."""
    if context.sizing_object_path is None:
        return undetermined("no --sizing-object supplied")
    return {
        "present": context.sizing_frontmatter is not None,
        "path": str(context.sizing_object_path),
    }


def sizing_object_arrival(context: PredicateContext) -> dict[str, Any]:
    """`:32a` -> `gates.triage.sizing_object.arrival`.

    Read exclusively off `context.caller_flags["arrival"]` — never
    inferred from any artifact. An absent flag is `undetermined`."""
    arrival = context.caller_flags.get("arrival")
    if arrival not in ("fresh_inbound", "return_edge"):
        return undetermined("caller_flags['arrival'] not supplied")
    return {"arrival": arrival}


def sizing_object_narrative_fields(context: PredicateContext) -> dict[str, Any]:
    """`:32b` -> `gates.triage.sizing_object.intent`, `.estimate`,
    `.appetite` — each surfaced verbatim, never backfilled."""
    if context.sizing_frontmatter is None:
        return undetermined("no sizing object frontmatter available")
    return {
        "intent": context.sizing_frontmatter.get("intent"),
        "estimate": context.sizing_frontmatter.get("estimate"),
        "appetite": context.sizing_frontmatter.get("appetite"),
    }


def route(context: PredicateContext) -> dict[str, Any]:
    """`:33` -> `gates.triage.route` — surfaces `context.resolved_route`
    verbatim; never re-derived from the sizing object's other fields."""
    return {"route": context.resolved_route}


def roadmap_precondition(context: PredicateContext) -> dict[str, Any]:
    """`:34` (clean route arm ONLY) ->
    `gates.triage.roadmap_precondition.disqualified`.

    The same `route` field `:33` surfaces, read against the disqualifying
    set `{shape, roadmap, pm-decision}`. The XL-exit arm this contract line
    also names is WITHDRAWN — see this module's negative-spec block; no
    field for it exists anywhere in this package."""
    return {
        "disqualified": context.resolved_route
        in _ROADMAP_PRECONDITION_DISQUALIFYING_ROUTES
    }


def sizing_wall_fires(context: PredicateContext) -> dict[str, Any]:
    """`:37`/`:39` -> `gates.triage.sizing_wall.fires`.

    ONE producer for both contract lines — `:39`'s anti-gaming clause is
    the same negative artifact-presence check `:37` already performs, not
    a second one."""
    if context.sizing_object_path is None:
        return {"fires": True}
    return {"fires": context.sizing_frontmatter is None}


def sizing_wall_disposition(context: PredicateContext) -> dict[str, Any]:
    """`:38` -> `gates.triage.sizing_wall.disposition` — static
    route -> action table lookup, no disk read."""
    disposition = _SIZING_WALL_DISPOSITION.get(context.resolved_route)
    if disposition is None:
        return undetermined(
            f"no sizing_wall disposition mapped for route {context.resolved_route!r}"
        )
    return {"disposition": disposition}


def sizing_wall_via_memo(context: PredicateContext) -> dict[str, Any]:
    """`:40` -> `gates.triage.sizing_wall.via_memo`, `.source_memo`.

    Evidence is two-legged, per the contract's source column: the plan's
    own `source_memo:` frontmatter key is the IDENTIFIER, and
    `cross-repo/inbox/` is scanned only to confirm that same basename is
    present there. `.source_memo` carries the inbox path when the cited
    memo resolves, and the bare citation when it does not — a plan may
    legitimately cite a memo already swept to `cross-repo/archive/`.

    Negative-spec — the inbox scan may NOT stand in for a missing citation.
    An uncited plan is `via_memo: False` even when the inbox is full: a
    scan that returns the first file it finds reports an arbitrary memo as
    this plan's provenance, and on this repo the inbox is non-empty as a
    matter of course, so that shape is a false positive on essentially
    every plan rather than an edge case."""
    if context.plan_frontmatter is None:
        return undetermined("no plan frontmatter available")
    cited = context.plan_frontmatter.get("source_memo")
    if not cited:
        return {"via_memo": False, "source_memo": None}
    basename = Path(str(cited)).name
    inbox_path = Path(memo_corpus_root(str(context.repo_root))) / "inbox" / basename
    if inbox_path.is_file():
        return {
            "via_memo": True,
            "source_memo": inbox_path.relative_to(context.repo_root).as_posix(),
        }
    return {"via_memo": True, "source_memo": str(cited)}


def sizing_wall_carveout(context: PredicateContext) -> dict[str, Any]:
    """`:42` -> `gates.triage.sizing_wall.carveout`.

    Reads the SAME claim-entry signal `gates.claim_grant` already carries
    in the existing envelope, off `context.caller_flags["claim_grant"]` —
    this module builds no independent claim-grant resolver. A truthy value
    means this pass was entered via a claimed handoff/pickup; absence of
    the key is `undetermined`, never a guess of `none`."""
    if "claim_grant" not in context.caller_flags:
        return undetermined("caller_flags['claim_grant'] not supplied")
    return {
        "carveout": "handoff_pickup"
        if context.caller_flags["claim_grant"]
        else "none"
    }


def handoff_prescribes_plan(context: PredicateContext) -> dict[str, Any]:
    """`:50` -> `gates.triage.handoff_prescribes_plan`.

    Reads the handoff `predecessor_handoff:` already names on the plan's
    own frontmatter, and greps its body (case-insensitively) for the
    plan-trigger phrase ladder in `_PLAN_TRIGGER_PHRASES`. Absent
    frontmatter or an absent `predecessor_handoff:` key resolve to
    `undetermined` — never a `False` guess.

    Archive fallback: when the literal path is unreadable, this delegates
    to `pickup_assemble.resolve_archived_basename` — the SAME archive-dirs
    walk `pickup_assemble` already uses to find a swept handoff — rather
    than growing a second resolution ladder here (forbidden by this
    package's own negative-spec, see `residue.py`'s module docstring).
    Zero archive hits keeps today's `undetermined`; a single hit is read
    and scanned exactly as a literal hit would be; two-or-more hits stay
    `undetermined`, naming the ambiguity, since the resolver's own
    contract is detect-then-fail-loud on multi-hit, never first-wins."""
    if context.plan_frontmatter is None:
        return undetermined("no plan frontmatter available")
    handoff_ref = context.plan_frontmatter.get("predecessor_handoff")
    if not handoff_ref:
        return undetermined("plan frontmatter carries no predecessor_handoff key")
    handoff_path = context.repo_root / str(handoff_ref)
    try:
        body = handoff_path.read_text(encoding="utf-8")
    except OSError:
        basename = Path(str(handoff_ref)).name
        archive_hits = resolve_archived_basename(context.repo_root, basename)
        if not archive_hits:
            return undetermined(f"predecessor_handoff not readable: {handoff_path}")
        if len(archive_hits) > 1:
            hit_paths = ", ".join(
                str(hit.relative_to(context.repo_root)) for hit in archive_hits
            )
            return undetermined(
                f"predecessor_handoff ambiguous under archive: {hit_paths}"
            )
        try:
            body = archive_hits[0].read_text(encoding="utf-8")
        except OSError:
            return undetermined(
                f"predecessor_handoff not readable: {archive_hits[0]}"
            )
    lowered = body.lower()
    fires = any(phrase in lowered for phrase in _PLAN_TRIGGER_PHRASES)
    return {"handoff_prescribes_plan": fires}


def _repo_relative_posix(path: Path, repo_root: Path) -> str:
    """`path` as a repo-relative POSIX string.

    `basis` is emitted data a human and a sibling repo both read, so it
    carries ONE path shape regardless of host. A bare `str(Path)` yields
    backslashes on Windows, so the same field reads `state/sizings/x.yaml`
    from one arm and a backslash-separated form from another — and an
    absolute path would leak a machine-local root into a shared artifact.

    An out-of-tree path is NOT hypothetical and is why the fallback names
    rather than echoes: the CLI validates `--sizing-object`/`--plan` with a
    bare `is_file()` and never confines them under `repo_root`, so a path
    outside the tree reaches here, fails `relative_to`, and — if echoed —
    puts the operator's full home directory into emitted data. The fallback
    keeps the basename, which is all a diagnostic needs, and drops the
    machine-local prefix, which nothing downstream may see."""
    try:
        return Path(path).resolve().relative_to(Path(repo_root).resolve()).as_posix()
    except (ValueError, OSError):
        return f"<outside-repo-root>/{Path(path).name}"


def admission(context: PredicateContext) -> dict[str, Any]:
    """-> `gates.triage.admission` — `{value, basis, warning}`, the SIZING
    axis's disposition (`execution` / `sized` / `unsized`), surfaced
    verbatim from `sizing_disposition.compute_sizing_disposition`. This is
    the FK resolution `plan-assemble brief` previously left to the caller
    (see `pln-plan-assemble-admits-instead-o-e441e3`'s Problem): the row
    that removes the audit step, not a new one.

    Total, not partial — unlike every other row in this module, `admission`
    never emits the `undetermined` sentinel. `compute_sizing_disposition`
    is itself total over its `(root, fm)` input (an empty `fm` still
    resolves to its `unsized`/`basis=None` arm), so there is no missing-
    input case here for the sentinel to name.

    An explicitly-supplied `context.sizing_object_path` WINS over the
    inbound artifact's own citations: this row calls the predicate against
    a synthetic `{"sizing_object": <path>}` frontmatter in that case,
    never against `context.plan_frontmatter`. Passing the real plan
    frontmatter through unconditionally would let a `origin_plan_id`/
    `plan_ids` citation on the plan out-rank an operator's explicit
    `--sizing-object` inside the predicate's own precedence order (plan-FK
    before `sizing_object`) — the opposite of AC2's "explicit still wins".
    Bypassing the plan-FK precedence entirely for this arm, rather than
    trying to out-rank it from outside, is what keeps this a single call
    into the shared predicate instead of a second implementation of its
    ordering.

    Absent `--sizing-object`, this resolves the FK from the inbound
    artifact's own frontmatter (`context.plan_frontmatter`, or `{}` when no
    `--plan` was supplied either) — the audit step AC1 removes; the result
    lands wherever the plan-FK/`deliverable_id`-inheritance/`sizing_object`
    precedence inside the predicate says it does, unmodified."""
    if context.sizing_object_path is not None:
        fm: dict[str, Any] = {"sizing_object": _repo_relative_posix(context.sizing_object_path, context.repo_root)}
    else:
        fm = context.plan_frontmatter or {}
    return sizing_disposition.compute_sizing_disposition(
        context.repo_root, fm, self_path=context.plan_path
    )


__all__ = [
    "sizing_object_present",
    "sizing_object_arrival",
    "sizing_object_narrative_fields",
    "route",
    "roadmap_precondition",
    "sizing_wall_fires",
    "sizing_wall_disposition",
    "sizing_wall_via_memo",
    "sizing_wall_carveout",
    "handoff_prescribes_plan",
    "admission",
]
