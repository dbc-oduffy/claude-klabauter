"""
coordinator_core.plan_assemble.residue — the `plan-assemble brief`
computed-skill engine backing the `/plan` skill's route-selected
residue-brief seam.

Purpose: `/plan` needs a small, route-appropriate set of standing residue
(doctrine reminders) rendered alongside a plan draft, scoped to the route
the sizing lobby has ALREADY resolved and written to disk — today `/plan`
either carries the whole residue corpus or re-derives the selection itself.
This module composes ONE `brief(explicit_route)` op that (1) resolves which
route is in play per the `--route RESOLUTION CONTRACT` below, (2) reads
every segment file under the residue directory, filters by resolved route,
sorts by declared `order`, and (3) emits the result through the shared
`coordinator_core.contract.decision_object.envelope` chokepoint — mirroring
`coordinator_core.review_assemble.residue`'s `brief(...)` idiom exactly:
`brief(...) -> build_envelope(...) -> return dict(_emit(...))`.

`brief(...)` also accepts optional `plan_path`/`sizing_object_path` — when
given, this module builds a `coordinator_core.plan_assemble.predicates.
PredicateContext` and runs the wave-2 predicate producers (Layer 0 -> Layer
1 -> Layer 2) to populate the envelope's `gates` key with the four
`gates.triage.*` / `gates.substrate.*` / `gates.composition.*` /
`gates.exit.*` namespaces the predicates package computes. This is the
ONLY module that assembles those namespaces — every sibling predicate
producer module documents itself as deliberately NOT touching this wiring
(see e.g. `predicates/__init__.py`'s own negative-spec). When both paths
are absent (the wave-1 shape), every row's context input is `None` and
every producer resolves its own row(s) to the `undetermined` sentinel —
`gates` is always populated with the full row set, never `{}`, though a
wave-1 caller that never asked for predicates need not read it.

Residue directory (fixed resolution, no parallel ladder):
    os.path.join(resolve_content_root(), "skills", "plan", "residue")
via `coordinator_core.resolve_coordinator_clone.resolve_content_root` — the
same content-root resolver every other computed-skill engine uses.

Segment contract (frozen, authored alongside this module): each file
directly under the residue directory carries YAML frontmatter with exactly
four keys —

    ---
    segment_id: <stable-kebab-case-id, unique within the residue directory>
    route: plan | spec-dispatch | shared
    class: protected | droppable
    order: <integer>
    ---

The directory listing IS the manifest — there is no separate registry file
a segment must additionally appear in. A segment's own frontmatter is its
registration; this module discovers segments purely by listing the
directory.

`--route RESOLUTION CONTRACT` (implemented exactly, not improvised) — ONE
step, not a ladder, and there is no inference of any kind:
  1. `explicit_route` is `None`                    -> resolved_route =
     `DEFAULT_ROUTE` (`"plan"`). Not an error, not a judgment point, not an
     inference from any artifact — the caller simply did not narrow it.
  2. `explicit_route` is one of `EXPLICIT_ROUTES`   -> resolved_route =
     explicit_route.
  3. otherwise                                      -> `RouteUsageError`,
     raised BEFORE any disk access (content root, residue directory) is
     touched.

`gates` ASSEMBLY (chunk C13) — the wave-2 predicate producers this module
composes, and where each namespace's rows land:
  - `gates.triage.*` — `predicates.triage`'s ten rows (`:30`, `:32a`,
    `:32b`, `:33`, `:34` clean-route arm, `:37`/`:39`, `:38`, `:40`, `:42`,
    `:50`), plus Layer 2's `:44`/`:57`/`:139` composed over Layer 0/1
    fields this module already has in hand, plus `admission` — the SIZING
    axis disposition off `triage.admission`, added by
    `pln-plan-assemble-admits-instead-o-e441e3` chunk C1. `next_move` (see
    `_next_move_for_admission`) is composed from this row rather than the
    literal string this function used to return unconditionally.
  - `gates.substrate.*` — `predicates.substrate_seven_dim.compute(...)`,
    `predicates.substrate_scans.compute(...)`, `predicates.
    shared_booleans`'s two Layer-1 rows (`:105(3a)`, `:105(3d)`, folded
    into the same `collapse.*` sub-tree `:106`/`:107`/`:108` already
    populate), `predicates.citation_staleness`'s two legs (`:85-87`),
    `predicates.concurrent_preflight` (`:83`), and Layer 2's `:90(7)`,
    `:91`, `:105(1)`, `:105(2)`, `:134` composed on top.
  - `gates.composition.*` — `predicates.composition_lints`'s seven rows
    (`:136`, `:137`, `:143`, `:150`, `:152`, `:153`, `:172`),
    `predicates.composition_graph`'s four rows (`:151`, `:156`, `:160`,
    `:162`), and `predicates.supersedes_index.supersedes_plan` (`:164`).
  - `gates.exit.*` — `predicates.exit_gates.build_exit_gates(...)`
    (`:189`), plus Layer 2's `:195-198` terminal-table lookup.
  - `judgment_points[]` — `:59`'s `architectural_tier_judgment_point(...)`
    is the ONE entry this module ever appends here; it is NOT a `gates.*`
    field (see Negative-spec).

Negative-spec:
  - Does NOT port `review_assemble.residue`'s `--surface` inference ladder
    (artifact-shape probe, git-diff probe, judgment-point-on-ambiguity).
    There is no artifact argument to this module's `brief` at all, and no
    `judgment_points[]` entry is ever raised for route resolution — the
    route is a fact the sizing lobby already resolved and wrote to disk,
    not one this module infers.
  - Does NOT widen `--route` to `dispatch`/`shape`/`roadmap`/`pm-decision`.
    Those are illegal values by contract: they never reach the segment set,
    and an attempt to pass one is a usage error (exit 2), never a
    legal-but-empty business result (exit 1) — the two failure modes stay
    distinct.
  - Does NOT make `contract.residue_segments.select_segments` fail-loud.
    Its negative-spec (zero matches -> `[]`, silently) is deliberate and
    shared by every consumer of that loader; the zero-applicable-segments
    check is made HERE, after the call, exactly as
    `review_assemble.residue` does it.
  - Does NOT construct a parallel resolution ladder for the residue
    directory — the one `resolve_content_root()` call is the only
    resolution step; no fallback probing of alternate locations.
  - Does NOT copy `pickup_assemble`'s forked `_emit`, and does NOT import
    `review_assemble`'s `ResidueAssembleError` alias — this module aliases
    `SegmentLoadError` locally so it does not depend on review's package
    for an exception type (one hierarchy per consumer, not a shared one
    across packages).
  - RESIDUE/PREDICATE SPLIT (chunk C13 defect fix — read before touching
    the zero-segment path): a bare wave-1 call — `plan_path is None and
    sizing_object_path is None` — still does NOT emit a well-formed
    envelope carrying zero residue. An empty residue directory, or a
    segment set that resolves to zero applicable segments after route
    filtering, is STILL a fail-loud `ResidueAssembleError` for that caller
    shape, same type, same message, unconditionally. That negative-spec
    binds ONLY for the bare-call shape. When `plan_path` and/or
    `sizing_object_path` IS supplied (predicates requested), the SAME
    zero-segment/missing-directory condition does NOT abort — the residue
    corpus is a sibling team's deliverable this caller has no dependency
    on, and `gates` (this caller's actual ask) must still assemble. The
    envelope still emits: `segments` is `[]`, and the absence is reported
    in-band via `narration` and `decisions["residue_unavailable"]`, never
    silently and never as a ninth envelope key. Do NOT "restore" this
    split into one unconditional fail-loud path — that recouples the
    predicate half of `brief()` to a residue corpus it has no dependency
    on, the exact defect this split fixes. `ResolveCoordinatorCloneError`
    is UNCHANGED by this split — it always propagates unchanged for both
    caller shapes, being a transport failure, not a content-absence one.
  - Does NOT add a ninth top-level envelope key. `gates` is populated, not
    joined by a sibling — `segments[]` stays the one non-canonical key,
    byte-identical to wave 1's shape.
  - Does NOT widen `--route` resolution or raise a judgment point for it
    just because `plan_path`/`sizing_object_path` are now accepted — those
    two params feed `PredicateContext` only; the `--route RESOLUTION
    CONTRACT` above is untouched by their presence or absence.
  - Does NOT emit anything for `:43` (vacuous by construction), `:34`'s
    XL-exit arm (withdrawn), or `:121` (withdrawn) — no field, no
    `undetermined` entry, nothing. See each producer module's own
    negative-spec for why.
  - Does NOT re-implement any predicate producer's own row logic. This
    module's ENTIRE `gates` assembly is calls into `predicates.*` modules
    plus the dict-shaping needed to land each row at its documented
    `gates.<namespace>.*` path — never a parallel computation.
  - Does NOT fold `admission` into an `admitted` boolean or any field named
    `*.verdict`/`*.fires`/`*.recommended` spanning a `U`- or `G`-typed
    triage row. `admission` is scoped to the SIZING axis alone (entirely
    `C`-typed); `_next_move_for_admission` narrates that one axis's
    resolved value, never a PM-intent or JTBD-falsifiability judgment the
    engine has no business making. See
    `pln-plan-assemble-admits-instead-o-e441e3`'s Anti-scope.

Spec backlink: pln-plan-assemble-brief-route-the-2d016a, chunk C1
Spec backlink: pln-plan-assemble-wave-2-the-predi-fad89b, chunk C13
Spec backlink: pln-plan-assemble-admits-instead-o-e441e3, chunk C1
Spec backlink: pln-plan-assemble-admits-instead-o-e441e3, chunk C2
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Union

from coordinator_core.contract.decision_object.envelope import (
    build_envelope,
    _emit as _envelope_emit,
)
from coordinator_core.contract.residue_segments import (
    SegmentLoadError,
    load_segments,
    select_segments,
)
from coordinator_core import sizing_disposition
from coordinator_core.plan_assemble.predicates import PredicateContext, undetermined
from coordinator_core.plan_assemble.predicates import citation_staleness
from coordinator_core.plan_assemble.predicates import composed
from coordinator_core.plan_assemble.predicates import composition_graph
from coordinator_core.plan_assemble.predicates import composition_lints
from coordinator_core.plan_assemble.predicates import concurrent_preflight as _concurrent_preflight
from coordinator_core.plan_assemble.predicates import exit_gates
from coordinator_core.plan_assemble.predicates import shared_booleans
from coordinator_core.plan_assemble.predicates import substrate_scans
from coordinator_core.plan_assemble.predicates import substrate_seven_dim
from coordinator_core.plan_assemble.predicates import supersedes_index
from coordinator_core.plan_assemble.predicates import triage
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.resolve_coordinator_clone import resolve_content_root

#: The three legal values of a segment's `route:` frontmatter field.
#: `shared` applies to every resolved route; `plan`/`spec-dispatch` apply
#: only when that route is the one actually resolved this call.
SEGMENT_ROUTES: tuple[str, ...] = ("plan", "spec-dispatch", "shared")

#: The two legal values of an explicit `--route` argument — deliberately
#: narrower than `SEGMENT_ROUTES` (no `shared`; a caller resolves to a
#: concrete route, never to the segment-authoring category).
EXPLICIT_ROUTES: tuple[str, ...] = ("plan", "spec-dispatch")

#: The route an absent `--route` resolves to. Not an error, not a
#: judgment point — see the module docstring's RESOLUTION CONTRACT step 1.
DEFAULT_ROUTE = "plan"


#: The plan-side name for the shared segment-loader's failure type — an
#: alias, not a subclass, kept local to this package rather than importing
#: `review_assemble.residue.ResidueAssembleError` (one hierarchy per
#: consumer, no cross-package exception-type dependency).
ResidueAssembleError = SegmentLoadError


class RouteUsageError(RuntimeError):
    """Raised when an explicit `--route` value is given but is not one of
    `EXPLICIT_ROUTES`. A CLI wrapper maps this to its own usage exit code
    (2) — never a silent fallthrough to inference, and raised BEFORE any
    residue directory or content-root state is touched. Deliberately a
    distinct type from `ResidueAssembleError` (a business/content-shape
    failure): this is a caller-usage failure."""


#: The one true residue directory, relative to the content root — the
#: `segment_dir` parameter this module passes to the shared loader.
_RESIDUE_SEGMENT_DIR = "skills/plan/residue"

#: Timeout for the one best-effort `git rev-parse --show-toplevel` shell-out
#: this module makes, to resolve `PredicateContext.repo_root` when no
#: caller-supplied value is available. A slow/hung git never blocks
#: `brief(...)` — see `_default_repo_root`.
_GIT_TIMEOUT_SEC = 10


def _residue_dir(content_root: Path) -> Path:
    """Resolve the one true residue directory relative to *content_root*:
    ``<content-root>/skills/plan/residue``."""
    return content_root / _RESIDUE_SEGMENT_DIR


def _resolve_route(explicit_route: Optional[str]) -> str:
    """Resolve the active route per the `--route RESOLUTION CONTRACT` in
    the module docstring — ONE step, not a ladder, and no disk access.

    Raises `RouteUsageError` if `explicit_route` is given but is not one of
    `EXPLICIT_ROUTES`.
    """
    if explicit_route is None:
        return DEFAULT_ROUTE
    if explicit_route in EXPLICIT_ROUTES:
        return explicit_route
    raise RouteUsageError(
        "plan-assemble residue: --route must be one of "
        f"{EXPLICIT_ROUTES!r}, got {explicit_route!r}"
    )


def _default_repo_root() -> Path:
    """Best-effort repo-root resolution from the current working directory,
    falling back to `Path.cwd()` on any failure (not a git repo, `git`
    unavailable, or a timeout). Never raises — every `predicates.*` row
    that reads `repo_root` already treats a non-existent or unrelated
    directory as "nothing found there", not as an error, so a best-effort
    guess here is always safe. Delegates to the shared
    `coordinator_core.git.repo_root.show_toplevel` seam (walks for the
    ordinary case, spawns only as a last resort)."""
    toplevel = show_toplevel()
    if toplevel:
        return Path(toplevel)
    return Path.cwd()


def _is_undetermined(value: Any) -> bool:
    return isinstance(value, dict) and value.get("undetermined") is True


def _unpack(row: Any, *fields: str) -> dict[str, Any]:
    """Fan a Layer-0 row's return value out to one dict entry per named
    contract sub-field.

    Several `gates.<namespace>.<group>.*` sub-trees are documented by their
    own producer modules as merges of MULTIPLE independent row functions
    into one flat dict (e.g. `gates.triage.sizing_object.{present,path,
    arrival,intent,estimate,appetite}` off three separate `triage.py`
    functions). Each source function still independently resolves to
    either a populated dict or `predicates.undetermined(...)` — this
    helper preserves that per-function sentinel at EVERY field it
    contributes, rather than collapsing a partially-undetermined merge
    into one misleading combined value.
    """
    if _is_undetermined(row):
        return {field: row for field in fields}
    # Review: F2 fix — `row[field]` (not `.get`) so a producer that omits a
    # documented field raises KeyError instead of silently synthesizing a
    # `None` the coverage oracle cannot distinguish from a legitimately-
    # None-valued field. A legitimately-`None`-valued field stays legal —
    # only a genuinely-ABSENT key now fails loudly.
    return {field: row[field] for field in fields}


def _assemble_gates(
    context: PredicateContext,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run every wave-2 predicate producer over *context* and assemble
    `(gates, judgment_points)`: the four `gates.*` namespaces this module
    owns exclusively (chunk C13), plus `:59`'s one `judgment_points[]`
    entry — never a `gates.*` field itself (see the module docstring).

    Pure fan-out and dict-shaping over `predicates.*` — no independent
    disk/git read happens here; every read is a `predicates.*` producer's
    own named read.
    """
    # Computed once, up front: `substrate_seven_dim.compute`/`substrate_
    # scans.compute` are each a single-pass fan-out over their own rows,
    # and `:57` (triage's nontrivial_disjunction) needs three of
    # `substrate_scans.compute`'s fields — reusing this bundle instead of
    # calling those row functions a second time.
    seven_dim_bundle = substrate_seven_dim.compute(context)
    scans_bundle = substrate_scans.compute(context)

    # --- gates.triage.* ------------------------------------------------
    sizing_object_present_row = triage.sizing_object_present(context)
    sizing_object_arrival_row = triage.sizing_object_arrival(context)
    sizing_object_narrative_row = triage.sizing_object_narrative_fields(context)
    route_row = triage.route(context)
    sizing_wall_fires_row = triage.sizing_wall_fires(context)
    sizing_wall_disposition_row = triage.sizing_wall_disposition(context)
    sizing_wall_via_memo_row = triage.sizing_wall_via_memo(context)
    sizing_wall_carveout_row = triage.sizing_wall_carveout(context)

    scope_file_count_row = shared_booleans.collapse_scope_file_count(context)
    no_cross_repo_contract_row = shared_booleans.collapse_no_cross_repo_contract(context)

    gates_triage: dict[str, Any] = {
        "sizing_object": {
            **_unpack(sizing_object_present_row, "present", "path"),
            **_unpack(sizing_object_arrival_row, "arrival"),
            **_unpack(sizing_object_narrative_row, "intent", "estimate", "appetite"),
        },
        "route": route_row,
        "roadmap_precondition": triage.roadmap_precondition(context),
        "sizing_wall": {
            **_unpack(sizing_wall_fires_row, "fires"),
            **_unpack(sizing_wall_disposition_row, "disposition"),
            **_unpack(sizing_wall_via_memo_row, "via_memo", "source_memo"),
            **_unpack(sizing_wall_carveout_row, "carveout"),
        },
        "handoff_prescribes_plan": triage.handoff_prescribes_plan(context),
        # chunk C1 (pln-plan-assemble-admits-instead-o-e441e3) — the SIZING
        # axis admission, verbatim from `triage.admission`. Never merged
        # with a `U`/`G` row: see this module's `next_move` composition
        # below, and `triage.admission`'s own docstring, for why.
        "admission": triage.admission(context),
        # Layer 2 — pure composition over the two Layer-1 booleans above.
        "trivial_conjunction": composed.trivial_conjunction(
            scope_file_count_row, no_cross_repo_contract_row
        ),
        "nontrivial_disjunction": composed.nontrivial_disjunction(
            scope_file_count_row,
            no_cross_repo_contract_row,
            scans_bundle["reverses_teardown"],
            scans_bundle["mutates_shared_symbol"],
            scans_bundle["scaffold_checklist"],
        ),
        # :139 — reuses gates.triage.route tested against the
        # review-triggering set; lives beside `route` itself.
        "route_triggers_review": composed.route_triggers_review(route_row),
    }

    # --- gates.substrate.* ----------------------------------------------
    staleness_scope_row = citation_staleness.scope_paths_staleness(context)
    staleness_cited_row = citation_staleness.cited_lines_staleness(context)

    all_green_value = composed.seven_dim_all_green(seven_dim_bundle["seven_dim"])

    gates_substrate: dict[str, Any] = {
        "problem_set": seven_dim_bundle["problem_set"],
        "scope_mode": seven_dim_bundle["scope_mode"],
        "seven_dim": {
            **seven_dim_bundle["seven_dim"],
            "fix_locus": composed.seven_dim_fix_locus(scans_bundle["fix_locus"]),
            "all_green": all_green_value,
        },
        "premise_gate": seven_dim_bundle["premise_gate"],
        "trampoline": seven_dim_bundle["trampoline"],
        "peer_sha_lint": scans_bundle["peer_sha_lint"],
        "collapse": {
            **scans_bundle["collapse"],
            **_unpack(scope_file_count_row, "scope_file_count_le_2", "scope_file_count"),
            **_unpack(no_cross_repo_contract_row, "no_cross_repo_contract", "crossing_paths"),
            "seven_dim_green": composed.collapse_seven_dim_green(all_green_value),
            "premise_gate_green": composed.collapse_premise_gate_green(
                seven_dim_bundle["premise_gate"]
            ),
        },
        "fix_locus": scans_bundle["fix_locus"],
        "citations_verified": scans_bundle["citations_verified"],
        "symbol_liveness": scans_bundle["symbol_liveness"],
        "reverses_teardown": scans_bundle["reverses_teardown"],
        "native_code_plan": scans_bundle["native_code_plan"],
        "registered_dispatch_added": scans_bundle["registered_dispatch_added"],
        "port_seam": scans_bundle["port_seam"],
        "mutates_shared_symbol": scans_bundle["mutates_shared_symbol"],
        "scaffold_checklist": scans_bundle["scaffold_checklist"],
        "staleness": {
            **_unpack(staleness_scope_row, "scope_paths_stale", "stale_paths"),
            **_unpack(staleness_cited_row, "cited_lines_stale", "stale_citations"),
        },
        "concurrent_preflight": _concurrent_preflight.concurrent_preflight(context),
        # :134 — reuses gates.substrate.scope_mode tested for non-null.
        "scope_mode_declared": composed.scope_mode_declared(seven_dim_bundle["scope_mode"]),
    }

    # --- gates.composition.* ---------------------------------------------
    concurrency_shared_state_row = composition_lints.concurrency_shared_state(context)

    gates_composition: dict[str, Any] = {
        "spine_row_shape": composition_lints.spine_row_shape(context),
        "ac_reject_list": composition_lints.ac_reject_list(context),
        "deferral_case_against": composition_lints.deferral_case_against(context),
        "hard_constraints_block": composition_lints.hard_constraints_block(context),
        "chunk_overlap": composition_graph.chunk_overlap(context),
        "stub_spawns_subagents": composition_lints.stub_spawns_subagents(context),
        "concurrency_shared_state": concurrency_shared_state_row,
        "path_rename_or_move": composition_graph.path_rename_or_move(context),
        "cross_plan_conflict": composition_graph.cross_plan_conflict(context),
        "amends_assumption": composition_graph.amends_assumption(context),
        "supersedes_plan": supersedes_index.supersedes_plan(context),
        "chunk_index_sidecar": composition_lints.chunk_index_sidecar(context),
    }

    # --- gates.exit.* ------------------------------------------------------
    gates_exit: dict[str, Any] = {
        **exit_gates.build_exit_gates(context),
        "terminal_table": composed.terminal_table_result(route_row),
    }

    gates: dict[str, Any] = {
        "triage": gates_triage,
        "substrate": gates_substrate,
        "composition": gates_composition,
        "exit": gates_exit,
    }

    # `:59` -> the ONE `judgment_points[]` entry, built here (not stored
    # under `gates`) since it consumes the same three rows this function
    # already has in hand and is never itself a `gates.*` field.
    #
    # Gated on the caller having supplied a plan, which `gates` deliberately
    # is not: an unpopulated `gates` row still carries information (the row
    # exists and could not be read), whereas a judgment point whose every
    # candidate criterion is `undetermined` presents the EM nothing to judge.
    # Suppressing it is also what keeps a wave-1 caller's envelope
    # byte-identical, per this module's `judgment_points[]` negative-spec.
    judgment_points: list[dict[str, Any]] = []
    if context.plan_path is not None:
        judgment_points.append(
            composed.architectural_tier_judgment_point(
                no_cross_repo_contract_row,
                concurrency_shared_state_row,
                scans_bundle["fix_locus"],
            )
        )

    return gates, judgment_points


#: Segments are always the render procedure, never the instruction — every
#: arm below still tells the caller to render them; only the sentence
#: naming what to DO before/around that changes with the admission arm.
_RENDER_SEGMENTS_CLAUSE = "Render segments[] in order."


def _next_move_for_admission(admission: dict[str, Any], resolved_route: str) -> str:
    """The computed `next_move` instruction for `admission`'s resolved arm
    (chunk C2, `pln-plan-assemble-admits-instead-o-e441e3`) — replaces the
    literal `"Render segments[] in order."` this function used to be.

    - `unsized`    -> `sizing_disposition.unsized_next_move_prefix(admission)`
      names `coordinator:sizing` as the room, not `plan` — reused verbatim,
      never a hand-written third sentence (see that helper's own
      docstring for why there are exactly two unsized-arm texts).
    - `sized`      -> names the lane THIS CALL was invoked for
      (`resolved_route`) plus `admission["basis"]`'s object path, so nothing
      downstream re-looks-it-up. It deliberately does NOT say the sizing
      lobby routed this here: `resolved_route` is the caller's `--route`,
      and nothing on this path cross-checks it against the cited object's
      own `route:` field. Asserting provenance this function has not
      verified would state a falsehood on exactly the misrouted call an
      operator most needs to catch — the register rule is one fact, stated
      once, and never a fact we did not establish.
    - `execution`  -> names the plan `admission["basis"]` resolved to, and
      says plainly that sizing/planning are not re-litigated.

    `admission["value"]` is always one of the three — `triage.admission`
    never emits `undetermined` (see its own docstring) — so this function
    has no fourth arm."""
    if admission["value"] == "unsized":
        prefix = sizing_disposition.unsized_next_move_prefix(admission)
        return f"{prefix}{_RENDER_SEGMENTS_CLAUSE}"
    if admission["value"] == "sized":
        return (
            f"Sizing resolves ({admission['basis']}); resume in the "
            f"{resolved_route!r} lane this call was invoked for. "
            f"{_RENDER_SEGMENTS_CLAUSE}"
        )
    # "execution"
    return (
        f"Already planned ({admission['basis']}); sizing and planning are "
        f"not re-litigated — resume execution. {_RENDER_SEGMENTS_CLAUSE}"
    )


def brief(
    *,
    explicit_route: Optional[str] = None,
    plan_path: Optional[Union[str, Path]] = None,
    sizing_object_path: Optional[Union[str, Path]] = None,
    caller_flags: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Compute the plan-residue decision object for `explicit_route`.

    `explicit_route` is the raw `--route` value the caller passed (already
    resolved by the sizing lobby, e.g. `spec-dispatch` for a
    scope-dispatch route) — resolution follows the `--route RESOLUTION
    CONTRACT` in the module docstring exactly: `None` resolves to
    `DEFAULT_ROUTE` (`"plan"`), a value in `EXPLICIT_ROUTES` resolves to
    itself, anything else raises `RouteUsageError` before any disk access.

    `plan_path`/`sizing_object_path` are both optional and independent of
    `explicit_route` — when given, they feed a `predicates.PredicateContext`
    that this function uses to populate the returned envelope's `gates`
    key (see the module docstring's `gates` ASSEMBLY section). Absent
    either, the corresponding context field is `None` and every predicate
    row keyed on it resolves to the `undetermined` sentinel — `gates` is
    still fully populated with every row's key, never `{}`.

    `caller_flags` forwards verbatim into `PredicateContext.caller_flags`
    (Review: caller-flags fix — wires `:32a`/`:100`/`:108`, which
    previously resolved `undetermined` on every real CLI invocation because
    nothing populated this dict). `None` (the default) resolves to `{}` —
    identical to every flag being absent — never backfilled to any other
    value; a row keyed on a flag this dict does not carry still emits
    `undetermined`, exactly as before this param existed.

    Read-only: performs no disk mutation. A bare wave-1 call (`plan_path`
    and `sizing_object_path` both `None`) performs no git operation of any
    kind either. When predicates ARE requested, this function itself makes
    ONE best-effort `git rev-parse --show-toplevel` shell-out (see
    `_default_repo_root`) to resolve `PredicateContext.repo_root` when no
    caller-supplied value is available — unconditional for that caller
    shape, not merely "some `predicates.*` producers'" own read-only
    shell-outs (those exist too, and are separate from this one).

    Raises `RouteUsageError` if `explicit_route` is given but is not one of
    `EXPLICIT_ROUTES`. Raises `ResolveCoordinatorCloneError` (propagated
    unchanged) always — a transport failure, not a content-absence one.

    The residue/predicate SPLIT (chunk C13 defect fix): whether a
    zero-segment / missing-residue-directory condition (`ResidueAssembleError`)
    is fail-loud or reported in-band depends ENTIRELY on whether
    `plan_path`/`sizing_object_path` were supplied —
      - `plan_path is None and sizing_object_path is None` (wave-1 bare
        call): fail-louds exactly as before — raises `ResidueAssembleError`,
        same type, same message. Wave 1's negative-spec (does NOT emit a
        well-formed envelope carrying zero residue) still binds for THIS
        caller shape only.
      - `plan_path is not None or sizing_object_path is not None`
        (predicates requested): the residue corpus is a sibling team's
        deliverable this caller does not depend on — a zero-segment/missing
        condition does NOT abort. `brief()` still returns a well-formed
        envelope with `gates` fully populated; `segments` is `[]` and the
        absence is stated in `narration` and `decisions["residue_unavailable"]`,
        never silently.
    """
    resolved_route = _resolve_route(explicit_route)

    # Predicates were requested when either path is supplied — this is the
    # ONLY thing that decides whether a zero-segment/missing-directory
    # condition fail-louds (wave-1 shape) or is reported in-band (wave-2
    # predicates-requested shape). See module docstring's residue/predicate
    # SPLIT section.
    predicates_requested = plan_path is not None or sizing_object_path is not None

    content_root = Path(resolve_content_root())
    residue_dir = _residue_dir(content_root)

    selected: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    residue_error: Optional[ResidueAssembleError] = None

    try:
        segments = load_segments(
            content_root,
            _RESIDUE_SEGMENT_DIR,
            filter_key="route",
            legal_values=SEGMENT_ROUTES,
        )
        selected = select_segments(
            segments, filter_key="route", active_values={resolved_route, "shared"}
        )
        if not selected:
            residue_error = ResidueAssembleError(
                "plan-assemble residue: resolved route "
                f"{resolved_route!r} has zero applicable segments in {residue_dir}"
            )
    except ResidueAssembleError as exc:
        residue_error = exc

    if residue_error is not None:
        if not predicates_requested:
            raise residue_error
        # Predicates were requested — the residue corpus is a sibling
        # team's deliverable this caller does not depend on. Report the
        # absence in-band (narration + decisions) rather than aborting the
        # predicate half of brief(). segments[] stays [] per the wave-1
        # byte-identical shape's `segments[]` contract.
        selected = []
        segments = []

    resolved_plan_path = Path(plan_path) if plan_path is not None else None
    resolved_sizing_object_path = (
        Path(sizing_object_path) if sizing_object_path is not None else None
    )

    # Review: F1 fix — the git shell-out only matters to a `predicates.*`
    # producer, and every producer resolves `undetermined` on a bare
    # wave-1 call regardless of `repo_root`'s value. Only spawn the
    # subprocess when predicates were actually requested; `Path.cwd()` is
    # free and unused on the bare-call path (see `_default_repo_root`).
    repo_root = _default_repo_root() if predicates_requested else Path.cwd()

    predicate_context = PredicateContext.from_paths(
        repo_root=repo_root,
        plan_path=resolved_plan_path,
        sizing_object_path=resolved_sizing_object_path,
        resolved_route=resolved_route,
        caller_flags=dict(caller_flags) if caller_flags is not None else {},
    )

    gates, judgment_points = _assemble_gates(predicate_context)

    decisions: dict[str, Any] = {
        "residue_dir": residue_dir.relative_to(content_root).as_posix()
    }
    if residue_error is not None:
        decisions["residue_unavailable"] = str(residue_error)
        narration = (
            f"plan-assemble residue: route={resolved_route!r}; residue corpus "
            f"unavailable ({residue_error}); predicates requested and gates "
            "populated regardless — segments[] is []."
        )
    else:
        narration = (
            f"plan-assemble residue: route={resolved_route!r}, "
            f"{len(selected)} segment(s) selected of {len(segments)} total."
        )

    envelope = build_envelope(
        artifact={"route": resolved_route},
        preflight={},
        gates=gates,
        directives=[],
        judgment_points=judgment_points,
        decisions=decisions,
        narration=narration,
        next_move=_next_move_for_admission(gates["triage"]["admission"], resolved_route),
    )
    result = dict(_envelope_emit(envelope))
    result["segments"] = selected
    return result
