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

Contract (frozen, reviewed): coordinator-content-repo coordinator/docs/wiki/computed-skills.md
DR-090 (coordinator-content-repo docs/decisions/DR-090-the-unit-of-extraction-is-the-mechanical-step.md)
Spec backlink: coordinator-content-repo coordinator-content-repo:pln-sizing-skill-sizing-object-art-3eb413, chunk C7 (Design D4/D5)
Input shape: coordinator-content-repo coordinator/schemas/sizing-object.schema.json (C1)
Registration seam: this module ships no bash veneer and needs none — it is
consumed directly by the `coordinator/bin/sizing-assemble` trampoline (mirrors
`coordinator/bin/pickup-assemble`'s direct-import template-variant #1).

Measured process time (2026-09-06 engine-half-roadmap-verification-debt
plan, C3): a fresh in-tree `getrusage(RUSAGE_CHILDREN)` measurement of
`route()` through its real CLI on 2026-09-17 (k=20, fresh process per
sample) read ~56.8ms process time, well under the ≤200ms budget ceiling
(DR-344 §7) and under the 120.3ms figure `roadmap_planning_assemble` /
`sprint_planning_assemble` cite as a copied build-time reference target.
The 428.1ms regression a 2026-08-23 handoff attributed to this module was
against the klabauter mirror's build, not this tree — its own Session
Ledger later refuted that attribution as mirror-side, which this
measurement corroborates: no perf cut is needed here.

Measured process time, C6 (2026-09-27-sizing-carries-exit-criterion-and-
interaction-mode plan): a fresh in-tree `getrusage(RUSAGE_CHILDREN)`
measurement (k=20, fresh process per sample; this session's container, not
the ~56.8ms baseline's own machine, so the two absolute figures are not
directly comparable — the DELTA is what this note establishes) of
`main()` through the real CLI read ~44ms for `import
coordinator_core.sizing_assemble` alone versus ~51ms for a full
`--tshirt M --exit-criterion x --interaction-mode pm` run (a ~7ms delta,
regardless of whether `--interaction-mode` is passed — `route()`'s own
`_validate_interaction_mode` lazily imports `mode_resolution` on every
call, flag or not). `mode_resolution`'s own cold-import cost, measured
standalone the same way (`import coordinator_core.session.mode_resolution`,
k=20, fresh process), read ~47ms against a ~12ms bare-interpreter floor in
this same container (~35ms marginal). The ~7ms observed delta here is
comfortably inside that import-cost envelope, and the absolute run time
(~51ms) stays well under the ≤200ms budget ceiling (DR-344 §7) — no perf
cut needed.

READ-ONLY, by construction (`route()`; `write_back()`, reached only through
`main()`'s `--write <state/sizings/x.yaml>`, is the one writer): `route()` only reads its arguments — it never
touches disk, never writes a sizing-object, never shells out; its lazy,
function-local `mode_resolution` import (`_validate_interaction_mode`)
reaches only that module's pure constants/validation, never
`resolve_mode`'s fleet-record read. `main()` is the one caller that reads
disk here, through `_resolve_interaction_mode_and_source`'s
`resolve_mode`/`read_fleet_mode` calls, when no `--interaction-mode` flag
was given. The caller (the sizing skill / a future scaffolder) is
responsible for persisting the returned decision into a
`state/sizings/*.yaml` sizing-object; this module computes the routing
fields (`route`, `detents`, `fork`) of that object, plus transport-only
convenience keys (`resolved_estimate`, `scout_evidence`, `narration`,
`next_move`, `exit_criterion`, `interaction_mode`,
`interaction_mode_source`, `touchpoints`) the caller doesn't have to
recompute — the schema-persisted fields are `route`/`detents`/`fork`/
`exit_criterion`/`interaction_mode`, not the full return payload (see
`route()`'s own docstring for the complete return shape).

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
2026-08-05-coordinator-content-repo-em-premise-provenance-detent-sizing-assemble.md).

Premise provenance (advisory detent, warn-never-block — DR-068 precedent;
cross-repo memo 2026-08-05-coordinator-content-repo-em-premise-provenance-detent-sizing-
assemble.md): `premise_provenance` is one of `executed` | `read` |
`not-applicable` | `unrecorded` | None, validated unconditionally by
`_validate_premise_provenance` (same unconditional-validation property as
`_validate_probe_signal` — Finding 5, code-reviewer 2026-07-24). When
provenance is `read` AND the RESIZED t-shirt is in `_PREMISE_DETENT_TSHIRTS`
(M/L/XL/XXL), the `premise_unproven` detent fires; when provenance is
`not-applicable` under the same size gate, `premise_not_applicable` fires
instead. Both land in the same `DETENT_ENUM` widen (never staggered — the
DoE-side schema parity test asserts symmetric set equality against this
tuple). The gate keys on resized SIZE, not resolved ROUTE, so it fires
identically on `plan`-, `shape`-, `pm-decision`-, and `goal-setting`-routed
M/L/XL/XXL — routing away from `plan` does not reduce the premise-truth-
value multiplier the detent exists to name. `_PREMISE_DETENT_TSHIRTS` is a
constant DISTINCT from `_LARGE_TSHIRTS`, which also gates the shape-route
condition `(resized_tshirt in _LARGE_TSHIRTS and jtbd_unclear)`; widening
`_LARGE_TSHIRTS` to include "M" instead of introducing this sibling
constant would silently reroute an M-sized `jtbd_unclear` sizing from
`plan` to `shape`. Do not merge the two constants. Like
`appetite_exceeded` and `pm_decision_pending`,
this detent NEVER alters `route` and NEVER populates `xl_exit` — it is
advisory only, discharged by citing executed evidence inline, never by
producing a spike-result artifact (a structural-in-mechanism,
never-in-ceremony design per the memo's PM ruling 2).

Boundary-in-notch (advisory detent, warn-never-block — same shape as premise
provenance above; cross-repo memo 2026-08-10-coordinator-content-repo-em-sizing-guard-
flags.md): `boundary_in_notch` is one of `yes` | `no` | None, validated
unconditionally by `_validate_boundary_in_notch`. A `yes` answers "a
cross-repo boundary, memo, relay, or assent gate contributed to this notch"
and fires `boundary_counted_in_notch`. The rule it makes checkable is
already written — DoE `coordinator/skills/sizing/SKILL.md` § *A cross-team
dependency is a gate, not a size*: a MEMO (one ask, the sibling implements
on their own surface) does not move the notch; NEGOTIATED CO-DESIGN, where
the shared contract itself is the unknown, does. The engine cannot tell
those apart — the flag has two values, not three — so the detent names the
tell and hands the discriminator back to the EM: collapse the notch, or
record the co-design justification. That is the whole point of the ask.
Prose guards are read at flow Step 1, before the EM has evidence to check
them against, and nothing re-presents them at Step 3 when the notch is
actually committed to a flag; a required answer reaching this validator is
what closes that gap (the memo's own framing of why `--premise-provenance`
works and its prose siblings do not).

DELIBERATELY NOT size-gated, unlike `_PREMISE_DETENT_TSHIRTS`. A boundary
counted into the notch is doctrine-wrong at XS exactly as at XXL — the size
is the thing the answer is suspected of having inflated, so gating the check
on it would exempt the very reads the guard exists to catch, and would have
exempted nothing in the motivating incident (an S sized XL). The detent is
advisory and cheap; there is no noise argument that survives that.

Scout-evidence kind (advisory detent, warn-never-block): `scout_evidence_kind`
is one of `mention-count` | `change-set` | `site-count` | None, validated
unconditionally by `_validate_scout_evidence_kind`. `mention-count` fires
`scout_evidence_mention_count` — a grep count is not a change-set until
someone has asked what a compatibility layer absorbs. Motivating incident
(same memo): a scout returned ~108 files that MENTION a literal, and that
count was passed as though it described a change-set; under a back-compat
shim every one of those files resolves unchanged.

This is a TYPED FIELD BESIDE the free text, and is emphatically NOT a licence
to start reading `scout_evidence` strings (see the negative-spec below, which
is unchanged and still binds). The discriminator has to be a typed flag
PRECISELY BECAUSE that rule holds: the kind of a piece of evidence is not
recoverable from its prose by this module or by anyone. `scout_evidence_kind`
describes the evidence; it never reaches into it.

Negative-spec:
    - Do NOT add a mutating code path here. This module returns data; it
      never writes state/sizings/*.yaml itself.
    - Do NOT let `boundary_counted_in_notch` or `scout_evidence_mention_count`
      alter `route` or populate `xl_exit`, and do NOT add either to the
      `express_lane` short-circuit — identical advisory contract to the
      premise detents (D3: that path returns before any detent computation).
    - Do NOT infer `scout_evidence_kind` from the CONTENTS or the LENGTH of
      the `scout_evidence` list. A 108-element list is not a mention-count and
      a 1-element list is not a change-set; inferring either would be the
      free-text parsing the negative-spec below forbids, wearing a counting
      costume. The kind is supplied by the caller who ran the scout, or it is
      absent.
    - Do NOT make `boundary_in_notch="yes"` collapse the notch automatically.
      `yes` is collapsible ONLY when no co-design is involved, and this module
      cannot see which — auto-collapsing would resize on an unread
      discriminator, which is the § Hard gate violation (route corrected by
      changing the size WITHOUT evidence) rather than a shortcut around it.
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

from datetime import date
from pathlib import Path
from typing import Any, Optional

from coordinator_core.roadmap_planning_assemble.scaffold_directive import (
    Flag,
    build_scaffold_directive,
)

# sizing-object` at write time; that refusal is the EXECUTOR's problem
_SIZING_OBJECT_FLAG_SPEC: tuple[Flag, ...] = (
    Flag("--title", "title", required=False),
    # C6: threaded so `d-scaffold-sizing-object` scaffolds the PM-facing
    # exit criterion and the mode this sizing ran under, matching C3's
    # `_scaffold_sizing --exit-criterion/--interaction-mode` flags exactly.
    Flag("--exit-criterion", "exit_criterion", required=False),
    Flag("--interaction-mode", "interaction_mode", required=False),
    Flag("--tshirt", "tshirt", required=False),
    Flag("--route", "route", required=False),
    Flag("--premise", "premise", required=False),
    Flag("--premise-evidence", "premise_evidence", required=False),
)

# `unrecorded` is migration-only provenance that doc-new refuses on a new scaffold.
_RECORDABLE_PREMISES = ("executed", "read", "not-applicable")


def _slug(text: str) -> str:
    out = []
    prev_dash = False
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-") or "untitled"


_LABEL_MAX_WORDS = 8
_SLUG_MAX_CHARS = 60


def _short_label(name: Optional[str], intent: Optional[str]) -> Optional[str]:
    """The sizing object's title: `name` as given, else the first
    `_LABEL_MAX_WORDS` words of `intent` (marked with an ellipsis when cut)."""
    if name and name.strip():
        return name.strip()
    if not intent or not intent.strip():
        return None
    words = intent.split()
    if len(words) <= _LABEL_MAX_WORDS:
        return " ".join(words)
    return " ".join(words[:_LABEL_MAX_WORDS]) + "..."


def _capped_slug(text: str) -> str:
    slug = _slug(text)
    if len(slug) <= _SLUG_MAX_CHARS:
        return slug
    cut = slug[:_SLUG_MAX_CHARS]
    return (cut.rsplit("-", 1)[0] if "-" in cut else cut).strip("-") or "untitled"


def _sizing_object_scaffold_directive(
    intent: Optional[str],
    name: Optional[str] = None,
    exit_criterion: Optional[str] = None,
    interaction_mode: Optional[str] = None,
    tshirt: Optional[str] = None,
    route: Optional[str] = None,
    premise: Optional[str] = None,
    premise_evidence: Optional[str] = None,
) -> dict[str, Any]:
    root = Path.cwd()
    today = date.today().isoformat()
    title = _short_label(name, intent)
    slug = _capped_slug(title) if title else "untitled"
    resolved: dict[str, Any] = {
        "title": title,
        "out": f"state/sizings/{today}-{slug}.yaml",
        "exit_criterion": exit_criterion,
        "interaction_mode": interaction_mode,
        "tshirt": tshirt,
        "route": route,
        "premise": premise if premise in _RECORDABLE_PREMISES else None,
        "premise_evidence": (premise_evidence or "").strip() or None,
    }
    directive = build_scaffold_directive(
        "d-scaffold-sizing-object",
        "sizing-object",
        resolved,
        _SIZING_OBJECT_FLAG_SPEC,
        root=root,
    )
    # doc-new refuses a sizing-object scaffold with no premise provenance and
    # evidence; naming them keeps the printed command from being one that fails.
    missing = [
        flag
        for flag, key in (
            ("--premise-provenance", "premise"),
            ("--premise-evidence", "premise_evidence"),
        )
        if resolved[key] is None
    ]
    # Provenance is threaded only with its evidence: the two are accepted
    # together or not at all, and a provenance-only directive would make the
    # decision's directives vary with a value `route()` promises never alters it.
    if "--premise-evidence" in missing:
        args = [a for a in directive["args"] if not a.startswith("--premise=")]
        directive["args"] = args
    if missing:
        directive["missing"] = missing
    return directive

# `_BASE_ROUTE_BY_TSHIRT`'s HARD GATE comment for why a new KEY is an
TSHIRT_ORDER = ["XS", "S", "M", "L", "XL", "XXL"]
TSHIRT_WEIGHT = {"XS": 1, "S": 2, "M": 4, "L": 8, "XL": 16, "XXL": 32}

APPETITE_ENUM = ("small", "medium", "large")
ROUTE_ENUM = (
    "dispatch",
    "spec-dispatch",
    "shape",
    "plan",
    "roadmap",
    "pm-decision",
    "goal-setting",
)

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
    # APPENDED AT THE END, in this order, never re-sorted — enum ORDER is
    # load-bearing against DoE's EQUAL_VERSION_SHAPE_DRIFT gate, and the
    "boundary_counted_in_notch",
    "scout_evidence_mention_count",
    # EQUAL_VERSION_SHAPE_DRIFT gate — never re-sort.
    "intent_em_elaborated",
    "precedent_shipped_before",
    "probe_raise_on_substrate_condition",
    "probe_raise_ask_scope_asserted",
    # EQUAL_VERSION_SHAPE_DRIFT gate.
    "probe_raise_on_breadth",
    # APPENDED by C6 (2026-09-27-sizing-carries-exit-criterion-and-interaction-mode),
    # never re-sorted — order is load-bearing against DoE's
    # EQUAL_VERSION_SHAPE_DRIFT gate, same as every prior append above.
    "exit_criterion_pending",
)

PREMISE_PROVENANCE_ENUM = ("executed", "read", "not-applicable", "unrecorded")

BOUNDARY_IN_NOTCH_ENUM = ("yes", "no")

SCOUT_EVIDENCE_KIND_ENUM = ("mention-count", "change-set", "site-count")

INTENT_SOURCE_ENUM = ("pm-verbatim", "em-elaborated")

# almost by construction. `shipped-before` is ADVISORY, never an auto-collapse —
PRECEDENT_ENUM = ("shipped-before", "novel")

# SUBSTRATE's condition, or the touchpoint BREADTH. These are different claims
PROBE_RAISE_BASIS_ENUM = ("ask-scope", "substrate-condition", "breadth")

_PROBE_RAISE_SUPPRESSION_DETENT = {
    "substrate-condition": "probe_raise_on_substrate_condition",
    "breadth": "probe_raise_on_breadth",
}

# DELIBERATELY NOT widened for XXL (2026-08-07 sizing-ladder-xxl-notch-and-
_APPETITE_CEILING_TSHIRT = {"small": "S", "medium": "M", "large": "XL"}

# Adding a KEY for a new size (XXL, below) is an EXTENSION of this table's
# HARD GATE.
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
# (XXL notch). Distinct from `_LARGE_TSHIRTS` above —
_POST_SIZE_PROMPT_TSHIRTS = ("M", "L", "XL", "XXL")

# (premise_unproven / premise_not_applicable). Distinct from `_LARGE_TSHIRTS`
# `(resized_tshirt in _LARGE_TSHIRTS and jtbd_unclear)`, so widening
# `_LARGE_TSHIRTS` itself to include "M" would silently reroute an M-sized
_PREMISE_DETENT_TSHIRTS = ("M", "L", "XL", "XXL")


class SizingAssembleError(ValueError):
    pass


def _validate_tshirt(tshirt: str) -> None:
    if tshirt not in TSHIRT_WEIGHT:
        raise SizingAssembleError(
            f"estimate.tshirt must be one of {TSHIRT_ORDER}, got {tshirt!r}"
        )


def _step_tshirt(tshirt: str, delta: int) -> str:
    idx = TSHIRT_ORDER.index(tshirt)
    new_idx = max(0, min(len(TSHIRT_ORDER) - 1, idx + delta))
    return TSHIRT_ORDER[new_idx]


def _validate_appetite(appetite: Optional[str]) -> None:
    if appetite is not None and appetite not in APPETITE_ENUM:
        raise SizingAssembleError(f"appetite must be one of {APPETITE_ENUM}, got {appetite!r}")


def _validate_probe_signal(probe_signal: Optional[str]) -> None:
    if probe_signal is not None and probe_signal not in ("collapse", "raise"):
        raise SizingAssembleError(
            f"probe_signal must be one of (None, 'collapse', 'raise'), got {probe_signal!r}"
        )


def _validate_premise_provenance(premise_provenance: Optional[str]) -> None:
    if premise_provenance is not None and premise_provenance not in PREMISE_PROVENANCE_ENUM:
        raise SizingAssembleError(
            f"premise_provenance must be one of (None, {PREMISE_PROVENANCE_ENUM}), "
            f"got {premise_provenance!r}"
        )


def _validate_boundary_in_notch(boundary_in_notch: Optional[str]) -> None:
    if boundary_in_notch is not None and boundary_in_notch not in BOUNDARY_IN_NOTCH_ENUM:
        raise SizingAssembleError(
            f"boundary_in_notch must be one of (None, {BOUNDARY_IN_NOTCH_ENUM}), "
            f"got {boundary_in_notch!r}"
        )


def _validate_scout_evidence_kind(scout_evidence_kind: Optional[str]) -> None:
    if scout_evidence_kind is not None and scout_evidence_kind not in SCOUT_EVIDENCE_KIND_ENUM:
        raise SizingAssembleError(
            f"scout_evidence_kind must be one of (None, {SCOUT_EVIDENCE_KIND_ENUM}), "
            f"got {scout_evidence_kind!r}"
        )


def _validate_intent_source(intent_source: Optional[str]) -> None:
    if intent_source is not None and intent_source not in INTENT_SOURCE_ENUM:
        raise SizingAssembleError(
            f"intent_source must be one of (None, {INTENT_SOURCE_ENUM}), got {intent_source!r}"
        )


def _validate_precedent(precedent: Optional[str]) -> None:
    if precedent is not None and precedent not in PRECEDENT_ENUM:
        raise SizingAssembleError(
            f"precedent must be one of (None, {PRECEDENT_ENUM}), got {precedent!r}"
        )


def _validate_probe_raise_basis(probe_raise_basis: Optional[str]) -> None:
    if probe_raise_basis is not None and probe_raise_basis not in PROBE_RAISE_BASIS_ENUM:
        raise SizingAssembleError(
            f"probe_raise_basis must be one of (None, {PROBE_RAISE_BASIS_ENUM}), "
            f"got {probe_raise_basis!r}"
        )


def _apply_symmetric_resize(
    tshirt: str,
    probe_signal: Optional[str],
    probe_raise_basis: Optional[str] = None,
) -> tuple[str, bool, bool]:
    if probe_signal is None:
        return tshirt, False, False
    if probe_signal == "collapse":
        resized = _step_tshirt(tshirt, -1)
        return resized, resized != tshirt, False
    if probe_raise_basis in ("substrate-condition", "breadth"):
        return tshirt, False, True
    resized = _step_tshirt(tshirt, +1)
    return resized, resized != tshirt, False


#: skill owns everything after it. Keyed on every member of `ROUTE_ENUM` — the
_LOBBY_CHAINS = {
    "dispatch": ["the work"],
    "spec-dispatch": [
        "light plan (scope_mode: spec-dispatch)",
        "executor dispatch",
        "scoped code-reviewer + review-integrator",
    ],
    "plan": ["plan", "plan review", "execute-plan"],
}

_ROOM_ENTRY = {
    "shape": "enter coordinator:shape",
    "roadmap": "enter coordinator:roadmap-planning",
    "goal-setting": "enter coordinator:goal-setting (PM-gated)",
    "pm-decision": "surface the XL exits to the PM; record the pick in xl_exit",
}

_LIGHT_TERMINAL_TSHIRTS = ("XS", "S")

# Mirrors `coordinator_core.session.mode_resolution.INTERACTION_MODES`
# order. Duplicated locally (never imported at module top level) for the
# same reason ROUTE_ENUM/DETENT_ENUM above are hand-authored copies of the
# vendored schema rather than a live import: `route()` must stay
# import-light (Design § Engine), and this totality check runs at IMPORT
# TIME, before any lazy `mode_resolution` import has happened. Drift
# between this tuple and C2's canonical one is caught by
# `test_interaction_mode_touchpoints.py`, which imports both and asserts
# equality.
_INTERACTION_MODES_EXPECTED = ("hands-on", "pm", "ceo")


def _validate_interaction_mode(interaction_mode: str) -> None:
    # Lazy, function-local import (Design § Engine: "importing mode_resolution
    # lazily ... so that route() stays import-light") -- validates against
    # C2's canonical registry, not the local mirror above, so a caller
    # actually gets checked against the live source of truth.
    from coordinator_core.session.mode_resolution import (
        INTERACTION_MODE_VALUES,
        INTERACTION_MODES,
    )

    if interaction_mode not in INTERACTION_MODE_VALUES:
        raise SizingAssembleError(
            f"interaction_mode must be one of {INTERACTION_MODES}, got {interaction_mode!r}"
        )


#: Design § Engine's touchpoint table. Each mode's ids, IN ORDER, with the
#: human-facing "asks" gloss from the PM ruling (target-design.md § 11):
#: PM mode is two touchpoints (accept sizing, which includes the exit
#: criterion, then accept the result); CEO mode is one (accept the exit
#: criterion); hands-on keeps today's richer gates (sizing, execute, wrap-up).
TOUCHPOINTS_BY_MODE: dict[str, tuple[dict[str, str], ...]] = {
    "hands-on": (
        {"id": "accept_sizing", "asks": "size + exit criterion"},
        {"id": "execute_go", "asks": "whether to execute the plan"},
        {"id": "wrap_up", "asks": "whether to wrap up"},
    ),
    "pm": (
        {"id": "accept_sizing", "asks": "size + exit criterion"},
        {"id": "accept_result", "asks": "accept the result"},
    ),
    "ceo": (
        {"id": "accept_exit_criterion", "asks": "accept the exit criterion"},
    ),
}

#: The sizing-stage touchpoint ids dropped at a resized XS/S (Design §
#: Engine "Size rule" -- XS/S never ask today, so the later touchpoints
#: stay but the size-gate ask is skipped).
_SIZING_STAGE_TOUCHPOINT_IDS = frozenset({"accept_sizing", "accept_exit_criterion"})


def _assert_touchpoint_table_total() -> None:
    """Every member of `_INTERACTION_MODES_EXPECTED` has a `TOUCHPOINTS_BY_MODE`
    entry, and vice versa -- import-time, in the style of
    `_assert_stage_table_total` above."""
    covered = set(TOUCHPOINTS_BY_MODE)
    expected = set(_INTERACTION_MODES_EXPECTED)
    missing = expected - covered
    extra = covered - expected
    if missing or extra:
        raise AssertionError(
            f"sizing_assemble: TOUCHPOINTS_BY_MODE not total over interaction "
            f"modes: missing={sorted(missing)} extra={sorted(extra)}"
        )


_assert_touchpoint_table_total()


def touchpoints(interaction_mode: str, resized_tshirt: str) -> list[dict[str, str]]:
    """Which human gates `interaction_mode` has at `resized_tshirt` (Design §
    Engine). At a resized XS/S the sizing-stage touchpoint is dropped (it
    never asks today); the later touchpoints in the mode's chain are kept."""
    base = TOUCHPOINTS_BY_MODE[interaction_mode]
    if resized_tshirt in _LIGHT_TERMINAL_TSHIRTS:
        return [dict(t) for t in base if t["id"] not in _SIZING_STAGE_TOUCHPOINT_IDS]
    return [dict(t) for t in base]


def _assert_stage_table_total() -> None:
    """Every route in `ROUTE_ENUM` is either lobby-owned or room-owned.

    Import-time rather than call-time: a route present in the enum and absent
    from both tables is a defect the moment it is added, and the caller that
    would discover it at call time is a session whose recorder is already
    short a stage.
    """
    covered = set(_LOBBY_CHAINS) | set(_ROOM_ENTRY)
    missing = [r for r in ROUTE_ENUM if r not in covered]
    if missing:
        raise AssertionError(
            f"sizing_assemble: routes with no stage chain: {missing}"
        )


_assert_stage_table_total()


def stages(resolved_route: str, resized_tshirt: str) -> dict:
    """The stage chain `resolved_route` commits the session to, at that size.

    The flight recorder the sizing lobby opens is a pure function of the two
    fields `route()` already resolves, which is the whole reason this lives
    beside it and adds no op: the EM was transcribing a table the engine had
    already computed, and a transcribed table is one an EM can mistype, skip a
    row of, or stop short of the terminal on.

    Returns ``{"rows": [...], "terminal": <str|None>, "owned_by": <str>}``.
    ``rows`` is the chain in order, ending WITH the terminal row when the lobby
    owns the chain; ``terminal`` is ``None`` for a room-owned route, whose room
    owns its own close.

    NEGATIVE SPEC — the session-goal row is NOT emitted here. It has to cite the
    sizing-object's path, and `route()` never sees one: it is called before the
    object is scaffolded, and on the express lane no object is ever written. The
    caller composes that row; this function answers only what follows it.

    NEGATIVE SPEC — this cannot tell an EXTENSION from a RESTART. `execute-plan`
    Phase 2 and the spec-dispatch light terminal open per-chunk tasks that must
    land beneath these rows rather than reopening the list, and the harness task
    list is not readable from the engine, so nothing here can see whether a
    recorder is already open. That discriminator stays doctrine-side
    (coordinator-content-repo-em memo, 2026-09-05).
    """
    terminal = (
        "quick-wrap"
        if resized_tshirt in _LIGHT_TERMINAL_TSHIRTS
        else "/workstream-complete"
    )

    if resolved_route in _ROOM_ENTRY:
        return {
            "rows": [_ROOM_ENTRY[resolved_route]],
            "terminal": None,
            "owned_by": resolved_route,
        }

    return {
        "rows": [*_LOBBY_CHAINS[resolved_route], terminal],
        "terminal": terminal,
        "owned_by": "lobby",
    }


def _render_d_lobby_lane(resolved_route: str, tshirt: str) -> str:
    chain = stages(resolved_route, tshirt)
    rows = " -> ".join(chain["rows"])
    if chain["terminal"] is None:
        return (
            f"{rows}. The {chain['owned_by']} room owns everything after "
            "this row; the lobby opens no further stages."
        )
    return f"{rows}. The lobby owns this chain through {chain['terminal']}."


#: DECISION POINTS the sizing band actually discriminates, never lane prose —
#: every served arm is PROJECTED at call time from values `route()` has
#: already computed; the registry stores no arm text.
DISPOSITION_REGISTRY = (
    {
        "id": "d-lobby-lane",
        "decision_point": "which stage chain does this route/tshirt commit the session to",
        "inputs": ("tshirt", "route"),
        "render": _render_d_lobby_lane,
    },
    {
        "id": "d-xl-exit",
        "decision_point": "which XL exit does the session pick",
        "inputs": ("route",),
        "judgment_input": (
            "PM assent plus the falsifiable-restatement test named in "
            "docs/plans/2026-09-11-serve-the-arm-that-applies.md"
        ),
    },
)


def _assert_disposition_registry_total() -> None:
    for entry in DISPOSITION_REGISTRY:
        has_render = "render" in entry
        has_judgment = "judgment_input" in entry
        if has_render == has_judgment:
            raise AssertionError(
                f"sizing_assemble: disposition entry {entry.get('id')!r} must "
                "carry exactly one of render/judgment_input"
            )
        if has_render and not callable(entry["render"]):
            raise AssertionError(
                f"sizing_assemble: disposition entry {entry['id']!r} render is "
                "not callable"
            )


_assert_disposition_registry_total()


def dispositions(
    resolved_route: str,
    resized_tshirt: str,
    *,
    pre_resize_tshirt: bool = False,
) -> dict[str, Any]:
    served: list[dict[str, Any]] = []
    no_arm: list[dict[str, Any]] = []
    for entry in DISPOSITION_REGISTRY:
        basis: dict[str, Any] = {}
        for inp in entry["inputs"]:
            if inp == "tshirt":
                basis["tshirt"] = resized_tshirt
            elif inp == "route":
                basis["route"] = resolved_route
        if "render" in entry:
            if pre_resize_tshirt and "tshirt" in basis:
                basis["tshirt_stage"] = "pre-resize"
            served.append(
                {
                    "id": entry["id"],
                    "decision_point": entry["decision_point"],
                    "arm": entry["render"](resolved_route, resized_tshirt),
                    "basis": basis,
                }
            )
        else:
            no_arm.append(
                {
                    "id": entry["id"],
                    "decision_point": entry["decision_point"],
                    "judgment_input": entry["judgment_input"],
                    "basis": basis,
                }
            )
    return {"served": served, "no_arm": no_arm}


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
    boundary_in_notch: Optional[str] = None,
    scout_evidence_kind: Optional[str] = None,
    intent: Optional[str] = None,
    intent_source: Optional[str] = None,
    precedent: Optional[str] = None,
    probe_raise_basis: Optional[str] = None,
    name: Optional[str] = None,
    exit_criterion: Optional[str] = None,
    interaction_mode: str = "hands-on",
    interaction_mode_source: Optional[str] = None,
    premise_evidence: Optional[str] = None,
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
            resized M/L/XL/XXL sets `premise_unproven`; `not-applicable` at a
            resized M/L/XL/XXL sets `premise_not_applicable`. Never alters
            `route` or `xl_exit` (see module docstring's "Premise
            provenance" note).
        boundary_in_notch: None | "yes" | "no" — did a cross-repo boundary,
            memo, relay, or assent gate contribute to this notch? `yes` sets
            the advisory `boundary_counted_in_notch` detent at EVERY size (not
            size-gated, unlike the premise detents — see module docstring).
            Never alters `route` or `xl_exit`.
        scout_evidence_kind: None | "mention-count" | "change-set" |
            "site-count" — what the accompanying `scout_evidence` counts.
            `mention-count` sets the advisory `scout_evidence_mention_count`
            detent. A typed field BESIDE the free text; `scout_evidence`
            itself is still never parsed. Never alters `route` or `xl_exit`.
        exit_criterion: the PM-facing primary success / exit criterion
            sentence, or `None` when none has been proposed yet. Never
            alters `route`. A resized t-shirt at or above `"M"` sets the
            `exit_criterion_pending` detent regardless of whether a
            statement was passed — this module never sees PM acceptance
            (that is `sizing.accept_exit_criterion`, C4).
        interaction_mode: "hands-on" (default) | "pm" | "ceo" — which human
            touchpoints this sizing's size commits to (Design § Engine).
            Validated against `mode_resolution.INTERACTION_MODES`. Never
            alters `route`, `fork`, or `xl_exit` — it selects which
            `touchpoints` render, exactly as `compaction_warnings` selects
            an advisory variant without ever suppressing the advisory
            itself. In `ceo` mode only, `post_size_prompt_pending` is
            suppressed (ceo's single touchpoint is the exit criterion).
        interaction_mode_source: "flag" | "fleet" | "default" — set by
            `main()`; `route()` never resolves this itself and only echoes
            the value it is given back into the return payload.

    Returns:
        A dict: {route, detents, fork, xl_exit, resolved_estimate, stages,
        dispositions, scout_evidence, narration, next_move, exit_criterion,
        interaction_mode, interaction_mode_source, touchpoints} —
        READ-ONLY, mutates nothing.
    """
    _validate_appetite(appetite)
    tshirt = estimate.get("tshirt")
    _validate_tshirt(tshirt)
    _validate_probe_signal(probe_signal)
    _validate_premise_provenance(premise_provenance)
    _validate_boundary_in_notch(boundary_in_notch)
    _validate_scout_evidence_kind(scout_evidence_kind)
    _validate_intent_source(intent_source)
    _validate_precedent(precedent)
    _validate_probe_raise_basis(probe_raise_basis)
    _validate_interaction_mode(interaction_mode)
    scout_evidence = list(scout_evidence or [])
    exit_criterion_field = (
        {"statement": exit_criterion, "accepted": None} if exit_criterion else None
    )

    if express_lane:
        return {
            "route": "dispatch",
            "detents": [],
            "fork": None,
            "xl_exit": None,
            "intent": intent,
            "resolved_estimate": {"tshirt": tshirt, "provisional": True},
            "scout_evidence": scout_evidence,
            "stages": stages("dispatch", tshirt),
            "dispositions": dispositions("dispatch", tshirt, pre_resize_tshirt=True),
            "narration": "Express lane: trivial ask, no sizing ceremony.",
            "next_move": "Dispatch directly. No sizing-object persisted (D3).",
            "directives": [],
            "exit_criterion": exit_criterion_field,
            "interaction_mode": interaction_mode,
            "interaction_mode_source": interaction_mode_source,
            "touchpoints": [],
        }

    resized_tshirt, resize_changed, raise_suppressed = _apply_symmetric_resize(
        tshirt, probe_signal, probe_raise_basis
    )

    detents: list[str] = []

    pre_resize_route = _BASE_ROUTE_BY_TSHIRT[tshirt]
    post_resize_route = _BASE_ROUTE_BY_TSHIRT[resized_tshirt]
    if resize_changed and pre_resize_route != post_resize_route:
        detents.append("route_boundary_crossed")

    resized_weight = TSHIRT_WEIGHT[resized_tshirt]

    # sole divergence signal. `fork` is the sizing skill's RESOLUTION slot,
    fork: Optional[str] = None
    xl_exit: Optional[str] = None
    if appetite is not None:
        ceiling_tshirt = _APPETITE_CEILING_TSHIRT[appetite]
        ceiling_weight = TSHIRT_WEIGHT[ceiling_tshirt]
        if resized_weight > ceiling_weight:
            detents.append("appetite_exceeded")
        else:
            detents.append("appetite_conform")
    elif resized_tshirt in _POST_SIZE_PROMPT_TSHIRTS and interaction_mode != "ceo":
        # ceo suppresses this ask (Design § Engine): its single touchpoint
        # is the exit criterion, put via `exit_criterion_pending` below.
        detents.append("post_size_prompt_pending")

    if resized_tshirt in _POST_SIZE_PROMPT_TSHIRTS:
        # Fires in EVERY mode, including ceo and including when a statement
        # was passed -- this module never sees PM acceptance (C4's job).
        detents.append("exit_criterion_pending")

    if (resized_tshirt in _LARGE_TSHIRTS and jtbd_unclear) or well_trodden_step_change:
        resolved_route = "shape"
        detents.append("scope_boundary_acknowledged")
    else:
        resolved_route = _BASE_ROUTE_BY_TSHIRT[resized_tshirt]

    if resolved_route == "pm-decision":
        detents.append("pm_decision_pending")

    if resolved_route == "goal-setting":
        detents.append("goal_setting_pm_gated")

    # ACCEPTED counter-proposal to DoE's originally specced
    # trustworthy" — but `--probe-signal raise` is a caller ASSERTION, not
    # This predicate tests `scout_evidence` EMPTINESS only, never its
    if resized_tshirt == "XXL" and not scout_evidence:
        detents.append("xxl_unprobed")

    if resized_tshirt in _PREMISE_DETENT_TSHIRTS:
        if premise_provenance == "read":
            detents.append("premise_unproven")
        elif premise_provenance == "not-applicable":
            detents.append("premise_not_applicable")

    # Boundary-in-notch detent (advisory, warn-never-block). DELIBERATELY not
    if boundary_in_notch == "yes":
        detents.append("boundary_counted_in_notch")

    if scout_evidence_kind == "mention-count":
        detents.append("scout_evidence_mention_count")

    if intent_source == "em-elaborated":
        detents.append("intent_em_elaborated")

    if precedent == "shipped-before":
        detents.append("precedent_shipped_before")

    if raise_suppressed:
        detents.append(_PROBE_RAISE_SUPPRESSION_DETENT[probe_raise_basis])

    if probe_signal == "raise" and probe_raise_basis == "ask-scope":
        detents.append("probe_raise_ask_scope_asserted")

    if resolved_route not in ROUTE_ENUM:  # pragma: no cover - defensive, table-driven
        raise SizingAssembleError(f"internal: resolved route {resolved_route!r} not in {ROUTE_ENUM}")

    narration_bits = [f"Resolved tshirt {resized_tshirt}"]
    if resize_changed:
        narration_bits.append(f"(probe {probe_signal}d from {tshirt})")
    if appetite is not None:
        narration_bits.append(f"against appetite={appetite} -> route={resolved_route}.")
    else:
        narration_bits.append(f"-> route={resolved_route}.")
    raise_line: Optional[str] = None
    if resize_changed and probe_signal == "raise":
        raise_line = (
            f"probe raise: {tshirt} -> {resized_tshirt}, "
            f"basis={probe_raise_basis or 'unstated'}, route={resolved_route}"
        )
        narration_bits.append(raise_line)
    narration = " ".join(narration_bits)

    #      PM-gated framing is APPENDED to this arm's text below rather than
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

    if "goal_setting_pm_gated" in detents and "appetite_exceeded" in detents:
        next_move += (
            " This also resolves to goal-setting (XXL, OKR-programme scale) — route to "
            "coordinator:goal-setting, which is PM-gated, in addition to the appetite "
            "fork above."
        )

    # (XXL plan's call — it is a vocabulary value in XL_EXIT_ENUM, not a
    if "post_size_prompt_pending" in detents:
        if resolved_route == "pm-decision":
            next_move += (
                f" Put that to the PM as ONE open ask, not two: looks like a "
                f"{resized_tshirt}, shall we go with that or want to split it, cut it, "
                "what's up?"
            )
        else:
            next_move += (
                f" Looks like a {resized_tshirt}, shall we go with that or want to split it, "
                "cut it, what's up?"
            )

    # advisory). ADVISORY, warn-never-block (DR-277) — never withholds the
    if "xxl_unprobed" in detents:
        next_move += (
            " ADVISORY (warn, never block; does not alter the route above): "
            "this XXL resolved with no scout_evidence recorded. Discharge by "
            "citing inline evidence for the size — never by producing a "
            "spike-result artifact."
        )

    # ADVISORY, stated in words: it is discharged by citing inline evidence
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

    # States the DISCRIMINATOR the engine cannot apply (memo vs co-design)
    if "boundary_counted_in_notch" in detents:
        next_move += (
            " ADVISORY (warn, never block; does not alter the route above): "
            "a cross-repo boundary, memo, relay, or assent gate contributed "
            "to this notch. A memo — one ask, the sibling implements it on "
            "their own surface — is a GATE, not a size, and does not move the "
            "notch: record it in blocked_by/awaiting_gate and collapse the "
            "estimate. Only negotiated co-design, where the shared contract "
            "itself is the unknown, earns the notch — and owes a recorded "
            "justification naming the unconverged contract."
        )

    if "scout_evidence_mention_count" in detents:
        next_move += (
            " ADVISORY (warn, never block; does not alter the route above): "
            "the scout evidence behind this size is a MENTION-COUNT, not a "
            "change-set. A grep count is not a change-set until someone has "
            "asked what a compatibility layer absorbs — under a back-compat "
            "shim, mentioning sites resolve unchanged and count zero. "
            "Discharge by citing the change-set, or collapse the estimate."
        )

    if "probe_raise_on_substrate_condition" in detents:
        next_move += (
            f" NOTE (this one DID change the size): the probe raise was NOT applied — "
            f"you declared it based on the substrate's condition, not the ask's scope, "
            f"so the estimate stands at {resized_tshirt}. Problems found in the area you are "
            "about to touch do not make the requested work bigger; they are their own "
            "items. If the ASK itself genuinely grew, re-run with "
            "--probe-raise-basis ask-scope and name what the PM asked for that the "
            "original notch missed."
        )
    elif "probe_raise_on_breadth" in detents:
        next_move += (
            f" NOTE (this one DID change the size): the probe raise was NOT applied — "
            f"you declared it based on touchpoint breadth, not the ask's scope, so the "
            f"estimate stands at {resized_tshirt}. A count of sites is a dispatch shape, "
            "not a size signal, PROVIDED the sites are uniform — each needing the same "
            "call, none interacting. If any site needs its own call, or sites interact, "
            "that is depth wearing a count: re-run with --probe-raise-basis ask-scope and "
            "name what the PM asked for that the original notch missed."
        )

    if "probe_raise_ask_scope_asserted" in detents:
        next_move += (
            " ADVISORY (warn, never block; does not alter the route above): "
            "this raise was declared ASK-SCOPE — you have asserted the ask "
            "itself is bigger than the original notch, not that the substrate "
            "is untidy. That is a falsifiable claim and this detent is what "
            "makes it findable later. Name, in scout_evidence, what the PM "
            "asked for that the first read missed."
        )

    if "precedent_shipped_before" in detents:
        next_move += (
            " ADVISORY (warn, never block; does not alter the route above): "
            "this operation has SHIPPED IN THIS REPO BEFORE. A repeat of an "
            "operation whose runbook already exists and has landed is "
            "dispatch-shaped by default — the prior run is the evidence. "
            "Discharge by naming what is specifically different this time "
            "(a migration riding along, a changed contract, a first-time "
            "surface), or collapse the estimate to match the precedent."
        )

    if "intent_em_elaborated" in detents:
        next_move += (
            " ADVISORY (warn, never block; does not alter the route above): "
            "the recorded intent is the EM's restatement, not the PM's own "
            "words. The sizing skill requires intent verbatim precisely "
            "because restatement is where scope silently grows — clauses the "
            "PM never said get sized as though they had. Re-read the ask as "
            "typed and confirm the size is against THAT, not against the "
            "elaboration."
        )

    mode_touchpoints = touchpoints(interaction_mode, resized_tshirt)

    if "exit_criterion_pending" in detents:
        if exit_criterion:
            criterion_clause = f'accept the exit criterion: "{exit_criterion}"'
        else:
            criterion_clause = (
                "propose an exit criterion for the PM to accept -- none was passed"
            )
        if interaction_mode == "ceo":
            next_move += (
                f" CEO mode's single touchpoint: ask the PM to {criterion_clause}."
            )
        else:
            next_move += f" Also ask the PM to {criterion_clause}."

    touchpoint_ids = ", ".join(t["id"] for t in mode_touchpoints) or "none"
    next_move += (
        f" Mode {interaction_mode} (source: {interaction_mode_source or 'default'}) "
        f"touchpoints at this size: {touchpoint_ids}."
    )

    return {
        "route": resolved_route,
        "detents": detents,
        "fork": fork,
        "xl_exit": xl_exit,
        "intent": intent,
        "resolved_estimate": {"tshirt": resized_tshirt, "provisional": True},
        "stages": stages(resolved_route, resized_tshirt),
        "dispositions": dispositions(resolved_route, resized_tshirt),
        "scout_evidence": scout_evidence,
        "narration": narration,
        "probe_raise": raise_line,
        "next_move": next_move,
        "exit_criterion": exit_criterion_field,
        "interaction_mode": interaction_mode,
        "interaction_mode_source": interaction_mode_source,
        "touchpoints": mode_touchpoints,
        # the object is minted regardless of RESOLVED route; only D3's
        # short-circuit skips it. `--interaction-mode` is threaded onto the
        # directive only when the caller (main(), via interaction_mode_source)
        # actually resolved one from a flag or the fleet record -- never
        # merely route()'s own "hands-on" default, so a caller that never
        # asked about mode still gets today's byte-identical directive.
        "directives": [
            _sizing_object_scaffold_directive(
                intent,
                name,
                exit_criterion=exit_criterion,
                interaction_mode=(
                    interaction_mode
                    if interaction_mode_source in ("flag", "fleet")
                    else None
                ),
                tshirt=resized_tshirt,
                route=resolved_route,
                premise=premise_provenance,
                premise_evidence=premise_evidence,
            )
        ],
    }


def _render_block(mapping: dict) -> str:
    import yaml

    dumped = yaml.safe_dump(
        mapping, default_flow_style=False, sort_keys=False, allow_unicode=True, width=100000
    )
    return "".join(f"  {line}\n" for line in dumped.rstrip("\n").split("\n"))


def _set_scalar(text: str, key: str, raw: str) -> str:
    from coordinator_core.frontmatter.primitives import (
        insert_fm_field_raw,
        read_fm_field,
        replace_fm_field_raw,
    )

    if read_fm_field(text, key) is None:
        return insert_fm_field_raw(text, key, raw)
    return replace_fm_field_raw(text, key, raw)


def _scaffold_missing(
    root: Path,
    target: Path,
    decision: dict[str, Any],
    premise_provenance: Optional[str],
    premise_evidence: Optional[str],
) -> None:
    """Create the sizing-object `--write` points at, via doc-new's own scaffold."""
    import importlib.util

    intent = (decision.get("intent") or "").strip()
    if not intent:
        raise SizingAssembleError(
            f"sizing-object not found on disk: {target.name}; pass --intent to scaffold it"
        )
    doc_new = root / "coordinator" / "bin" / "coordinator-doc-new.py"
    if not doc_new.is_file():
        doc_new = Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "coordinator-doc-new.py"
    spec = importlib.util.spec_from_file_location("coordinator_doc_new_for_sizing_write", doc_new)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    recorded = premise_provenance not in (None, "unrecorded")
    text = mod._scaffold_sizing(
        title=intent,
        premise=premise_provenance if recorded else None,
        premise_evidence=premise_evidence if recorded else None,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "x", encoding="utf-8") as fh:
        fh.write(text)
    from coordinator_core.session.declared_writes import declare_write

    declare_write(target)


def write_back(
    root: Path,
    sizing: str,
    decision: dict[str, Any],
    *,
    exit_criterion: Optional[str] = None,
    interaction_mode: Optional[str] = None,
    premise_provenance: Optional[str] = None,
    premise_evidence: Optional[str] = None,
    scout_evidence: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Apply a `route()` decision to an existing `state/sizings/` sizing under
    `locked_rmw`, schema-validated before the write.

    Sets estimate.tshirt, route, detents, fork/xl_exit (only when the decision
    resolved one), scout_evidence and premise (only when given), and status
    `draft` -> `sized`. exit_criterion.statement is written only while
    `accepted` is null; interaction_mode only while absent. Raises
    `SizingAssembleError` on any refusal, with nothing written.
    """
    import json

    import yaml

    from coordinator_core.frontmatter.primitives import write_fm_nested_field
    from coordinator_core.frontmatter.schema_validate import (
        format_validation_errors,
        validate_frontmatter,
    )
    from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
    from coordinator_core.ops._path_guard import contained_path

    schema = (
        Path(__file__).resolve().parent.parent
        / "frontmatter"
        / "schemas"
        / "sizing-object.schema.json"
    )
    candidate = Path(sizing)
    if not candidate.is_absolute():
        candidate = root / candidate
    target = contained_path(candidate, [root / "state" / "sizings"])
    if target is None:
        raise SizingAssembleError(f"{sizing!r} escapes state/sizings/")
    if not target.is_file():
        _scaffold_missing(root, target, decision, premise_provenance, premise_evidence)
    record_premise = premise_provenance not in (None, "unrecorded")
    if record_premise and not (premise_evidence or "").strip():
        raise SizingAssembleError("--premise-provenance given without --premise-evidence")

    def mutate(old: str) -> str:
        try:
            doc = yaml.safe_load(old) or {}
        except Exception as exc:  # noqa: BLE001
            raise MutateAbort(f"YAML parse error: {exc}") from exc
        if not isinstance(doc, dict):
            raise MutateAbort("sizing-object is not a YAML mapping")
        text = old

        estimate = dict(doc.get("estimate") or {})
        estimate["tshirt"] = decision["resolved_estimate"]["tshirt"]
        estimate.setdefault("provisional", True)
        text = write_fm_nested_field(text, "estimate", _render_block(estimate))
        text = _set_scalar(text, "route", decision["route"])
        text = _set_scalar(text, "detents", json.dumps(list(decision["detents"])))
        for key in ("fork", "xl_exit"):
            if decision.get(key) is not None:
                text = _set_scalar(text, key, decision[key])
        if scout_evidence:
            text = _set_scalar(text, "scout_evidence", json.dumps(list(scout_evidence)))
        if record_premise:
            text = write_fm_nested_field(
                text,
                "premise",
                _render_block(
                    {
                        **(doc.get("premise") or {}),
                        "provenance": premise_provenance,
                        "evidence": premise_evidence.strip(),
                    }
                ),
            )
        if exit_criterion and exit_criterion.strip():
            existing = doc.get("exit_criterion")
            existing = dict(existing) if isinstance(existing, dict) else {}
            if existing.get("accepted") is None:
                existing["statement"] = exit_criterion.strip()
                existing["accepted"] = None
                text = write_fm_nested_field(text, "exit_criterion", _render_block(existing))
        if interaction_mode and not doc.get("interaction_mode"):
            text = _set_scalar(text, "interaction_mode", interaction_mode)
        if doc.get("status") == "draft":
            text = _set_scalar(text, "status", "sized")

        try:
            new_doc = yaml.safe_load(text) or {}
        except Exception as exc:  # noqa: BLE001
            raise MutateAbort(f"post-mutation YAML parse error: {exc}") from exc
        errors = validate_frontmatter(new_doc, schema)
        if errors:
            raise MutateAbort(
                f"post-mutation schema validation failed: {format_validation_errors(errors)}"
            )
        return text

    try:
        locked_rmw(target, mutate, repo_root=root)
    except MutateAbort as exc:
        raise SizingAssembleError(str(exc.args[0]) if exc.args else "mutation aborted") from exc
    except LockTimeout as exc:
        raise SizingAssembleError(f"timed out waiting for file lock on {target}: {exc}") from exc
    return {"path": str(target), "status": "written"}


EXIT_OK = 0
EXIT_BUSINESS_FAIL = 1
EXIT_USAGE = 2
EXIT_TRANSPORT_FAIL = 3


def _usage(prog: str, stream=None) -> int:
    stream = __import__("sys").stderr if stream is None else stream
    print(
        f"{prog}: usage: {prog} --tshirt <XS|S|M|L|XL|XXL> "
        "[--appetite small|medium|large] "
        "[--express-lane] [--probe-signal collapse|raise] [--jtbd-unclear] "
        "[--well-trodden-step-change] "
        "[--premise-provenance executed|read|not-applicable|unrecorded] "
        "[--premise-evidence <str>] "
        "[--boundary-in-notch yes|no] "
        "[--scout-evidence-kind mention-count|change-set|site-count] "
        "[--scout-evidence <str> ...] "
        "[--intent <str>] [--intent-source pm-verbatim|em-elaborated] "
        "[--name <short label>] "
        "[--precedent shipped-before|novel] "
        "[--probe-raise-basis ask-scope|substrate-condition|breadth] "
        "[--exit-criterion <str>] "
        "[--interaction-mode hands-on|pm|ceo] "
        "[--write <state/sizings/x.yaml>]",
        file=stream,
    )
    return EXIT_USAGE


#: `main()`'s own dummy session id for the `interaction_mode` fleet-record
#: read below. Never consulted for anything else: `interaction_mode`
#: declares `session_pair=None` (C2), so `resolve_mode`'s session-scoped
#: rung is never reached for this key -- any literal string works here.
_CLI_SESSION_ID = "sizing-assemble-cli"


def _resolve_interaction_mode_and_source(cli_value: Optional[str]) -> tuple[str, str]:
    """flag -> fleet -> default (Design § Mode resolution). Lazy,
    function-local import (`main()`'s own "importing mode_resolution lazily
    ... so that route() stays import-light") -- resolving through C2's
    `resolve_mode` (never a hand-rolled copy of its fleet-wins precedence)
    keeps this in sync with the registry by construction."""
    if cli_value is not None:
        return cli_value, "flag"

    from coordinator_core.session.fleet_mode import read_fleet_mode
    from coordinator_core.session.mode_resolution import resolve_mode

    resolved = resolve_mode("interaction_mode", _CLI_SESSION_ID, env=None)
    fleet_map = read_fleet_mode()
    source = "fleet" if fleet_map.get("interaction_mode") == resolved else "default"
    return resolved, source


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
    boundary_in_notch = None
    scout_evidence_kind = None
    intent = None
    intent_source = None
    name = None
    precedent = None
    probe_raise_basis = None
    scout_evidence: list[str] = []
    exit_criterion = None
    interaction_mode_flag = None
    premise_evidence = None
    write_path = None

    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("--help", "-h"):
            _usage(prog, stream=sys.stdout)
            return EXIT_OK
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
        elif tok == "--premise-evidence" and i + 1 < len(argv):
            premise_evidence = argv[i + 1]
            i += 2
        elif tok == "--write" and i + 1 < len(argv):
            write_path = argv[i + 1]
            i += 2
        elif tok == "--boundary-in-notch" and i + 1 < len(argv):
            boundary_in_notch = argv[i + 1]
            i += 2
        elif tok == "--scout-evidence-kind" and i + 1 < len(argv):
            scout_evidence_kind = argv[i + 1]
            i += 2
        elif tok == "--scout-evidence" and i + 1 < len(argv):
            scout_evidence.append(argv[i + 1])
            i += 2
        elif tok == "--intent" and i + 1 < len(argv):
            intent = argv[i + 1]
            i += 2
        elif tok == "--intent-source" and i + 1 < len(argv):
            intent_source = argv[i + 1]
            i += 2
        elif tok == "--name" and i + 1 < len(argv):
            name = argv[i + 1]
            i += 2
        elif tok == "--precedent" and i + 1 < len(argv):
            precedent = argv[i + 1]
            i += 2
        elif tok == "--probe-raise-basis" and i + 1 < len(argv):
            probe_raise_basis = argv[i + 1]
            i += 2
        elif tok == "--exit-criterion" and i + 1 < len(argv):
            exit_criterion = argv[i + 1]
            i += 2
        elif tok == "--interaction-mode" and i + 1 < len(argv):
            interaction_mode_flag = argv[i + 1]
            i += 2
        elif tok == "--json":
            i += 1
        else:
            print(f"{prog}: unrecognized argument {tok!r}", file=sys.stderr)
            return _usage(prog)

    if tshirt is None:
        return _usage(prog)

    interaction_mode, interaction_mode_source = _resolve_interaction_mode_and_source(
        interaction_mode_flag
    )

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
            boundary_in_notch=boundary_in_notch,
            scout_evidence_kind=scout_evidence_kind,
            intent=intent,
            intent_source=intent_source,
            precedent=precedent,
            probe_raise_basis=probe_raise_basis,
            name=name,
            exit_criterion=exit_criterion,
            interaction_mode=interaction_mode,
            interaction_mode_source=interaction_mode_source,
            premise_evidence=premise_evidence,
        )
    except SizingAssembleError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - structural backstop, mirrors pickup_assemble
        print(f"{prog}: unexpected failure: {exc}", file=sys.stderr)
        return EXIT_TRANSPORT_FAIL

    if write_path is not None:
        try:
            decision["write"] = write_back(
                Path.cwd(),
                write_path,
                decision,
                exit_criterion=exit_criterion,
                interaction_mode=(
                    interaction_mode if interaction_mode_source in ("flag", "fleet") else None
                ),
                premise_provenance=premise_provenance,
                premise_evidence=premise_evidence,
                scout_evidence=scout_evidence,
            )
        except SizingAssembleError as exc:
            print(f"{prog}: --write refused: {exc}", file=sys.stderr)
            return EXIT_BUSINESS_FAIL

    print(json.dumps(decision, indent=2, sort_keys=True))
    return EXIT_OK
