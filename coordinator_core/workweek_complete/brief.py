"""
coordinator_core.workweek_complete.brief — the `workweek-complete` computed-
skill engine's READ-ONLY compute half.

Purpose: computes `coordinator/commands/workweek-complete.md`'s mechanical
step inventory (Step 1a/1b/2/2.5/3.5/4-counts/4b-4k-guard-battery/5/6/
7-illegal-path-backstop/7.6/7.7/10-version-check/13-archive/13.5/13.6) into
the canonical 8-key decision-object envelope, and surfaces every
judgment-shaped step as an overridable `judgment_points[]` offer rather than
deciding it for the caller. Mirrors `coordinator_core.workday_complete.brief`
(C2)'s shape — see that module for the shared design rationale.

This module imports its envelope/judgment-point constructors from the shipped
canonical-resolution-engine library (`coordinator_core.resolution.facade`,
`coordinator_core.contract.decision_object.{envelope,judgment}`) rather than
reimplementing them — see the negative-spec below.

Contract (frozen, reviewed): DoE-claude coordinator/docs/wiki/computed-skills.md
Spec backlink: DoE-claude:pln-b1-ceremony-complete-computed--9ffa54, chunk C5

Consumes-manifest (C4 census, plan § Tasks C4 body) — orchestrates,
reimplements none of the following existing atomic CLIs/scripts under
`coordinator/bin/` (every `directives[].cli` value below is a literal name
drawn from exactly this set):
    list-week-changelog, backfill-week-changelog-gaps,
    validate-fast-and-packageability, lint-frontmatter,
    workweek-complete-advisories, query-records,
    detect-initiative-candidates, coordinator-initiative, cruft-sweep,
    check-wsc-inline-budget, reassess-goal-krs,
    workweek-complete-drift-guards, workweek-complete-reverse-drift-gate,
    check-competitor-positioning-nudge, check-no-illegal-paths,
    workweek-trail-scope, check-arch-audit-staleness,
    check-atlas-watch-drift, query-completions, workweek-complete-close,
    check-version-consistency, coordinator-ceremony-hook, emit-cadence,
    workweek-complete-doc-staleness, workweek-complete-doc-verify

Negative-spec:
    - Does NOT reimplement `build_envelope`/`emit`/`build_judgment_point`/
      `build_disposition`/`resolve_operator_config` — imported from the
      shipped library, mirroring C2's own negative-spec.
    - Does NOT add a mutating code path — every directive names an existing
      CLI for the apply half (`coordinator_core.workweek_complete.apply`) to
      invoke.
    - Does NOT represent `scc`, `node run.js`, or `gh release` as
      `directives[].cli` values — C4's census names these as third-party
      tools/external invocations with no corresponding project script under
      `coordinator/bin/`, so representing any of them as a `directives[].cli`
      value would violate the "every directive names an EXISTING CLI, never
      a phantom verb" rule (AC15c). All three are noted in `narration`
      instead — a deliberate scope line, mirroring C2's own Step 2/Step 5
      exclusion.
    - Does NOT hand-maintain its own copy of the Step 13.5/13.6 close-tail
      directives — AC9's byte-identity check against `workday_complete.
      brief`'s Step 10.5/10.6 pair found the two tails identical in every
      load-bearing field (same two CLIs, empty args, `depends_on=None`),
      differing only in `hard_block` — a key this module's own uniform
      post-build pass stamps onto EVERY directive it builds, tail included,
      not a tail-specific divergence. Both assemblers now consume
      `coordinator_core.ceremony_common.tail.build_ceremony_close_tail`.

Wire-vocabulary note (2026-08-04): the Step 7.5 judgment-point id was renamed
from its prior persona-named form to the current role-based
`jp_step7_5_staff_eng_fire_discretion` per the PM ruling that a persona slug
must never be a wire key/value/enum member (state/sizings/2026-08-04-persona-
wire-vocabulary-rename.yaml; docs/decisions/DR-262 Amendment cl.3). The
`question` field's human-facing "the Staff Engineer" reference is left as-is by design —
persona names remain legal as presentation prose (a publish-time scrub swaps
them), only the identifier moved. Coordinated with DoE-claude via cross-repo
memo (cross-repo/inbox/2026-08-04-doe-claude-em-correction-the-shard-key-
coupling-is-three-keys-five-files.md) — DoE's reader site moves in the same
window, no back-compat/dual-spelling transition.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ceremony_common.tail import build_ceremony_close_tail
from coordinator_core.contract.decision_object.envelope import (
    build_envelope,
    emit,
    extend_exit_codes,
)
from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_judgment_point,
    partition_reportable,
)
from coordinator_core.git.repo_root import git_common_dir
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.resolution.facade import resolve_operator_config

# ---------------------------------------------------------------------------
# Exit-code contract (brief-side, 0-3) — locally scoped to this compute half,
# NOT shared with the apply half's own 0-4 enumeration (see apply.py's own
# `WorkweekApplyExitCode`; computed-skills.md § Exit-code contract for a
# mutating half requires each half to pin its own set).
# ---------------------------------------------------------------------------
WorkweekExitCode = extend_exit_codes(
    "WorkweekExitCode",
    BUSINESS_FAIL=1,
    USAGE=2,
    TRANSPORT_FAIL=3,
)

#: The C4 consumes-manifest (plan § Tasks C4 body) — the CLOSED set of CLI
#: names any `directives[].cli` value in this module is drawn from. Never
#: extended ad hoc; a new mechanical step needs a manifest update first.
CONSUMES_MANIFEST: tuple[str, ...] = (
    "list-week-changelog",
    "backfill-week-changelog-gaps",
    "validate-fast-and-packageability",
    "lint-frontmatter",
    "workweek-complete-advisories",
    "query-records",
    "detect-initiative-candidates",
    "coordinator-initiative",
    "cruft-sweep",
    "check-wsc-inline-budget",
    "reassess-goal-krs",
    "workweek-complete-drift-guards",
    "workweek-complete-reverse-drift-gate",
    "check-competitor-positioning-nudge",
    "check-no-illegal-paths",
    "workweek-trail-scope",
    "check-arch-audit-staleness",
    "check-atlas-watch-drift",
    "query-completions",
    "workweek-complete-close",
    "check-version-consistency",
    "coordinator-ceremony-hook",
    "emit-cadence",
    "workweek-complete-doc-staleness",
    "workweek-complete-doc-verify",
    "tier-u-grant-cli",
)


#: The canonical dashed ceremony name — the `coordinator.local.md` key
#: `coordinator-ceremony-hook` resolves, AND the `ceremony` field the Tier-U
#: grant carries. One constant so the handback's `--only-ceremony` guard can
#: never drift from the value the write stamped: a mismatch would silently
#: turn every handback into a no-op and leave the grant live past the
#: ceremony, the exact unbounded-grant defect the write exists to bound.
_CEREMONY_NAME = "workweek-complete"

#: Stored VERBATIM in the grant record's `note` (write_tier_u_grant never
#: normalizes it). Names the ceremony's Tier-U consumers so an auditor
#: reading a live grant can tell what it was minted for: Step 2's
#: `plugin-ecosystem/run.js` and Step 8's `/parallel-code-review` Test-Output
#: Capture, both of which fire before Step 16's nested `/merge-to-main`.
_TIER_U_GRANT_NOTE = (
    "implicit ceremony grant: /workweek-complete Step 0.9 — bounds Step 2 "
    "(plugin-ecosystem suite) and Step 8 (/parallel-code-review Test-Output "
    "Capture); handed back at d_step13_7_tier_u_grant_handback"
)


def _directive(
    id: str,
    *,
    cli: str,
    args: list[str],
    depends_on: Optional[str] = None,
    already_satisfied: bool = False,
) -> dict[str, Any]:
    """One `directives[]` entry. `cli` MUST be a member of `CONSUMES_MANIFEST`
    (enforced by `_build_directives`'s own assertion, mirroring C2's
    `_directive` helper)."""
    return {
        "id": id,
        "cli": cli,
        "args": list(args),
        "depends_on": depends_on,
        "already_satisfied": already_satisfied,
    }


def _resolve_repo_root_for_doc_staleness(start: Optional[Path] = None) -> Optional[str]:
    """`git rev-parse --path-format=absolute --git-common-dir` for `start`
    (default cwd), then `main_worktree_root(common_dir)` -- same two-step
    ladder as `workday_complete.brief._resolve_repo_common_dir_for_ceremony`
    (never a bare `git rev-parse --show-toplevel`, so a ceremony invoked
    from a linked worktree still resolves the doc registry against the
    MAIN worktree's `coordinator.local.md`, matching where the C4 registry
    actually lives). Returns `None` on any resolution failure -- never
    raises; the caller degrades to an empty stale-docs list."""
    cwd = start or Path.cwd()
    out = git_common_dir(str(cwd))
    if not out:
        return None
    try:
        return str(main_worktree_root(Path(out)))
    except Exception:  # noqa: BLE001 - never fail the ceremony
        return None


def _compute_doc_staleness_report() -> list[dict[str, Any]]:
    """Read-only C5 leg for the doc-staleness gate (plan `docs/plans/2026-
    07-28-human-facing-doc-staleness-detector.md`, chunk C5): resolves the
    invoking repo's root, then calls `coordinator_core.ops.doc_staleness
    .build_doc_staleness_report_from_registry` (C1 -- landed concurrently
    with this chunk; reads the C4 doc registry + threshold overrides from
    `coordinator.local.md` itself, so this module supplies only the repo
    root).

    Never raises and never fails the ceremony (mirrors
    `workday_complete.brief._compute_open_day_goals`'s degradation
    posture) -- an unresolvable repo root, an absent op/registry module,
    or any other failure degrades to an EMPTY report (zero stale docs),
    never to a spurious ask: unlike `_compute_dirty_tree_verdict`'s "fail
    toward asking" (a git-state safety probe), a doc-staleness false
    negative is the correct default here -- this gate is
    advisory-additive, not safety-critical, so degrading silent is
    preferred over surfacing a judgment point with no real evidence
    behind it.

    Returns the report's `docs` list verbatim -- each entry carries AC6's
    evidence fields for `status: ok` docs (`path`, `stale`, `commits_since`,
    `days_since`, `last_touch_sha`, `last_touch_date`, `changed_areas`,
    `threshold_commits`, `threshold_days`), or just `{"path", "status"}`
    for `absent`/`no_content_modifying_history` docs -- `_stale_doc_entries`
    filters on the `stale` key so those shapes never reach the judgment
    points below.
    """
    repo_root = _resolve_repo_root_for_doc_staleness()
    if repo_root is None:
        return []
    try:
        from coordinator_core.ops.doc_staleness import (  # noqa: PLC0415
            build_doc_staleness_report_from_registry,
        )

        report = build_doc_staleness_report_from_registry(repo_root)
        return list(report.get("docs", []))
    except Exception as exc:  # noqa: BLE001 - never fail the ceremony
        print(
            "workweek_complete.brief: doc-staleness report unavailable, "
            f"degrading to zero stale docs: {exc}",
            file=sys.stderr,
        )
        return []


def _stale_doc_entries(report: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filters a doc-staleness report down to entries the op marked
    `stale` with a non-empty `path` -- guards a malformed/partial entry
    from silently producing a judgment point with no doc to name."""
    return [entry for entry in report if entry.get("stale") and entry.get("path")]


def _doc_staleness_slug(path: str) -> str:
    """Deterministic filesystem-path -> id-fragment slug (e.g.
    `coordinator/README.md` -> `coordinator_readme_md`) -- stable across
    runs for the same path and collision-free across a repo's doc
    registry (each registry path is already unique)."""
    return re.sub(r"[^a-z0-9]+", "_", path.lower()).strip("_")


def _doc_staleness_jp_id(path: str) -> str:
    return f"jp_doc_staleness_{_doc_staleness_slug(path)}"


def _doc_staleness_ack_directive_id(path: str) -> str:
    return f"d_doc_staleness_ack_{_doc_staleness_slug(path)}"


def _doc_staleness_evidence(entry: dict[str, Any]) -> str:
    """AC6 evidence line for one stale-doc judgment point -- every field
    the detector op is specced to emit, rendered verbatim rather than
    summarized, so the EM's disposition is made against the same numbers
    the guard-sweep advisory entry (`d_step4b_4k_doc_staleness`) shows."""
    return (
        f"{entry.get('path')}: commits_since={entry.get('commits_since')} "
        f"(threshold {entry.get('threshold_commits')}), "
        f"days_since={entry.get('days_since')} "
        f"(threshold {entry.get('threshold_days')}), "
        f"last_touch={entry.get('last_touch_sha')}@{entry.get('last_touch_date')}, "
        f"changed_areas={entry.get('changed_areas')}"
    )


def _compute_doc_verify_findings() -> list[dict[str, Any]]:
    """Read-only C6c leg for the doc-content-verification gate (plan
    `docs/plans/2026-07-28-human-facing-doc-staleness-detector.md`, chunk
    C6c): resolves the invoking repo's root (reuses
    `_resolve_repo_root_for_doc_staleness` -- that helper's git-common-dir +
    main-worktree-root ladder is generic, not staleness-specific, so this
    leg does not duplicate it), then calls `coordinator_core.ops.
    doc_content_verify.build_findings_report_from_registry` directly --
    mirrors how `_compute_doc_staleness_report` consumes `doc_staleness
    .build_doc_staleness_report_from_registry` for the sibling signal, so
    the two read consistently to anyone scanning this file. Consumes typed
    `Finding`/`Citation` objects (surfaced as the same JSON-shaped dict list
    the op's own CLI prints) rather than shelling out to
    `doc_content_verify.main` and scraping its captured stdout as an ad hoc
    API -- that seam was fragile on this hot path (depends on the CLI's
    stdout formatting, swallows/interleaves anything else written to stdout
    during the call, and breaks the moment the CLI adds a log line).

    Never raises and never fails the ceremony -- mirrors
    `_compute_doc_staleness_report`'s degradation posture: an unresolvable
    repo root, an absent op, or any other failure degrades to an EMPTY
    findings list, never to a spurious ask.

    Returns the report's `findings` list verbatim -- each entry carries
    `doc`, `line`, `token`, `reason` (`"absent"` or `"moved"`; never
    `"resolves-cross-repo"` -- see `doc_content_verify`'s own negative-spec).
    """
    repo_root = _resolve_repo_root_for_doc_staleness()
    if repo_root is None:
        return []
    try:
        from coordinator_core.ops.doc_content_verify import (  # noqa: PLC0415
            build_findings_report_from_registry,
        )

        report = build_findings_report_from_registry(repo_root)
        return list(report.get("findings", []))
    except Exception as exc:  # noqa: BLE001 - never fail the ceremony
        print(
            "workweek_complete.brief: doc-verify findings unavailable, "
            f"degrading to zero findings: {exc}",
            file=sys.stderr,
        )
        return []


def _verify_findings_by_doc(
    findings: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Groups a flat findings list by `doc` -- the AC14 cardinality bound:
    verification findings are per-citation-per-line and unbounded, so the
    gate carries at most ONE judgment point per doc (this grouping), with
    the finding list attached as evidence, never one judgment point per
    finding."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for finding in findings:
        doc = finding.get("doc")
        if not doc:
            continue
        grouped.setdefault(doc, []).append(finding)
    return grouped


def _doc_verify_slug(path: str) -> str:
    """Delegates to `_doc_staleness_slug` -- same path->id-fragment rule,
    named for this gate's own id family rather than duplicating the
    regex."""
    return _doc_staleness_slug(path)


def _doc_verify_jp_id(path: str) -> str:
    return f"jp_doc_verify_{_doc_verify_slug(path)}"


def _doc_verify_ack_directive_id(path: str) -> str:
    return f"d_doc_verify_ack_{_doc_verify_slug(path)}"


def _doc_verify_evidence(path: str, findings: list[dict[str, Any]]) -> str:
    """One evidence line per doc, listing every attached finding verbatim
    (line/token/reason) -- the doc-level judgment point's evidence is the
    full finding list, per AC14, never a single summarized count."""
    rendered = "; ".join(
        f"line {finding.get('line')} `{finding.get('token')}` "
        f"({finding.get('reason')})"
        for finding in findings
    )
    return f"{path}: {len(findings)} finding(s) -- {rendered}"


def _build_directives(
    stale_docs: Optional[list[dict[str, Any]]] = None,
    doc_verify_findings_by_doc: Optional[dict[str, list[dict[str, Any]]]] = None,
) -> list[dict[str, Any]]:
    """Tier-1 (mechanical, no open question) directive entries — one per
    collapsed workweek-complete.md step: 1a/1b/2/2.5/3.5/4-counts/
    4b-4k-guard-battery/5/6/7-illegal-path-backstop/7.6/7.7/10-version-check/
    13-archive/13.5/13.6. Every `cli` value is a literal member of
    `CONSUMES_MANIFEST`.

    The 4b-4k guard battery (10 lettered advisory/drift gates per C4's
    census, DoE-claude coordinator/commands/workweek-complete.md § Step
    4b-4k guard-sweep census) collapses to one directive per underlying
    CLI/subcommand rather than one per lettered step — `hard_block` marks
    the four gates the census identified as hard-blocking (4c UBT pending-
    record merge, 4g reverse-drift, 4k vendored-schema drift, plus the
    Step-10 version-consistency check); the rest are advisory. All Tier-1
    directives here are ungated (`depends_on=None`) — unlike C2's workday
    assembler, no Tier-1 workweek step was named in this chunk's spec as
    gated on a specific judgment point.

    `workweek-complete-drift-guards` bundles several 4b-4k subcommands
    behind one CLI whose per-subcommand severity differs (`description-
    length`/`enabled-plugins`/`cve-recheck` are advisory-only,
    `schema-drift-gate` is 4k's hard-blocking gate) — per DoE's own
    per-subcommand exit-code docstring (coordinator/bin/workweek-complete-
    drift-guards.py), a single `hard_block` boolean covering the whole CLI
    cannot be correct for both sides of that bundle. This module therefore
    emits ONE directive per subcommand (`d_step4b_4k_description_length`,
    `d_step4b_4k_enabled_plugins`, `d_step4b_4k_cve_recheck`,
    `d_step4b_4k_schema_drift`) rather than a single `args=[]` directive
    naming the bundling CLI with no subcommand at all — `pcli-drift-gate`
    is deliberately NOT one of them (the ceremony doc's own prose: "no
    directive emits its subcommand" — it stays a by-hand Step 5 invocation)
    and neither is `shellcheck-sweep`/`console-flash-guard`/`multi-event-
    hook-guard` (Step 6 concerns, outside the 4b-4k census).

    `d_step4c_ubt_pending_merge_gate` (4c, hard-blocking) delegates to
    `workweek-complete-advisories`'s `ubt-unresolved <repo-root>`
    subcommand — the only caller of `scan_unresolved_ubt_records`
    (coordinator_core.ops.scan_unresolved_ubt_records). Repo-root is
    resolved via `_resolve_repo_root_for_doc_staleness()` (the same
    git-common-dir + main-worktree-root ladder every other repo-root-
    needing directive in this module reuses), falling back to `"."`
    on resolution failure — never omitting the required positional arg.
    Non-UE-repo degradation is intentionally NOT special-cased here:
    `scan_unresolved_ubt_records` already returns `[]` for a repo with no
    `state/review-trail/` tree (absent-dir-safe, per that module's own
    negative-spec), and the CLI subcommand always exits 0 regardless of
    finding count (`hard_block` is render-time metadata only, per this
    function's own comment below — apply.py's halt contract never reads
    it), so a non-UE repo silently no-ops through the same code path a UE
    repo with zero unresolved markers takes — no separate no-op branch to
    write or test. No override-env plumbing is added here: neither this
    CLI nor `scan_unresolved_ubt_records` reads a
    `COORDINATOR_OVERRIDE_UBT_GATE`-shaped variable today (unlike
    `workweek-complete-reverse-drift-gate.py`'s own
    `COORDINATOR_OVERRIDE_REVERSE_DRIFT`, which that CLI reads directly,
    not via a directive-threaded arg) — inventing one here would be a new
    convention this chunk was not asked to build; the ceremony doc's
    `COORDINATOR_OVERRIDE_UBT_GATE=1` line is a Step-5 human-procedure
    escape hatch, not something a directive's `args[]` carries.

    Doc-staleness gate (plan `docs/plans/2026-07-28-human-facing-doc-
    staleness-detector.md`, chunk C5) — composes the two existing
    primitives, adding ONE advisory entry to the battery (now eleven) and
    leaving the existing hard-blocking-three/advisory-seven split
    otherwise untouched (B1 anti-scope):
      (a) `d_step4b_4k_doc_staleness` — ordinary advisory entry
          (`hard_block=False`), ungated, always runs; its own CLI output
          carries the AC6 evidence per doc into the Step 5 render, same
          shape as `d_step4b_4k_arch_audit_staleness`.
      (b) One per-stale-doc follow-on directive
          (`d_doc_staleness_ack_<slug>`), gated via `depends_on` on that
          doc's judgment point (`_build_judgment_points`) — reuses the
          same CLI scoped to one doc (`--doc <path> --ack`), never fires
          until the EM records an explicit disposition. `depends_on` is
          singular per directive (contract, `_directive`), which is why
          this is one directive per doc rather than N judgment points
          fanning into a single shared directive. `stale_docs` is
          `_compute_doc_staleness_report`'s report, pre-filtered to
          `stale` entries by `brief()` before being threaded into both
          this function and `_build_judgment_points`.

    Doc-content-verification gate (chunk C6c) — same delivery shape as (a)/
    (b) above, ONE gate with TWO signal types (AC14): staleness asks "has
    this doc moved recently", verification asks "is what it says still
    true" (the `b644d5a9` incident shape). Adds ONE further advisory entry
    to the battery (now twelve):
      (c) `d_step4b_4k_doc_verify` — ordinary advisory entry
          (`hard_block=False`), ungated, always runs; its CLI output
          carries every citation finding across the declared doc registry
          into the Step 5 render, same shape as (a).
      (d) One per-doc follow-on directive (`d_doc_verify_ack_<slug>`), gated
          via `depends_on` on that doc's judgment point, ONLY for docs that
          actually carry findings — `doc_verify_findings_by_doc` is
          `_compute_doc_verify_findings`'s findings grouped by doc
          (`_verify_findings_by_doc`), never one directive per individual
          finding (AC14 cardinality bound: findings are per-citation and
          unbounded, so the fan-out is bounded by doc count, not finding
          count).
    """
    stale_docs = stale_docs or []
    doc_verify_findings_by_doc = doc_verify_findings_by_doc or {}
    directives = [
        _directive(
            "d_step0_9_tier_u_grant_write",
            cli="tier-u-grant-cli",
            args=[
                "grant",
                "ceremony",
                _TIER_U_GRANT_NOTE,
                "--ceremony",
                _CEREMONY_NAME,
            ],
        ),
        _directive("d_step1a_list_changelog", cli="list-week-changelog", args=[]),
        _directive(
            "d_step1b_backfill_changelog_gaps",
            cli="backfill-week-changelog-gaps",
            args=[],
        ),
        _directive(
            "d_step2_resolve_validation_cmd",
            cli="validate-fast-and-packageability",
            args=["fast"],
        ),
        _directive("d_step2_5_lint_frontmatter", cli="lint-frontmatter", args=[]),
        _directive(
            "d_step3_5_advisories", cli="workweek-complete-advisories", args=[]
        ),
        _directive("d_step4_counts_query_records", cli="query-records", args=[]),
        _directive(
            "d_step4_counts_initiative_candidates",
            cli="detect-initiative-candidates",
            args=[],
        ),
        _directive(
            "d_step4_counts_coordinator_initiative",
            cli="coordinator-initiative",
            args=["--list"],
        ),
        _directive("d_step4_counts_cruft_sweep", cli="cruft-sweep", args=[]),
        _directive(
            "d_step4_counts_wsc_budget", cli="check-wsc-inline-budget", args=[]
        ),
        _directive("d_step4_counts_goal_krs", cli="reassess-goal-krs", args=[]),
        _directive(
            "d_step4b_4k_description_length",
            cli="workweek-complete-drift-guards",
            args=["description-length"],
        ),
        _directive(
            "d_step4b_4k_enabled_plugins",
            cli="workweek-complete-drift-guards",
            args=["enabled-plugins"],
        ),
        _directive(
            "d_step4b_4k_cve_recheck",
            cli="workweek-complete-drift-guards",
            args=["cve-recheck"],
        ),
        _directive(
            "d_step4b_4k_schema_drift",
            cli="workweek-complete-drift-guards",
            args=["schema-drift-gate"],
        ),
        _directive(
            "d_step4c_ubt_pending_merge_gate",
            cli="workweek-complete-advisories",
            args=["ubt-unresolved", _resolve_repo_root_for_doc_staleness() or "."],
        ),
        _directive(
            "d_step4b_4k_reverse_drift",
            cli="workweek-complete-reverse-drift-gate",
            args=[],
            already_satisfied=False,
        ),
        _directive(
            "d_step4b_4k_version_consistency",
            cli="check-version-consistency",
            args=[],
        ),
        _directive(
            "d_step4b_4k_competitor_positioning",
            cli="check-competitor-positioning-nudge",
            args=[],
        ),
        _directive(
            "d_step4b_4k_atlas_watch_drift", cli="check-atlas-watch-drift", args=[]
        ),
        _directive(
            "d_step4b_4k_arch_audit_staleness",
            cli="check-arch-audit-staleness",
            args=[],
        ),
        _directive(
            "d_step4b_4k_doc_staleness",
            cli="workweek-complete-doc-staleness",
            args=[],
        ),
        _directive(
            "d_step4b_4k_doc_verify",
            cli="workweek-complete-doc-verify",
            args=[],
        ),
        _directive(
            "d_step5_7_illegal_path_backstop",
            cli="check-no-illegal-paths",
            args=[],
        ),
        _directive("d_step6_query_completions", cli="query-completions", args=[]),
        _directive("d_step7_6_trail_scope", cli="workweek-trail-scope", args=[]),
        _directive("d_step13_archive_close", cli="workweek-complete-close", args=[]),
        *(
            _directive(
                _doc_staleness_ack_directive_id(entry["path"]),
                cli="workweek-complete-doc-staleness",
                args=["--doc", entry["path"], "--ack"],
                depends_on=_doc_staleness_jp_id(entry["path"]),
            )
            for entry in stale_docs
        ),
        *(
            _directive(
                _doc_verify_ack_directive_id(doc_path),
                cli="workweek-complete-doc-verify",
                args=["--doc", doc_path, "--ack"],
                depends_on=_doc_verify_jp_id(doc_path),
            )
            for doc_path in doc_verify_findings_by_doc
        ),
        *build_ceremony_close_tail(
            post_command_hook_id="d_step13_5_post_command_hook",
            emit_cadence_id="d_step13_6_emit_cadence",
            ceremony_name=_CEREMONY_NAME,
        ),
        _directive(
            "d_step13_7_tier_u_grant_handback",
            cli="tier-u-grant-cli",
            args=["revoke", "--only-ceremony", _CEREMONY_NAME],
        ),
    ]
    # `hard_block` is metadata only — the halt contract in apply.py does not
    # read it; it exists so the skill-body render (C6) can preserve
    # hard-block-vs-advisory granularity per AC9/C8 (see C4's census note on
    # which of the 4b-4k gates are hard-blocking).
    # A grant that could not be minted (or handed back) must not turn a
    # ceremony that otherwise fully succeeded into `PARTIAL_MUTATION`,
    # whose contract tells the operator to stop and reconcile. Both legs
    # are best-effort for the same reason `merge_assemble.apply`'s handler
    # tolerates exit 1: `write_tier_u_grant`/`revoke_tier_u_grant` return
    # False on an INFRA condition (unresolvable sid — routine on a box
    # running dozens of concurrent sessions), and the DR-088 layer-5 guard
    # fails CLOSED, so an unminted grant refuses the Tier-U consumer rather
    # than authorizing it. The failure still reaches the operator, in
    # `report["degraded"]`.
    best_effort_ids = {
        "d_step0_9_tier_u_grant_write",
        "d_step13_7_tier_u_grant_handback",
    }
    hard_block_ids = {
        "d_step4c_ubt_pending_merge_gate",
        "d_step4b_4k_schema_drift",
        "d_step4b_4k_reverse_drift",
        "d_step4b_4k_version_consistency",
    }
    for entry in directives:
        assert entry["cli"] in CONSUMES_MANIFEST, (
            f"_build_directives: directive {entry['id']!r} names {entry['cli']!r}, "
            "not a member of CONSUMES_MANIFEST (AC15c: no phantom verbs)"
        )
        entry["hard_block"] = entry["id"] in hard_block_ids
        if entry["id"] in best_effort_ids:
            entry["best_effort"] = True
    return directives


def _build_judgment_points(
    stale_docs: Optional[list[dict[str, Any]]] = None,
    doc_verify_findings_by_doc: Optional[dict[str, list[dict[str, Any]]]] = None,
) -> list[dict[str, Any]]:
    """Tier-2 (judgment, recommendation required) and Tier-3 (your-call, no
    recommendation) judgment-point entries.

    Tier-2 (recommendation required): `jp_step4_triage_dispatch` (triage
    dispatch + prior-art scan), `jp_step7_rule5_already_reviewed_span`,
    `jp_step7_5_staff_eng_fire_discretion`, `jp_step8_5_loe_high_water`,
    `jp_step9_editorial_bucketing`, `jp_step10_semver_judgment`.

    Tier-3 (your-call, no recommendation — `recommendation=None`, `reason`
    naming PM-authority or irreversibility, NEVER `insufficient-evidence`
    per the Staff Engineer F3 against C2's Step 2.5 miscast): `jp_step1c_pm_recollection_match`
    (PM must confirm the week's recollection matches disk — a PM-authority
    confirmation, not a missing-evidence gap), `jp_step9_pm_release_notes_gate`
    (PM-authority: the release-notes framing is the PM's editorial call),
    `jp_step10_5_gh_release_publish` (irreversibility: `gh release` publish
    is an external, irreversible action).

    Doc-staleness gate (C5, see `_build_directives`): one
    `jp_doc_staleness_<slug>` per `stale_docs` entry, `recommendation=None`
    with `reason="pm-scoped-tradeoff"` — same category as
    `jp_step7_5_staff_eng_fire_discretion`/`jp_step9_editorial_bucketing`
    (an editorial/prioritization call the evidence doesn't pre-decide, not
    a missing-evidence gap: AC6's full evidence is attached). Emitted only
    for docs the op actually marked stale — a repo with a clean report
    emits none, mirroring `jp_step2_5_dirty_tree_ambiguous`'s
    only-when-live conditional shape in the sibling workday assembler.
    `resolves` on each disposition names that doc's own follow-on
    directive (`d_doc_staleness_ack_<slug>`) — never auto-resolved.

    Doc-content-verification gate (C6c, see `_build_directives` for the
    paired directive shape): one `jp_doc_verify_<slug>` per doc that carries
    at least one finding (AC14's per-doc, not per-finding, cardinality
    bound) — `recommendation=None` with `reason="pm-scoped-tradeoff"`, same
    Tier-3 category as the staleness judgment point above. Disposition
    vocabulary is deliberately DIFFERENT from staleness's softer
    `reviewed_ok`/`queued_for_update` pair: `fixed` / `bug-filed` /
    `resolves-cross-repo` / `not-a-defect-with-reason` — no `noted` option.
    That asymmetry is the point (AC14): a citation that fails verification
    is a correctness defect (the `b644d5a9` shape), not a freshness nudge,
    so every disposition in this vocabulary names a resolution, mirroring
    the global fix-by-default discriminator for break-class findings rather
    than offering a passive acknowledgment. `resolves-cross-repo` covers the
    case where the EM's own investigation finds the citation IS valid
    against a sibling root the automated extractor's `sibling_checkers`
    didn't cover — a legitimate non-defect outcome distinct from
    `not-a-defect-with-reason` (e.g. an intentionally-unresolvable
    placeholder). Evidence carries the full per-doc finding list
    (`_doc_verify_evidence`) — never one judgment point per citation, which
    on an unbounded per-line finding set would reproduce the `--force`
    muscle memory the anti-scope forbids. `resolves` on each disposition
    names that doc's own follow-on directive (`d_doc_verify_ack_<slug>`).
    """
    stale_docs = stale_docs or []
    doc_verify_findings_by_doc = doc_verify_findings_by_doc or {}
    doc_staleness_points = [
        build_judgment_point(
            None,
            id=_doc_staleness_jp_id(entry["path"]),
            question=(
                f"'{entry['path']}' looks stale per the doc-staleness "
                "detector — reviewed and still current, or queue it for "
                "an update?"
            ),
            dispositions=[
                build_disposition(
                    "reviewed_ok",
                    resolves=[_doc_staleness_ack_directive_id(entry["path"])],
                ),
                build_disposition(
                    "queued_for_update",
                    resolves=[_doc_staleness_ack_directive_id(entry["path"])],
                ),
            ],
            evidence=_doc_staleness_evidence(entry),
            reason="pm-scoped-tradeoff",
            revalidate_at_dispatch=True,
            round_trip="terminal",
        )
        for entry in stale_docs
    ]
    doc_verify_points = [
        build_judgment_point(
            None,
            id=_doc_verify_jp_id(doc_path),
            question=(
                f"'{doc_path}' has {len(findings)} content-verification "
                "finding(s) — citation(s) that don't resolve on disk. How "
                "should these be disposed?"
            ),
            dispositions=[
                build_disposition(
                    "fixed",
                    resolves=[_doc_verify_ack_directive_id(doc_path)],
                ),
                build_disposition(
                    "bug-filed",
                    resolves=[_doc_verify_ack_directive_id(doc_path)],
                ),
                build_disposition(
                    "resolves-cross-repo",
                    resolves=[_doc_verify_ack_directive_id(doc_path)],
                ),
                build_disposition(
                    "not-a-defect-with-reason",
                    resolves=[_doc_verify_ack_directive_id(doc_path)],
                ),
            ],
            evidence=_doc_verify_evidence(doc_path, findings),
            reason="pm-scoped-tradeoff",
            revalidate_at_dispatch=True,
            round_trip="terminal",
        )
        for doc_path, findings in doc_verify_findings_by_doc.items()
    ]
    return [
        build_judgment_point(
            {
                "disposition": "dispatch",
                "rationale": (
                    "The weekly triage/prior-art scan is the primary signal "
                    "for what this week's summary foregrounds; skip only "
                    "when there is nothing new to triage."
                ),
            },
            id="jp_step4_triage_dispatch",
            question="Dispatch the Step 4 triage/prior-art-scan worker for this week?",
            dispositions=[
                build_disposition("dispatch"),
                build_disposition("skip_no_new_work"),
            ],
            evidence="query-records / detect-initiative-candidates this week's row count",
            reason="dispatch-decision",
            revalidate_at_dispatch=False,
            round_trip="round_trip",
            # Action-class, explicitly decided (plan's C1b correction,
            # premise-finding sidecar channel 3): the EM dispatches a
            # worker off this answer, with no directive and no gate --
            # demoting it into narration would silence a real dispatch
            # decision. `False`, not left unmarked, so this reads as a
            # deliberate call rather than an oversight the census could
            # otherwise flag.
            reportable=False,
        ),
        build_judgment_point(
            {
                "disposition": "extend_span",
                "rationale": (
                    "Rule-5's already-reviewed span should extend to cover "
                    "this week's new commits unless a reviewer explicitly "
                    "re-scoped it."
                ),
            },
            id="jp_step7_rule5_already_reviewed_span",
            question="Extend Rule-5's already-reviewed span to include this week's commits?",
            dispositions=[
                build_disposition("extend_span"),
                build_disposition("keep_prior_span"),
            ],
            evidence="Step 7 Rule-5 span vs this week's commit range",
            reason="pm-scoped-tradeoff",
            revalidate_at_dispatch=True,
            round_trip="terminal",
            # Action-class: `extend_span` widens what Rule-5 treats as already
            # reviewed, and no directive applies that -- the EM does. A
            # `pm-scoped-tradeoff` is by definition an answer that matters, so
            # demoting it into narration would silence the one kind of question
            # this mechanism exists to preserve.
            reportable=False,
        ),
        build_judgment_point(
            None,
            id="jp_step7_5_staff_eng_fire_discretion",
            question="Fire the Step 7.5 the Staff Engineer review this week, or defer it?",
            dispositions=[
                build_disposition("fire"),
                build_disposition("defer"),
            ],
            evidence="Step 7.5 discretionary-fire signal (no fixed cadence)",
            reason="pm-scoped-tradeoff",
            revalidate_at_dispatch=True,
            round_trip="terminal",
        ),
        build_judgment_point(
            None,
            id="jp_step8_5_loe_high_water",
            question="This week's LoE high-water mark — accept computed value or override?",
            dispositions=[
                build_disposition("accept_computed"),
                build_disposition("override"),
            ],
            evidence="Step 8.5 LoE high-water computation",
            reason="pm-scoped-tradeoff",
            revalidate_at_dispatch=True,
            round_trip="terminal",
        ),
        build_judgment_point(
            None,
            id="jp_step9_editorial_bucketing",
            question="Release-notes editorial bucketing/framing for this week — confirm grouping?",
            dispositions=[
                build_disposition("accept_grouping"),
                build_disposition("regroup"),
            ],
            evidence="Step 9 release-notes draft bucketing",
            reason="pm-scoped-tradeoff",
            revalidate_at_dispatch=True,
            round_trip="terminal",
        ),
        build_judgment_point(
            None,
            id="jp_step10_semver_judgment",
            question="Step 10 version bump — patch/minor/major?",
            dispositions=[
                build_disposition("patch"),
                build_disposition("minor"),
                build_disposition("major"),
            ],
            evidence="check-version-consistency + this week's changelog diff",
            reason="pm-scoped-tradeoff",
            revalidate_at_dispatch=True,
            round_trip="terminal",
        ),
        build_judgment_point(
            None,
            id="jp_step1c_pm_recollection_match",
            question="Does this week's computed summary match the PM's own recollection of the week?",
            dispositions=[
                build_disposition("confirmed_match"),
                build_disposition("flag_discrepancy"),
            ],
            evidence="Step 1c PM-recollection-match confirmation prompt",
            reason="pm-authority",
            revalidate_at_dispatch=False,
            round_trip="terminal",
        ),
        build_judgment_point(
            None,
            id="jp_step9_pm_release_notes_gate",
            question="PM release-notes review gate — approve this week's release-notes framing?",
            dispositions=[
                build_disposition("approved"),
                build_disposition("revise"),
            ],
            evidence="Step 9 PM release-notes review gate",
            reason="pm-authority",
            revalidate_at_dispatch=False,
            round_trip="terminal",
        ),
        build_judgment_point(
            None,
            id="jp_step10_5_gh_release_publish",
            question="Publish this week's `gh release` now? This is irreversible once published.",
            dispositions=[
                build_disposition("publish"),
                build_disposition("hold"),
            ],
            evidence="Step 10.5 gh-release publish confirmation",
            reason="irreversible-external-action",
            revalidate_at_dispatch=True,
            round_trip="terminal",
        ),
    ] + doc_staleness_points + doc_verify_points


def _reported_narration(reported_points: list[dict[str, Any]]) -> str:
    """Renders each `reported`-partition judgment point (see
    `partition_reportable`) as a `narration` sentence: the question plus its
    recommendation's `rationale`, so the EM still sees the fact without being
    asked to answer a question that gates no directive. Returns `""` when
    `reported_points` is empty -- callers must not append a stray separator
    in that case. Only ever called with recommendation-carrying points (see
    `brief()`), so `recommendation` is never `None` here in practice, but a
    missing key still degrades to an empty rationale rather than raising --
    this is narration prose, not a control-flow input, and must never fail
    the ceremony over a formatting concern."""
    lines = []
    for point in reported_points:
        recommendation = point.get("recommendation") or {}
        rationale = recommendation.get("rationale", "")
        lines.append(
            f"{point.get('id')} reports (not asked -- gates no directive): "
            f"{point.get('question')} Recommendation rationale: {rationale}"
        )
    return " ".join(lines)


def brief(
    *, decisions: Optional[dict[str, Any]] = None, env: Optional[dict[str, str]] = None
) -> tuple[int, dict[str, Any]]:
    """Compute the workweek-complete decision object. Read-only — never
    mutates disk/git state itself; every mutation is a named `directives[]`
    entry the apply half (`coordinator_core.workweek_complete.apply`)
    executes. `decisions` (an EM-supplied `{judgment_point_id: {disposition,
    ...}}` map) is accepted and threaded through unchanged in the returned
    envelope's `decisions` key — this module does not resolve it itself.

    Returns `(exit_code, envelope)` using `WorkweekExitCode` (0-3, brief-side
    only — see module docstring).
    """
    try:
        resolve_operator_config(env=env)
    except Exception as exc:  # noqa: BLE001 - mirrors workday_complete.brief's own backstop
        return int(WorkweekExitCode.TRANSPORT_FAIL), {"error": str(exc)}

    stale_docs = _stale_doc_entries(_compute_doc_staleness_report())
    doc_verify_findings_by_doc = _verify_findings_by_doc(_compute_doc_verify_findings())
    directives = _build_directives(stale_docs, doc_verify_findings_by_doc)
    all_judgment_points = _build_judgment_points(stale_docs, doc_verify_findings_by_doc)

    # Partitioned via the shared predicate, scoped to recommendation-carrying
    # points only -- `partition_reportable` itself has no recommendation carve-out,
    # but this plan's premise (and this module's Tier-2/Tier-3 docstring
    # split above) is specifically about recommendation-carrying points; the
    # Tier-3 `recommendation=None` points (PM-authority, irreversible-action,
    # pm-scoped-tradeoff without a recommendation) stay asked unconditionally
    # and are never fed to the predicate.
    recommendation_carrying = [
        point for point in all_judgment_points if point.get("recommendation") is not None
    ]
    _, reported_points = partition_reportable(recommendation_carrying, directives)
    reported_ids = {point.get("id") for point in reported_points}
    judgment_points = [
        point for point in all_judgment_points if point.get("id") not in reported_ids
    ]

    narration = (
        "`scc`, `node run.js`, and `gh release` have no consumes-manifest "
        "project script and are NOT represented as directives[] entries "
        "— see this module's negative-spec."
    )
    reported_narration = _reported_narration(reported_points)
    if reported_narration:
        narration = f"{narration} {reported_narration}"

    envelope = build_envelope(
        artifact={"kind": "ceremony", "name": "workweek-complete"},
        preflight={"consumes_manifest": list(CONSUMES_MANIFEST)},
        gates={},
        directives=directives,
        judgment_points=judgment_points,
        decisions=decisions if decisions is not None else {},
        narration=narration,
        next_move="resolve open judgment_points, then dispatch apply()",
    )
    emit(envelope)
    return int(WorkweekExitCode.SUCCESS), envelope


def main(argv: list[str]) -> int:
    """`main()`'s `brief` dispatch — no argv options today (mirrors
    `workday_complete.brief`'s CLI shape). Prints the envelope as JSON."""
    import json

    exit_code, envelope = brief()
    print(json.dumps(envelope, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
