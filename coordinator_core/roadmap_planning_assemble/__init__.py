"""
coordinator_core.roadmap_planning_assemble — the `roadmap-planning-assemble`
computed-skill engine (DR-047 computed-skills contract: DoE-claude
coordinator/docs/wiki/computed-skills.md), spine seam.

Purpose: computes the mechanical routing over `roadmap-planning`'s
**spine** — Phase 1 synthesis, N sprint descriptors, the cross-sprint edge
set, no batons — into one read-only decision object shaped
{artifact, preflight, gates, directives, judgment_points, decisions,
narration, next_move} per the contract's Decision-Object Schema-of-Record.
The sprint-scoped half (everything inside one sprint) is a SEPARATE
assembler (`sprint_planning_assemble`, chunk C11) — this module computes
only the census rows whose `seam` is `spine` or `both`
(DoE-claude state/plan-sidecars/roadmap-planning.census-steps.md, schema
2.0.0, source_sha 1e598af754b15144a717673cd1b90002e5b6ee61).

Spec backlink: docs/plans/2026-08-21-engine-half-of-the-roadmap-sprint-spine-split.md,
chunk C10. Source memo:
cross-repo/inbox/2026-08-20-doe-claude-em-roadmap-sprint-split-assembler-ops.md.

Consumes manifest (13 ops, corrected census — NOT 25, DoE's original count
was wrong): 3 literal-name (`roadmap-number-stubs`, `audit-roadmap`,
`coordinator-doc-new`), 3 capability matches under a different name
(`read-authoring-session-path`, `stamp-deployment-state`,
`reject-path-shaped-gate-dependency`), 7 adapted-not-bound
(`inventory-corpus-files`, `read-sizing-object-fields`,
`detect-pm-gate-signal`, `generate-stub-index-query-callout`,
`dispatch-cluster-scout`, `stamp-problem-set-field`,
`stamp-sizing-object-field`). This module names candidate ops in
`directives[].cli` for the spine/both rows it reaches; it never imports or
calls any of them (an assembler `brief` is read-only per the contract, same
as `sizing_assemble.route()` never writes state).

Class A's eight (`apply-roadmap-seed-precondition-exemption`,
`route-to-phase1`, `bind-problem-set-as-corpus`,
`seed-clusters-from-problem-set`, `seed-phase1-from-stub-scope`,
`seed-phase1-inventory-from-sizing-object`, `derive-run-id-from-stub-id`,
`cite-sizing-object-in-overview`) were NEVER callable on either disk — they
are skill-body control flow and transcription rules the census's
"Consumes-manifest correction" section names explicitly as
"assembler-internal glue, not a consumed op". This module expresses each as
a computed decision-object field or `directives[]` content (never a
`directives[].cli` binding, never imported, never invoked) — see
`_class_a_glue_directives()`.

HARD performance constraint (AC2/AC3/AC4, this chunk's own non-negotiable):
NO module-scope import of `coordinator_core.ops`. `sizing_assemble` is the
reference shape this module copies verbatim: module-scope imports are
`typing`/`__future__` only, `json`/`sys` deferred into the CLI entry
(`main()`), no `register_op`, no `OP_CLASSIFICATION`/`_EAGER_OP_MODULES`/
`OP_MODULE_MAP` entry. Target: sizing-assemble's measured shape
(120.3ms / 2.0 procs per call through its real CLI), budget ceiling
≤200ms / ≤2.0 procs/call (DR-344 §7's single-process bar).

READ-ONLY, by construction: `brief()` only reads its arguments — it never
touches disk, never shells out, never writes a stub/OVERVIEW/spine record.
Mirrors `sizing_assemble.route()`'s and `pickup_assemble.brief()`'s
read-only compute-half contract; a future mutating `apply` half (per the
contract's § The compute/apply split) is out of this chunk's writes list.

Entry points (A/B/C/D, per residue/entry-points-b-c-d.md): exactly one of
`input_corpus_path` (A), `stub_id` (B), `problem_set_path` (C), or
`sizing_object_path` (D) is the expected caller shape; more than one
supplied is the `entryD-4-resolve-competing-entries` judgment point (tier 2
advisory — this module cannot decide which shape reflects the "actual input
shape", it can only surface that more than one arrived).

Negative-spec:
    - Do NOT import `coordinator_core.ops` at module scope, or at all,
      anywhere in this module. That import alone costs +371.9ms
      (docs/research/spike-verdicts/2026-08-21-assembler-ops-under-the-
      brightline.md) — the entire reason this module exists as a hand-built
      CLI rather than a registered op.
    - Do NOT call any of the 13 consumed ops or Class A's eight glue names.
      This module composes their names/output SHAPE into the decision
      object; it never imports or subprocess-invokes them. Execution is the
      contract's `apply` half's job (§ The compute/apply split), not
      shipped in this chunk's writes list.
    - Do NOT manufacture a `recommendation` for a judgment point this
      module has no evidence to narrow. Every advisory JUDGMENT/MIXED-
      judgment-half row from the census is semantic-authoring content
      (clustering granularity, verdict authoring, OVERVIEW prose) this
      module cannot compute — each is emitted as tier 3
      (`recommendation: null, reason: "insufficient-evidence"`), never a
      manufactured tier-2 offer (computed-skills.md § Candor is the design
      principle).
    - Do NOT emit a `recommendation` on a PM gate. Every `untrusted_gate`
      row (p1.5.0-pm-authorize-research-depth, p1.5.4-pm-round1,
      p1.5.6-pm-round2, entryC-5-precondition-confirm) is the
      recommendation-forbidden security class per computed-skills.md § The
      three-tier model: the PM's disposition is sourced from PM-authored
      content this engine does not itself compute, so no consumer may
      derive a disposition from an engine-authored offer here.
    - Do NOT include a sprint-seam-only census row. `residue-gate-text-
      subsystem-named` and `residue-frontmatter-fields-fill` (seam:
      "sprint") belong to `sprint_planning_assemble` (C11), not here.
"""
from __future__ import annotations

from typing import Any, Optional

from coordinator_core.contract.decision_object.envelope import (
    build_envelope,
    _emit as _envelope_emit,
)
from coordinator_core.contract.decision_object.judgment import (
    build_judgment_point as _build_judgment_point,
    build_untrusted_gate_judgment_point as _build_untrusted_gate_judgment_point,
)

# --- Class A's eight: assembler-internal glue, never a consumed op. -------
# Never appears in a `directives[].cli` value; never imported; never
# invoked. Named here only as the closed vocabulary
# `_class_a_glue_directives()` iterates to build computed `directives[]`
# entries whose `cli` field is deliberately absent (glue, not an
# invocation).
CLASS_A_GLUE = (
    "apply-roadmap-seed-precondition-exemption",
    "route-to-phase1",
    "bind-problem-set-as-corpus",
    "seed-clusters-from-problem-set",
    "seed-phase1-from-stub-scope",
    "seed-phase1-inventory-from-sizing-object",
    "derive-run-id-from-stub-id",
    "cite-sizing-object-in-overview",
)

ENTRY_POINTS = ("A", "B", "C", "D")

# Spine/both-seam census rows this assembler reaches, mapped to the
# corrected 13-op consumes manifest's candidate_op binding (census
# "Consumes-manifest correction" section, 2026-08-21). Sprint-only rows
# (residue-gate-text-subsystem-named, residue-frontmatter-fields-fill) are
# sprint_planning_assemble's (C11), never this module's.
_SPINE_CANDIDATE_OPS: dict[str, str] = {
    "p1.5.1-dispatch-scouts": "dispatch-cluster-scout",
    "p2.1.5-number-stubs": "roadmap-number-stubs",
    "p2.4-disjointness-audit": "audit-roadmap",
    "p2.6-2.7-audit-roadmap-close": "audit-roadmap",
    "entryB-1-read-authoring-session": "read-authoring-session-path",
    "entryB-lifecycle-stamps": "stamp-deployment-state",
    "entryC-4-inherit-problem-set-ref": "stamp-problem-set-field",
    "entryD-1-read-sizing-object": "read-sizing-object-fields",
}


class RoadmapPlanningAssembleError(ValueError):
    """Raised for a malformed input to brief() — a usage error, never a
    business-logic divergence (mirrors sizing_assemble.SizingAssembleError:
    divergence is expressed via the decision object, never an exception)."""


def _directive(id_: str, cli: Optional[str], args: list[str], depends_on, already_satisfied: bool) -> dict[str, Any]:
    return {
        "id": id_,
        "cli": cli,
        "args": list(args),
        "depends_on": depends_on,
        "already_satisfied": already_satisfied,
    }


def _judgment_point(
    id_: str,
    question: str,
    evidence: str,
    dispositions: list[dict[str, Any]],
    round_trip: str,
    revalidate_at_dispatch: bool = False,
    recommendation: Optional[dict[str, str]] = None,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    """Tier 2 (`recommendation` an object) or tier 3 (`recommendation: null`,
    `reason` required) constructor. Never used for a recommendation-
    forbidden PM gate — see `_pm_gate_judgment_point`.

    Thin call-shape wrapper over the shared
    `coordinator_core.contract.decision_object.judgment.build_judgment_point`
    (Review: code-reviewer — C1, reuse over a hand-rolled parallel shape).
    Keeps this module's own positional/keyword call-site shape (id_ first,
    `reason` optional) so every existing call site below is unchanged."""
    return _build_judgment_point(
        recommendation,
        id=id_,
        question=question,
        dispositions=dispositions,
        evidence=evidence,
        reason=reason or "insufficient-evidence",
        revalidate_at_dispatch=revalidate_at_dispatch,
        round_trip=round_trip,
    )


def _pm_gate_judgment_point(
    id_: str,
    question: str,
    evidence: str,
    dispositions: list[dict[str, Any]],
    round_trip: str,
) -> dict[str, Any]:
    """Recommendation-forbidden security-class constructor (computed-skills.md
    § The three-tier model). Hardcodes `recommendation: null,
    reason: "recommendation-forbidden"` — structurally unreachable for a
    caller to fill, unlike the ordinary tier-3 `_judgment_point` path.

    Thin call-shape wrapper over the shared
    `coordinator_core.contract.decision_object.judgment.
    build_untrusted_gate_judgment_point`, whose signature has deliberately
    no `recommendation` parameter at all (Review: code-reviewer — C1)."""
    return _build_untrusted_gate_judgment_point(
        id=id_,
        question=question,
        dispositions=dispositions,
        evidence=evidence,
        reason="recommendation-forbidden",
        revalidate_at_dispatch=False,
        round_trip=round_trip,
    )


def _resolve_entry_point(
    input_corpus_path: Optional[str],
    stub_id: Optional[str],
    problem_set_path: Optional[str],
    sizing_object_path: Optional[str],
) -> tuple[Optional[str], list[str]]:
    """Returns (entry_point letter or None, list of every entry point that
    was supplied). More than one supplied is the entryD-4 overlap-
    resolution judgment call, never auto-resolved here."""
    supplied = []
    if input_corpus_path is not None:
        supplied.append("A")
    if stub_id is not None:
        supplied.append("B")
    if problem_set_path is not None:
        supplied.append("C")
    if sizing_object_path is not None:
        supplied.append("D")
    if len(supplied) == 1:
        return supplied[0], supplied
    return None, supplied


def _class_a_glue_directives(
    stub_id: Optional[str],
    problem_set_path: Optional[str],
    sizing_object_path: Optional[str],
    entry_point: Optional[str],
) -> list[dict[str, Any]]:
    """Class A's eight, expressed as computed decision-object fields/
    directives[] GLUE — `cli: None` on every entry, since none of these
    names is an invocable capability (census "Consumes-manifest correction":
    "assembler-internal glue, not a consumed op"). Each entry's `args`
    carries the computed VALUE (e.g. the derived run_id), not a CLI
    invocation payload."""
    directives: list[dict[str, Any]] = []

    if entry_point == "B" and stub_id is not None:
        directives.append(
            _directive("g-derive-run-id-from-stub-id", None, [f"run_id={stub_id}"], None, False)
        )
        directives.append(
            _directive("g-apply-roadmap-seed-precondition-exemption", None, ["exempt=true"], None, True)
        )
        directives.append(_directive("g-route-to-phase1", None, ["phase=1"], None, False))

    if entry_point == "C" and problem_set_path is not None:
        directives.append(
            _directive("g-bind-problem-set-as-corpus", None, [f"input_corpus_path={problem_set_path}"], None, False)
        )
        directives.append(
            _directive("g-seed-clusters-from-problem-set", None, [f"source={problem_set_path}"], None, False)
        )

    if entry_point == "B" and stub_id is not None:
        directives.append(
            _directive("g-seed-phase1-from-stub-scope", None, [f"stub_id={stub_id}"], None, False)
        )

    if entry_point == "D" and sizing_object_path is not None:
        directives.append(
            _directive(
                "g-seed-phase1-inventory-from-sizing-object",
                None,
                [f"sizing_object_path={sizing_object_path}"],
                None,
                False,
            )
        )
        directives.append(
            _directive("g-cite-sizing-object-in-overview", None, [f"sizing_object_path={sizing_object_path}"], None, False)
        )

    return directives


def _spine_mechanical_directives(entry_point: Optional[str], run_id: Optional[str]) -> list[dict[str, Any]]:
    """Directives for the MECHANICAL spine/both-seam census rows this
    assembler reaches, naming a REAL consumed-op CLI (never Class A's glue).
    Composed unconditionally (not entry-point-gated) except where the row's
    own locus is entry-point-specific — mirrors the census's own seam
    column, not this module's invention."""
    directives: list[dict[str, Any]] = []

    directives.append(
        _directive(
            "d-dispatch-cluster-scout",
            _SPINE_CANDIDATE_OPS["p1.5.1-dispatch-scouts"],
            ["--run-id", run_id or ""],
            None,
            False,
        )
    )
    directives.append(
        _directive(
            "d-number-stubs",
            _SPINE_CANDIDATE_OPS["p2.1.5-number-stubs"],
            ["--run-id", run_id or ""],
            None,
            False,
        )
    )
    directives.append(
        _directive(
            "d-disjointness-audit",
            _SPINE_CANDIDATE_OPS["p2.4-disjointness-audit"],
            [run_id or ""],
            None,
            False,
        )
    )
    directives.append(
        _directive(
            "d-audit-roadmap-close",
            _SPINE_CANDIDATE_OPS["p2.6-2.7-audit-roadmap-close"],
            [run_id or ""],
            None,
            False,
        )
    )

    if entry_point == "B":
        directives.append(
            _directive(
                "d-read-authoring-session",
                _SPINE_CANDIDATE_OPS["entryB-1-read-authoring-session"],
                ["--stub-id", run_id or ""],
                None,
                False,
            )
        )
        directives.append(
            _directive(
                "d-stamp-deployment-state",
                _SPINE_CANDIDATE_OPS["entryB-lifecycle-stamps"],
                ["--run-id", run_id or "", "--state", "in_flight"],
                None,
                False,
            )
        )

    if entry_point == "C":
        directives.append(
            _directive(
                "d-stamp-problem-set-field",
                _SPINE_CANDIDATE_OPS["entryC-4-inherit-problem-set-ref"],
                ["--run-id", run_id or ""],
                None,
                False,
            )
        )

    if entry_point == "D":
        directives.append(
            _directive(
                "d-read-sizing-object-fields",
                _SPINE_CANDIDATE_OPS["entryD-1-read-sizing-object"],
                ["--run-id", run_id or ""],
                None,
                False,
            )
        )

    return directives


def _spine_judgment_points(entry_point: Optional[str], entry_points_supplied: list[str]) -> list[dict[str, Any]]:
    """Every spine/both-seam JUDGMENT row and MIXED judgment-half from the
    census, tiered per computed-skills.md § The three-tier model. Advisory
    rows are tier 3 (this module has no semantic evidence to offer a
    recommendation from — § Candor); untrusted_gate PM rows use the
    recommendation-forbidden constructor."""
    points: list[dict[str, Any]] = []

    points.append(
        _judgment_point(
            "j-p1.1-inventory-summaries",
            "Write a title + summary line per inventoried input file?",
            "directives[?id=='d-inventory-corpus-files']",
            [{"value": "authored", "resolves": []}],
            "terminal",
        )
    )
    points.append(
        _judgment_point(
            "j-p1.2-cluster",
            "How should the inventoried inputs cluster into 20-60 coverage units?",
            "artifact.input_corpus_path",
            [{"value": "clustered", "resolves": []}],
            "terminal",
        )
    )
    points.append(
        _judgment_point(
            "j-p1.2b-subfloor-fold",
            "Does any sub-floor cluster grain-fold into a sibling, or hold as its own MERGE?",
            "j-p1.2-cluster",
            [
                {"value": "grain-fold", "resolves": []},
                {"value": "merge", "resolves": []},
            ],
            "terminal",
        )
    )
    points.append(
        _judgment_point(
            "j-p1.3-verdict",
            "What verdict (MERGE/DEFER/KEEP/DROP/MOVE) does each cluster get?",
            "j-p1.2-cluster",
            [
                {"value": v, "resolves": []}
                for v in ("merge", "defer", "keep", "drop", "move")
            ],
            "terminal",
        )
    )
    points.append(
        _judgment_point(
            "j-p1.4-resolutions",
            "For each conflicting-cluster pair, what is the conflict statement, resolution, and rationale?",
            "j-p1.3-verdict",
            [{"value": "authored", "resolves": []}],
            "terminal",
        )
    )
    points.append(
        _judgment_point(
            "j-p1.5.0-research-depth-recommend",
            "Recommend escalating any KEEP/MERGE cluster to /research (deep-research)?",
            "residue/research-depth-assessment.md escalation-criteria checklist",
            [
                {"value": "recommend-escalate", "resolves": []},
                {"value": "no-escalation", "resolves": []},
            ],
            "terminal",
        )
    )
    points.append(
        _pm_gate_judgment_point(
            "j-p1.5.0-pm-authorize-research-depth",
            "Authorize, decline, or partially authorize the deep-research escalation?",
            "j-p1.5.0-research-depth-recommend",
            [
                {"value": "authorize", "resolves": ["d-dispatch-cluster-scout"]},
                {"value": "decline", "resolves": ["d-dispatch-cluster-scout"]},
                {"value": "partial", "resolves": ["d-dispatch-cluster-scout"]},
            ],
            "round_trip",
        )
    )
    points.append(
        _judgment_point(
            "j-p1.5.2-author-overview",
            "Author one OVERVIEW.md section per KEEP cluster, citing the research corpus and carrying a Contested block?",
            "j-p1.3-verdict",
            [{"value": "authored", "resolves": []}],
            "terminal",
        )
    )
    points.append(
        _judgment_point(
            "j-p1.5.3-peer-team-asks",
            "What content (unblocks/deliverable/rationale/contact/sharp-question) does each peer-team ask carry?",
            "j-p1.2-cluster",
            [{"value": "authored", "resolves": []}, {"value": "none-needed", "resolves": []}],
            "terminal",
        )
    )
    points.append(
        _pm_gate_judgment_point(
            "j-p1.5.4-pm-round1",
            "PM shape approval — proceed to sequential reviews and shape-approved status?",
            "j-p1.5.2-author-overview",
            [
                {"value": "approved", "resolves": ["d-dispatch-cluster-scout"]},
                {"value": "changes-requested", "resolves": []},
            ],
            "round_trip",
        )
    )
    points.append(
        _judgment_point(
            "j-p1.5.5-sequential-reviews",
            "What content does each selected reviewer's review carry?",
            "j-p1.5.4-pm-round1",
            [{"value": "conducted", "resolves": []}],
            "terminal",
        )
    )
    points.append(
        _pm_gate_judgment_point(
            "j-p1.5.6-pm-round2",
            "PM final approval — diff vs. shape-approved acceptable, proceed to Phase 2?",
            "j-p1.5.5-sequential-reviews",
            [
                {"value": "approved", "resolves": []},
                {"value": "changes-requested", "resolves": []},
            ],
            "round_trip",
        )
    )
    points.append(
        _judgment_point(
            "j-p2.1.5-multi-sprint-boundary",
            "Where does each multi-sprint boundary land, beyond what roadmap-number-stubs itself resolves?",
            "directives[?id=='d-number-stubs']",
            [{"value": "assigned", "resolves": []}],
            "round_trip",
        )
    )
    points.append(
        _judgment_point(
            "j-p2.4-disjointness-fold",
            "Does any surviving wave-N stub pair need a fold+renumber audit-roadmap cannot itself detect?",
            "directives[?id=='d-disjointness-audit']",
            [{"value": "fold-and-renumber", "resolves": []}, {"value": "none-needed", "resolves": []}],
            "round_trip",
        )
    )
    points.append(
        _judgment_point(
            "j-p2.8-sequential-reviews",
            "Conduct/integrate the Phase 2 review, or author the domain-reviewer skip rationale?",
            "j-p2.1.5-multi-sprint-boundary",
            [{"value": "conducted", "resolves": []}, {"value": "skipped-with-rationale", "resolves": []}],
            "terminal",
        )
    )
    points.append(
        _judgment_point(
            "j-residue-stub-dedup-canonicalization",
            "Among filename-timestamp duplicate stubs, which is canonical (per git provenance, never the HHMMSS prefix)?",
            "preflight.dedup_candidates",
            [{"value": "canonicalized", "resolves": []}, {"value": "no-duplicates", "resolves": []}],
            "terminal",
        )
    )

    if entry_point is None and len(entry_points_supplied) > 1:
        points.append(
            _judgment_point(
                "j-entryD-4-resolve-competing-entries",
                f"More than one entry point supplied ({', '.join(entry_points_supplied)}) — which reflects the actual input shape?",
                "artifact.entry_points_supplied",
                [{"value": ep, "resolves": []} for ep in entry_points_supplied],
                "terminal",
            )
        )
    elif entry_point is None and not entry_points_supplied:
        points.append(
            _judgment_point(
                "j-entry-point-unresolved",
                "No entry point (input_corpus_path/stub_id/problem_set_path/sizing_object_path) was supplied — which applies?",
                "artifact.entry_points_supplied",
                [{"value": ep, "resolves": []} for ep in ENTRY_POINTS],
                "terminal",
            )
        )

    if entry_point == "C":
        points.append(
            _pm_gate_judgment_point(
                "j-entryC-5-precondition-confirm",
                "Fewer than 5 problem-set items — confirm with the PM before proceeding rather than a single coordinator:plan call?",
                "artifact.problem_set_item_count",
                [
                    {"value": "confirmed-proceed", "resolves": []},
                    {"value": "route-single-plan-instead", "resolves": []},
                ],
                "terminal",
            )
        )

    return points


def brief(
    *,
    run_id: Optional[str] = None,
    input_corpus_path: Optional[str] = None,
    stub_id: Optional[str] = None,
    problem_set_path: Optional[str] = None,
    sizing_object_path: Optional[str] = None,
    decisions: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Computes and returns the roadmap-planning spine's decision object,
    per DR-047's Decision-Object Schema-of-Record. READ-ONLY: touches no
    disk, mutates nothing, calls nothing.

    Exactly one of `input_corpus_path` (Entry Point A), `stub_id` (B),
    `problem_set_path` (C), or `sizing_object_path` (D) is the expected
    caller shape. `run_id` is required unless derivable from `stub_id`
    (Entry Point B: `derive-run-id-from-stub-id`, Class A glue — `run_id :=
    stub_id` verbatim).

    Returns:
        A dict: {artifact, preflight, gates, directives, judgment_points,
        decisions, narration, next_move} — see module docstring.
    """
    decisions = dict(decisions or {})

    entry_point, entry_points_supplied = _resolve_entry_point(
        input_corpus_path, stub_id, problem_set_path, sizing_object_path
    )

    resolved_run_id = run_id
    if resolved_run_id is None and entry_point == "B" and stub_id is not None:
        resolved_run_id = stub_id  # entryB-3-set-runid: run-id := stub_id verbatim

    if resolved_run_id is None and entry_point is not None:
        raise RoadmapPlanningAssembleError(
            "run_id is required and could not be derived (only Entry Point B derives it from stub_id)"
        )

    artifact = {
        "run_id": resolved_run_id,
        "entry_point": entry_point,
        "entry_points_supplied": entry_points_supplied,
        "input_corpus_path": input_corpus_path,
        "stub_id": stub_id,
        "problem_set_path": problem_set_path,
        "sizing_object_path": sizing_object_path,
    }

    preflight = {
        "seam": "spine",
        "dedup_candidates": [],
    }

    gates = {
        "entry_point_resolution": {
            "verdict": "resolved" if entry_point is not None else "ambiguous-or-unresolved",
        },
    }

    directives: list[dict[str, Any]] = []
    directives.extend(
        _class_a_glue_directives(stub_id, problem_set_path, sizing_object_path, entry_point)
    )
    directives.extend(_spine_mechanical_directives(entry_point, resolved_run_id))

    judgment_points = _spine_judgment_points(entry_point, entry_points_supplied)

    narration = (
        f"Ran ahead of you: computed the roadmap-planning spine routing for entry point "
        f"{entry_point or 'UNRESOLVED'}, {len(directives)} directive(s), "
        f"{len(judgment_points)} judgment point(s)."
    )
    next_move = ""
    if entry_point is None:
        next_move = (
            "Entry point unresolved — resolve which of input_corpus_path/stub_id/"
            "problem_set_path/sizing_object_path applies (see j-entry-point-unresolved "
            "or j-entryD-4-resolve-competing-entries) before proceeding."
        )

    envelope = build_envelope(
        artifact=artifact,
        preflight=preflight,
        gates=gates,
        directives=directives,
        judgment_points=judgment_points,
        decisions=decisions,
        narration=narration,
        next_move=next_move,
    )
    return dict(_envelope_emit(envelope))


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_TRANSPORT_FAIL = 3


def _usage(prog: str, stream=None) -> int:
    stream = __import__("sys").stderr if stream is None else stream
    print(
        f"{prog}: usage: {prog} [--run-id <id>] [--input-corpus <path>] "
        "[--stub-id <id>] [--problem-set <path>] [--sizing-object <path>] "
        "[--decisions <json>]",
        file=stream,
    )
    return EXIT_USAGE


def main(argv: list[str]) -> int:
    import json
    import sys

    prog = "roadmap-planning-assemble"
    run_id = None
    input_corpus_path = None
    stub_id = None
    problem_set_path = None
    sizing_object_path = None
    decisions: dict[str, Any] = {}

    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("--help", "-h"):
            _usage(prog, stream=sys.stdout)
            return EXIT_OK
        if tok == "--run-id" and i + 1 < len(argv):
            run_id = argv[i + 1]
            i += 2
        elif tok == "--input-corpus" and i + 1 < len(argv):
            input_corpus_path = argv[i + 1]
            i += 2
        elif tok == "--stub-id" and i + 1 < len(argv):
            stub_id = argv[i + 1]
            i += 2
        elif tok == "--problem-set" and i + 1 < len(argv):
            problem_set_path = argv[i + 1]
            i += 2
        elif tok == "--sizing-object" and i + 1 < len(argv):
            sizing_object_path = argv[i + 1]
            i += 2
        elif tok == "--decisions" and i + 1 < len(argv):
            try:
                decisions = json.loads(argv[i + 1])
            except json.JSONDecodeError as exc:
                print(f"{prog}: malformed --decisions JSON: {exc}", file=sys.stderr)
                return EXIT_USAGE
            i += 2
        else:
            print(f"{prog}: unrecognized argument {tok!r}", file=sys.stderr)
            return _usage(prog)

    try:
        decision = brief(
            run_id=run_id,
            input_corpus_path=input_corpus_path,
            stub_id=stub_id,
            problem_set_path=problem_set_path,
            sizing_object_path=sizing_object_path,
            decisions=decisions,
        )
    except RoadmapPlanningAssembleError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - structural backstop, mirrors sizing_assemble
        print(f"{prog}: unexpected failure: {exc}", file=sys.stderr)
        print(json.dumps({"error": str(exc), "transport_failure": True}))
        return EXIT_TRANSPORT_FAIL

    print(json.dumps(decision, indent=2, sort_keys=True))
    return EXIT_OK
