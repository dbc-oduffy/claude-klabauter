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

WorkweekExitCode = extend_exit_codes(
    "WorkweekExitCode",
    BUSINESS_FAIL=1,
    USAGE=2,
    TRANSPORT_FAIL=3,
)

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
    "reap-claims-for-repos",
    "handoff-housekeeping",
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
    "workweek-complete-doc-staleness",
    "workweek-complete-doc-verify",
    "tier-u-grant-cli",
)


_CEREMONY_NAME = "workweek-complete"

#: Stored VERBATIM in the grant record's `note` (write_tier_u_grant never
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
    cwd = start or Path.cwd()
    out = git_common_dir(str(cwd))
    if not out:
        return None
    try:
        return str(main_worktree_root(Path(out)))
    except Exception:  # noqa: BLE001 - never fail the ceremony
        return None


def _compute_doc_staleness_report() -> list[dict[str, Any]]:
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
    return [entry for entry in report if entry.get("stale") and entry.get("path")]


def _doc_staleness_slug(path: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", path.lower()).strip("_")


def _doc_staleness_jp_id(path: str) -> str:
    return f"jp_doc_staleness_{_doc_staleness_slug(path)}"


def _doc_staleness_ack_directive_id(path: str) -> str:
    return f"d_doc_staleness_ack_{_doc_staleness_slug(path)}"


def _doc_staleness_evidence(entry: dict[str, Any]) -> str:
    return (
        f"{entry.get('path')}: commits_since={entry.get('commits_since')} "
        f"(threshold {entry.get('threshold_commits')}), "
        f"days_since={entry.get('days_since')} "
        f"(threshold {entry.get('threshold_days')}), "
        f"last_touch={entry.get('last_touch_sha')}@{entry.get('last_touch_date')}, "
        f"changed_areas={entry.get('changed_areas')}"
    )


def _compute_doc_verify_findings() -> list[dict[str, Any]]:
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
    grouped: dict[str, list[dict[str, Any]]] = {}
    for finding in findings:
        doc = finding.get("doc")
        if not doc:
            continue
        grouped.setdefault(doc, []).append(finding)
    return grouped


def _doc_verify_slug(path: str) -> str:
    return _doc_staleness_slug(path)


def _doc_verify_jp_id(path: str) -> str:
    return f"jp_doc_verify_{_doc_verify_slug(path)}"


def _doc_verify_ack_directive_id(path: str) -> str:
    return f"d_doc_verify_ack_{_doc_verify_slug(path)}"


def _doc_verify_evidence(path: str, findings: list[dict[str, Any]]) -> str:
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
    the two gates the census identified as hard-blocking (4g reverse-drift,
    plus the Step-10 version-consistency check); the rest are advisory. The
    4c UBT pending-record merge id (`d_step4c_ubt_pending_merge_gate`) was
    never constructed by any directive-builder call here — dead, removed
    from `hard_block_ids`. Vendored-schema drift (formerly 4k) is
    no longer a directive here — the requirement it served was retired by
    PM ruling and is now met by the doctor probe's `vendor_drift` sentinel
    key (`bin/claude-klabauter-doctor-probe.py`), not by a ceremony-time gate. All
    Tier-1 directives here are ungated (`depends_on=None`) — unlike C2's
    workday assembler, no Tier-1 workweek step was named in this chunk's
    spec as gated on a specific judgment point.

    `workweek-complete-drift-guards` bundles several 4b-4k subcommands
    behind one CLI whose per-subcommand severity differs — all three
    remaining subcommands (`description-length`/`enabled-plugins`/
    `cve-recheck`) are advisory-only, per DoE's own per-subcommand
    exit-code docstring (coordinator/bin/workweek-complete-drift-
    guards.py). This module therefore emits ONE directive per subcommand
    (`d_step4b_4k_description_length`, `d_step4b_4k_enabled_plugins`,
    `d_step4b_4k_cve_recheck`) rather than a single `args=[]` directive
    naming the bundling CLI with no subcommand at all — `pcli-drift-gate`
    is deliberately NOT one of them (the ceremony doc's own prose: "no
    directive emits its subcommand" — it stays a by-hand Step 5 invocation)
    and neither is `shellcheck-sweep`/`console-flash-guard`/`multi-event-
    hook-guard` (Step 6 concerns, outside the 4b-4k census).

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
    _repo_root = _resolve_repo_root_for_doc_staleness()
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
            args=(
                ["--no-stdin", "--root", _repo_root]
                if _repo_root
                else ["--no-stdin"]
            ),
        ),
        _directive(
            "d_step4_counts_coordinator_initiative",
            cli="coordinator-initiative",
            args=["--list"],
        ),
        _directive(
            "d_step4_counts_cruft_sweep",
            cli="cruft-sweep",
            args=(
                ["--repo-root", _repo_root]
                if _repo_root
                else []
            ),
        ),
        _directive(
            "d_step4_counts_reap_claims",
            cli="reap-claims-for-repos",
            args=[],
        ),
        _directive(
            "d_step4_counts_handoff_housekeeping",
            cli="handoff-housekeeping",
            args=[],
        ),
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
            "d_step4b_4k_reverse_drift",
            cli="workweek-complete-reverse-drift-gate",
            args=[],
            already_satisfied=False,
        ),
        _directive(
            "d_step4b_4k_version_consistency",
            cli="check-version-consistency",
            args=(["--repo-root", _repo_root] if _repo_root else []),
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
            ceremony_name=_CEREMONY_NAME,
        ),
        _directive(
            "d_step13_7_tier_u_grant_handback",
            cli="tier-u-grant-cli",
            args=["revoke", "--only-ceremony", _CEREMONY_NAME],
        ),
    ]
    # ceremony that otherwise fully succeeded into `PARTIAL_MUTATION`,
    best_effort_ids = {
        "d_step0_9_tier_u_grant_write",
        "d_step13_7_tier_u_grant_handback",
    }
    hard_block_ids = {
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
    try:
        resolve_operator_config(env=env)
    except Exception as exc:  # noqa: BLE001 - mirrors workday_complete.brief's own backstop
        return int(WorkweekExitCode.TRANSPORT_FAIL), {"error": str(exc)}

    stale_docs = _stale_doc_entries(_compute_doc_staleness_report())
    doc_verify_findings_by_doc = _verify_findings_by_doc(_compute_doc_verify_findings())
    directives = _build_directives(stale_docs, doc_verify_findings_by_doc)
    all_judgment_points = _build_judgment_points(stale_docs, doc_verify_findings_by_doc)

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
    import json

    exit_code, envelope = brief()
    print(json.dumps(envelope, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
