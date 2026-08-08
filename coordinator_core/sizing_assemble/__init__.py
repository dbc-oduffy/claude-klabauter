"""
coordinator_core.sizing_assemble — the `sizing-assemble` computed-skill engine.

Purpose: computes `coordinator/skills/sizing/SKILL.md`'s routing TABLE
(express-lane / plan-check-first / shape / roadmap-re-aim) server-side, from
the appetite<->estimate delta plus scout signals, into one read-only decision
object. Mirrors `pickup_assemble.brief()`'s shape (a compute layer over a
frozen contract; the EM's job collapses to resolving the judgment residue
this module surfaces — the appetite<->estimate fork, AC5) rather than a
routing table living as prose in the skill body (the "deterministic if/else
in a markdown fence at the logic level" defect `invisible-doctrine.md`
realization #6 forbids).

Contract (frozen, reviewed): example-doctrine-repo coordinator/docs/wiki/computed-skills.md
DR-090 (example-doctrine-repo docs/decisions/DR-090-the-unit-of-extraction-is-the-mechanical-step.md)
Spec backlink: example-doctrine-repo docs/plans/2026-07-24-sizing-lobby-core.md, chunk C7 (Design D4/D5)
Input shape: example-doctrine-repo coordinator/schemas/sizing-object.schema.json (C1)
Registration seam: this module ships no bash veneer and needs none — it is
consumed directly by the `coordinator/bin/sizing-assemble` trampoline (mirrors
`coordinator/bin/pickup-assemble`'s direct-import template-variant #1).

READ-ONLY, by construction: `route()` only reads its arguments — it never
touches disk, never writes a sizing-object, never shells out. The caller
(the sizing skill / a future scaffolder) is responsible for persisting the
returned decision into a `state/sizings/*.yaml` sizing-object; this module
computes the routing fields (`route`, `detents`, `fork`) of that object, plus
transport-only convenience keys (`resolved_estimate`, `scout_evidence`,
`narration`, `next_move`) the caller doesn't have to recompute — the
schema-persisted fields are `route`/`detents`/`fork`, not the full return
payload (see `route()`'s own docstring for the complete return shape).

Design D4 (route computed here, not chosen by skill prose): `route()` is the
ONLY place the express-lane / plan-check-first / shape / roadmap-re-aim
routing table lives. `coordinator/skills/sizing/SKILL.md` calls this and
pushes the result — it carries no branch-selection prose duplicating this
table (AC4/AC6).

Route table (PM directive 2026-07-30 — see `_BASE_ROUTE_BY_TSHIRT` for the
hard-gate ruling): XS->dispatch, S->spec-dispatch, M->plan, L->plan,
XL->pm-decision, XXL->goal-setting (2026-08-07 sizing-ladder-xxl-notch-and-
goal-setting-route plan, C1). `goal-setting` is a terminal room, not a halt
like `pm-decision`: an XXL resolves an OKR-scale programme rather than a
plan, and `coordinator:goal-setting` is PM-gated (see `goal_setting_pm_gated`
in the Negative-spec / detent notes below). `spec-dispatch` is a light plan
artifact
(`scope_mode: spec-dispatch`) — substrate verification, scaffold-plus-commit,
and the cross-plan conflict scan still run; the four-lens body composition
and the Opus plan review do not. `pm-decision` is not a room: the engine
resolves the route and sets the `pm_decision_pending` detent, but never
picks among the PM's four XL exits (`split` / `shape` / `roadmap` /
`accept_multi_session`) — that choice is recorded in the sizing-object's
`xl_exit` field, engineered exactly like `fork` (see below).

Design D5 (shape-entry conditions, PM directive 2026-07-24): `route=shape`
is never a size threshold alone. It fires ONLY when (a) the resolved size is
large (plan/roadmap-scale) AND the JTBD is unclear, OR (b) the space is
recently well-trodden and the ask wants a step-change. Conditions (a) and
(b) are independent alternative triggers — (b) is NOT size-gated, only (a)
is (Finding 1, code-reviewer 2026-07-24: the plan text and this module's own
docstring both state (b) with no size clause; a coded `and _LARGE_TSHIRTS`
across the whole disjunction was a defect, not an interpretation). A
large-but-clear ask routes straight to plan/roadmap, skipping shape. These
two conditions are encoded here (`jtbd_unclear` / `well_trodden_step_change`
params), not left as an EM gut-call — see D5's "routing-in-the-head... sneaks
back in" framing.

Symmetric sizing (MANDATORY, Finding 2, the Staff Engineer review 2026-07-24): the coarse
`estimate.tshirt` is resized in BOTH directions before the route is resolved
— an over-read COLLAPSES down and an under-read RAISES up. The resize
direction is supplied by the caller as `probe_signal` (the on-demand
substrate probe described in the sizing skill's flow step 2 determines
*whether* the evidence collapses or raises the estimate; `route()`'s job is
to apply that resize as a structural step of route resolution, not an
optional prose instruction the skill could skip) — the resize is unskippable
here specifically so an under-read cannot leak through un-caught, closing
the "only over-reads get collapsed" gap Finding 2 named. When the resize
changes which routing tier the estimate lands in, the `route_boundary_crossed`
detent is set, naming the catch explicitly.

Appetite<->estimate reconciliation (AC5): appetite is OPTIONAL and is never
collected before a size is delivered (PM directive 2026-08-07 — sizing comes
first; the EM does not ask for or assume a budget up front). When a caller
DOES volunteer an appetite, a diverging estimate (bigger than the budget's
ceiling) is surfaced via the `appetite_exceeded` detent — the sole divergence
signal `route()` emits for that path (Finding 2, code-reviewer 2026-07-24).
`fork` stays `None` from this module always; it is the RESOLUTION slot the
sizing *skill* fills once the PM has actually picked cut_to_fit vs
raise_appetite (the point the schema's `status` lifecycle flips
draft->sized->routed). `fork` non-null means "the PM resolved this," full
stop — never an engine placeholder pre-filled with a default guess. A
within-budget or under-budget estimate never sets a detent for this and
`fork` stays `None`, same as the divergent case — the only observable
difference is `appetite_exceeded` in `detents`.

When appetite is ABSENT (the default path), neither `appetite_conform` nor
`appetite_exceeded` fires — there is no budget to compare against. Instead,
a resized t-shirt at or above `"M"` (see `_POST_SIZE_PROMPT_TSHIRTS`) sets
the `post_size_prompt_pending` detent and `next_move` appends an OPEN
question asking the PM whether to proceed, split, or cut — never the closed
cut_to_fit/raise_appetite pair, which presumes a budget the PM was never
asked for. XS/S never ask; `route` itself is identical across appetite
absent/small/medium/large for every t-shirt (AC2) — appetite never feeds the
route, only the detent.

Express lane (D3): `express_lane=True` short-circuits straight to
`route="dispatch"` with no detents/fork and no reconciliation — no
sizing-object litter for trivial asks (AC7's "costs ~zero" ergonomics AC).
This includes the premise-provenance detent below: express_lane returns
before ANY detent computation runs, so `premise_unproven` /
`premise_not_applicable` never fire on that path regardless of
`premise_provenance` — that is correct by size (D3), not an oversight to
"fix" by adding the detent to the short-circuit (cross-repo memo
2026-08-05-example-doctrine-repo-em-premise-provenance-detent-sizing-assemble.md).

Premise provenance (advisory detent, warn-never-block — DR-068 precedent;
cross-repo memo 2026-08-05-example-doctrine-repo-em-premise-provenance-detent-sizing-
assemble.md): `premise_provenance` is one of `executed` | `read` |
`not-applicable` | `unrecorded` | None, validated unconditionally by
`_validate_premise_provenance` (same unconditional-validation property as
`_validate_probe_signal` — Finding 5, code-reviewer 2026-07-24). When
provenance is `read` AND the RESIZED t-shirt is in `_LARGE_TSHIRTS`
(L/XL/XXL), the `premise_unproven` detent fires; when provenance is
`not-applicable` under the same size gate, `premise_not_applicable` fires
instead. Both land in the same `DETENT_ENUM` widen (never staggered — the
Example-doctrine-repo-side schema parity test asserts symmetric set equality against this
tuple). The gate keys on resized SIZE, not resolved ROUTE, so it fires
identically on `shape`-, `pm-decision`-, and `goal-setting`-routed L/XL/XXL,
not only `plan`-routed — routing away from `plan` does not reduce the
premise-truth-value multiplier the detent exists to name. Like
`appetite_exceeded` and `pm_decision_pending`,
this detent NEVER alters `route` and NEVER populates `xl_exit` — it is
advisory only, discharged by citing executed evidence inline, never by
producing a spike-result artifact (a structural-in-mechanism,
never-in-ceremony design per the memo's PM ruling 2).

Negative-spec:
    - Do NOT add a mutating code path here. This module returns data; it
      never writes state/sizings/*.yaml itself.
    - Do NOT parse free-text `scout_evidence` strings for sizing signal. The
      schema types `scout_evidence` as a plain list of provenance strings
      (paths/citations) with no embedded semantics — inventing NLP heuristics
      over that list would be exactly the kind of undocumented judgment call
      this contract exists to prevent. The symmetric-resize signal is a typed
      `probe_signal` parameter the caller (which ran the actual scouting)
      supplies explicitly; `scout_evidence` here is provenance passthrough
      only, echoed back unmodified for the caller to persist.
    - Do NOT set `fork` from this module. `appetite_exceeded` in `detents`
      is the sole divergence signal; `fork` is the skill's resolution slot,
      populated only once the PM has actually chosen cut_to_fit vs
      raise_appetite. Setting `fork` here to either enum value pre-emptively
      would misread as "engine already decided" to any caller checking
      `fork is not None` as its divergence test.
    - Do NOT set `xl_exit` from this module. It is engineered identically to
      `fork`: this module always emits `None`, and the sizing skill fills it
      once the PM has actually chosen among the four XL exits (`split` /
      `shape` / `roadmap` / `accept_multi_session`). A `None` `xl_exit` is a
      legitimate open state — it means the PM has not chosen yet, never that
      the multi-session exit was accepted by default.
    - Do NOT let `premise_unproven` / `premise_not_applicable` alter `route`
      or populate `xl_exit`. The detent is advisory (warn, never block) —
      it is emitted and named, never a routing input. Do NOT add it to the
      `express_lane` short-circuit; that path returns before any detent
      computation by design (D3) and stays exactly as-is.
"""
from __future__ import annotations

from typing import Any, Optional

# Reuses the loe.tshirt XS-XXL enum + weights verbatim (schema-mandated, D1.2
# — "do not invent a parallel scale"). See coordinator/schemas/
# completion-entry.schema.json loe.tshirt for the canonical source. XXL is
# the sixth notch (2026-08-07 sizing-ladder-xxl-notch-and-goal-setting-route
# plan, C1) — it routes to `goal-setting`, not `pm-decision`; see
# `_BASE_ROUTE_BY_TSHIRT`'s HARD GATE comment for why a new KEY is an
# extension of this table's domain, not an override of it.
TSHIRT_ORDER = ["XS", "S", "M", "L", "XL", "XXL"]
TSHIRT_WEIGHT = {"XS": 1, "S": 2, "M": 4, "L": 8, "XL": 16, "XXL": 32}

APPETITE_ENUM = ("small", "medium", "large")
ROUTE_ENUM = ("dispatch", "spec-dispatch", "shape", "plan", "roadmap", "pm-decision", "goal-setting")
FORK_ENUM = ("cut_to_fit", "raise_appetite")
XL_EXIT_ENUM = ("split", "shape", "roadmap", "accept_multi_session")
DETENT_ENUM = (
    "appetite_conform",
    "appetite_exceeded",
    "route_boundary_crossed",
    "scope_boundary_acknowledged",
    "pm_decision_pending",
    "premise_unproven",
    "premise_not_applicable",
    "post_size_prompt_pending",
    "xxl_unprobed",
    "goal_setting_pm_gated",
)

PREMISE_PROVENANCE_ENUM = ("executed", "read", "not-applicable", "unrecorded")

# Appetite budget ceiling, expressed as the heaviest tshirt weight the
# budget comfortably absorbs without the estimate being flagged as
# exceeding it. Small/medium/large map onto the same coarse bands the
# sizing skill's gut-read step already uses.
#
# DELIBERATELY NOT widened for XXL (2026-08-07 sizing-ladder-xxl-notch-and-
# goal-setting-route plan, C1 Part B): `large` topping out at "XL" is what
# makes an XXL correctly read `appetite_exceeded` against the largest
# budget. Widening this to include "XXL" would silence exactly the
# divergence signal the lobby exists to surface — do not "fix" this.
_APPETITE_CEILING_TSHIRT = {"small": "S", "medium": "M", "large": "XL"}

# Base route by resolved tshirt weight, before the D5 shape-entry gate is
# applied to the "large" (L/XL) band. This IS the routing table AC4/AC6
# require to live in the engine, not in skill prose.
#
# EM-ratified interpretation of the boundary the plan delegates to this
# engine (D4) without dictating specific tshirt cuts (Finding 3, code-reviewer
# 2026-07-24): XS = trivial -> dispatch (no plan ceremony earned); M/L =
# decision-weight -> plan (schema `route` description: "plan= decision-weight
# work needing coordinator:plan"). No PM ask required for that original cut
# — the plan explicitly delegates the table's specific cuts to the engine,
# this is that delegation being exercised.
#
# S and XL were re-cut by explicit PM direction (2026-07-30), narrowing the
# original XS/S and L/XL bands: S now routes `spec-dispatch` — a light plan
# artifact (substrate verification + scaffold-plus-commit + cross-plan
# conflict scan still run; the four-lens body composition and the Opus plan
# review do not) — rather than sharing XS's no-artifact dispatch. XL no
# longer auto-routes `roadmap`; it routes `pm-decision`, because
# initiative-scale work has four legitimate exits (split / shape / roadmap /
# accept_multi_session) and picking one of them without the PM is exactly
# the in-the-head routing this table exists to discharge — see `route()`'s
# `xl_exit` handling and the module docstring's "Route table" note.
#
# HARD GATE — this table has no named-reason override and no ratifier
# (PM directive 2026-07-30). Two sanctioned correction mechanisms already
# exist, and both change the *input* (the size) rather than the *output*
# (the route): `_apply_symmetric_resize`, which fires before the route
# resolves, and the plan->sizing return edge (the caller's job, not this
# module's), which fires after substrate verification has produced real
# evidence. A free-text route override on top of this table would restore
# exactly the in-the-head routing the lobby exists to discharge — if the
# route feels wrong, the size read was wrong: fix the size, with evidence,
# and let the table re-resolve. XL is not an exception: `pm-decision` is a
# routed outcome, and the PM's pick lands in `xl_exit` — that is not an
# override of this map, it is the map's own designed terminal for XL.
#
# Adding a KEY for a new size (XXL, below) is an EXTENSION of this table's
# domain, not an override of the HARD GATE above — the gate governs the
# output for a given size, not the set of sizes the table has an opinion
# about. XXL routes straight to `goal-setting` rather than halting at
# `pm-decision` because XL has FOUR legitimate exits (split / shape /
# roadmap / accept_multi_session) and picking one without the PM is exactly
# the in-the-head routing this table exists to discharge — whereas XXL has
# ONE room, so routing it is a determinate map, not a guess. The relocated
# PM gate is made observable by the `goal_setting_pm_gated` detent proposed
# in C0 (2026-08-07 sizing-ladder-xxl-notch-and-goal-setting-route plan) and
# recorded by C2 below — this comment does not claim the gate "relocates"
# as an already-true fact independent of that detent existing.
_BASE_ROUTE_BY_TSHIRT = {
    "XS": "dispatch",
    "S": "spec-dispatch",
    "M": "plan",
    "L": "plan",
    "XL": "pm-decision",
    "XXL": "goal-setting",
}

_LARGE_TSHIRTS = ("L", "XL", "XXL")

# "M and above" — any TSHIRT_ORDER notch added above M MUST be added here
# too. See state/sizings/2026-08-07-sizing-ladder-xxl-notch-and-goal-setting.yaml
# (XXL notch). Distinct from `_LARGE_TSHIRTS` above —
# that band gates the premise-provenance advisory; this one gates the
# post-size open-appetite prompt. Do not couple the two (see the appetite
# plan's Anti-scope).
_POST_SIZE_PROMPT_TSHIRTS = ("M", "L", "XL", "XXL")


class SizingAssembleError(ValueError):
    """Raised for a malformed input to route() — a usage error, not a
    business-logic divergence (divergence is expressed via `fork`, never an
    exception)."""


def _validate_tshirt(tshirt: str) -> None:
    if tshirt not in TSHIRT_WEIGHT:
        raise SizingAssembleError(
            f"estimate.tshirt must be one of {TSHIRT_ORDER}, got {tshirt!r}"
        )


def _step_tshirt(tshirt: str, delta: int) -> str:
    # Clamp mechanism unchanged by the XXL notch (2026-08-07 sizing-ladder-
    # xxl-notch-and-goal-setting-route plan, C1 Part B) — it correctly
    # bounds a one-notch step to the ladder's ends. Behaviour DOES change,
    # though: a `raise` probe at XL was a no-op before the sixth notch
    # (clamped at the old top) and is now a promotion XL -> XXL ->
    # `goal-setting`. This is the substrate fact C2's `xxl_unprobed`
    # predicate is built on.
    idx = TSHIRT_ORDER.index(tshirt)
    new_idx = max(0, min(len(TSHIRT_ORDER) - 1, idx + delta))
    return TSHIRT_ORDER[new_idx]


def _validate_appetite(appetite: Optional[str]) -> None:
    """Fails loud on a malformed `appetite` regardless of whether the
    caller's branch will end up consuming it — same unconditional-validation
    property as `_validate_probe_signal` / `_validate_premise_provenance`:
    optional, validated when present, fails loud unconditionally. Appetite is
    no longer collected before a size is delivered (PM directive
    2026-08-07); `None` is the default, expected path, not a malformed
    input."""
    if appetite is not None and appetite not in APPETITE_ENUM:
        raise SizingAssembleError(f"appetite must be one of {APPETITE_ENUM}, got {appetite!r}")


def _validate_probe_signal(probe_signal: Optional[str]) -> None:
    """Fails loud on a malformed `probe_signal` regardless of whether the
    caller's branch (e.g. express_lane) will end up consuming it — every
    other malformed-input path in this module validates unconditionally;
    probe_signal must not be the one exception (Finding 5, code-reviewer
    2026-07-24)."""
    if probe_signal is not None and probe_signal not in ("collapse", "raise"):
        raise SizingAssembleError(
            f"probe_signal must be one of (None, 'collapse', 'raise'), got {probe_signal!r}"
        )


def _validate_premise_provenance(premise_provenance: Optional[str]) -> None:
    """Fails loud on a malformed `premise_provenance` regardless of whether
    the caller's branch (e.g. express_lane) will end up consuming it — same
    unconditional-validation property as `_validate_probe_signal` (Finding 5,
    code-reviewer 2026-07-24): validation fires BEFORE the express_lane
    short-circuit, not only on paths that reach detent computation."""
    if premise_provenance is not None and premise_provenance not in PREMISE_PROVENANCE_ENUM:
        raise SizingAssembleError(
            f"premise_provenance must be one of (None, {PREMISE_PROVENANCE_ENUM}), "
            f"got {premise_provenance!r}"
        )


def _apply_symmetric_resize(tshirt: str, probe_signal: Optional[str]) -> tuple[str, bool]:
    """Applies the Finding-2-mandated symmetric resize. Returns (resized_tshirt,
    changed). `probe_signal` is None (no probe ran / gut-read stands),
    "collapse" (over-read, move one band down), or "raise" (under-read, move
    one band up) — supplied by the caller's on-demand substrate probe.
    Assumes `probe_signal` was already validated by `_validate_probe_signal`."""
    if probe_signal is None:
        return tshirt, False
    if probe_signal == "collapse":
        resized = _step_tshirt(tshirt, -1)
    else:
        resized = _step_tshirt(tshirt, +1)
    return resized, resized != tshirt


def route(
    *,
    appetite: Optional[str] = None,
    estimate: dict[str, Any],
    scout_evidence: Optional[list[str]] = None,
    express_lane: bool = False,
    probe_signal: Optional[str] = None,
    jtbd_unclear: bool = False,
    well_trodden_step_change: bool = False,
    premise_provenance: Optional[str] = None,
) -> dict[str, Any]:
    """Resolves the sizing-object's route/detents/fork fields (C1 shape).

    Args:
        appetite: one of small|medium|large (Shape-Up budget enum), or
            `None` (the default). Appetite is never collected before a size
            is delivered — `None` means no budget has been stated yet, not a
            missing required value. When `None`, neither `appetite_conform`
            nor `appetite_exceeded` fires, `route` is identical to what it
            would be for any stated appetite (AC2), and a resized t-shirt at
            or above `"M"` sets `post_size_prompt_pending` instead (see
            module docstring's "Appetite<->estimate reconciliation" note).
        estimate: dict with at least "tshirt" (XS..XXL); "provisional" is
            echoed back true unconditionally per the schema's const:true.
        scout_evidence: provenance passthrough only — NOT parsed for signal
            (see module docstring's negative-spec).
        express_lane: D3 short-circuit — trivial ask, no ceremony.
        probe_signal: None | "collapse" | "raise" — the symmetric resize
            direction the caller's substrate probe determined (Finding 2).
        jtbd_unclear: D5 shape-entry condition (a).
        well_trodden_step_change: D5 shape-entry condition (b).
        premise_provenance: None | "executed" | "read" | "not-applicable" |
            "unrecorded" — where the sizing's underlying mechanism claim
            came from. Advisory only (warn, never block): `read` at a
            resized L/XL/XXL sets `premise_unproven`; `not-applicable` at a
            resized L/XL/XXL sets `premise_not_applicable`. Never alters
            `route` or `xl_exit` (see module docstring's "Premise
            provenance" note).

    Returns:
        A dict: {route, detents, fork, xl_exit, resolved_estimate,
        scout_evidence, narration, next_move} — READ-ONLY, mutates nothing.
    """
    _validate_appetite(appetite)
    tshirt = estimate.get("tshirt")
    _validate_tshirt(tshirt)
    _validate_probe_signal(probe_signal)
    _validate_premise_provenance(premise_provenance)
    scout_evidence = list(scout_evidence or [])

    if express_lane:
        return {
            "route": "dispatch",
            "detents": [],
            "fork": None,
            "xl_exit": None,
            "resolved_estimate": {"tshirt": tshirt, "provisional": True},
            "scout_evidence": scout_evidence,
            "narration": "Express lane: trivial ask, no sizing ceremony.",
            "next_move": "Dispatch directly. No sizing-object persisted (D3).",
        }

    resized_tshirt, resize_changed = _apply_symmetric_resize(tshirt, probe_signal)

    detents: list[str] = []

    pre_resize_route = _BASE_ROUTE_BY_TSHIRT[tshirt]
    post_resize_route = _BASE_ROUTE_BY_TSHIRT[resized_tshirt]
    if resize_changed and pre_resize_route != post_resize_route:
        detents.append("route_boundary_crossed")

    resized_weight = TSHIRT_WEIGHT[resized_tshirt]

    # `fork` stays None from this module always — appetite_exceeded is the
    # sole divergence signal. `fork` is the sizing skill's RESOLUTION slot,
    # filled only once the PM has picked cut_to_fit vs raise_appetite (see
    # module docstring's "Appetite<->estimate reconciliation" note).
    fork: Optional[str] = None
    # `xl_exit` stays None from this module always — same design as `fork`
    # (see negative-spec). The sizing skill fills it once the PM has picked
    # one of the four XL exits.
    xl_exit: Optional[str] = None
    if appetite is not None:
        ceiling_tshirt = _APPETITE_CEILING_TSHIRT[appetite]
        ceiling_weight = TSHIRT_WEIGHT[ceiling_tshirt]
        if resized_weight > ceiling_weight:
            detents.append("appetite_exceeded")
        else:
            detents.append("appetite_conform")
    elif resized_tshirt in _POST_SIZE_PROMPT_TSHIRTS:
        detents.append("post_size_prompt_pending")

    if (resized_tshirt in _LARGE_TSHIRTS and jtbd_unclear) or well_trodden_step_change:
        resolved_route = "shape"
        detents.append("scope_boundary_acknowledged")
    else:
        resolved_route = _BASE_ROUTE_BY_TSHIRT[resized_tshirt]

    if resolved_route == "pm-decision":
        detents.append("pm_decision_pending")

    # `goal_setting_pm_gated` (2026-08-07 sizing-ladder-xxl-notch-and-goal-
    # setting-route plan, C0 item 2 / C2): the observable marker equivalent
    # to `pm_decision_pending` above, for the relocated PM gate — an XXL
    # sizing-object otherwise carries no machine-readable sign that
    # `coordinator:goal-setting` is PM-gated (the earlier claim that "the PM
    # gate relocates into the room" was true in prose but false in this
    # module's observable output; this detent is the fix). Advisory only,
    # same append shape as every other detent here — never alters `route`.
    if resolved_route == "goal-setting":
        detents.append("goal_setting_pm_gated")

    # `xxl_unprobed` (same plan, C2 item 2, Key-decision section — the
    # ACCEPTED counter-proposal to example-doctrine-repo's originally specced
    # `probe_signal is None and not scout_evidence`, inbound memo
    # `2026-08-07-example-doctrine-repo-em-xxl-notch-four-answers.md` item 1). Testing
    # `probe_signal is None` reads "a probe ran, therefore the size is
    # trustworthy" — but `--probe-signal raise` is a caller ASSERTION, not
    # evidence, and `_step_tshirt`'s clamp (see its own comment) means a
    # raise probe reaches XXL from either `--tshirt XL` (promotion) or
    # `--tshirt XXL` (no-op clamp) — both are the exact false-HIGH this
    # advisory exists to catch, and example-doctrine-repo's original predicate exempts both.
    # This predicate tests `scout_evidence` EMPTINESS only, never its
    # contents (module docstring's negative-spec).
    if resized_tshirt == "XXL" and not scout_evidence:
        detents.append("xxl_unprobed")

    # Premise-provenance detent (advisory, warn-never-block — DR-068
    # precedent): keyed on RESIZED size, never resolved route, so it fires
    # identically on shape/pm-decision/goal-setting/plan L/XL/XXL (see
    # module docstring).
    if resized_tshirt in _LARGE_TSHIRTS:
        if premise_provenance == "read":
            detents.append("premise_unproven")
        elif premise_provenance == "not-applicable":
            detents.append("premise_not_applicable")

    if resolved_route not in ROUTE_ENUM:  # pragma: no cover - defensive, table-driven
        raise SizingAssembleError(f"internal: resolved route {resolved_route!r} not in {ROUTE_ENUM}")

    narration_bits = [f"Resolved tshirt {resized_tshirt}"]
    if resize_changed:
        narration_bits.append(f"(probe {probe_signal}d from {tshirt})")
    if appetite is not None:
        narration_bits.append(f"against appetite={appetite} -> route={resolved_route}.")
    else:
        narration_bits.append(f"-> route={resolved_route}.")
    narration = " ".join(narration_bits)

    # Precedence, in order (settled design §3/§4; goal-setting branch added
    # 2026-08-07 sizing-ladder-xxl-notch-and-goal-setting-route plan, C2):
    #   1. appetite_exceeded + pm-decision together -> ONE combined message,
    #      so the EM makes a single PM ask rather than two separate ones.
    #   2. appetite_exceeded alone -> unchanged cut-vs-raise fork message.
    #      NOTE (review-integrator, appetite plan P1 fix): this arm also
    #      catches the goal-setting route (an XXL never resolves
    #      pm-decision, so rule 1 never fires for it) -- the goal-setting/
    #      PM-gated framing is APPENDED to this arm's text below rather than
    #      going unreachable; see the append block right after this chain.
    #   3. pm-decision alone -> surface the XL exits.
    #   4. goal-setting -> OKR-programme framing, PM-gated. Only reached when
    #      appetite is absent/conforming -- reachable, not shadowed, when
    #      rule 2 above doesn't claim the cell first.
    #   5. shape -> unchanged.
    #   6. spec-dispatch -> light-plan-lane framing.
    #   7. else -> unchanged generic "Route to X."
    if "appetite_exceeded" in detents and resolved_route == "pm-decision":
        next_move = (
            "Estimate exceeds the appetite budget AND resolves to an XL. Make ONE combined "
            "PM ask covering both forks: (1) cut_to_fit vs raise_appetite for the appetite "
            "divergence, and (2) which XL exit applies — split / shape / roadmap / "
            "accept_multi_session. The PM's XL pick is recorded in the sizing-object's "
            "xl_exit field; the engine never auto-selects it, and a null xl_exit never means "
            "accept."
        )
    elif "appetite_exceeded" in detents:
        next_move = (
            "Estimate exceeds the appetite budget. Surface the cut-vs-raise fork to the "
            "PM (cut_to_fit vs raise_appetite) — do not auto-resolve."
        )
    elif resolved_route == "pm-decision":
        next_move = (
            "XL resolves to pm-decision, not a room. Surface the exits to the PM — "
            "shape (JTBD unclear), roadmap (spans >=2 workstreams / needs an initiative "
            "FK), or accept_multi_session (one coherent job, simply large, PM assents). "
            "The choice is recorded in the sizing-object's xl_exit field; the engine does "
            "not auto-select, and a null xl_exit never means accept."
        )
    elif resolved_route == "goal-setting":
        next_move = (
            "XXL resolves to goal-setting, not a plan. This ask is OKR-programme scale — "
            "route to coordinator:goal-setting, which is PM-gated."
        )
    elif resolved_route == "shape":
        next_move = "Route to /shape for PM problem-alignment before plan/roadmap."
    elif resolved_route == "spec-dispatch":
        next_move = (
            "Route to a light plan artifact (scope_mode: spec-dispatch): substrate "
            "verification, scaffold-plus-commit, and the cross-plan conflict scan still "
            "run; the four-lens body composition and the Opus plan review do not. Then "
            "dispatch."
        )
    else:
        next_move = f"Route to {resolved_route}."

    # Goal-setting framing, appended when the goal_setting_pm_gated detent
    # fired but rule 2 above (appetite_exceeded alone) already claimed the
    # base next_move text for this cell -- an XXL never resolves
    # pm-decision, so rule 1's combined-ask arm never applies here; without
    # this append the goal-setting/PM-gated framing the XXL plan's C2 item
    # 3(b) specced would silently drop for a caller who volunteers an
    # appetite (review-integrator P1 fix, coordinator:code-reviewer
    # 2026-08-07). APPEND, not a new precedence branch — matches the
    # append-not-reorder discipline the other advisories below use, and
    # keeps the closed cut_to_fit/raise_appetite fork text intact (AC6: the
    # volunteered-appetite path keeps that closed fork, unlike the absent-
    # appetite open question).
    if "goal_setting_pm_gated" in detents and "appetite_exceeded" in detents:
        next_move += (
            " This also resolves to goal-setting (XXL, OKR-programme scale) — route to "
            "coordinator:goal-setting, which is PM-gated, in addition to the appetite "
            "fork above."
        )

    # Open appetite prompt — appended to whichever branch above was selected
    # (never a new precedence branch), matching the advisory appends below.
    # Suppressed at `resolved_route == "pm-decision"`: that branch already
    # asks the PM to pick among the XL exits (which include an open-ended
    # "split it" resolution), so the open appetite question is already
    # being asked there (precedence rule #1 — ONE combined PM ask, not two).
    # Ordered BEFORE the xxl_unprobed/premise advisories: those are
    # explicitly non-actionable ("does not alter the route above"), so a
    # live PM question after them would read as qualified by them.
    if "post_size_prompt_pending" in detents and resolved_route != "pm-decision":
        next_move += (
            f" Looks like a {resized_tshirt}, shall we go with that or want to split it, "
            "cut it, what's up?"
        )

    # `xxl_unprobed` advisory statement — appended to whichever branch above
    # was selected, BEFORE the premise-provenance advisory (2026-08-07
    # sizing-ladder-xxl-notch-and-goal-setting-route plan, C2 item 5
    # ordering: route text -> appetite question -> xxl_unprobed -> premise
    # advisory). ADVISORY, warn-never-block (DR-277) — never withholds the
    # route above.
    if "xxl_unprobed" in detents:
        next_move += (
            " ADVISORY (warn, never block; does not alter the route above): "
            "this XXL resolved with no scout_evidence recorded. Discharge by "
            "citing inline evidence for the size — never by producing a "
            "spike-result artifact."
        )

    # Premise-provenance advisory statement — appended to whichever branch
    # above was selected (never a new branch in the precedence chain).
    # ADVISORY, stated in words: it is discharged by citing inline evidence
    # that the mechanism was executed, never by producing a spike-result
    # artifact (structural-in-mechanism, never-in-ceremony — memo PM ruling
    # 2). Never withholds the route above; warn, never block (DR-068).
    if "premise_unproven" in detents:
        next_move += (
            " ADVISORY (warn, never block; does not alter the route above): "
            "this size's underlying mechanism premise was READ, not executed. "
            "Discharge by citing inline evidence that the mechanism was "
            "actually executed — never by producing a spike-result artifact."
        )
    elif "premise_not_applicable" in detents:
        next_move += (
            " ADVISORY (warn, never block; does not alter the route above): "
            "this size's underlying mechanism premise was claimed "
            "not-applicable. Discharge by citing inline evidence for why no "
            "mechanism claim applies here — never by producing a "
            "spike-result artifact."
        )

    return {
        "route": resolved_route,
        "detents": detents,
        "fork": fork,
        "xl_exit": xl_exit,
        "resolved_estimate": {"tshirt": resized_tshirt, "provisional": True},
        "scout_evidence": scout_evidence,
        "narration": narration,
        "next_move": next_move,
    }


EXIT_OK = 0
EXIT_BUSINESS_FAIL = 1
EXIT_USAGE = 2
EXIT_TRANSPORT_FAIL = 3


def _usage(prog: str) -> int:
    print(
        f"{prog}: usage: {prog} --tshirt <XS|S|M|L|XL|XXL> "
        "[--appetite small|medium|large] "
        "[--express-lane] [--probe-signal collapse|raise] [--jtbd-unclear] "
        "[--well-trodden-step-change] "
        "[--premise-provenance executed|read|not-applicable|unrecorded] "
        "[--scout-evidence <str> ...]",
        file=__import__("sys").stderr,
    )
    return EXIT_USAGE


def main(argv: list[str]) -> int:
    import json
    import sys

    prog = "sizing-assemble"
    appetite = None
    tshirt = None
    express_lane = False
    probe_signal = None
    jtbd_unclear = False
    well_trodden_step_change = False
    premise_provenance = None
    scout_evidence: list[str] = []

    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--appetite" and i + 1 < len(argv):
            appetite = argv[i + 1]
            i += 2
        elif tok == "--tshirt" and i + 1 < len(argv):
            tshirt = argv[i + 1]
            i += 2
        elif tok == "--express-lane":
            express_lane = True
            i += 1
        elif tok == "--probe-signal" and i + 1 < len(argv):
            probe_signal = argv[i + 1]
            i += 2
        elif tok == "--jtbd-unclear":
            jtbd_unclear = True
            i += 1
        elif tok == "--well-trodden-step-change":
            well_trodden_step_change = True
            i += 1
        elif tok == "--premise-provenance" and i + 1 < len(argv):
            premise_provenance = argv[i + 1]
            i += 2
        elif tok == "--scout-evidence" and i + 1 < len(argv):
            scout_evidence.append(argv[i + 1])
            i += 2
        elif tok == "--json":
            i += 1
        else:
            print(f"{prog}: unrecognized argument {tok!r}", file=sys.stderr)
            return _usage(prog)

    if tshirt is None:
        return _usage(prog)

    try:
        decision = route(
            appetite=appetite,
            estimate={"tshirt": tshirt, "provisional": True},
            scout_evidence=scout_evidence,
            express_lane=express_lane,
            probe_signal=probe_signal,
            jtbd_unclear=jtbd_unclear,
            well_trodden_step_change=well_trodden_step_change,
            premise_provenance=premise_provenance,
        )
    except SizingAssembleError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - structural backstop, mirrors pickup_assemble
        print(f"{prog}: unexpected failure: {exc}", file=sys.stderr)
        print(json.dumps({"error": str(exc), "transport_failure": True}))
        return EXIT_TRANSPORT_FAIL

    print(json.dumps(decision, indent=2, sort_keys=True))
    return EXIT_OK
