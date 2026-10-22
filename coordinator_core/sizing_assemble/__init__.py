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
`xl_exit` field, engineered exactly like `fork` (see below). A bundle of 2+ independent
initiatives at XL (`--initiatives`) sets the `bundle` detent and the PM question offers
`split` first; it still never sets `xl_exit`.

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
asked for. XS/S never ask, and neither does any size where
`sizing_acceptance_skipped` holds (a plan/dispatch M or L goes straight to
execution in every mode); `route` itself is identical across appetite
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

import re
from datetime import date
from pathlib import Path
from typing import Any, Optional

from coordinator_core import warp_fire as _warp_fire

from coordinator_core.ops.sizing_acceptance import ENGINE_SIZE_RULE, sizing_acceptance_skipped  # noqa: F401 -- ENGINE_SIZE_RULE re-exported
from coordinator_core.roadmap.post_stamp_clause import suite_tier_refusal

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


def _cli_repo_root(sizing: Optional[str]) -> Path:
    """The repo a CLI sizing path belongs to, read off the filesystem (no spawn): an absolute
    path's own repo, else the repo enclosing the cwd. A relative path is repo-relative, so a
    shell parked in a subdirectory still names `state/sizings/x.yaml`. No `.git` found: cwd."""
    start = Path(sizing).parent if sizing and Path(sizing).is_absolute() else Path.cwd()
    start = start.resolve()
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return Path.cwd()


def _derive_register_cli(
    prog: str, write_path: str, scout_path: Optional[str], supersede: bool
) -> int:
    """`--derive-register`: record `derive_register`'s register on a sizing that already
    embeds its PM turns (a sizing written before derivation existed, or one cut by hand)."""
    import json
    import sys

    import yaml

    from coordinator_core.ops.sizing_record_register import _handler as record_register

    root = _cli_repo_root(write_path)
    target = Path(write_path) if Path(write_path).is_absolute() else root / write_path
    try:
        doc = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        scout = yaml.safe_load(_read_path_or_stdin(scout_path)) if scout_path else None
    except Exception as exc:  # noqa: BLE001
        print(f"{prog}: cannot read {write_path} or its scout report: {exc}", file=sys.stderr)
        return EXIT_USAGE
    rows, unrelated, refs, dropped = derive_register(doc, scout if isinstance(scout, dict) else None)
    if not rows:
        print(f"{prog}: {write_path} carries no PM words to derive a register from", file=sys.stderr)
        return EXIT_BUSINESS_FAIL
    try:
        sizing_rel = target.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        sizing_rel = f"state/sizings/{target.name}"
    register = _derived_register_block(rows, unrelated, refs, sizing_rel)
    if dropped:
        print(
            f"{sizing_rel}: scout register row(s) {', '.join(dropped)} dropped: source_text is not "
            "verbatim in any PM turn",
            file=sys.stderr,
        )
    result = record_register(
        {"sizing": write_path, "register": register, "supersede": supersede}, repo_root=root
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return EXIT_OK if result["exit_code"] == 0 else EXIT_BUSINESS_FAIL


def _read_path_or_stdin(path: str) -> str:
    """The text of `path`, or of stdin when `path` is `-`. Stdin is decoded from raw bytes by BOM
    sniff (UTF-16 BOM -> utf-16, else utf-8-sig) so a PowerShell pipe survives; a stdin without
    `.buffer` falls back to its text read."""
    if path == "-":
        import sys

        buffer = getattr(sys.stdin, "buffer", None)
        if buffer is None:
            return sys.stdin.read().lstrip("﻿")
        raw = buffer.read()
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            return raw.decode("utf-16")
        return raw.decode("utf-8-sig")
    return Path(path).read_text(encoding="utf-8-sig")


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
    # APPENDED by C4c (multi-ask sibling runs): 2+ independent initiatives at XL.
    "bundle",
    # APPENDED by I47 C2 (sizing-object 1.45.0), never re-sorted — order is
    # load-bearing against DoE's EQUAL_VERSION_SHAPE_DRIFT gate.
    "xxl_infra_check_owed",
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


SUITE_TIER_REWRITE = "tests covering touched files pass"

_SINGLE_CLAIM_BREAK = re.compile(r"[;\n]|\.\s|[,:]|\b(?:and|then|or|but|not|no|except|unless|without|plus|also)\b", re.IGNORECASE)


def _suite_tier_rewrite_is_safe(statement: object) -> bool:
    """True when a tier-naming statement is one tests-on-touched-files-pass claim.

    Only then does replacing the whole statement with SUITE_TIER_REWRITE lose
    nothing; a bare tier name or a multi-claim statement is refused instead.
    """
    text = " ".join(str(statement or "").split())
    if _SINGLE_CLAIM_BREAK.search(text):
        return False
    return bool(
        re.search(r"\btests?\b", text, re.IGNORECASE)
        and re.search(r"\btouched\b", text, re.IGNORECASE)
        and re.search(r"\bpass(?:es)?\b", text, re.IGNORECASE)
    )


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
    "roadmap": (
        "fire coordinator:roadmap-blitz on this sizing; enter coordinator:roadmap-planning "
        "only when the PM clearly asks to work it hands-on"
    ),
    "goal-setting": (
        "fire coordinator:goal-blitz on this sizing (PM-gated); enter coordinator:goal-setting "
        "only when the PM clearly asks to work it hands-on"
    ),
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

#: The touchpoint ids the engine skips when `sizing_acceptance_skipped` holds, in every
#: mode (PM ruling 2026-10-10: below XL a sizing routes straight to execution, no ask);
#: the closing touchpoints (`wrap_up`, `accept_result`) are kept.
_SIZING_STAGE_TOUCHPOINT_IDS = frozenset({"accept_sizing", "accept_exit_criterion", "execute_go"})

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


def touchpoints(interaction_mode: str, resized_tshirt: str, route: str) -> list[dict[str, str]]:
    """Which human gates `interaction_mode` has for `route` at `resized_tshirt` (Design §
    Engine). Where `sizing_acceptance_skipped` holds the sizing-stage touchpoints are
    dropped; the later touchpoints in the mode's chain are kept."""
    base = TOUCHPOINTS_BY_MODE[interaction_mode]
    if sizing_acceptance_skipped(route, resized_tshirt):
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


#: Stands in for the sizing path in the fire line until `coordinator-doc-new` scaffolds it.
_SCAFFOLDED_SIZING = "<the sizing path coordinator-doc-new prints>"


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


def pm_exit_question(
    resized_tshirt: str, resolved_route: str, *, prompt_pending: bool, bundle: bool = False
) -> Optional[str]:
    """The question the PM answers when a sizing halts for them; None when none is owed.
    Lower-case lead on `pm-decision`, where it follows a framing clause in `next_move`.

    `prompt_pending` is the `post_size_prompt_pending` detent. A `shape` route owes the
    room-entry hand-back instead of a post-size question.
    """
    if resolved_route == "shape":
        return _ROOM_ENTRY["shape"]
    if not prompt_pending:
        return None
    if bundle:
        return (
            f"Looks like a {resized_tshirt} bundling independent initiatives: split them into "
            "separate runs (recommended), or shape, roadmap, or accept it as one multi-session run?"
        )
    return _post_size_question(resized_tshirt, resolved_route)


def _post_size_question(resized_tshirt: str, resolved_route: str) -> str:
    lead = "looks like a" if resolved_route == "pm-decision" else "Looks like a"
    return f"{lead} {resized_tshirt}, shall we go with that or want to split it, cut it, what's up?"


def bundle_labels(initiatives: list[dict[str, Any]]) -> Optional[list[str]]:
    """Labels of `initiatives` when there are two or more and their `rows` are pairwise
    disjoint; None otherwise (one initiative, or overlapping rows, is not a bundle)."""
    if len(initiatives) < 2:
        return None
    seen: set[str] = set()
    for item in initiatives:
        rows = set(item["rows"])
        if seen & rows:
            return None
        seen |= rows
    return [item["label"] for item in initiatives]


def parse_initiatives(raw: str) -> tuple[Optional[list[dict[str, Any]]], list[str]]:
    """The `--initiatives` value as a list of `{label, rows}`, with one refusal
    string per bad value. `label` is non-empty and `rows` a non-empty list of non-empty ids."""
    import json

    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, [f"--initiatives is not valid JSON: {exc}"]
    if not isinstance(data, list) or not data:
        return None, ["--initiatives must be a non-empty JSON array"]
    out: list[dict[str, Any]] = []
    errors: list[str] = []
    for n, item in enumerate(data):
        if not isinstance(item, dict):
            errors.append(f"--initiatives item {n} is not an object")
            continue
        extra = sorted(set(item) - {"label", "rows"})
        if extra:
            errors.append(f"--initiatives item {n} has unknown keys {extra}")
        label, rows = item.get("label"), item.get("rows")
        if not (isinstance(label, str) and label.strip()):
            errors.append(f"--initiatives item {n} needs a non-empty label")
        if not (isinstance(rows, list) and rows and all(isinstance(r, str) and r.strip() for r in rows)):
            errors.append(f"--initiatives item {n} needs rows: a non-empty list of non-empty ids")
        out.append(item)
    return (None, errors) if errors else (out, [])


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
    click_paths: Optional[list] = None,
    interaction_mode: str = "hands-on",
    interaction_mode_source: Optional[str] = None,
    premise_evidence: Optional[str] = None,
    initiatives: Optional[list[dict[str, Any]]] = None,
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
        interaction_mode: "hands-on" | "pm" | "ceo" — which human
            touchpoints apply. The engine default is "ceo", resolved by the CLI
            through `mode_resolution`. This parameter's own "hands-on" default
            covers only direct library calls that pass no mode. Which human
            touchpoints this sizing's size commits to (Design § Engine).
            Validated against `mode_resolution.INTERACTION_MODES`. Never
            alters `route`, `fork`, or `xl_exit` — it selects which
            `touchpoints` render, exactly as `compaction_warnings` selects
            an advisory variant without ever suppressing the advisory
            itself. In `ceo` mode, `post_size_prompt_pending` is
            suppressed (ceo's single touchpoint is the exit criterion); in
            every mode it is also dropped wherever
            `sizing_acceptance_skipped(route, tshirt)` holds.
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
    tier_refusal = suite_tier_refusal(exit_criterion)
    if tier_refusal is not None:
        if not _suite_tier_rewrite_is_safe(exit_criterion):
            raise SizingAssembleError(tier_refusal)
        exit_criterion = SUITE_TIER_REWRITE
    if click_paths and not exit_criterion:
        raise SizingAssembleError("--click-path needs --exit-criterion: a click path belongs to a criterion")
    exit_criterion_field = (
        {"statement": exit_criterion, "accepted": None} if exit_criterion else None
    )
    if exit_criterion_field is not None and click_paths:
        exit_criterion_field["click_paths"] = click_paths

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
            "next_move": "No sizing-object persisted (D3). " + _warp_fire.fire_step(),
            "fire": _warp_fire.fire_command(),
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

    if sizing_acceptance_skipped(resolved_route, resized_tshirt):
        # The engine carries this straight to execution in every mode, hands-on included
        # (PM ruling 2026-10-10). Appetite stays PM-stated-only; nothing here infers it.
        dropped = {"exit_criterion_pending", "post_size_prompt_pending"}
        detents = [d for d in detents if d not in dropped]

    if resolved_route == "pm-decision":
        detents.append("pm_decision_pending")

    # Advisory like every detent: marks the bundle so the gate offers `split` first; never
    # alters `route` and never sets `xl_exit`. One initiative, or rows that overlap, is no bundle.
    bundled_labels = (
        bundle_labels(initiatives) if resized_tshirt == "XL" and initiatives else None
    )
    if bundled_labels:
        detents.append("bundle")

    if resolved_route == "goal-setting":
        detents.append("goal_setting_pm_gated")

    # ACCEPTED counter-proposal to DoE's originally specced
    # trustworthy" — but `--probe-signal raise` is a caller ASSERTION, not
    # This predicate tests `scout_evidence` EMPTINESS only, never its
    if resized_tshirt == "XXL" and not scout_evidence:
        detents.append("xxl_unprobed")

    # Advisory; keyed on the resolved size only, never on scout_evidence.
    if resized_tshirt == "XXL":
        detents.append("xxl_infra_check_owed")

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
            ("The ask bundles independent initiatives: offer `split` first. " if bundled_labels else "")
            + "XL resolves to pm-decision, not a room. Surface the exits to the PM — "
            "shape (JTBD unclear), roadmap (spans >=2 workstreams / needs an initiative "
            "FK), or accept_multi_session (one coherent job, simply large, PM assents). "
            "The choice is recorded in the sizing-object's xl_exit field; the engine does "
            "not auto-select, and a null xl_exit never means accept."
        )
    elif resolved_route == "goal-setting":
        next_move = (
            "XXL resolves to goal-setting, not a plan. This ask is OKR-programme scale — "
            "fire coordinator:goal-blitz on this sizing (PM-gated). Enter coordinator:goal-setting "
            "only when the PM clearly asks to work it hands-on; in doubt, fire the workflow."
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
    elif resolved_route == "plan" and sizing_acceptance_skipped(resolved_route, resized_tshirt):
        # next_move is the line the EM pushes verbatim, so the no-ask rule lives here, not in prose.
        next_move = (
            f"Route to {resolved_route}. No PM ask at {resized_tshirt}: the engine has "
            "accepted this sizing. Write it with --write, then make the "
            f"{_warp_fire.SUBMIT_WORKFLOW_NAME} submission call it prints, this turn."
        )
    else:
        next_move = f"Route to {resolved_route}."

    if "goal_setting_pm_gated" in detents and "appetite_exceeded" in detents:
        next_move += (
            " This also resolves to goal-setting (XXL, OKR-programme scale) — fire "
            "coordinator:goal-blitz on this sizing (PM-gated), in addition to the appetite "
            "fork above."
        )

    # (XXL plan's call — it is a vocabulary value in XL_EXIT_ENUM, not a
    if "post_size_prompt_pending" in detents:
        question = _post_size_question(resized_tshirt, resolved_route)
        if resolved_route == "pm-decision":
            next_move += f" Put that to the PM as ONE open ask, not two: {question}"
        else:
            next_move += f" {question}"

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

    mode_touchpoints = touchpoints(interaction_mode, resized_tshirt, resolved_route)

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

    fire = None
    if resolved_route in _warp_fire.FIRE_ROUTES:
        # The sizing's path is known once scaffolded; the run carries the route from there.
        fire = _warp_fire.fire_command(_SCAFFOLDED_SIZING)
        next_move = _warp_fire.fire_step(_SCAFFOLDED_SIZING) + " " + next_move
    touchpoint_ids = ", ".join(t["id"] for t in mode_touchpoints) or "none"
    next_move += (
        f" Mode {interaction_mode} (source: {interaction_mode_source or 'default'}) "
        f"touchpoints at this size: {touchpoint_ids}."
    )

    return {
        "route": resolved_route,
        "detents": detents,
        "initiatives": bundled_labels,
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
        "fire": fire,
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


def parse_click_path(raw: str) -> dict:
    """`"<role>: <nav step> > <step> > ..."` -> `{role, steps}`, nav entry first."""
    role, sep, rest = raw.partition(":")
    steps = [s.strip() for s in rest.split(">") if s.strip()]
    if not sep or not role.strip() or len(steps) < 2:
        raise SizingAssembleError(
            f"--click-path {raw!r}: expected '<role>: <nav step> > <step>' with at least two steps"
        )
    return {"role": role.strip(), "steps": steps}


def _render_block(mapping: dict | list) -> str:
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
    deliverable_id: Optional[str] = None,
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
    from coordinator_core.bin_lib_binding import exec_module_bin_bound

    exec_module_bin_bound(spec.loader, mod, str(doc_new.parent))
    recorded = premise_provenance not in (None, "unrecorded")
    text = mod._scaffold_sizing(
        title=intent,
        deliverable_id=deliverable_id,
        premise=premise_provenance if recorded else None,
        premise_evidence=premise_evidence if recorded else None,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "x", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    from coordinator_core.session.declared_writes import declare_write

    declare_write(target)


_CLAUSE_RE = re.compile(
    r"^[ \t]*(?:\d+[.)]|\(\d+\)|\([A-Za-z]\)|[-*•▪●◦‣])[ \t]+(\S.*?)[ \t]*$"
)
_AGENT_MESSAGE_RE = re.compile(r"<agent-message\b.*?</agent-message>", re.DOTALL)
_FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")


def pm_clause_list(texts: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """The enumerated clauses of the PM texts, in the order given.

    `texts` is `[(key, text)]`, `key` the id stem of the text (`T16`, `I`, `V0`). A text
    contributes only when it holds two or more enumerated lines (`N.`, `N)`, `(N)`, `(a)`,
    `-`, `*`, a bullet glyph); `<agent-message>` blocks, fenced code and `>`-quoted lines are
    not PM words and are skipped. Each clause is `{id, anchor, ref, text}`: `id` is
    `<key>.<k>` (`k` 1-based within its text), `text` the line with its enumerator stripped.
    Pure; stdlib `re` only."""
    clauses: list[dict[str, Any]] = []
    for key, text in texts:
        text = _AGENT_MESSAGE_RE.sub("", text or "")
        found: list[str] = []
        fenced = False
        for line in text.replace("\r\n", "\n").split("\n"):
            if _FENCE_RE.match(line):
                fenced = not fenced
                continue
            if fenced or line.lstrip().startswith(">"):
                continue
            m = _CLAUSE_RE.match(line)
            if m:
                found.append(m.group(1))
        if len(found) < 2:
            continue
        anchor_stem = _anchor_stem(key)
        for k, clause in enumerate(found, 1):
            clauses.append(
                {"id": f"{key}.{k}", "anchor": f"{anchor_stem}, clause {k}", "ref": anchor_stem,
                 "text": clause}
            )
    return clauses


def _pm_texts(doc: dict[str, Any]) -> list[tuple[str, str]]:
    """The PM words of a sizing record: every `pm_verbatims[].text` in turn order, then `intent`
    when `intent_source` is `pm-verbatim`."""
    verbatims = [v for v in (doc.get("pm_verbatims") or []) if isinstance(v, dict)]
    keyed = []
    for i, v in enumerate(verbatims):
        turn = v.get("turn")
        keyed.append(
            (turn if isinstance(turn, int) else i, f"T{turn}" if isinstance(turn, int) else f"V{i}",
             str(v.get("text") or ""))
        )
    texts = [(key, text) for _, key, text in sorted(keyed, key=lambda e: e[0])]
    if doc.get("intent_source") == "pm-verbatim" and doc.get("intent"):
        texts.append(("I", str(doc["intent"])))
    return texts


_ROW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_UNRELATED_RE = re.compile(r"^\s*unrelated\b\s*[-—–:]*\s*(.*)$", re.IGNORECASE | re.DOTALL)


def _anchor_stem(key: str) -> str:
    if key == "I":
        return "intent"
    return f"pm_verbatims turn {key[1:]}" if key.startswith("T") else f"pm_verbatims entry {key[1:]}"


def _scout_unrelated(accounting: Any) -> dict[int, str]:
    """`{turn: reason}` for the turns a scout's `verbatim_accounting` marks `unrelated — <reason>`.
    Accepts a `{turn: account}` mapping or a list of `{turn, ...}` entries whose `unrelated`
    value, or any string value, carries the reason."""
    entries: list[tuple[Any, Any]] = []
    if isinstance(accounting, dict):
        entries = list(accounting.items())
    elif isinstance(accounting, list):
        for e in accounting:
            if isinstance(e, dict) and "turn" in e:
                rest = {k: v for k, v in e.items() if k != "turn"}
                value = rest.get("unrelated")
                if isinstance(value, str):
                    value = f"unrelated — {value}"
                elif value is None:
                    value = next((v for v in rest.values() if isinstance(v, str)), None)
                entries.append((e["turn"], value))
    out: dict[int, str] = {}
    for turn, value in entries:
        try:
            turn = int(turn)
        except (TypeError, ValueError):
            continue
        m = _UNRELATED_RE.match(value) if isinstance(value, str) else None
        if m:
            out[turn] = m.group(1).strip() or "unrelated (scout gave no reason)"
    return out


RELAYED_TURN_REASON = "relayed ask: session PM turn, not the relayed ask's text"


def derive_register(
    doc: dict[str, Any],
    scout: Optional[dict[str, Any]] = None,
    relayed_ask: Optional[str] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], list[str]]:
    """The requirement register a sizing's PM words imply: `(rows, unrelated_turns, refs, dropped)`.

    Every PM text (`_pm_texts`) ends covered in the `coverage_gaps` sense. In order:
      - the scout's `register_rows` whose `source_text` is verbatim (whitespace-normalised) in
        some PM text; one that is not is listed in `dropped` by id, never paraphrased in;
      - the scout's `verbatim_accounting` `unrelated — <reason>` turns, for an embedded turn
        no kept row covers;
      - an uncovered text with two or more enumerated lines: one row per clause;
      - any other uncovered text: one row per PM-worded segment between `<agent-message>`
        blocks; a turn with none is `unrelated` (relayed agent message only).
    Rows are `open`, unclaimed, `surface` the scout's when valid else `internal`.
    With `relayed_ask` text: exactly one row `R1` carrying that text, every embedded `T<n>` turn
    listed unrelated, and the scout's rows not admitted. Pure."""
    from coordinator_core.ops import requirement_register as rr

    texts = _pm_texts(doc)
    if relayed_ask is not None and relayed_ask.strip():
        from coordinator_core.ops.dispatch_emit.requirements_ledger import ORIGIN_RELAYED

        row = {
            rr.ROW_ID: "R1", rr.ROW_TEXT: relayed_ask.strip(), rr.ROW_ANCHOR: ORIGIN_RELAYED,
            rr.ROW_SURFACE: "internal", rr.ROW_STATUS: rr.STATUS_OPEN, rr.ROW_CLAIMED_BY: [],
        }
        turns = sorted(int(k[1:]) for k, _ in texts if k.startswith("T"))
        return [row], [{"turn": t, "reason": RELAYED_TURN_REASON} for t in turns], ["relayed ask"], []
    normed = {key: rr._norm(text) for key, text in texts}
    rows: list[dict[str, Any]] = []
    refs: list[str] = []
    dropped: list[str] = []
    taken: set[str] = set()

    def add(rid: str, source_text: str, key: str, anchor: str, surface: str = "internal") -> None:
        base, k = rid, 1
        while rid in taken:
            k += 1
            rid = f"{base}-{k}"
        taken.add(rid)
        rows.append({
            rr.ROW_ID: rid, rr.ROW_TEXT: source_text, rr.ROW_ANCHOR: anchor,
            rr.ROW_SURFACE: surface, rr.ROW_STATUS: rr.STATUS_OPEN, rr.ROW_CLAIMED_BY: [],
        })
        ref = _anchor_stem(key)
        if ref not in refs:
            refs.append(ref)

    def covered(key: str) -> bool:
        return any(rr._norm(r[rr.ROW_TEXT]) in normed[key] for r in rows)

    scout = scout if isinstance(scout, dict) else {}
    for n, proposed in enumerate(scout.get("register_rows") or [], 1):
        if not isinstance(proposed, dict):
            continue
        rid = str(proposed.get("id") or "").strip()
        rid = rid if _ROW_ID_RE.match(rid) else f"S{n}"
        source_text = str(proposed.get("source_text") or "").strip()
        needle = rr._norm(source_text)
        key = next((k for k, _ in texts if needle and needle in normed[k]), None)
        if key is None:
            dropped.append(rid)
            continue
        surface = proposed.get("surface")
        add(rid, source_text, key, _anchor_stem(key),
            surface if surface in rr.SURFACES else "internal")

    unrelated: list[dict[str, Any]] = []
    turn_keys = {int(k[1:]): k for k, _ in texts if k.startswith("T")}
    for turn, reason in sorted(_scout_unrelated(scout.get("verbatim_accounting")).items()):
        if turn in turn_keys and not covered(turn_keys[turn]):
            unrelated.append({"turn": turn, "reason": reason})
    waived = {e["turn"] for e in unrelated}

    for key, text in texts:
        if covered(key) or (key.startswith("T") and int(key[1:]) in waived):
            continue
        clauses = pm_clause_list([(key, text)])
        if clauses:
            for c in clauses:
                add(c["id"], c["text"], key, c["anchor"])
            continue
        segments = [s.strip() for s in _AGENT_MESSAGE_RE.split(text or "") if s.strip()]
        if not segments:
            if key.startswith("T"):
                unrelated.append({"turn": int(key[1:]), "reason": "relayed agent message only, no PM words"})
            continue
        for k, segment in enumerate(segments, 1):
            add(key if len(segments) == 1 else f"{key}.p{k}", segment, key, _anchor_stem(key))
    unrelated.sort(key=lambda e: e["turn"])
    return rows, unrelated, refs, dropped


def _derived_register_block(
    rows: list[dict[str, Any]], unrelated: list[dict[str, Any]], refs: list[str], sizing_rel: str,
) -> dict[str, Any]:
    """The register block for `derive_register`'s output."""
    from coordinator_core.ops import requirement_register as rr

    block: dict[str, Any] = {rr.SOURCES_KEY: [{"doc": sizing_rel, "ref": ref} for ref in refs]}
    if unrelated:
        block[rr.UNRELATED_TURNS_KEY] = unrelated
    block[rr.ROWS_KEY] = rows
    return block


def _mint_register(
    text: str, rows: list[dict[str, Any]], unrelated: list[dict[str, Any]], refs: list[str],
    sizing_rel: str, now: Any,
) -> str:
    """`text` with `derive_register`'s register written through
    `requirement_register.write_register_text`."""
    from coordinator_core.ops import requirement_register as rr

    return rr.write_register_text(
        text, _derived_register_block(rows, unrelated, refs, sizing_rel), now
    )


def write_back(root: Path, sizing: str, decision: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """`_write_back`, holding its nothing-written-on-refusal contract for a record it scaffolds:
    a scaffold created by this call is removed when the write is then refused."""
    candidate = Path(sizing) if Path(sizing).is_absolute() else Path(root) / sizing
    existed = candidate.is_file()
    try:
        return _write_back(root, sizing, decision, **kwargs)
    except SizingAssembleError:
        if not existed:
            candidate.unlink(missing_ok=True)
        raise


def _write_back(
    root: Path,
    sizing: str,
    decision: dict[str, Any],
    *,
    exit_criterion: Optional[str] = None,
    click_paths: Optional[list] = None,
    interaction_mode: Optional[str] = None,
    premise_provenance: Optional[str] = None,
    premise_evidence: Optional[str] = None,
    scout_evidence: Optional[list[str]] = None,
    deliverable_id: Optional[str] = None,
    research: Optional[dict[str, Any]] = None,
    design_route: Optional[str] = None,
    verbatim_turns: Optional[tuple[int, int]] = None,
    intent_verbatim: bool = False,
    name: Optional[str] = None,
    scout_register: Optional[dict[str, Any]] = None,
    doc_consumers: Optional[list[str]] = None,
    ask_conflicts: Optional[list[dict[str, str]]] = None,
    embed_verbatims: bool = True,
    relayed_ask_text: Optional[str] = None,
    acceptance_rows: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Apply a `route()` decision to an existing `state/sizings/` sizing under
    `locked_rmw`, schema-validated before the write.

    Sets estimate.tshirt, route, detents, fork/xl_exit (only when the decision
    resolved one), scout_evidence, the scout's doc_consumers and ask_conflicts, and premise
    (only when given; an empty list is a claim and is written), and status
    `draft`/`sized` -> `routed` (terminal statuses are never regressed); a
    scaffold-placeholder `intent` is replaced by the computed one. exit_criterion.statement is written only while
    `accepted` is null or the engine's own record; interaction_mode only while absent. Where
    the size rule skips the acceptance ask and a statement exists, `accepted` is
    the engine-size-rule record; a re-size out of the rule clears it. Raises
    `SizingAssembleError` on any refusal, with nothing written. `verbatim_turns=(A, B)` embeds
    exactly turns A..B (unbudgeted) instead of the recent window, and refuses a missing index or
    an already-embedded `pm_verbatims`. The result's `pm_verbatims` reports what was embedded.
    `intent_verbatim=True` overwrites `intent` with the decision's intent byte-for-byte (never stripped) and sets
    `intent_source: pm-verbatim`. `name` (silently capped at 60 chars) becomes the real `name:` line,
    replacing the commented template line, unless the record already carries a `name`.
    A first embed with no register on the record derives one (`derive_register`), seeded by
    `scout_register` (the scout's report: `register_rows`, `verbatim_accounting`) when given.
    """
    import json

    import yaml

    from coordinator_core.frontmatter.primitives import write_fm_nested_field
    from coordinator_core.frontmatter.schema_validate import (
        format_validation_errors,
        validate_frontmatter,
    )
    from datetime import datetime, timezone

    from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
    from coordinator_core.ops._path_guard import contained_path
    from coordinator_core.ops.requirement_register import REGISTER_KEY

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
        _scaffold_missing(
            root, target, decision, premise_provenance, premise_evidence, deliverable_id
        )
    record_premise = premise_provenance not in (None, "unrecorded")
    if record_premise and not (premise_evidence or "").strip():
        raise SizingAssembleError("--premise-provenance given without --premise-evidence")
    from coordinator_core.ops.baton_pm_turns import bound_window

    if not embed_verbatims:
        pm_verbatims = []
    elif verbatim_turns is None:
        # Bound by content: the turns the intent quotes, whatever their age, then recent ones.
        pm_verbatims = bound_window(decision.get("intent"), cwd=str(root))
    else:
        from coordinator_core.ops.baton_pm_turns import read_turns

        lo, hi = verbatim_turns
        chosen = read_turns(start=lo, end=hi, cwd=str(root))["turns"]
        have = {t["turn"] for t in chosen}
        missing = [n for n in range(lo, hi + 1) if n not in have]
        if missing:
            raise SizingAssembleError(f"--verbatim-turns {lo}-{hi}: no recorded turn {missing}")
        pm_verbatims = [
            {"turn": t["turn"], "ts": t.get("ts"), "text": t["prompt"]}
            for t in sorted(chosen, key=lambda t: t["turn"], reverse=True)
        ]
    embedded: list[dict[str, Any]] = []
    minted: list[str] = []
    dropped: list[str] = []
    coverage: list[Any] = []
    try:
        sizing_rel = target.relative_to(root).as_posix()
    except ValueError:
        sizing_rel = f"state/sizings/{target.name}"

    def mutate(old: str) -> str:
        try:
            doc = yaml.safe_load(old) or {}
        except Exception as exc:  # noqa: BLE001
            raise MutateAbort(f"YAML parse error: {exc}") from exc
        if not isinstance(doc, dict):
            raise MutateAbort("sizing-object is not a YAML mapping")
        text = old
        if name and name.strip() and not doc.get("name"):
            raw_name = json.dumps(name.strip()[:60], ensure_ascii=False)
            text, swapped = re.subn(
                r"^# name:.*$", lambda _m: f"name: {raw_name}", text, count=1, flags=re.MULTILINE
            )
            if not swapped:
                text = _set_scalar(text, "name", raw_name)
        # Sizing an existing baton joins its deliverable; a different id already on the record is never overwritten.
        if deliverable_id:
            current = doc.get("deliverable_id")
            if current and current != deliverable_id:
                raise MutateAbort(
                    f"deliverable_id is {current!r}, not {deliverable_id!r}; refusing to re-key the record"
                )
            if not current:
                text = _set_scalar(text, "deliverable_id", json.dumps(deliverable_id))
        elif not doc.get("deliverable_id"):
            # The plan authored from this sizing carries its id verbatim
            # (doc-new's cited-sizing carry), so the spine join key is minted
            # once, here, by the shared minter, never again on the plan side.
            from coordinator_core.ops.mint_deliverable_id import mint, slug_from_title

            slug = slug_from_title(str(decision.get("intent") or doc.get("intent") or "")) or slug_from_title(
                target.stem
            )
            text = _set_scalar(text, "deliverable_id", json.dumps(mint(slug=slug)[0]))

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
        if doc_consumers is not None:
            text = _set_scalar(text, "doc_consumers", json.dumps(list(doc_consumers)))
        if ask_conflicts is not None:
            text = _set_scalar(text, "ask_conflicts", json.dumps(list(ask_conflicts)))
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
        existing = doc.get("exit_criterion")
        existing = dict(existing) if isinstance(existing, dict) else {}
        prior_accepted = existing.get("accepted")
        # The engine's own record is the engine's to refresh: a re-size out of the rule
        # (e.g. to XL) must not leave a stale skip behind. PM and APM records are never touched.
        engine_owned = (
            prior_accepted is None
            or (isinstance(prior_accepted, dict) and prior_accepted.get("source") == ENGINE_SIZE_RULE)
        )
        if engine_owned:
            statement = (exit_criterion or "").strip() or str(existing.get("statement") or "").strip()
            if exit_criterion and exit_criterion.strip():
                existing["statement"] = statement
                if click_paths:
                    existing["click_paths"] = click_paths
            mode = doc.get("interaction_mode") or interaction_mode
            route_now, tshirt_now = decision["route"], decision["resolved_estimate"]["tshirt"]
            skipped = bool(statement) and sizing_acceptance_skipped(route_now, tshirt_now)
            accepted = (
                {
                    "source": ENGINE_SIZE_RULE,
                    "rule": ENGINE_SIZE_RULE,
                    "route": route_now,
                    "tshirt": tshirt_now,
                    "on": date.today().isoformat(),
                    "mode": mode,
                }
                if skipped
                else None
            )
            if skipped and isinstance(prior_accepted, dict) and all(
                prior_accepted.get(k) == accepted[k] for k in ("route", "tshirt", "mode")
            ):
                accepted = prior_accepted  # unchanged skip keeps its original date
            if (exit_criterion and exit_criterion.strip()) or accepted != prior_accepted:
                existing["accepted"] = accepted
                text = write_fm_nested_field(text, "exit_criterion", _render_block(existing))
        if research:
            text = write_fm_nested_field(text, "research", _render_block(research))
        if design_route is not None:
            from coordinator_core.ops.dispatch_emit import design_route as _dr

            try:
                block = _dr.with_route(doc.get(_dr.DESIGN_ROUTE_KEY), design_route)
            except ValueError as exc:
                raise MutateAbort(str(exc)) from exc
            text = write_fm_nested_field(text, _dr.DESIGN_ROUTE_KEY, _render_block(block))
        if acceptance_rows:
            text = write_fm_nested_field(text, "acceptance_rows", _render_block(acceptance_rows))
        if decision.get("initiatives"):
            # Holds the bundle's free-form labels (route()'s `bundled_labels`), not the
            # state/initiatives ids the vendored schema description names; the schema types only strings.
            text = write_fm_nested_field(text, "initiatives", _render_block(decision["initiatives"]))
        if interaction_mode and not doc.get("interaction_mode"):
            text = _set_scalar(text, "interaction_mode", interaction_mode)
        # First write only: once present, the EM curates it by hand and a
        # re-run must not restore what they cut.
        if verbatim_turns is not None and "pm_verbatims" in doc:
            raise MutateAbort(
                "sizing already carries pm_verbatims; hand-edit the pm_verbatims block "
                "instead of re-embedding"
            )
        if "pm_verbatims" not in doc and pm_verbatims:
            embedded[:] = pm_verbatims
            text = write_fm_nested_field(text, "pm_verbatims", _render_block(pm_verbatims))
        intent_now = doc.get("intent")
        computed_intent = (decision.get("intent") or "").strip()
        if intent_verbatim:
            text = _set_scalar(text, "intent", json.dumps(decision.get("intent") or "", ensure_ascii=False))
            text = _set_scalar(text, "intent_source", "pm-verbatim")
        elif (
            computed_intent
            and isinstance(intent_now, str)
            and intent_now.startswith("PLACEHOLDER")
        ):
            text = _set_scalar(text, "intent", json.dumps(computed_intent))
        # A computed route is a routed sizing; only the pre-route statuses advance.
        if doc.get("status") in ("draft", "sized"):
            text = _set_scalar(text, "status", "routed")

        try:
            new_doc = yaml.safe_load(text) or {}
        except Exception as exc:  # noqa: BLE001
            raise MutateAbort(f"post-mutation YAML parse error: {exc}") from exc
        if (embedded or intent_verbatim) and isinstance(new_doc, dict):
            ruled = new_doc.get("pm_resolution")
            ruled = isinstance(ruled, dict) and REGISTER_KEY in ruled
            if REGISTER_KEY not in new_doc and not ruled:
                rows, unrelated, refs, dropped[:] = derive_register(
                    new_doc, scout_register, relayed_ask_text
                )
                if rows:
                    text = _mint_register(
                        text, rows, unrelated, refs, sizing_rel, datetime.now(timezone.utc)
                    )
                    try:
                        new_doc = yaml.safe_load(text) or {}
                    except Exception as exc:  # noqa: BLE001
                        raise MutateAbort(f"post-mint YAML parse error: {exc}") from exc
                    if REGISTER_KEY in new_doc:
                        minted[:] = [r["id"] for r in rows]
        errors = validate_frontmatter(new_doc, schema)
        if errors:
            raise MutateAbort(
                f"post-mutation schema validation failed: {format_validation_errors(errors)}"
            )
        if embedded and isinstance(new_doc, dict):
            from coordinator_core.ops import requirement_register as _rr

            coverage[:] = [len(_rr.embedded_turns(new_doc)), _rr.coverage_gaps(new_doc)]
        return text

    try:
        locked_rmw(target, mutate, repo_root=root)
    except MutateAbort as exc:
        raise SizingAssembleError(str(exc.args[0]) if exc.args else "mutation aborted") from exc
    except LockTimeout as exc:
        raise SizingAssembleError(f"timed out waiting for file lock on {target}: {exc}") from exc
    import sys

    if minted:
        print(
            f"derived {len(minted)} requirement_register rows ({', '.join(minted)}) on {sizing_rel}; "
            f"amend: sizing-assemble --register - --write {sizing_rel} (stdin overlay: rows by id, "
            "drop: [ids], unrelated_turns)",
            file=sys.stderr,
        )
    if dropped:
        print(
            f"{sizing_rel}: scout register row(s) {', '.join(dropped)} dropped: source_text is not "
            "verbatim in any PM turn",
            file=sys.stderr,
        )
    register_coverage = None
    if coverage:
        total, gaps = coverage
        register_coverage = {"turns_total": total, "turns_uncovered": gaps}
        if gaps:
            print(
                f"{sizing_rel}: PM turn(s) {', '.join(map(str, gaps))} not covered by any "
                "requirement_register row; amend: sizing-assemble --register - "
                f"--write {sizing_rel}",
                file=sys.stderr,
            )
    turns = [e["turn"] for e in embedded if isinstance(e.get("turn"), int)]
    if turns:
        from coordinator_core.ops.baton_pm_turns import mark_sized_through

        mark_sized_through(max(turns), cwd=str(root))
    return {
        "path": str(target),
        "status": "written",
        "pm_verbatims": {
            "embedded": len(embedded),
            "turns": [min(turns), max(turns)] if turns else None,
        },
        **({"register_coverage": register_coverage} if register_coverage else {}),
    }


def parse_acceptance_rows(raw: str) -> tuple[Optional[list[dict[str, Any]]], list[str]]:
    """The `--acceptance-rows-json` value as a list of `{statement, writes, check}` rows, with
    one refusal string per bad value. Every string must be non-empty and `writes` a non-empty
    list of them; rows are rebuilt with exactly those three keys."""
    import json

    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, [f"--acceptance-rows-json is not valid JSON: {exc}"]
    if not isinstance(data, list) or not data:
        return None, ["--acceptance-rows-json must be a non-empty JSON array"]
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for n, item in enumerate(data):
        if not isinstance(item, dict):
            errors.append(f"--acceptance-rows-json row {n} is not an object")
            continue
        errors_before = len(errors)
        extra = sorted(set(item) - {"statement", "writes", "check"})
        if extra:
            errors.append(f"--acceptance-rows-json row {n} has unknown keys {extra}")
        statement, writes, check = item.get("statement"), item.get("writes"), item.get("check")
        if not (isinstance(statement, str) and statement.strip()):
            errors.append(f"--acceptance-rows-json row {n} needs a non-empty statement")
        if not (
            isinstance(writes, list)
            and writes
            and all(isinstance(w, str) and w.strip() for w in writes)
        ):
            errors.append(f"--acceptance-rows-json row {n} needs writes: a non-empty list of non-empty strings")
        if not (isinstance(check, str) and check.strip()):
            errors.append(f"--acceptance-rows-json row {n} needs a non-empty check")
        if len(errors) == errors_before:
            rows.append({"statement": statement, "writes": list(writes), "check": check})
    return (None, errors) if errors else (rows, [])


def build_research_block(
    value_class: Optional[str],
    appetite: Optional[str],
    sources: list[str],
    depth: Optional[str],
    targets: list[str],
    questions: Optional[list[str]] = None,
    destination: Optional[str] = None,
    question_mode: Optional[str] = None,
    question_targets: Optional[list[str]] = None,
    seeds: Optional[list[str]] = None,
) -> tuple[Optional[dict[str, Any]], list[str]]:
    """The sizing-object `research:` block from the --research-* flag values, with
    one refusal string per bad value. `(None, [])` when no research flag was given.
    `question_targets` are raw `<n>=<source>[:<ref>]` strings, split on the first `=` and
    then the first `:` so a ref holding a colon stays intact. `seeds` are raw `<kind>=<url>`
    strings split on the first `=`; repeats of a (kind, url) pair are dropped."""
    questions = [q.strip() for q in questions or ()]
    question_targets = list(question_targets or ())
    seeds = list(seeds or ())
    if not (
        value_class or appetite or sources or depth or targets or questions or destination
        or question_mode or question_targets or seeds
    ):
        return None, []
    from coordinator_core.ops import _research_contract as rc

    errors: list[str] = []
    if value_class is None:
        errors.append("--research-class is required whenever another --research-* flag is given")
    elif value_class not in rc.VALUE_CLASSES:
        errors.append(f"--research-class must be one of {rc.VALUE_CLASSES}, got {value_class!r}")
    if appetite is not None and appetite not in rc.APPETITES:
        errors.append(f"--research-appetite must be one of {rc.APPETITES}, got {appetite!r}")
    ordered_sources: list[str] = []
    for src in sources:
        if src not in rc.SOURCES:
            errors.append(f"--research-source must be one of {rc.SOURCES}, got {src!r}")
        elif src not in ordered_sources:
            ordered_sources.append(src)
    if depth is not None and depth not in rc.DEPTHS:
        errors.append(f"--research-depth must be one of {rc.DEPTHS}, got {depth!r}")
    parsed_targets: list[dict[str, str]] = []
    for raw in targets:
        source, sep, ref = raw.partition("=")
        if not sep or not ref.strip():
            errors.append(f"--research-target {raw!r}: expected <source>=<ref>")
        elif source not in rc.SOURCES:
            errors.append(f"--research-target source must be one of {rc.SOURCES}, got {source!r}")
        else:
            parsed_targets.append({"source": source, "ref": ref.strip()})
    parsed_seeds: list[dict[str, str]] = []
    for raw in seeds:
        kind, sep, url = raw.partition("=")
        url = url.strip()
        if not sep or not url:
            errors.append(f"--research-seed {raw!r}: expected <kind>=<url>")
        elif kind not in rc.SEED_KINDS:
            errors.append(f"--research-seed kind must be one of {rc.SEED_KINDS}, got {kind!r}")
        elif any(ch.isspace() for ch in url):
            errors.append(f"--research-seed {raw!r}: url must not contain whitespace")
        elif {"kind": kind, "url": url} not in parsed_seeds:
            parsed_seeds.append({"kind": kind, "url": url})
    if any(not q for q in questions):
        errors.append("--research-question must not be empty")
    if destination is not None and destination not in rc.DESTINATIONS:
        errors.append(f"--research-destination must be one of {rc.DESTINATIONS}, got {destination!r}")
    if question_mode is not None and question_mode not in rc.SCOUT_MODES:
        errors.append(f"--research-question-mode must be one of {rc.SCOUT_MODES}, got {question_mode!r}")
    parsed_question_targets: list[dict[str, Any]] = []
    flag = "--research-question-target"
    for raw in question_targets:
        n_raw, sep, rest = raw.partition("=")
        if not sep:
            errors.append(f"{flag} {raw!r}: expected <n>=<source>[:<ref>]")
            continue
        try:
            n = int(n_raw.strip())
        except ValueError:
            errors.append(f"{flag} {raw!r}: question number must be a positive integer")
            continue
        if n < 1:
            errors.append(f"{flag} {raw!r}: question number must be a positive integer")
            continue
        source, colon, ref = rest.partition(":")
        source, ref = source.strip(), ref.strip()
        if source not in rc.SCOUT_TARGETS:
            errors.append(f"{flag} {raw!r}: source must be one of {rc.SCOUT_TARGETS}, got {source!r}")
        elif source == "web" and colon and ref:
            errors.append(f"{flag} {raw!r}: a web target takes no ref")
        elif source != "web" and not ref:
            errors.append(f"{flag} {raw!r}: a {source} target needs a ref (<n>={source}:<ref>)")
        elif questions and n > len(questions):
            errors.append(f"{flag} {raw!r}: question {n} exceeds the {len(questions)} --research-question values")
        else:
            entry: dict[str, Any] = {"question": n, "source": source}
            if source != "web":
                entry["ref"] = ref
            parsed_question_targets.append(entry)
    if question_targets and not questions:
        errors.append("--research-question-target needs --research-question")
    if errors:
        return None, errors
    block: dict[str, Any] = {"value_class": value_class}
    if appetite is not None:
        block["appetite"] = appetite
    if ordered_sources:
        block["sources"] = ordered_sources
    if depth is not None:
        block["depth"] = depth
    if parsed_targets:
        block["targets"] = parsed_targets
    if parsed_seeds:
        block["seeds"] = parsed_seeds
    if questions:
        block["questions"] = questions
    if destination is not None:
        block["destination"] = destination
    if question_mode is not None:
        block["question_mode"] = question_mode
    if parsed_question_targets:
        block["question_targets"] = parsed_question_targets
    return block, []


#: A research sizing fires its own chain; the t-shirt's route is not this ask's room.
RESEARCH_STAGES = ("research fire (emit-dispatch-workflow --from-sizing)", "research.close")


def _route_to_research(decision: dict[str, Any], write_path: str) -> None:
    """Point the reply's chain at the research fire: `route` stays the size's room on disk,
    but `stages` and `next_move` name the research chain the /research command runs."""
    decision["stages"] = {"rows": list(RESEARCH_STAGES), "terminal": RESEARCH_STAGES[-1], "owned_by": "research"}
    decision["next_move"] = (
        f"Research sizing: fire it with `emit-dispatch-workflow --from-sizing {write_path}`, then "
        "run the reply's `next_action` (research.close) on return. Do not route to "
        f"{decision['route']}: that is the size's room, not this ask's chain."
    )


def amend_research(root: Path, sizing: str, given: dict[str, Any]) -> dict[str, Any]:
    """Rewrite only the sizing's `research:` block: `given` keys (flag spellings: `sources` and
    `targets` as raw flag lists) replace the recorded ones, the rest are kept. Validated by
    `build_research_block` and the sizing schema; nothing else on the record moves."""
    import yaml

    from coordinator_core.frontmatter.primitives import write_fm_nested_field
    from coordinator_core.frontmatter.schema_validate import format_validation_errors, validate_frontmatter
    from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
    from coordinator_core.ops._path_guard import contained_path

    schema = Path(__file__).resolve().parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"
    candidate = Path(sizing) if Path(sizing).is_absolute() else root / sizing
    target = contained_path(candidate, [root / "state" / "sizings"])
    if target is None or not target.is_file():
        raise SizingAssembleError(
            f"{sizing!r} is not a sizing under {(root / 'state' / 'sizings').as_posix()}/ "
            f"(read as {candidate.as_posix()})"
        )
    out: dict[str, Any] = {}

    def mutate(old: str) -> str:
        doc = yaml.safe_load(old) or {}
        prior = doc.get("research") if isinstance(doc.get("research"), dict) else {}
        merged = {
            "value_class": given.get("value_class", prior.get("value_class")),
            "appetite": given.get("appetite", prior.get("appetite")),
            "sources": given.get("sources", prior.get("sources") or []),
            "depth": given.get("depth", prior.get("depth")),
            "targets": given.get(
                "targets", [f"{t['source']}={t['ref']}" for t in prior.get("targets") or []]
            ),
            "seeds": (
                []
                if given.get("clear_seeds") and not given.get("seeds")
                else given.get("seeds", [f"{s['kind']}={s['url']}" for s in prior.get("seeds") or []])
            ),
            "questions": given.get("questions", prior.get("questions") or []),
            "destination": given.get("destination", prior.get("destination")),
            "question_mode": given.get("question_mode", prior.get("question_mode")),
            "question_targets": given.get(
                "question_targets",
                [
                    f"{t['question']}={t['source']}" + (f":{t['ref']}" if t.get("ref") else "")
                    for t in prior.get("question_targets") or []
                ],
            ),
        }
        if given.get("sources") and merged["seeds"]:
            from coordinator_core.ops import _research_contract as rc

            stranded = [
                s for s in merged["seeds"]
                if rc.SEED_KIND_SOURCE.get(s.partition("=")[0]) not in (None, *merged["sources"])
            ]
            if stranded:
                raise MutateAbort(
                    f"--research-source {','.join(merged['sources'])} excludes the source of seeds "
                    f"{', '.join(stranded)}; clear them with --clear-research-seeds "
                    "or replace them with --research-seed <kind>=<url>"
                )
        block, errors = build_research_block(
            merged["value_class"], merged["appetite"], list(merged["sources"]), merged["depth"],
            list(merged["targets"]), list(merged["questions"]), merged["destination"],
            merged["question_mode"], list(merged["question_targets"]), list(merged["seeds"]),
        )
        if errors:
            raise MutateAbort("; ".join(errors))
        text = write_fm_nested_field(old, "research", _render_block(block))
        problems = validate_frontmatter(yaml.safe_load(text) or {}, schema)
        if problems:
            raise MutateAbort(f"schema validation failed: {format_validation_errors(problems)}")
        out["research"] = block
        return text

    try:
        locked_rmw(target, mutate, repo_root=root)
    except MutateAbort as exc:
        raise SizingAssembleError(str(exc.args[0]) if exc.args else "mutation aborted") from exc
    except LockTimeout as exc:
        raise SizingAssembleError(f"timed out waiting for file lock on {target}: {exc}") from exc
    return {"path": str(target), "status": "amended", "research": out["research"]}


EXIT_OK = 0
EXIT_BUSINESS_FAIL = 1
EXIT_USAGE = 2
EXIT_TRANSPORT_FAIL = 3


def _parse_turn_range(raw: str) -> Optional[tuple[int, int]]:
    """`A-B` with ints 0 <= A <= B, else None."""
    lo, sep, hi = raw.partition("-")
    if not sep or not lo.isascii() or not hi.isascii() or not lo.isdigit() or not hi.isdigit():
        return None
    a, b = int(lo), int(hi)
    return (a, b) if a <= b else None


def _usage(prog: str, stream=None) -> int:
    stream = __import__("sys").stderr if stream is None else stream
    print(
        f"{prog}: usage: {prog} --tshirt <XS|S|M|L|XL|XXL>\n"
        "    [--appetite small|medium|large]\n"
        "    [--express-lane] [--probe-signal collapse|raise] [--jtbd-unclear]\n"
        "    [--well-trodden-step-change]\n"
        "    [--premise-provenance executed|read|not-applicable|unrecorded]\n"
        "    [--premise-evidence <str>]\n"
        "    [--boundary-in-notch yes|no]\n"
        "    [--scout-evidence-kind mention-count|change-set|site-count]\n"
        "    [--scout-evidence <str> ...]\n"
        "    [--intent <str> | --intent-turn N] [--intent-source pm-verbatim|em-elaborated]\n"
        "    [--doc-consumer <path> ... | --no-doc-consumers]\n"
        "    [--ask-conflict <path>[=<conflict>] ... | --no-ask-conflicts]\n"
        "    [--name <short label>]\n"
        "    [--precedent shipped-before|novel]\n"
        "    [--probe-raise-basis ask-scope|substrate-condition|breadth]\n"
        "    [--exit-criterion <str>]\n"
        "    [--interaction-mode hands-on|pm|ceo]\n"
        "    [--research-class scouts|corpus|deep (required with any other --research-*)]\n"
        "    [--research-appetite small|medium|large (research budget; distinct from --appetite)]\n"
        "    [--research-source web|repo|structured|notebooklm ...]\n"
        "    [--research-depth standard|deeper|deepest]\n"
        "    [--research-target <source>=<ref> ...]\n"
        "    [--research-seed <video|doc|repo|paper>=<url> ... (a URL to study; routed to its kind's pipeline)]\n"
        "    [--research-question <text> ...]\n"
        "    [--research-question-mode split|combine]\n"
        "    [--research-question-target <n>=<source>[:<ref>] ... (source web|repo|local; ref for repo/local)]\n"
        "    [--research-destination repo|local-only]\n"
        "    [--design-route canvas|reviewer|none (records design_route; requires --write)]\n"
        "    [--initiatives '[{\"label\":..,\"rows\":[..]}]' (2+ with disjoint rows at XL sets the bundle detent)]\n"
        "    [--acceptance-rows-json '[{\"statement\":..,\"writes\":[..],\"check\":..}]' (requires --write)]\n"
        "    [--write <state/sizings/x.yaml> [--json (print the decision instead of 'written: <path>')]\n"
        "    [--verbatim-turns A-B | --no-verbatims]\n"
        "    [--relayed-ask-file <path> (register only that text; embedded PM turns go to unrelated_turns)]]\n"
        "or: --amend-research <state/sizings/x.yaml> --research-* ... (rewrites only the research block;\n"
        "    --clear-research-seeds drops every seed, --research-seed replaces them)\n"
        "or: --xl-exit shape|roadmap|accept_multi_session --pm-quote <str>\n"
        "    [--decided-on YYYY-MM-DD] --write <state/sizings/x.yaml>\n"
        "or: --pm-resolution <key> --pm-quote <str>\n"
        "    [--decided-on YYYY-MM-DD] [--supersede] --write <state/sizings/x.yaml>\n"
        "    [--no-plan-hand-off (write only; decision carries pm_exit_question, no obligation or acceptance fires)]\n"
        "    [--scout-register <scout report json|yaml, or - for stdin> (seeds the derived register)]\n"
        "or: --register <yaml, or - for stdin> [--supersede] --write <state/sizings/x.yaml>\n"
        "    (an overlay on the recorded register: rows merged by id, drop: [ids], unrelated_turns\n"
        "    by turn; --supersede replaces it whole)\n"
        "    register yaml: sources: [{doc, ref}] (at least one)\n"
        "                   rows: [{id, source_text, surface: ui|api|cli|internal|visual, status?}]\n"
        "    status defaults to open, claimed_by to []\n"
        "or: --derive-register [--scout-register <path|->] [--supersede] --write <state/sizings/x.yaml>\n"
        "    (records the register derived from the sizing's embedded PM turns)",
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


def _open_routed_obligation(write_path: str) -> None:
    """Queue this session's sizing-routed watchdog obligation for the sizing just written.

    Fail-soft: the sizing is already on disk, and a missed obligation costs a nudge, never the write.
    """
    import os

    from coordinator_core.git.repo_root import show_toplevel
    from coordinator_core.hooks.watchdog_undischarged_next_move import open_sizing_routed

    root = show_toplevel(str(_cli_repo_root(write_path)))
    if root is None:
        return
    target = Path(write_path) if Path(write_path).is_absolute() else Path(root) / write_path
    from coordinator_core.session.core import resolve_session_id

    # The resolver, not os.environ: a warm-served request carries the caller's id in
    # a per-request binding that the server's own environment never holds.
    session_id = resolve_session_id() or None
    try:
        rel = target.resolve().relative_to(Path(root).resolve()).as_posix()
        open_sizing_routed(root, rel, session_id)
    except (ValueError, OSError):
        return
    # This write happens under Bash, which the Write/Edit touch tracker never sees;
    # without the claim the dispatch gate keeps answering "No sizing this session".
    from coordinator_core.session.touch_record import KIND_WRITE, append_touch_claims

    append_touch_claims([rel], session_id, root, kind=KIND_WRITE)


def _hand_to_engine_ask(write_path: str, decision: dict[str, Any]) -> int:
    """An accepted plan/spec-dispatch sizing at S-L prints the one `coordinator:engine-ask`
    submission call. A live run on the sizing refuses (decision JSON on stdout, the refusal
    naming the call and the run on stderr, business-fail), the check the submit hook denies on (session id only; no transcript path here)."""
    import json
    import os
    import sys

    from coordinator_core import warp_fire, warp_live

    repo_root = _cli_repo_root(write_path).resolve()
    target = Path(write_path) if Path(write_path).is_absolute() else repo_root / write_path
    try:
        sizing_rel = target.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        print(
            f"sizing-assemble: {write_path} is outside {repo_root}; make the "
            f"{warp_fire.SUBMIT_WORKFLOW_NAME} submission call by hand with a repo-relative sizing path",
            file=sys.stderr,
        )
        print(json.dumps(decision, indent=2, sort_keys=True))
        return EXIT_BUSINESS_FAIL
    fire = warp_fire.fire_command(sizing_rel)
    live = warp_live.live_run_for_sizing(
        repo_root, sizing_rel, session_id=os.environ.get("COORDINATOR_SESSION_ID") or None
    )
    if live:
        print(f"sizing-assemble: run {live} is live on {sizing_rel}; not re-firing {fire}", file=sys.stderr)
        print(json.dumps(decision, indent=2, sort_keys=True))
        return EXIT_BUSINESS_FAIL
    print(fire)
    return EXIT_OK


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
    doc_consumers: Optional[list[str]] = None
    ask_conflicts: Optional[list[dict[str, str]]] = None
    exit_criterion = None
    click_paths: list = []
    interaction_mode_flag = None
    premise_evidence = None
    write_path = None
    no_plan_hand_off = False
    json_out = False
    deliverable_id = None
    xl_exit_pick = None
    pm_resolution_key = None
    register_path = None
    scout_register_path = None
    derive_register_flag = False
    supersede = False
    pm_quote = None
    decided_on = None
    amend_research_path = None
    research_class = None
    research_appetite = None
    research_sources: list[str] = []
    research_depth = None
    research_targets: list[str] = []
    research_seeds: list[str] = []
    clear_research_seeds = False
    research_questions: list[str] = []
    research_question_mode = None
    research_question_targets: list[str] = []
    research_destination = None
    design_route_flag = None
    acceptance_rows_json = None
    initiatives_json = None
    verbatim_turns_raw = None
    intent_turn_raw = None
    no_verbatims = False
    relayed_ask_file = None

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
        elif tok == "--deliverable-id" and i + 1 < len(argv):
            deliverable_id = argv[i + 1]
            i += 2
        elif tok == "--write" and i + 1 < len(argv):
            write_path = argv[i + 1]
            i += 2
        elif tok == "--no-verbatims":
            no_verbatims = True
            i += 1
        elif tok == "--relayed-ask-file" and i + 1 < len(argv):
            relayed_ask_file = argv[i + 1]
            i += 2
        elif tok == "--verbatim-turns" and i + 1 < len(argv):
            verbatim_turns_raw = argv[i + 1]
            i += 2
        elif tok == "--amend-research" and i + 1 < len(argv):
            amend_research_path = argv[i + 1]
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
        elif tok == "--doc-consumer" and i + 1 < len(argv):
            doc_consumers = [*(doc_consumers or []), argv[i + 1]]
            i += 2
        elif tok == "--no-doc-consumers":
            doc_consumers = doc_consumers or []
            i += 1
        elif tok == "--ask-conflict" and i + 1 < len(argv):
            # `<path>=<what it says against the ask>`; the conflict text is optional.
            path, sep, conflict = argv[i + 1].partition("=")
            entry = {"path": path.strip()}
            if sep and conflict.strip():
                entry["conflict"] = conflict.strip()
            ask_conflicts = [*(ask_conflicts or []), entry]
            i += 2
        elif tok == "--no-ask-conflicts":
            ask_conflicts = ask_conflicts or []
            i += 1
        elif tok == "--intent" and i + 1 < len(argv):
            intent = argv[i + 1]
            i += 2
        elif tok == "--intent-turn" and i + 1 < len(argv):
            intent_turn_raw = argv[i + 1]
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
        elif tok == "--click-path" and i + 1 < len(argv):
            click_paths.append(argv[i + 1])
            i += 2
        elif tok == "--interaction-mode" and i + 1 < len(argv):
            interaction_mode_flag = argv[i + 1]
            i += 2
        elif tok == "--xl-exit" and i + 1 < len(argv):
            xl_exit_pick = argv[i + 1]
            i += 2
        elif tok == "--pm-resolution" and i + 1 < len(argv):
            pm_resolution_key = argv[i + 1]
            i += 2
        elif tok == "--register" and i + 1 < len(argv):
            register_path = argv[i + 1]
            i += 2
        elif tok == "--scout-register" and i + 1 < len(argv):
            scout_register_path = argv[i + 1]
            i += 2
        elif tok == "--derive-register":
            derive_register_flag = True
            i += 1
        elif tok == "--supersede":
            supersede = True
            i += 1
        elif tok == "--pm-quote" and i + 1 < len(argv):
            pm_quote = argv[i + 1]
            i += 2
        elif tok == "--decided-on" and i + 1 < len(argv):
            decided_on = argv[i + 1]
            i += 2
        elif tok == "--research-class" and i + 1 < len(argv):
            research_class = argv[i + 1]
            i += 2
        elif tok == "--research-appetite" and i + 1 < len(argv):
            research_appetite = argv[i + 1]
            i += 2
        elif tok == "--research-source" and i + 1 < len(argv):
            research_sources.append(argv[i + 1])
            i += 2
        elif tok == "--research-depth" and i + 1 < len(argv):
            research_depth = argv[i + 1]
            i += 2
        elif tok == "--research-target" and i + 1 < len(argv):
            research_targets.append(argv[i + 1])
            i += 2
        elif tok == "--clear-research-seeds":
            clear_research_seeds = True
            i += 1
            continue
        elif tok == "--research-seed" and i + 1 < len(argv):
            research_seeds.append(argv[i + 1])
            i += 2
        elif tok == "--research-question" and i + 1 < len(argv):
            research_questions.append(argv[i + 1])
            i += 2
        elif tok == "--research-question-mode" and i + 1 < len(argv):
            if research_question_mode is not None:
                print(f"{prog}: --research-question-mode given more than once", file=sys.stderr)
                return EXIT_USAGE
            research_question_mode = argv[i + 1]
            i += 2
        elif tok == "--research-question-target" and i + 1 < len(argv):
            research_question_targets.append(argv[i + 1])
            i += 2
        elif tok == "--research-destination" and i + 1 < len(argv):
            research_destination = argv[i + 1]
            i += 2
        elif tok == "--design-route" and i + 1 < len(argv):
            design_route_flag = argv[i + 1]
            i += 2
        elif tok == "--initiatives" and i + 1 < len(argv):
            initiatives_json = argv[i + 1]
            i += 2
        elif tok == "--acceptance-rows-json" and i + 1 < len(argv):
            acceptance_rows_json = argv[i + 1]
            i += 2
        elif tok == "--json":
            json_out = True
            i += 1
        elif tok == "--no-plan-hand-off":
            no_plan_hand_off = True
            i += 1
        else:
            print(f"{prog}: unrecognized argument {tok!r}", file=sys.stderr)
            return _usage(prog)

    if no_verbatims and (
        write_path is None or verbatim_turns_raw is not None or intent_turn_raw is not None
    ):
        print(
            f"{prog}: --no-verbatims needs --write and excludes --verbatim-turns and --intent-turn",
            file=sys.stderr,
        )
        return EXIT_USAGE

    relayed_ask_text = None
    if relayed_ask_file is not None:
        if write_path is None or no_verbatims:
            print(
                f"{prog}: --relayed-ask-file needs --write and excludes --no-verbatims",
                file=sys.stderr,
            )
            return EXIT_USAGE
        from coordinator_core.ops._path_guard import contained_path

        try:
            ask_root = _cli_repo_root(write_path)
            ask_candidate = Path(relayed_ask_file)
            if not ask_candidate.is_absolute():
                ask_candidate = ask_root / ask_candidate
            ask_path = contained_path(ask_candidate, [ask_root])
            if ask_path is None:
                raise ValueError("path is outside the repo root")
            relayed_ask_text = ask_path.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            print(f"{prog}: --relayed-ask-file unreadable: {exc}", file=sys.stderr)
            return EXIT_USAGE
        if not relayed_ask_text.strip():
            print(f"{prog}: --relayed-ask-file is empty", file=sys.stderr)
            return EXIT_USAGE

    verbatim_turns = None
    if verbatim_turns_raw is not None:
        verbatim_turns = _parse_turn_range(verbatim_turns_raw)
        if verbatim_turns is None or write_path is None:
            print(
                f"{prog}: --verbatim-turns needs A-B (ints, 0 <= A <= B) and --write",
                file=sys.stderr,
            )
            return EXIT_USAGE

    intent_verbatim = False
    if intent_turn_raw is not None:
        if (
            not intent_turn_raw.isascii()
            or not intent_turn_raw.isdigit()
            or intent is not None
            or intent_source not in (None, "pm-verbatim")
        ):
            print(
                f"{prog}: --intent-turn needs an int N >= 0, and excludes --intent and "
                "--intent-source em-elaborated",
                file=sys.stderr,
            )
            return EXIT_USAGE
        from coordinator_core.ops.baton_pm_turns import read_turns

        n = int(intent_turn_raw)
        picked = read_turns(turn=n, cwd=str(_cli_repo_root(write_path)))
        if not picked["ok"]:
            print(
                f"{prog}: --intent-turn {n}: no such recorded turn "
                f"(log holds {picked['count']}): {picked['reason']}",
                file=sys.stderr,
            )
            return EXIT_USAGE
        intent = picked["turns"][0]["prompt"]
        intent_source = "pm-verbatim"
        intent_verbatim = True

    if derive_register_flag:
        if write_path is None or register_path is not None:
            print(
                f"{prog}: --derive-register takes --write <state/sizings/x.yaml> and no --register",
                file=sys.stderr,
            )
            return EXIT_USAGE
        return _derive_register_cli(prog, write_path, scout_register_path, supersede)

    if register_path is not None:
        if xl_exit_pick is not None or pm_resolution_key is not None:
            print(
                f"{prog}: --register, --xl-exit and --pm-resolution are separate verbs",
                file=sys.stderr,
            )
            return EXIT_USAGE
        if write_path is None:
            print(
                f"{prog}: --register requires --write <state/sizings/x.yaml>",
                file=sys.stderr,
            )
            return EXIT_USAGE
        from coordinator_core.ops.requirement_register import load_register_yaml
        from coordinator_core.ops.sizing_record_register import _handler as record_register

        try:
            register = load_register_yaml(_read_path_or_stdin(register_path))
        except Exception as exc:  # noqa: BLE001
            print(f"{prog}: cannot read register {register_path}: {exc}", file=sys.stderr)
            return EXIT_USAGE

        result = record_register(
            {"sizing": write_path, "register": register, "supersede": supersede,
             "amend": not supersede},
            repo_root=_cli_repo_root(write_path),
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return EXIT_OK if result["exit_code"] == 0 else EXIT_BUSINESS_FAIL

    if pm_resolution_key is not None:
        if xl_exit_pick is not None:
            print(f"{prog}: --pm-resolution and --xl-exit are separate verbs", file=sys.stderr)
            return EXIT_USAGE
        if write_path is None:
            print(
                f"{prog}: --pm-resolution requires --write <state/sizings/x.yaml>",
                file=sys.stderr,
            )
            return EXIT_USAGE
        from coordinator_core.ops.sizing_record_pm_resolution import (
            _handler as record_pm_resolution,
        )

        result = record_pm_resolution(
            {
                "sizing": write_path,
                "key": pm_resolution_key,
                "pm_quote": pm_quote,
                "decided_on": decided_on,
                "supersede": supersede,
            },
            repo_root=_cli_repo_root(write_path),
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return EXIT_OK if result["exit_code"] == 0 else EXIT_BUSINESS_FAIL

    if xl_exit_pick is not None:
        if write_path is None:
            print(f"{prog}: --xl-exit requires --write <state/sizings/x.yaml>", file=sys.stderr)
            return EXIT_USAGE
        from coordinator_core.ops.sizing_record_xl_exit import _handler as record_xl_exit

        result = record_xl_exit(
            {
                "sizing": write_path,
                "xl_exit": xl_exit_pick,
                "pm_quote": pm_quote,
                "decided_on": decided_on,
            },
            repo_root=_cli_repo_root(write_path),
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return EXIT_OK if result["exit_code"] == 0 else EXIT_BUSINESS_FAIL

    if amend_research_path is not None:
        given = {
            k: v
            for k, v in (
                ("value_class", research_class), ("appetite", research_appetite),
                ("sources", research_sources), ("depth", research_depth), ("targets", research_targets),
                ("questions", research_questions), ("destination", research_destination),
                ("question_mode", research_question_mode),
                ("question_targets", research_question_targets),
                ("seeds", research_seeds), ("clear_seeds", clear_research_seeds),
            )
            if v
        }
        if not given or tshirt is not None or write_path is not None:
            print(f"{prog}: --amend-research takes only --research-* flags", file=sys.stderr)
            return EXIT_USAGE
        try:
            result = amend_research(_cli_repo_root(amend_research_path), amend_research_path, given)
        except SizingAssembleError as exc:
            print(f"{prog}: --amend-research refused: {exc}", file=sys.stderr)
            return EXIT_BUSINESS_FAIL
        # Per-session under the seam id: re-opens in the amending session only; a plan-terminal
        # row opened by another writer session is out of reach.
        _open_routed_obligation(amend_research_path)
        print(json.dumps(result, indent=2, sort_keys=True))
        return EXIT_OK

    if tshirt is None:
        print(f"{prog}: missing required --tshirt", file=sys.stderr)
        return _usage(prog)

    research_block, research_errors = build_research_block(
        research_class, research_appetite, research_sources, research_depth, research_targets,
        research_questions, research_destination, research_question_mode, research_question_targets,
        research_seeds,
    )
    if research_errors:
        for message in research_errors:
            print(f"{prog}: {message}", file=sys.stderr)
        return EXIT_USAGE
    if research_block is not None and write_path is None:
        print(f"{prog}: --research-* requires --write <state/sizings/x.yaml>", file=sys.stderr)
        return EXIT_USAGE
    if design_route_flag is not None:
        from coordinator_core.ops.dispatch_emit.design_route import ROUTES as _DESIGN_ROUTES

        if design_route_flag not in _DESIGN_ROUTES:
            print(
                f"{prog}: --design-route must be one of {'|'.join(_DESIGN_ROUTES)}, got {design_route_flag!r}",
                file=sys.stderr,
            )
            return EXIT_USAGE
        if write_path is None:
            print(f"{prog}: --design-route requires --write <state/sizings/x.yaml>", file=sys.stderr)
            return EXIT_USAGE
    acceptance_rows = None
    if acceptance_rows_json is not None:
        acceptance_rows, acceptance_errors = parse_acceptance_rows(acceptance_rows_json)
        if acceptance_errors:
            for message in acceptance_errors:
                print(f"{prog}: {message}", file=sys.stderr)
            return EXIT_USAGE
        if write_path is None:
            print(f"{prog}: --acceptance-rows-json requires --write <state/sizings/x.yaml>", file=sys.stderr)
            return EXIT_USAGE

    initiatives = None
    if initiatives_json is not None:
        initiatives, initiatives_errors = parse_initiatives(initiatives_json)
        if initiatives_errors:
            for message in initiatives_errors:
                print(f"{prog}: {message}", file=sys.stderr)
            return EXIT_USAGE

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
            click_paths=[parse_click_path(c) for c in click_paths] or None,
            interaction_mode=interaction_mode,
            interaction_mode_source=interaction_mode_source,
            premise_evidence=premise_evidence,
            initiatives=initiatives,
        )
    except SizingAssembleError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - structural backstop, mirrors pickup_assemble
        print(f"{prog}: unexpected failure: {exc}", file=sys.stderr)
        return EXIT_TRANSPORT_FAIL

    if research_block is not None:
        _route_to_research(decision, write_path)

    scout_register = None
    if scout_register_path is not None:
        if write_path is None:
            print(f"{prog}: --scout-register requires --write <state/sizings/x.yaml>", file=sys.stderr)
            return EXIT_USAGE
        import yaml

        try:
            scout_register = yaml.safe_load(_read_path_or_stdin(scout_register_path))
        except Exception as exc:  # noqa: BLE001
            print(f"{prog}: cannot read scout report {scout_register_path}: {exc}", file=sys.stderr)
            return EXIT_USAGE
        if not isinstance(scout_register, dict):
            print(f"{prog}: scout report {scout_register_path} is not a mapping", file=sys.stderr)
            return EXIT_USAGE

    if write_path is not None:
        try:
            decision["write"] = write_back(
                _cli_repo_root(write_path),
                write_path,
                decision,
                exit_criterion=exit_criterion,
                click_paths=decision.get("exit_criterion", {}).get("click_paths") if decision.get("exit_criterion") else None,
                interaction_mode=interaction_mode,
                premise_provenance=premise_provenance,
                premise_evidence=premise_evidence,
                scout_evidence=scout_evidence,
                deliverable_id=deliverable_id,
                research=research_block,
                design_route=design_route_flag,
                verbatim_turns=verbatim_turns,
                intent_verbatim=intent_verbatim,
                name=name,
                scout_register=scout_register,
                doc_consumers=doc_consumers,
                ask_conflicts=ask_conflicts,
                embed_verbatims=not no_verbatims,
                relayed_ask_text=relayed_ask_text,
                acceptance_rows=acceptance_rows,
            )
        except SizingAssembleError as exc:
            print(f"{prog}: --write refused: {exc}", file=sys.stderr)
            return EXIT_BUSINESS_FAIL
        report = decision["write"]["pm_verbatims"]
        if report["embedded"]:
            lo, hi = report["turns"]
            print(f"embedded {report['embedded']} pm_verbatims (turns {lo}-{hi})", file=sys.stderr)
        else:
            print("embedded 0 pm_verbatims", file=sys.stderr)
        if no_plan_hand_off:
            decision["pm_exit_question"] = pm_exit_question(
                decision["resolved_estimate"]["tshirt"],
                decision["route"],
                prompt_pending="post_size_prompt_pending" in decision["detents"],
                bundle="bundle" in decision["detents"],
            )
        else:
            _open_routed_obligation(write_path)
        if (
            not no_plan_hand_off
            and research_block is None
            and decision.get("route") in ("plan", "spec-dispatch")
            and decision["resolved_estimate"]["tshirt"] != "XS"
            and sizing_acceptance_skipped(decision["route"], decision["resolved_estimate"]["tshirt"])
        ):
            return _hand_to_engine_ask(write_path, decision)
        if not json_out:
            _wp = Path(write_path)
            _root = _cli_repo_root(write_path)
            try:
                shown = _wp.resolve().relative_to(_root.resolve()).as_posix() if _wp.is_absolute() else _wp.as_posix()
            except ValueError:
                shown = _wp.as_posix()
            print(f"written: {shown}")
            return EXIT_OK

    print(json.dumps(decision, indent=2, sort_keys=True))
    return EXIT_OK
