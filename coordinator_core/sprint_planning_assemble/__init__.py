"""
coordinator_core.sprint_planning_assemble — the `sprint-planning-assemble`
computed-skill engine (DR-047 computed-skills contract: DoE-claude
coordinator/docs/wiki/computed-skills.md), sprint seam.

Purpose: computes the mechanical routing over `sprint-planning`'s scope —
one sprint's own research wave, its own OVERVIEW, its stubs, its
intra-sprint wave graph, and its gates — into one read-only decision
object shaped {artifact, preflight, gates, directives, judgment_points,
decisions, narration, next_move} per the contract's Decision-Object
Schema-of-Record. The spine half (Phase 1 synthesis at roadmap altitude, N
sprint descriptors, the cross-sprint edge set, no batons) is a SEPARATE
assembler (`roadmap_planning_assemble`, chunk C10) — this module computes
only the census rows whose `seam` is `sprint` or `both`
(DoE-claude state/plan-sidecars/roadmap-planning.census-steps.md, schema
2.0.0, source_sha 1e598af754b15144a717673cd1b90002e5b6ee61).

Spec backlink: docs/plans/2026-08-21-engine-half-of-the-roadmap-sprint-spine-split.md,
chunk C11. Source memo:
cross-repo/inbox/2026-08-20-doe-claude-em-roadmap-sprint-split-assembler-ops.md.

Entry contract (DoE-claude docs/plans/2026-08-20-split-roadmap-planning-at-the-
sprint-spine.md, chunk C6): `sprint-planning` is invoked once per sprint,
with a sprint id resolved against a run id — NOT the A/B/C/D entry-point
shape `roadmap_planning_assemble` resolves (every entryB/entryC/entryD
census row is `seam: spine` only; none is `seam: sprint` or `both`). Both
`run_id` and `sprint_id` are required.

Consumes manifest (sprint/both-seam subset of the corrected 13-op
manifest): `dispatch-cluster-scout` (both), `coordinator-doc-new` (sprint,
two loci — stub scaffold and origin-provenance frontmatter fill),
`stamp-sizing-object-field` (sprint), `detect-pm-gate-signal` (sprint),
`reject-path-shaped-gate-dependency` (sprint). This module names candidate
ops in `directives[].cli` for the sprint/both rows it reaches; it never
imports or calls any of them (an assembler `brief` is read-only per the
contract, same as `sizing_assemble.route()` never writes state).

Class A's eight glue names (`apply-roadmap-seed-precondition-exemption`,
`route-to-phase1`, `bind-problem-set-as-corpus`,
`seed-clusters-from-problem-set`, `seed-phase1-from-stub-scope`,
`seed-phase1-inventory-from-sizing-object`, `derive-run-id-from-stub-id`,
`cite-sizing-object-in-overview`) are ALL entry-point glue for Entry
Points B/C/D, and every one of those census rows is `seam: spine` only —
none applies to this module. This module carries no `CLASS_A_GLUE`
constant of its own; `roadmap_planning_assemble.CLASS_A_GLUE` is the sole
source (AC22/C13 read that constant, never re-type the list).

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
Mirrors `sizing_assemble.route()`'s, `pickup_assemble.brief()`'s, and
`roadmap_planning_assemble.brief()`'s read-only compute-half contract; a
future mutating `apply` half (per the contract's § The compute/apply
split) is out of this chunk's writes list.

Negative-spec:
    - Do NOT import `coordinator_core.ops` at module scope, or at all,
      anywhere in this module. That import alone costs +371.9ms
      (docs/research/spike-verdicts/2026-08-21-assembler-ops-under-the-
      brightline.md) — the entire reason this module exists as a hand-built
      CLI rather than a registered op.
    - Do NOT call any of the consumed ops. This module composes their
      names/output SHAPE into the decision object; it never imports or
      subprocess-invokes them. Execution is the contract's `apply` half's
      job (§ The compute/apply split), not shipped in this chunk's writes
      list.
    - Do NOT manufacture a `recommendation` for a judgment point this
      module has no evidence to narrow. Every advisory JUDGMENT/MIXED-
      judgment-half row from the census is semantic-authoring content
      (clustering granularity, verdict authoring, OVERVIEW prose, stub-body
      content, pm-gates row content, origin-provenance hand-fill) this
      module cannot compute — each is emitted as tier 3
      (`recommendation: null, reason: "insufficient-evidence"`), never a
      manufactured tier-2 offer (computed-skills.md § Candor is the design
      principle).
    - Do NOT emit a `recommendation` on a PM gate. Every `untrusted_gate`
      row this module reaches (p1.5.0-pm-authorize-research-depth,
      p1.5.4-pm-round1, p1.5.6-pm-round2 — all `seam: both`) is the
      recommendation-forbidden security class per computed-skills.md § The
      three-tier model: the PM's disposition is sourced from PM-authored
      content this engine does not itself compute, so no consumer may
      derive a disposition from an engine-authored offer here.
    - Do NOT include a spine-seam-only census row. Every entryB/entryC/
      entryD row, `p2.1.5-number-stubs`, `p2.3-stub-index`,
      `p2.4-disjointness-audit`, `p2.6-2.7-audit-roadmap-close`, and
      `residue-stub-dedup-canonicalization` (all `seam: spine`) belong to
      `roadmap_planning_assemble` (C10), not here.
    - Do NOT resolve an A/B/C/D entry point here. That shape is
      `roadmap_planning_assemble`'s alone (every entry-point row is
      `seam: spine`); this module's entry contract is `run_id` +
      `sprint_id` only, per DoE-claude C6's own body.
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

# Sprint/both-seam census rows this assembler reaches, mapped to the
# corrected 13-op consumes manifest's candidate_op binding (census
# "Consumes-manifest correction" section, 2026-08-21). Spine-only rows
# (every entryB/entryC/entryD row, p2.1.5-number-stubs, p2.3-stub-index,
# p2.4-disjointness-audit, p2.6-2.7-audit-roadmap-close,
# residue-stub-dedup-canonicalization) are roadmap_planning_assemble's
# (C10), never this module's.
_SPRINT_CANDIDATE_OPS: dict[str, str] = {
    "p1.5.1-dispatch-scouts": "dispatch-cluster-scout",
    "p2.1-scaffold-stub": "coordinator-doc-new",
    "p2.1a-carry-size": "stamp-sizing-object-field",
    "p2.5-pm-gates": "detect-pm-gate-signal",
    "residue-gate-text-subsystem-named": "reject-path-shaped-gate-dependency",
    "residue-frontmatter-fields-fill": "coordinator-doc-new",
}


class SprintPlanningAssembleError(ValueError):
    """Raised for a malformed input to brief() — a usage error, never a
    business-logic divergence (mirrors sizing_assemble.SizingAssembleError
    and roadmap_planning_assemble.RoadmapPlanningAssembleError: divergence
    is expressed via the decision object, never an exception)."""


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
    `coordinator_core.contract.decision_object.judgment.build_judgment_point`,
    mirroring `roadmap_planning_assemble._judgment_point`'s call shape."""
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

    Mirrors `roadmap_planning_assemble._pm_gate_judgment_point`'s call
    shape over the same shared
    `coordinator_core.contract.decision_object.judgment.
    build_untrusted_gate_judgment_point`."""
    return _build_untrusted_gate_judgment_point(
        id=id_,
        question=question,
        dispositions=dispositions,
        evidence=evidence,
        reason="recommendation-forbidden",
        revalidate_at_dispatch=False,
        round_trip=round_trip,
    )


def _sprint_mechanical_directives(run_id: str, sprint_id: str) -> list[dict[str, Any]]:
    """Directives for the MECHANICAL sprint/both-seam census rows this
    assembler reaches, naming a REAL consumed-op CLI. Composed
    unconditionally — sprint-planning has no entry-point branching (that
    shape belongs to roadmap_planning_assemble alone)."""
    directives: list[dict[str, Any]] = []

    directives.append(
        _directive(
            "d-dispatch-cluster-scout",
            _SPRINT_CANDIDATE_OPS["p1.5.1-dispatch-scouts"],
            ["--run-id", run_id, "--sprint-id", sprint_id],
            None,
            False,
        )
    )
    directives.append(
        _directive(
            "d-scaffold-stub",
            _SPRINT_CANDIDATE_OPS["p2.1-scaffold-stub"],
            ["--run-id", run_id, "--sprint-id", sprint_id],
            None,
            False,
        )
    )
    directives.append(
        _directive(
            "d-carry-size",
            _SPRINT_CANDIDATE_OPS["p2.1a-carry-size"],
            ["--run-id", run_id, "--sprint-id", sprint_id],
            ["d-scaffold-stub"],
            False,
        )
    )
    directives.append(
        _directive(
            "d-pm-gate-signal",
            _SPRINT_CANDIDATE_OPS["p2.5-pm-gates"],
            ["--run-id", run_id, "--sprint-id", sprint_id],
            None,
            False,
        )
    )
    directives.append(
        _directive(
            "d-reject-path-shaped-gate-dependency",
            _SPRINT_CANDIDATE_OPS["residue-gate-text-subsystem-named"],
            ["--run-id", run_id, "--sprint-id", sprint_id],
            None,
            False,
        )
    )
    directives.append(
        _directive(
            "d-frontmatter-fields-fill",
            _SPRINT_CANDIDATE_OPS["residue-frontmatter-fields-fill"],
            ["--run-id", run_id, "--sprint-id", sprint_id],
            ["d-scaffold-stub"],
            False,
        )
    )

    return directives


def _sprint_judgment_points() -> list[dict[str, Any]]:
    """Every sprint/both-seam JUDGMENT row and MIXED judgment-half from the
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
            "How should this sprint's inventoried inputs cluster into coverage units?",
            "artifact.run_id",
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
            "Author this sprint's OVERVIEW.md section per KEEP cluster, citing the research corpus and carrying a Contested block?",
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
            "PM final approval — diff vs. shape-approved acceptable, proceed to this sprint's Phase 2?",
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
            "j-p2.1.6-fold-to-size",
            "Band/collapse/split this sprint's waves into batons (loe: sizing) — how many stubs does step 2.1 scaffold?",
            "j-p1.3-verdict",
            [{"value": "banded", "resolves": ["d-scaffold-stub"]}],
            "round_trip",
        )
    )
    points.append(
        _judgment_point(
            "j-p2.2-stub-body",
            "What content does each stub body carry, in the fixed section order?",
            "directives[?id=='d-scaffold-stub']",
            [{"value": "authored", "resolves": []}],
            "terminal",
        )
    )
    points.append(
        _judgment_point(
            "j-p2.5-pm-gates",
            "Past the named-stakeholder role-word floor, who is actually on the hook for each detected PM-gate row — and what hand-authored rows does the detector never trip?",
            "directives[?id=='d-pm-gate-signal']",
            [{"value": "authored", "resolves": []}, {"value": "none-needed", "resolves": []}],
            "terminal",
        )
    )
    points.append(
        _judgment_point(
            "j-p2.8-sequential-reviews",
            "Conduct/integrate this sprint's Phase 2 review, or author the domain-reviewer skip rationale?",
            "j-p2.2-stub-body",
            [{"value": "conducted", "resolves": []}, {"value": "skipped-with-rationale", "resolves": []}],
            "terminal",
        )
    )
    points.append(
        _judgment_point(
            "j-residue-frontmatter-fields-fill",
            "What origin-provenance values (origin_session, origin_handoff, origin_plan_id, origin_goal_id) apply from live session context, and does origin_handoff exist before it is written?",
            "directives[?id=='d-frontmatter-fields-fill']",
            [{"value": "authored", "resolves": []}],
            "terminal",
        )
    )

    return points


def brief(
    *,
    run_id: Optional[str] = None,
    sprint_id: Optional[str] = None,
    decisions: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Computes and returns one sprint's decision object, per DR-047's
    Decision-Object Schema-of-Record. READ-ONLY: touches no disk, mutates
    nothing, calls nothing.

    Entry contract (DoE-claude C6): invoked once per sprint, with a sprint
    id resolved against a run id. Both `run_id` and `sprint_id` are
    required — unlike `roadmap_planning_assemble.brief()`, this module
    resolves no A/B/C/D entry point (that shape is spine-only).

    Returns:
        A dict: {artifact, preflight, gates, directives, judgment_points,
        decisions, narration, next_move} — see module docstring.
    """
    decisions = dict(decisions or {})

    if not run_id or not sprint_id:
        raise SprintPlanningAssembleError(
            "run_id and sprint_id are both required (sprint-planning's entry "
            "contract: a sprint id resolved against a run id)"
        )

    artifact = {
        "run_id": run_id,
        "sprint_id": sprint_id,
    }

    preflight = {
        "seam": "sprint",
        "dedup_candidates": [],
    }

    gates = {
        "sprint_id_resolution": {
            "verdict": "resolved",
        },
    }

    directives = _sprint_mechanical_directives(run_id, sprint_id)
    judgment_points = _sprint_judgment_points()

    narration = (
        f"Ran ahead of you: computed sprint {sprint_id} routing for run {run_id}, "
        f"{len(directives)} directive(s), {len(judgment_points)} judgment point(s)."
    )
    next_move = ""

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
        f"{prog}: usage: {prog} --run-id <id> --sprint-id <id> [--decisions <json>]",
        file=stream,
    )
    return EXIT_USAGE


def main(argv: list[str]) -> int:
    import json
    import sys

    prog = "sprint-planning-assemble"
    run_id = None
    sprint_id = None
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
        elif tok == "--sprint-id" and i + 1 < len(argv):
            sprint_id = argv[i + 1]
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
            sprint_id=sprint_id,
            decisions=decisions,
        )
    except SprintPlanningAssembleError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - structural backstop, mirrors sizing_assemble
        print(f"{prog}: unexpected failure: {exc}", file=sys.stderr)
        print(json.dumps({"error": str(exc), "transport_failure": True}))
        return EXIT_TRANSPORT_FAIL

    print(json.dumps(decision, indent=2, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
